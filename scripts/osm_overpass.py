#!/usr/bin/env python
"""OSM truck-repair layer via the Overpass API -> osm.truck_repair.

WHY THIS EXISTS ALONGSIDE scripts/osm_extract.py
------------------------------------------------
`osm_extract.py --job pois` walks the 12 GB US PBF. That is the right tool for
`amenity=fuel` (108k US rows) — no public API should be asked for a national
extract of that size.

It is the wrong tool for truck repair, and the numbers are not close.
Measured 2026-07-27:

    PBF pass      12 GB read + a node-location index that grew past 1 GB and
                  thrashed the page cache on a 15 GB laptop; the previous
                  successful US pass took 2 h 51 m (ops.source_runs run 981).
    Overpass      763 objects, 357 KB, ~2 minutes, no local index at all.

Both produce the SAME 763 rows, because that is all the truck-repair tagging
that exists in the US. Overpass is also fresher: it answers from a minutes-old
replica, where the PBF is a weekly snapshot.

So the rule this file encodes: **choose the transport by result size, not by
habit.** A national layer of a few thousand objects is an API query. A national
layer of a hundred thousand is a bulk file.

WHAT IT DOES NOT CHANGE
-----------------------
Licence containment is identical (brief D3): this is ODbL data, it lands in
`osm.*`, and only a match FLAG ever crosses into `core.mechanic_shops`.
Attribution stays "© OpenStreetMap contributors, ODbL".

HONESTY
-------
- `observed_at` = Overpass's own `osm3s.timestamp_osm_base` — when the data was
  true in OSM, never the load date. If the response omits it the run FAILS
  rather than stamping now() and quietly inventing a vintage.
- Tri-state booleans: NULL = tag absent = unknown, never False.
- Way geometry is the `out center` representative point, the same
  approximation the PBF path makes with its centroid — documented, not hidden.

POLITENESS
----------
One query, one small result, identifying User-Agent, generous server-side
timeout. Overpass is a donated public service; this asks it for 763 objects
once, not for a bulk national extract.

Audit: exactly one ops.source_runs row per invocation, same contract as
osm_extract.py. Exit 0 = success, 1 = failure (recorded on the run row).

Usage:
  uv run python scripts/osm_overpass.py --job truck_repair
  uv run python scripts/osm_overpass.py --job truck_repair --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402
from truckintel.loaders import snapshot_swap  # noqa: E402

SOURCE_ID = "osm_truck_repair_overpass"
SLO_HOURS = 400
TARGET = "osm.truck_repair"
ENDPOINT = "https://overpass-api.de/api/interpreter"
# Overpass is a donated service and answers 429/504 when it is busy — observed
# on the very first run here. Independently operated mirrors run the same
# software against the same data, so a 504 is a reason to wait and move on to
# the next host, never a reason to fail the load.
MIRRORS = (
    ENDPOINT,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
RETRY_WAITS_S = (5, 20, 60)          # per host, before giving up on it
USER_AGENT = "truck-intel/1.0 (public-data verification; OSM truck repair layer)"
SERVER_TIMEOUT_S = 280

# The same three tags scripts/osm_extract.py matches, so the two transports
# produce the same layer and can be compared row for row.
_QUERY = f"""
[out:json][timeout:{SERVER_TIMEOUT_S}];
area["ISO3166-1"="US"][admin_level=2]->.a;
(
  node["shop"="truck_repair"](area.a);
  way["shop"="truck_repair"](area.a);
  node["service:vehicle:truck_repair"="yes"](area.a);
  way["service:vehicle:truck_repair"="yes"](area.a);
  node["service:vehicle:trailer_repair"="yes"](area.a);
  way["service:vehicle:trailer_repair"="yes"](area.a);
);
out tags center;
"""

_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY".split()
)

# kind='derived' is load-bearing, not cosmetic. truckintel-tick runs
# registry.sync_sources every minute, and that sweep disables any ENABLED
# source with no registry/*.yaml behind it — unless its kind is 'derived',
# which is this repo's marker for registry-less, timer-driven sources
# (aaa_daily, osm_pois, quality_nightly all use it).
#
# Seeded as 'live_json' first time round, this row was disabled within sixty
# seconds. A disabled source is skipped by scripts/freshness_check.py, so the
# job would have kept running daily with NO staleness alerting behind it —
# the exact silent failure the SLO exists to prevent.
_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, url, kind, load_pattern, schedule_minutes,
     slo_hours, license, attribution_text, enabled, verify_status)
VALUES
    (%(sid)s,
     'Derived: OSM truck/trailer repair shops via Overpass API -> osm.truck_repair',
     'truck-intel mechanics track', %(url)s, 'derived', 'derived',
     NULL, %(slo)s, 'ODbL-1.0', '© OpenStreetMap contributors',
     TRUE, 'verified')
ON CONFLICT (source_id) DO UPDATE SET
    -- Repair a row seeded before this was understood: re-enable it and correct
    -- the kind, or the next tick disables it again and nothing says so.
    kind = 'derived', load_pattern = 'derived', enabled = TRUE,
    slo_hours = EXCLUDED.slo_hours
"""


# ---------------------------------------------------------------- tag parsing

def _tristate(value: str | None, *, true=("yes",), false=("no",)) -> bool | None:
    """Unrecognised/absent -> None (unknown), never False."""
    if value in true:
        return True
    if value in false:
        return False
    return None


def state_code(tags: dict) -> str | None:
    """addr:state only when it is a valid 2-letter USPS code; no reverse
    geocoding here (same rule as osm_extract.state_code)."""
    raw = (tags.get("addr:state") or "").strip().upper()
    return raw if raw in _US_STATES else None


