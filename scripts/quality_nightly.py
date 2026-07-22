"""Nightly quality ladder run (MASTER_PLAN §7; design/quality-ai.md §9 §11).

Recomputes per-record confidence (gate 5) for core.bridges,
core.parking_sites, core.tunnels and ACTIVE core.live_events, after running
the registered gate-4 consistency checks (conflicts open/close feed the
agreement term and penalty). Deterministic SQL only — no AI anywhere here.

Audited like a feed (MASTER_PLAN §7, binding CRITIQUE D3): every invocation
writes EXACTLY one ops.source_runs row under a synthetic source id —
'quality_nightly' (36 h SLO) for the nightly pass, 'quality_rescore' for
post-swap rescore jobs — so a silently-dead quality job trips the same
freshness alert as a dead feed. The 'quality_nightly' ops.sources row is
seeded here idempotently (kind='derived': sync_sources never disables it,
the engine worker never claims its jobs).

Usage:
  uv run python scripts/quality_nightly.py                    # full nightly pass
  uv run python scripts/quality_nightly.py --rescore core.bridges
  uv run python scripts/quality_nightly.py --rescore all      # all snapshot tables
  uv run python scripts/quality_nightly.py --claim-jobs       # drain derived queue

--rescore is the executor for the 'quality_rescore' jobs the engine enqueues
after every snapshot swap (engine.enqueue_rescore — a swap replaces the table
object, silently dropping the nightly-computed quality columns). --claim-jobs
drains those queued jobs via jobs.claim_job(derived=True) — the engine worker
never claims derived jobs, so they wait honestly 'queued' until this runs.

INTEGRATOR NOTE — worker_loop dispatch (do NOT edit engine.py from the quality
track; this is the patch the integrator applies OR skips by scheduling
`--claim-jobs` on a timer): in engine.worker_loop, after the non-derived
claim returns None, additionally claim derived jobs and shell out:

    job = jobs.claim_job(conn, derived=True)
    if job and job["source_id"] == engine.RESCORE_SOURCE_ID:
        subprocess -> uv run python scripts/quality_nightly.py --rescore all
        jobs.finish_job(conn, job["job_id"], "done")

Until that lands, adding `--claim-jobs` to the nightly service (or its own
timer) keeps the queue honest — see deploy/truckintel-quality.service.

Exit codes: 0 = success, 1 = run failed (the failure is also recorded on the
ops.source_runs row — the audit never lies).
"""
from __future__ import annotations

import argparse
import sys

from truckintel import jobs, quality
from truckintel.config import load_dotenv
from truckintel.db import get_conn
from truckintel.engine import RESCORE_SOURCE_ID, enqueue_rescore
from truckintel.quality import TABLE_SCORING, rescore_table, run_gate4

NIGHTLY_SOURCE_ID = "quality_nightly"
NIGHTLY_SLO_HOURS = 36

# Tables a post-swap rescore covers: the snapshot_swap-scored ones (the swap
# is what drops the quality columns). Active live_events join only in the
# full nightly pass — they are never snapshot-swapped.
SNAPSHOT_SCORED = ("bridges", "tunnels", "parking_sites")
NIGHTLY_SCORED = SNAPSHOT_SCORED + ("live_events",)

# Idempotent seed (mirrors the quality_rescore seed in sql/schema_phase2.sql;
# schedule_minutes NULL = never enqueued by the tick, it runs on its own
# systemd timer). slo_hours=36 per MASTER_PLAN §7.
# HONEST LIMITATION, for the integrator: scripts/freshness_check.py reads SLOs
# from registry/*.yaml only, so this DB-seeded SLO is not alerted on yet — its
# load_slos() needs a one-line union with
#   SELECT source_id, slo_hours FROM ops.sources
#   WHERE kind = 'derived' AND slo_hours IS NOT NULL
# (noted in the track handoff; freshness_check.py is not quality-track-owned).
_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s, 'Synthetic: nightly quality ladder (gate 4 + confidence rescore)',
     'truck-intel quality track', 'derived', 'derived', NULL, %(slo)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""


def ensure_nightly_source(conn) -> None:
    """Seed the 'quality_nightly' synthetic ops.sources row (idempotent)."""
    conn.execute(_SEED_SQL, {"sid": NIGHTLY_SOURCE_ID, "slo": NIGHTLY_SLO_HOURS})


def _start_run(source_id: str) -> int:
    with get_conn() as conn:
        if source_id == NIGHTLY_SOURCE_ID:
            ensure_nightly_source(conn)
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id",
            (source_id,),
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows_published: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, message, rows_published, run_id),
        )


