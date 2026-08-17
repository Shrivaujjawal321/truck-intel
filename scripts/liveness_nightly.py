#!/usr/bin/env python
"""Gate 6 nightly — refresh the presence ledger, then rescore liveness.

WHAT THIS ANSWERS
-----------------
Boss, 2026-08-17: "mechanic ki shop close hui vo nhi dikhani chiye hame."

Nothing in the schema could express that. A shop that closed in 2021 and a
shop open right now were byte-identical rows, both scored by Gate 5 as
well-formed, well-populated and federally sourced — because they were. Gate 5
scores the RECORD. This scores the SUBJECT. See truckintel/liveness.py for
the formula and the honesty rules it is bound by.

WHY IT RUNS NIGHTLY WHEN THE DATA IS MONTHLY
The inputs move on their own clocks — Overture publishes monthly, ATP runs
weekly, the licence registries daily — but DECAY is continuous. A row's
liveness falls every day nobody re-confirms it, and a driver querying at 2 a.m.
should get today's honest number, not the one computed on the day of the last
pull. The job is a few SQL statements over indexed columns; running it nightly
costs less than reasoning about when it needs to run.

ORDER MATTERS, and it is not arbitrary:
  1. presence   fold each table's current contents into the ledger, and stamp
                missing_since on anything upstream has stopped carrying
  2. chain      fold the chains' own store locators in as a distinct witness
                — this is the step that moves 2019 truck stops to 'open'
  3. rescore    one UPDATE per table
Running 3 before 2 would score every chain-confirmed site as uncorroborated
and publish a table full of 'unknown' truck stops that we can in fact vouch
for.

Audit: exactly one ops.source_runs row per invocation. Exit 0 = success,
1 = failure (recorded on the run row).

Usage:
  uv run python scripts/liveness_nightly.py
  uv run python scripts/liveness_nightly.py --dry-run   # report, write nothing
  uv run python scripts/liveness_nightly.py --table parking_sites
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel import liveness  # noqa: E402
from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402

SOURCE_ID = "liveness"

_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status, authority_class, base_trust, trust)
VALUES
    (%(sid)s,
     'Gate 6 liveness rescore (derived — presence ledger + corroboration)',
     'truck-intel', 'derived', 'derived', 1440, 48, TRUE, 'verified',
     'curated', 0.85, 0.85)
ON CONFLICT (source_id) DO NOTHING
"""

_DIST_SQL = """
SELECT live_state, count(*)
FROM {table}
GROUP BY 1
ORDER BY CASE live_state
           WHEN 'open' THEN 1 WHEN 'likely_open' THEN 2
           WHEN 'unknown' THEN 3 WHEN 'likely_closed' THEN 4
           WHEN 'closed' THEN 5 ELSE 6 END
"""


def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID})
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


def _report(conn, cfg: liveness.TableLiveness) -> str:
    rows = conn.execute(_DIST_SQL.format(table=cfg.table)).fetchall()
    total = sum(n for _, n in rows) or 1
    parts = []
    for state, n in rows:
        label = state if state is not None else "unscored"
        parts.append(f"{label}={n} ({100 * n // total}%)")
    return " ".join(parts)


def run(*, only: str | None = None, dry_run: bool = False) -> int:
    names = [only] if only else list(liveness.TABLE_LIVENESS)
    for n in names:
        if n not in liveness.TABLE_LIVENESS:
            raise SystemExit(
                f"unknown table {n!r}; known: "
                f"{', '.join(liveness.TABLE_LIVENESS)}")

    if dry_run:
        with get_conn() as conn:
            for n in names:
                cfg = liveness.TABLE_LIVENESS[n]
                print(f"[liveness] {n}: {_report(conn, cfg)}", flush=True)
        print("[dry-run] nothing written", flush=True)
        return 0

    run_id = _start_run()
    scored_total = 0
    try:
        summary: list[str] = []
        for n in names:
            cfg = liveness.TABLE_LIVENESS[n]
            with get_conn() as conn:
                seen, gone = liveness.refresh_presence(conn, cfg)
                chain = liveness.refresh_chain_presence(conn, cfg)
                scored = liveness.rescore_liveness(conn, cfg)
                dist = _report(conn, cfg)
            scored_total += scored
            print(f"[liveness] {n}: presence={seen} chain={chain} "
                  f"vanished={gone} scored={scored}", flush=True)
            print(f"[liveness] {n}: {dist}", flush=True)
            summary.append(f"{n}: {dist}")
        _finish_run(run_id, "success", message="; ".join(summary),
                    rows_published=scored_total)
        return scored_total
    except Exception as exc:
        _finish_run(run_id, "failed", message=f"{type(exc).__name__}: {exc}")
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report current distribution, write nothing")
    ap.add_argument("--table", default=None,
                    help="score one table only (default: all three)")
    a = ap.parse_args()
    load_dotenv()
    try:
        run(only=a.table, dry_run=a.dry_run)
    except Exception as exc:
        print(f"[liveness] FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