def hgv_access(tags: dict) -> bool | None:
    return _tristate(tags.get("hgv"), true=("yes", "designated"), false=("no",))


def to_row(el: dict, observed_at: datetime) -> dict | None:
    """Overpass element -> loaders.py row dict. None when it has no position."""
    tags = el.get("tags") or {}
    if el.get("type") == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:                                   # way: representative centre point
        centre = el.get("center") or {}
        lat, lon = centre.get("lat"), centre.get("lon")
    if lat is None or lon is None:
        return None
    truck = (tags.get("shop") == "truck_repair"
             or tags.get("service:vehicle:truck_repair") == "yes")
    return {
        "osm_id": f"{el['type']}/{el['id']}",   # same key shape as the PBF path
        "name": tags.get("name"),
        "brand": tags.get("brand"),
        "state": state_code(tags),
        "truck_repair": True if truck else _tristate(
            tags.get("service:vehicle:truck_repair")),
        "trailer_repair": _tristate(tags.get("service:vehicle:trailer_repair")),
        "hgv_access": hgv_access(tags),
        "lat": lat,
        "lon": lon,
        "observed_at": observed_at,
        "props": tags,                          # opening_hours travels in here
    }


# -------------------------------------------------------------------- fetch

def _fetch_once(endpoint: str, query: str) -> tuple[list[dict], datetime]:
    req = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=SERVER_TIMEOUT_S + 30) as resp:
        payload = json.loads(resp.read())
    stamp = (payload.get("osm3s") or {}).get("timestamp_osm_base")
    if not stamp:
        raise ValueError(
            "Overpass response carried no osm3s.timestamp_osm_base — refusing "
            "to load data whose vintage cannot be stated honestly")
    return payload.get("elements", []), datetime.fromisoformat(
        stamp.replace("Z", "+00:00"))


def fetch(query: str = _QUERY, *, mirrors: tuple[str, ...] = MIRRORS,
          sleep=None) -> tuple[list[dict], datetime]:
    """(elements, observed_at), trying each mirror with backoff.

    Only *transient* conditions move on to the next attempt — a busy server
    (429/504/502/503) or a network error. A 400 means our query is wrong and
    retrying it on three hosts would just be rude, so it raises immediately.
    """
    import time
    sleep = sleep or time.sleep
    last: BaseException | None = None
    for endpoint in mirrors:
        for attempt, wait in enumerate((0,) + RETRY_WAITS_S):
            if wait:
                print(f"[overpass] {endpoint} busy — waiting {wait}s "
                      f"(attempt {attempt + 1})", flush=True)
                sleep(wait)
            try:
                return _fetch_once(endpoint, query)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 502, 503, 504):
                    raise            # our fault, not theirs — do not retry
                last = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
        print(f"[overpass] giving up on {endpoint}", flush=True)
    raise RuntimeError(
        f"every Overpass mirror was unavailable ({len(mirrors)} tried); "
        f"last error: {last}")


# ---------------------------------------------------------------- audit + run

def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID, "slo": SLO_HOURS,
                                 "url": ENDPOINT})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id", (SOURCE_ID,)
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows_published: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, message, rows_published, run_id))


def run(*, target: str = TARGET, dry_run: bool = False) -> int:
    """Fetch, publish, re-derive route columns. Returns rows published."""
    if dry_run:
        elements, observed_at = fetch()
        rows = [r for r in (to_row(e, observed_at) for e in elements) if r]
        with_hours = sum(1 for r in rows if r["props"].get("opening_hours"))
        print(f"[dry-run] {len(elements)} elements -> {len(rows)} rows "
              f"(vintage {observed_at:%Y-%m-%dT%H:%M:%SZ}); "
              f"{with_hours} carry opening_hours; nothing written", flush=True)
        return len(rows)

    run_id = _start_run()
    print(f"{SOURCE_ID} run {run_id}: querying Overpass …", flush=True)
    try:
        elements, observed_at = fetch()
        rows, skipped = [], 0
        for el in elements:
            row = to_row(el, observed_at)
            if row is None:
                skipped += 1
                continue
            rows.append(row)
        with get_conn() as conn:
            published = snapshot_swap(conn, target, rows,
                                      source_id=SOURCE_ID, run_id=run_id)
        # The swap clones the table, so route columns come back NULL. Re-derive
        # in the same invocation — the assignment is a derivative of the swap,
        # not a separate schedule that could drift out of step.
        from truckintel.route_assign import add_route_columns, assign_nearest_route
        with get_conn() as conn:
            add_route_columns(conn, target)
            assigned = assign_nearest_route(conn, target, "osm_id")
    except BaseException as exc:
        _finish_run(run_id, "failed",
                    message=(str(exc) or type(exc).__name__)[:1000])
        raise

    message = (
        f"endpoint={ENDPOINT}; observed_at={observed_at:%Y-%m-%dT%H:%M:%SZ} "
        f"(overpass osm3s.timestamp_osm_base); {target}={published}; "
        f"elements={len(elements)} skipped_no_position={skipped}; "
        f"route_assigned={assigned}"
    )
    _finish_run(run_id, "success", message=message, rows_published=published)
    print(f"{SOURCE_ID} run {run_id}: {message}", flush=True)
    return published


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="OSM truck-repair layer via Overpass -> osm.truck_repair")
    ap.add_argument("--job", required=True, choices=("truck_repair",))
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    args = ap.parse_args(argv)
    load_dotenv()
    try:
        run(dry_run=args.dry_run)
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"osm_overpass --job {args.job} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
