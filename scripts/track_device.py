#!/usr/bin/env python
"""Register, list, revoke and prune tracking devices.

Device administration is deliberately NOT an API route. The tracking-ingest role
cannot write to core.truck_devices at all (sql/schema_tracking.sql grants it
SELECT plus UPDATE on three columns), so creating a device requires the owner
credentials — which means a shell, not an HTTP request that could be reached
from a phone that has one leaked token.

The token is printed ONCE, at creation, and only its sha256 is stored. There is
no recovery path: a lost token is re-issued with --rotate, which invalidates the
old one. That is the point — a table of live secrets is a liability, and "we can
show you the token again later" is the same thing as storing it in the clear.

Usage:
    uv run python scripts/track_device.py add truck-14 --label "Volvo VNL 760"
    uv run python scripts/track_device.py list
    uv run python scripts/track_device.py rotate truck-14
    uv run python scripts/track_device.py disable truck-14
    uv run python scripts/track_device.py enable  truck-14
    uv run python scripts/track_device.py prune --days 30      # old pings
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402

# 32 bytes of urlsafe randomness — long enough that the API's per-device rate
# limit is not the only thing standing between a guesser and a fake position.
TOKEN_BYTES = 32
# Ping retention default. One truck at a ping per 10 s is ~8.6k rows/day, so 30
# days of a 10-truck fleet is ~2.6M rows — comfortable, and bounded on purpose
# rather than growing until the disk decides for us.
DEFAULT_RETENTION_DAYS = 30


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _print_token(device_id: str, token: str) -> None:
    print()
    print(f"  device_id : {device_id}")
    print(f"  token     : {token}")
    print()
    print("  Shown once — only its sha256 is stored. Lost it? `rotate`.")
    print()


def cmd_add(args: argparse.Namespace) -> int:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with get_conn() as pg:
        existing = pg.execute(
            "SELECT 1 FROM core.truck_devices WHERE device_id = %s",
            (args.device_id,),
        ).fetchone()
        if existing:
            print(f"device '{args.device_id}' already exists — use rotate to "
                  f"re-issue its token", file=sys.stderr)
            return 1
        pg.execute(
            "INSERT INTO core.truck_devices (device_id, label, token_sha256) "
            "VALUES (%s, %s, %s)",
            (args.device_id, args.label, _sha256(token)),
        )
    print(f"registered '{args.device_id}'")
    _print_token(args.device_id, token)
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with get_conn() as pg:
        n = pg.execute(
            "UPDATE core.truck_devices SET token_sha256 = %s WHERE device_id = %s",
            (_sha256(token), args.device_id),
        ).rowcount
    if not n:
        print(f"no such device '{args.device_id}'", file=sys.stderr)
        return 1
    print(f"rotated '{args.device_id}' — the previous token no longer works")
    _print_token(args.device_id, token)
    return 0


def _set_active(device_id: str, active: bool) -> int:
    with get_conn() as pg:
        n = pg.execute(
            "UPDATE core.truck_devices SET active = %s WHERE device_id = %s",
            (active, device_id),
        ).rowcount
    if not n:
        print(f"no such device '{device_id}'", file=sys.stderr)
        return 1
    print(f"{'enabled' if active else 'disabled'} '{device_id}'")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    with get_conn() as pg:
        rows = pg.execute(
            """
            SELECT device_id, coalesce(label, '—') AS label, active,
                   last_seen_at,
                   round(EXTRACT(EPOCH FROM (now() - last_seen_at)))::bigint AS age_s,
                   ping_count, reject_count
            FROM core.truck_devices
            ORDER BY last_seen_at DESC NULLS LAST, device_id
            """
        ).fetchall()
    if not rows:
        print("no devices registered — `add <device_id>` to create one")
        return 0
    print(f"{'device':<20} {'label':<22} {'on':<4} {'last fix':<20} "
          f"{'pings':>8} {'rej':>5}")
    for d, label, active, last, age_s, pings, rej in rows:
        # "never" is not the same as "a moment ago" — an unused device must not
        # look like a silent one.
        when = "never" if last is None else f"{age_s}s ago"
        print(f"{d:<20} {label[:22]:<22} {'yes' if active else 'no':<4} "
              f"{when:<20} {pings:>8} {rej:>5}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete pings older than --days. Retention is a window, not an archive."""
    with get_conn() as pg:
        n = pg.execute(
            "DELETE FROM core.truck_positions "
            "WHERE observed_at < now() - make_interval(days => %s)",
            (args.days,),
        ).rowcount
    print(f"pruned {n:,} pings older than {args.days} days")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a device and print its token once")
    a.add_argument("device_id")
    a.add_argument("--label", default=None, help="what a human calls this truck")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("rotate", help="issue a new token, invalidating the old")
    r.add_argument("device_id")
    r.set_defaults(fn=cmd_rotate)

    d = sub.add_parser("disable", help="stop accepting pings from this device")
    d.add_argument("device_id")
    d.set_defaults(fn=lambda n: _set_active(n.device_id, False))

    e = sub.add_parser("enable", help="accept pings from this device again")
    e.add_argument("device_id")
    e.set_defaults(fn=lambda n: _set_active(n.device_id, True))

    sub.add_parser("list", help="all devices, newest fix first").set_defaults(
        fn=cmd_list)

    p = sub.add_parser("prune", help="delete pings older than N days")
    p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    p.set_defaults(fn=cmd_prune)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
