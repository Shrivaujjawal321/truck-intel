"""ops.job_queue plumbing — plain Postgres queue, FOR UPDATE SKIP LOCKED.

Ruling §3.1-10: the partial unique index (source_id) WHERE status IN
('queued','running') makes double-enqueue impossible while keeping unlimited
completed history. No Redis, no Celery.
"""
from __future__ import annotations

import psycopg


def enqueue_due(conn: psycopg.Connection) -> int:
    """Queue a job for every enabled source whose last successful run is older
    than its schedule_minutes (or that never ran).

    INSERT ... ON CONFLICT DO NOTHING against the partial unique index, so a
    source already queued/running is a silent no-op. Returns jobs inserted.
    """
    raise NotImplementedError


def claim_job(conn: psycopg.Connection) -> dict | None:
    """Claim the oldest queued job, or None when the queue is empty.

    SELECT ... FOR UPDATE SKIP LOCKED, mark status='running', set started_at —
    all inside the caller's transaction so a crashed worker releases the row.
    Returns {"job_id": int, "source_id": str} or None.
    """
    raise NotImplementedError


def finish_job(
    conn: psycopg.Connection,
    job_id: int,
    status: str,
    message: str | None = None,
) -> None:
    """Mark a claimed job done|failed with finished_at=now()."""
    raise NotImplementedError