def _run_ladder(source_id: str, table_names: tuple[str, ...], *,
                checks=None, scoring=None,
                conflicts_table: str = "quality.conflicts") -> int:
    """Gate 4 then gate 5 over table_names, under ONE audited run row.
    checks/scoring/conflicts_table overrides exist for the tests (scratch
    schemas — production callers pass nothing)."""
    run_id = _start_run(source_id)
    try:
        with get_conn() as conn:  # one transaction: conflicts + scores land together
            gate4 = run_gate4(conn, checks, conflicts_table=conflicts_table)
            counts = {name: rescore_table(conn, name, scoring=scoring,
                                          conflicts_table=conflicts_table)
                      for name in table_names}
    except BaseException as exc:
        _finish_run(run_id, "failed",
                    message=(str(exc) or type(exc).__name__)[:1000])
        raise
    gate4_msg = "; ".join(
        f"{n} opened={o} closed={c}" for n, (o, c) in gate4.items()
    ) or "no enabled checks"
    score_msg = ", ".join(f"{n}={c}" for n, c in counts.items())
    _finish_run(
        run_id, "success",
        message=f"gate4: {gate4_msg} | rescored (changed rows): {score_msg}",
        rows_published=sum(counts.values()),
    )
    print(f"{source_id} run {run_id}: gate4 [{gate4_msg}] rescored [{score_msg}]")
    return sum(counts.values())


def run_nightly(*, checks=None, scoring=None,
                conflicts_table: str = "quality.conflicts") -> int:
    """The 03:30 pass: gate 4 + rescore all four tables, audited under
    'quality_nightly'."""
    return _run_ladder(NIGHTLY_SOURCE_ID, NIGHTLY_SCORED, checks=checks,
                       scoring=scoring, conflicts_table=conflicts_table)


def run_rescore(table_names: tuple[str, ...], *, checks=None, scoring=None,
                conflicts_table: str = "quality.conflicts") -> int:
    """Post-swap rescore, audited under 'quality_rescore' (seeded by
    sql/schema_phase2.sql). Gate 4 re-runs first so conflicts reflect the
    freshly swapped data before the penalty/agreement terms are scored."""
    return _run_ladder(RESCORE_SOURCE_ID, table_names, checks=checks,
                       scoring=scoring, conflicts_table=conflicts_table)


def _requeue_if_swap_missed(conn, job: dict) -> None:
    """Ruling §3.1-10 guard: a snapshot swap that committed while this rescore
    job was RUNNING had its post-swap enqueue swallowed by the one-active-job
    partial unique index (engine.enqueue_rescore ON CONFLICT DO NOTHING) —
    and this run either scored the pre-swap table or failed on the dropped
    one. Re-enqueue so the fresh table's confidence columns are recomputed
    now, not at the 03:30 nightly. Runs after finish_job (this job is no
    longer active, so the insert can land); idempotent — a spurious extra
    rescore only rewrites changed rows."""
    if job.get("started_at") is not None and jobs.snapshot_swapped_since(
        conn, job["started_at"]
    ):
        enqueue_rescore(conn)


def claim_jobs(*, max_jobs: int = 10) -> int:
    """Drain queued DERIVED jobs (one pass, bounded): each claimed
    'quality_rescore' job triggers a full snapshot-table rescore. Other
    derived sources have no runner here and are finished 'failed' with an
    honest message (never left claimed-forever, never faked 'done')."""
    processed = 0
    while processed < max_jobs:
        with get_conn() as conn:  # claim in its own short transaction
            job = jobs.claim_job(conn, derived=True)
        if job is None:
            break
        processed += 1
        if job["source_id"] == RESCORE_SOURCE_ID:
            try:
                run_rescore(SNAPSHOT_SCORED)
            except BaseException as exc:
                with get_conn() as conn:
                    jobs.finish_job(conn, job["job_id"], "failed",
                                    (str(exc) or type(exc).__name__)[:500])
                    _requeue_if_swap_missed(conn, job)
                raise
            with get_conn() as conn:
                jobs.finish_job(conn, job["job_id"], "done")
                _requeue_if_swap_missed(conn, job)
        else:
            with get_conn() as conn:
                jobs.finish_job(
                    conn, job["job_id"], "failed",
                    f"no derived-job runner for {job['source_id']!r} "
                    "(quality_nightly.py runs only quality_rescore)",
                )
    if processed:
        print(f"claim-jobs: processed {processed} derived job(s)")
    else:
        print("claim-jobs: queue empty")
    return processed


def _resolve_tables(arg: str) -> tuple[str, ...]:
    if arg == "all":
        return SNAPSHOT_SCORED
    by_physical = {cfg.table: name for name, cfg in TABLE_SCORING.items()}
    name = by_physical.get(arg, arg)
    if name not in TABLE_SCORING:
        known = sorted(set(by_physical) | set(TABLE_SCORING) | {"all"})
        raise SystemExit(f"unknown table {arg!r} — expected one of {known}")
    return (name,)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="truck-intel nightly quality ladder (gates 4-5)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rescore", metavar="TABLE",
                      help="rescore one table (core.bridges | bridges | ... "
                           "| all) under source 'quality_rescore'")
    mode.add_argument("--claim-jobs", action="store_true",
                      help="drain queued derived 'quality_rescore' jobs")
    args = parser.parse_args()

    load_dotenv()
    try:
        if args.claim_jobs:
            claim_jobs()
        elif args.rescore:
            run_rescore(_resolve_tables(args.rescore))
        else:
            run_nightly()
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"quality run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
