#!/usr/bin/env python
"""Rebuild everything DERIVED from core.truck_routes, after that table changes.

THE BUG THIS CLOSES
-------------------
core.truck_routes refreshes on a weekly schedule through the engine. Five
things are derived from it and NONE of them were rebuilt when it did:

    route.edges           the routable graph
    route.node_component  connectivity labels
    route_snap_index      nearest-edge lookup for pickup/drop snapping
    route.edge_limits     per-edge height/weight/hazmat limits
    viewer_generalized    the low-zoom geometry the map draws

So a new NTAD vintage would land, the routes table would update, and the
router would keep answering from a graph built on the PREVIOUS network. No
error, no failed run, no stale-data alert — the SLO watches source freshness
and every source would be fresh. Just quietly wrong answers, until someone
noticed a route through a road that no longer exists.

The precedent already existed: scripts/osm_extract.py re-derives fuel stations'
route columns in the same invocation as its swap, for exactly this reason. This
script is that idea applied to the routes table itself, as a derived job so the
~50 minutes of work does not block the publish that triggered it.

ORDER IS NOT A PREFERENCE
------------------------
Noding splits edges at junctions the published geometry implies but does not
node — it CHANGES the topology. Components and the snap index are functions of
that topology, so both must be rebuilt after it, in that order. Limits are
per-edge and must follow the edges. Getting this order wrong produces a graph
that looks built and routes incorrectly, which is the failure mode this whole
script exists to prevent, so the sequence is hard-coded rather than configured.

Audited under source id 'route_rebuild' — one ops.source_runs row per run, so
freshness_check and ops_watch can both see it, and a rebuild that dies is
visible as a failed run instead of as a silently stale graph.

Usage:
  uv run python scripts/route_rebuild.py            # full rebuild
  uv run python scripts/route_rebuild.py --check    # report staleness, do nothing
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402

SOURCE_ID = "route_rebuild"
SLO_HOURS = 400          # weekly source + headroom; ops_watch flags overruns

# (label, kind, payload). Order is load-bearing — see the module docstring.
STEPS: list[tuple[str, str, str]] = [
    ("route graph",      "sql", "sql/route_graph.sql"),
    ("noding",           "sql", "sql/route_noding.sql"),
    ("components",       "py",  "scripts/route_components.py"),
    ("snap index",       "sql", "sql/route_snap_index.sql"),
    ("edge limits",      "sql", "sql/route_limits.sql"),
    ("viewer geometry",  "sql", "sql/viewer_generalized.sql"),
]

_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s,
     'Derived: routable graph + limits + viewer geometry from core.truck_routes',
     'truck-intel routing track', 'derived', 'derived', NULL, %(slo)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO UPDATE SET
    kind = 'derived', load_pattern = 'derived', enabled = TRUE
"""


def staleness() -> tuple[bool, str]:
    """(is_stale, human explanation).

    Stale when core.truck_routes has published more recently than the last
    successful rebuild — which is precisely the window in which the router
    answers from a graph that no longer matches the network.
    """
    with get_conn() as conn:
        routes_at = conn.execute("""
            SELECT max(finished_at) FROM ops.source_runs
            WHERE source_id = 'ntad_national_network' AND status = 'success'
        """).fetchone()[0]
        rebuilt_at = conn.execute("""
            SELECT max(finished_at) FROM ops.source_runs
            WHERE source_id = %s AND status = 'success'
        """, (SOURCE_ID,)).fetchone()[0]
        edges = conn.execute(
            "SELECT count(*) FROM route.edges").fetchone()[0] \
            if conn.execute("SELECT to_regclass('route.edges') IS NOT NULL"
                            ).fetchone()[0] else 0

    if routes_at is None:
        return False, "core.truck_routes has never published — nothing to derive"
    if rebuilt_at is None:
        return True, (f"routes published {routes_at:%Y-%m-%d %H:%M}, graph has "
                      f"NEVER been rebuilt through this job "
                      f"(route.edges holds {edges:,} rows from a manual build)")
    if routes_at > rebuilt_at:
        return True, (f"routes published {routes_at:%Y-%m-%d %H:%M} AFTER the "
                      f"last rebuild {rebuilt_at:%Y-%m-%d %H:%M} — the graph is "
                      f"built on the previous network")
    return False, (f"graph rebuilt {rebuilt_at:%Y-%m-%d %H:%M}, after the last "
                   f"routes publish {routes_at:%Y-%m-%d %H:%M}")


def _run_step(label: str, kind: str, payload: str) -> None:
    t0 = time.time()
    print(f"[rebuild] {label} …", flush=True)
    if kind == "sql":
        cmd = ["./scripts/db_psql.sh", "-v", "ON_ERROR_STOP=1", "-q",
               "-f", payload]
    else:
        cmd = [sys.executable, payload]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[-500:]}")
    print(f"[rebuild]   {label} ok ({time.time() - t0:.0f}s)", flush=True)


def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID, "slo": SLO_HOURS})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id", (SOURCE_ID,)
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, (message or "")[:1000] or None, rows, run_id))


def rebuild() -> int:
    """Run every step in order under one audited run row. Returns edge count."""
    run_id = _start_run()
    print(f"{SOURCE_ID} run {run_id}: rebuilding {len(STEPS)} derived artefacts",
          flush=True)
    t0 = time.time()
    try:
        for label, kind, payload in STEPS:
            _run_step(label, kind, payload)
        with get_conn() as conn:
            edges = conn.execute("SELECT count(*) FROM route.edges").fetchone()[0]
            nodes = conn.execute("SELECT count(*) FROM route.nodes").fetchone()[0]
    except BaseException as exc:
        _finish_run(run_id, "failed", message=str(exc) or type(exc).__name__)
        raise
    msg = (f"rebuilt in {(time.time() - t0) / 60:.1f} min; "
           f"route.edges={edges:,} route.nodes={nodes:,}; steps="
           + ",".join(s[0] for s in STEPS))
    _finish_run(run_id, "success", message=msg, rows=edges)
    print(f"{SOURCE_ID} run {run_id}: {msg}", flush=True)
    return edges


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report whether the graph is behind the routes table; "
                         "exit 1 if it is. Writes nothing.")
    ap.add_argument("--if-stale", action="store_true",
                    help="rebuild ONLY when the graph is behind (what the "
                         "scheduled job runs — a weekly rebuild of an "
                         "unchanged network is 50 minutes of nothing)")
    args = ap.parse_args()
    load_dotenv()

    stale, why = staleness()
    print(f"[rebuild] {'STALE' if stale else 'current'}: {why}", flush=True)
    if args.check:
        return 1 if stale else 0
    if args.if_stale and not stale:
        print("[rebuild] nothing to do", flush=True)
        return 0
    try:
        rebuild()
    except BaseException as exc:                                # noqa: BLE001
        print(f"route_rebuild failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
