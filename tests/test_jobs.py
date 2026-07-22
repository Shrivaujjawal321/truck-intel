"""ops.job_queue tests against the live dev DB. Every row created here is
deleted in teardown; core tables are never touched.
"""
from __future__ import annotations

import pytest

from tests.conftest import needs_db
from truckintel import jobs
from truckintel.db import get_conn

SRC = "test_jobs_src"

pytestmark = needs_db


@pytest.fixture
def temp_source():
    """A throwaway enabled source that is always due; yields the pre-test max
    job_id so teardown can delete every queue row the test created."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, kind, load_pattern, "
            "schedule_minutes, slo_hours, enabled) "
            "VALUES (%s, 'jobs test source', 'live_json', 'event_lifecycle', 1, 1, TRUE) "
            "ON CONFLICT (source_id) DO UPDATE SET enabled = TRUE",
            (SRC,),
        )
        max_job = conn.execute("SELECT coalesce(max(job_id), 0) FROM ops.job_queue").fetchone()[0]
    yield max_job
    with get_conn() as conn:
        conn.execute("DELETE FROM ops.job_queue WHERE job_id > %s", (max_job,))
        conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
        conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))


def test_enqueue_claim_finish_lifecycle(temp_source):
    with get_conn() as conn:
        inserted = jobs.enqueue_due(conn)
        assert inserted >= 1  # at least our never-ran temp source

        # Partial unique index: re-enqueue while queued is a silent no-op.
        assert jobs.enqueue_due(conn) == 0

    with get_conn() as conn:
        active = conn.execute(
            "SELECT count(*) FROM ops.job_queue "
            "WHERE source_id = %s AND status IN ('queued', 'running')",
            (SRC,),
        ).fetchone()[0]
    assert active == 1

    # Drain the queue until we hit our job (other due sources may be ahead).
    ours = None
    with get_conn() as conn:
        for _ in range(50):
            job = jobs.claim_job(conn)
            if job is None:
                break
            assert set(job) == {"job_id", "source_id"}
            if job["source_id"] == SRC:
                ours = job
                break
            jobs.finish_job(conn, job["job_id"], "done", "claimed by test, not run")
        assert ours is not None
        jobs.finish_job(conn, ours["job_id"], "done")

    with get_conn() as conn:
        status, finished_at = conn.execute(
            "SELECT status, finished_at FROM ops.job_queue WHERE job_id = %s",
            (ours["job_id"],),
        ).fetchone()
    assert status == "done" and finished_at is not None


def test_claim_returns_none_on_empty_queue(temp_source):
    with get_conn() as conn:
        jobs.enqueue_due(conn)
        for _ in range(100):  # drain everything queued
            job = jobs.claim_job(conn)
            if job is None:
                break
            jobs.finish_job(conn, job["job_id"], "done", "claimed by test, not run")
        assert jobs.claim_job(conn) is None


def test_recent_success_suppresses_enqueue(temp_source):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.source_runs (source_id, status, finished_at) "
            "VALUES (%s, 'success', now())",
            (SRC,),
        )
        jobs.enqueue_due(conn)
        queued = conn.execute(
            "SELECT count(*) FROM ops.job_queue "
            "WHERE source_id = %s AND status IN ('queued', 'running')",
            (SRC,),
        ).fetchone()[0]
    assert queued == 0  # ran moments ago; schedule_minutes=1 not yet elapsed


def test_finish_job_rejects_bad_status(temp_source):
    with get_conn() as conn:
        with pytest.raises(ValueError):
            jobs.finish_job(conn, 1, "exploded")


def test_failed_run_backs_off_not_tick_cadence(temp_source):
    """A just-failed source must NOT re-enqueue on the next tick — pipeline.md
    §10.1 backoff (5 min minimum), never a retry storm at tick cadence."""
    def queued() -> int:
        with get_conn() as conn:
            return conn.execute(
                "SELECT count(*) FROM ops.job_queue "
                "WHERE source_id = %s AND status IN ('queued', 'running')",
                (SRC,),
            ).fetchone()[0]

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.source_runs (source_id, status, started_at, finished_at) "
            "VALUES (%s, 'failed', now() - interval '2 minutes', "
            "        now() - interval '2 minutes')",
            (SRC,),
        )
        jobs.enqueue_due(conn)
    assert queued() == 0  # 2 min < the 5-min first-failure backoff

    # once the backoff window has passed, the source is due again
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET started_at = now() - interval '6 minutes' "
            "WHERE source_id = %s",
            (SRC,),
        )
        jobs.enqueue_due(conn)
    assert queued() == 1


def test_gated_streak_backoff_grows(temp_source):
    """Two consecutive gated runs -> 10-min backoff window; a 6-minute-old
    failure that would clear a streak-1 window must stay suppressed."""
    with get_conn() as conn:
        for age in ("20 minutes", "6 minutes"):
            conn.execute(
                "INSERT INTO ops.source_runs (source_id, status, started_at, finished_at) "
                f"VALUES (%s, 'gated', now() - interval '{age}', now() - interval '{age}')",
                (SRC,),
            )
        jobs.enqueue_due(conn)
        n = conn.execute(
            "SELECT count(*) FROM ops.job_queue "
            "WHERE source_id = %s AND status IN ('queued', 'running')",
            (SRC,),
        ).fetchone()[0]
    assert n == 0  # streak=2 -> 10-min backoff; last failure only 6 min old
