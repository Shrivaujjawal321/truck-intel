"""Weekly digest (MASTER_PLAN §4 Monitoring: "status.html + Telegram/ntfy
alerts + weekly digest"; Phase-2 deliverable D6).

One read-only sweep over ops.* + the core/osm tables, rendered to a plain-text
digest the solo operator can read in 20 seconds on a Sunday. Five sections:

  1. Ingestion activity (last N days)  — per source: runs, ok/skip/fail, rows.
  2. Freshness snapshot                — last success vs the source's SLO.
  3. Data holdings                     — what we actually serve, with vintage.
  4. Circuit breakers                  — any feed the engine has tripped open.
  5. Needs a human                     — the consolidated attention list.

Delivery (this is the alert hook freshness_check.py left as a post-MVP TODO —
implemented here, once, reused by both):
  * always writes the digest to --out (default <repo>/status_weekly.md) and
    prints it, so the report exists even with no messaging configured;
  * with --deliver, sends the plain-text body to Telegram
    (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) and/or ntfy (NTFY_TOPIC, optional
    NTFY_SERVER). Missing config is the eia_diesel skipped_no_key path — an
    honest "not configured, printed only" line, never a crash.

Exit codes: 0 ok, 2 database unreachable (a Sunday with no digest is itself a
page-worthy failure — never a silent success).

Run:  uv run python scripts/weekly_digest.py [--days 7] [--deliver] [--out PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from truckintel.db import fetch_all, get_conn

OUT_PATH = Path(__file__).resolve().parents[1] / "status_weekly.md"
FRESH_STATUSES = ("success", "skipped_unchanged")
_HTTP_TIMEOUT = 15

# The tables we serve, in report order: (label, qualified_table, has_observed_at).
# A fixed allow-list — never an information_schema sweep — so no user/DB-derived
# identifier ever reaches an interpolated query string (same discipline as the
# §5.1 SNAPSHOT_TARGETS allow-list).
_HOLDINGS = (
    ("bridges (NBI)",           "core.bridges",        True),
    ("tunnels (NTI)",           "core.tunnels",        True),
    ("parking / rest / weigh",  "core.parking_sites",  True),
    ("restrictions",            "core.restrictions",   True),
    ("businesses (Overture+FSQ)", "core.businesses",   True),
    ("fuel prices (EIA weekly)", "core.fuel_prices",   True),
    ("live events (open)",      "core.live_events",    True),
    ("osm ways",                "osm.ways",            True),
    ("osm fuel stations",       "osm.fuel_stations",   True),
    ("osm rest areas",          "osm.rest_areas",      True),
    ("osm weigh points",        "osm.weigh_points",    True),
)


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    return f"{hours / 24:.1f}d" if hours >= 48 else f"{hours:.1f}h"


# --------------------------------------------------------------- data gathering

def gather_activity(conn, days: int) -> list[dict]:
    """Per-source run rollup over the window, newest-active first."""
    rows = conn.execute(
        """
        SELECT source_id,
               count(*)                                              AS runs,
               count(*) FILTER (WHERE status = 'success')            AS ok,
               count(*) FILTER (WHERE status IN ('skipped_unchanged',
                                                 'skipped_no_key', 'gated')) AS skipped,
               count(*) FILTER (WHERE status = 'failed')             AS failed,
               count(*) FILTER (WHERE status = 'running')            AS running,
               coalesce(sum(rows_published), 0)                      AS rows_pub,
               max(started_at)                                       AS last_at
        FROM ops.source_runs
        WHERE started_at >= now() - make_interval(days => %s)
          AND source_id NOT LIKE '\\_test\\_%%'   -- test-fixture sources are audit noise in a human digest
        GROUP BY source_id
        ORDER BY max(started_at) DESC
        """,
        (days,),
    ).fetchall()
    return [
        {"source_id": sid, "runs": runs, "ok": ok, "skipped": skipped,
         "failed": failed, "running": running, "rows_pub": rows_pub, "last_at": last_at}
        for (sid, runs, ok, skipped, failed, running, rows_pub, last_at) in rows
    ]


def gather_freshness(conn) -> list[dict]:
    """Per enabled source with an SLO: age of last fresh run vs slo_hours.
    Derived event-driven sources (slo NULL) are reported separately as 'n/a'."""
    rows = conn.execute(
        """
        SELECT s.source_id, s.slo_hours, s.kind, ok.ok_at,
               last.status, left(last.message, 120)
        FROM ops.sources s
        LEFT JOIN LATERAL (
            SELECT coalesce(finished_at, started_at) AS ok_at
            FROM ops.source_runs
            WHERE source_id = s.source_id AND status = ANY(%s)
            ORDER BY started_at DESC LIMIT 1) ok ON TRUE
        LEFT JOIN LATERAL (
            SELECT status, message FROM ops.source_runs
            WHERE source_id = s.source_id ORDER BY started_at DESC LIMIT 1) last ON TRUE
        WHERE s.enabled
        ORDER BY s.source_id
        """,
        (list(FRESH_STATUSES),),
    ).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for (sid, slo, kind, ok_at, status, msg) in rows:
        age_h = (now - ok_at).total_seconds() / 3600 if ok_at else None
        if slo is None:
            state = "n/a"          # event-driven synthetic/derived source
        elif age_h is None:
            state = "STALE"        # never succeeded
        elif age_h > slo:
            state = "STALE"
        elif age_h >= 0.75 * slo:
            state = "warn"
        else:
            state = "fresh"
        out.append({"source_id": sid, "slo": slo, "kind": kind, "age_h": age_h,
                    "state": state, "status": status, "message": msg or ""})
    return out


def _holding(conn, table: str, has_observed: bool) -> tuple[int | None, str]:
    """(row_count, newest-observed_at label) for one table, degrading honestly
    if the table/column is absent (schema not applied yet)."""
    try:
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except Exception:
        return None, "table absent"
    if not has_observed or n == 0:
        return n, "—"
    try:
        newest = conn.execute(
            f"SELECT max(observed_at) FROM {table}").fetchone()[0]
    except Exception:
        return n, "no observed_at"
    return n, (newest.strftime("%Y-%m-%d") if newest else "unknown")


def gather_holdings(conn) -> list[dict]:
    out = []
    for label, table, has_obs in _HOLDINGS:
        # each table in its own savepoint so one missing table never aborts the txn
        conn.execute("SAVEPOINT h")
        try:
            n, vintage = _holding(conn, table, has_obs)
            conn.execute("RELEASE SAVEPOINT h")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT h")
            n, vintage = None, "table absent"
        out.append({"label": label, "table": table, "count": n, "vintage": vintage})
    return out


def gather_breakers(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT source_id, state, consecutive_failures, opened_at, "
            "last_failure_at FROM ops.feed_health "
            "WHERE state <> 'closed' ORDER BY opened_at NULLS LAST"
        ).fetchall()
    except Exception:
        return []   # feed_health not present (pre-phase2 DB) — honest empty
    return [
        {"source_id": sid, "state": st, "fails": f,
         "opened_at": op, "last_failure_at": lf}
        for (sid, st, f, op, lf) in rows
    ]


# ------------------------------------------------------------------ rendering

def render(activity, freshness, holdings, breakers, *, days: int,
           generated: str) -> str:
    """Plain-text/Markdown digest — readable both in a file and a chat bubble."""
    L: list[str] = []
    L.append(f"# truck-intel weekly digest — {generated}")
    L.append(f"_window: last {days} days · all times UTC_\n")

    # 5. attention list computed first so it can headline. Each source appears
    # at most once: STALE > BREAKER OPEN > FAILING (a source already surfaced by
    # a higher-priority reason is not repeated as FAILING).
    attention: list[str] = []
    stale_ids = {f["source_id"] for f in freshness if f["state"] == "STALE"}
    breaker_ids = {b["source_id"] for b in breakers}
    surfaced = stale_ids | breaker_ids
    attention += [f"STALE: {f['source_id']} (last ok {_fmt_age(f['age_h'])} ago, "
                  f"SLO {f['slo']}h)" for f in freshness if f["state"] == "STALE"]
    attention += [f"BREAKER OPEN: {b['source_id']} ({b['fails']} consecutive fails)"
                  for b in breakers if b["source_id"] not in stale_ids]
    attention += [f"FAILING: {a['source_id']} ({a['failed']} failed run(s) this window)"
                  for a in activity if a["failed"] and a["source_id"] not in surfaced]
    if attention:
        L.append("## ⚠ Needs a human")
        L += [f"- {line}" for line in attention]
    else:
        L.append("## ✓ Needs a human")
        L.append("- nothing — every enabled source is fresh and no breaker is open.")
    L.append("")

    # 1. ingestion activity
    L.append(f"## Ingestion activity ({days}d)")
    if activity:
        L.append("| source | runs | ok | skip | fail | rows pub | last |")
        L.append("|---|--:|--:|--:|--:|--:|---|")
        for a in activity:
            last = a["last_at"].strftime("%m-%d %H:%M") if a["last_at"] else "—"
            run_flag = f" +{a['running']} running" if a["running"] else ""
            L.append(f"| {a['source_id']} | {a['runs']} | {a['ok']} | "
                     f"{a['skipped']} | {a['failed']} | {a['rows_pub']:,} | {last}{run_flag} |")
    else:
        L.append("_no runs in the window._")
    L.append("")

    # 2. freshness
    L.append("## Freshness vs SLO")
    L.append("| source | last ok | SLO | state |")
    L.append("|---|---|--:|---|")
    for f in freshness:
        slo = f"{f['slo']}h" if f["slo"] is not None else "—"
        mark = {"fresh": "✓ fresh", "warn": "~ warn",
                "STALE": "✗ STALE", "n/a": "· n/a"}[f["state"]]
        L.append(f"| {f['source_id']} | {_fmt_age(f['age_h'])} | {slo} | {mark} |")
    L.append("")

    # 3. holdings
    L.append("## Data holdings (what we serve)")
    L.append("| dataset | rows | newest observed_at |")
    L.append("|---|--:|---|")
    for h in holdings:
        cnt = "—" if h["count"] is None else f"{h['count']:,}"
        L.append(f"| {h['label']} | {cnt} | {h['vintage']} |")
    L.append("")

    # 4. breakers (only if any — closed feeds are the boring happy path)
    if breakers:
        L.append("## Circuit breakers (open/half-open)")
        for b in breakers:
            op = b["opened_at"].strftime("%Y-%m-%d %H:%M") if b["opened_at"] else "?"
            L.append(f"- {b['source_id']}: {b['state']}, "
                     f"{b['fails']} consecutive fails, opened {op}")
        L.append("")

    L.append("---")
    L.append("_Advisory data from public sources — not for enforcement. "
             "A NULL renders as \"unknown\", never as \"no\". "
             "Every row carries (source_id, run_id, ingested_at, observed_at)._")
    return "\n".join(L)


# ------------------------------------------------------------------- delivery

_TELEGRAM_LIMIT = 4096   # Telegram sendMessage hard cap (chars)


def _telegram_body(body: str) -> str:
    """Fit the digest into Telegram's 4096-char cap with an HONEST truncation
    marker — never a silent cut (the digest is markdown-ish tables full of '_',
    so we send PLAIN TEXT: parse_mode='Markdown' would 400 on the underscores
    and the whole message would be silently rejected)."""
    if len(body) <= _TELEGRAM_LIMIT:
        return body
    marker = "\n…(truncated {} chars — full digest in status_weekly.md)"
    keep = _TELEGRAM_LIMIT - len(marker.format(999999))
    return body[:keep] + marker.format(len(body) - keep)


def deliver(body: str) -> list[dict]:
    """Best-effort send to any configured channel. Returns one dict per channel
    {channel, status, detail} where status is 'ok' | 'skipped' | 'error'; never
    raises (a delivery hiccup must not fail the digest — the file is already
    written). main() turns any 'error' into a non-zero exit so a monitor never
    reads a failed delivery as success."""
    results: list[dict] = []
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            # PLAIN TEXT (no parse_mode): the body is full of underscores
            # (source ids), which legacy Markdown would reject with HTTP 400.
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": _telegram_body(body),
                      "disable_web_page_preview": True},
                timeout=_HTTP_TIMEOUT)
            results.append({"channel": "telegram",
                            "status": "ok" if r.ok else "error",
                            "detail": f"HTTP {r.status_code}"
                            + ("" if r.ok else f" — {r.text[:120]}")})
        except Exception as exc:
            results.append({"channel": "telegram", "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
    else:
        results.append({"channel": "telegram", "status": "skipped",
                        "detail": "not configured (TELEGRAM_BOT_TOKEN/CHAT_ID "
                                  "unset) — printed only"})

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            # ntfy's message limit is far higher than Telegram's — send the
            # FULL body (no 4096 cut, which would be silent data loss here).
            r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                              headers={"Title": "truck-intel weekly digest",
                                       "Tags": "truck"}, timeout=_HTTP_TIMEOUT)
            results.append({"channel": "ntfy",
                            "status": "ok" if r.ok else "error",
                            "detail": f"HTTP {r.status_code}"})
        except Exception as exc:
            results.append({"channel": "ntfy", "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
    return results


def build_digest(conn, days: int) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    activity = gather_activity(conn, days)
    freshness = gather_freshness(conn)
    holdings = gather_holdings(conn)
    breakers = gather_breakers(conn)
    return render(activity, freshness, holdings, breakers,
                  days=days, generated=generated)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="rollup window (default 7)")
    ap.add_argument("--out", type=Path, default=OUT_PATH,
                    help=f"write the digest here (default {OUT_PATH})")
    ap.add_argument("--deliver", action="store_true",
                    help="also send to Telegram/ntfy if configured")
    args = ap.parse_args(argv)

    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 2
    try:
        with get_conn() as conn:
            body = build_digest(conn, args.days)
    except Exception as exc:
        print(f"CANNOT BUILD DIGEST: database unreachable "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2

    args.out.write_text(body + "\n")
    print(body)
    print(f"\n[wrote {args.out}]")
    if args.deliver:
        results = deliver(body)
        for r in results:
            print(f"[deliver] {r['channel']}: {r['status']} — {r['detail']}")
        # a real delivery failure must not read as success (per the module's
        # "never a silent success" contract); 'skipped' (unconfigured) is fine.
        if any(r["status"] == "error" for r in results):
            print("[deliver] one or more channels FAILED — digest written but "
                  "not delivered", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
