"""ops.job_queue plumbing — plain Postgres queue, FOR UPDATE SKIP LOCKED.

Ruling §3.1-10: the partial unique index (source_id) WHERE status IN
('queued','running') makes double-enqueue impossible while keeping unlimited
completed history. No Redis, no Celery.
"""
from __future__ import annotations

import psycopg

# A skip is a successful contact/decision for scheduling purposes — otherwise
# an unchanged (or keyless) source would re-enqueue every tick.
# Failed/gated runs are NOT free retries: they back off exponentially
# (5 min × 2^(streak-1), capped at 6 h per pipeline.md §10.1) — a persistently
# broken source must never be re-hit at tick cadence (politeness contract,
# MASTER_PLAN §9), and a degraded fast feed slows down, it never speeds up.
_ENQUEUE_SQL = """
INSERT INTO ops.job_queue (source_id)
SELECT s.source_id
FROM ops.sources s
LEFT JOIN LATERAL (
    SELECT max(r.started_at) AS last_ok_at
    FROM ops.source_runs r
    WHERE r.source_id = s.source_id
      AND r.status IN ('success', 'skipped_unchanged', 'skipped_no_key')
) ok ON TRUE
LEFT JOIN LATERAL (
    SELECT max(r.started_at) AS last_bad_at, count(*) AS streak
    FROM ops.source_runs r
    WHERE r.source_id = s.source_id
      AND r.status IN ('failed', 'gated')
      AND (ok.last_ok_at IS NULL OR r.started_at > ok.last_ok_at)
) bad ON TRUE
WHERE s.enabled
  AND (ok.last_ok_at IS NULL
       OR ok.last_ok_at < now() - make_interval(mins => s.schedule_minutes))
  AND (bad.last_bad_at IS NULL
       OR bad.last_bad_at < now() - make_interval(mins =>
              LEAST(360, 5 * (1 << LEAST(GREATEST(bad.streak - 1, 0), 7)::int))))
ON CONFLICT (source_id) WHERE status IN ('queued', 'running') DO NOTHING
"""

_CLAIM_SQL = """
UPDATE ops.job_queue
SET status = 'running', started_at = now()
WHERE job_id = (
    SELECT job_id FROM ops.job_queue
    WHERE status = 'queued'
    ORDER BY enqueued_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING job_id, source_id
"""


def enqueue_due(conn: psycopg.Connection) -> int:
    """Queue a job for every enabled source whose last successful run is older
    than its schedule_minutes (or that never ran).

    A trailing streak of failed/gated runs delays the next attempt by an
    exponential backoff (5 min doubling per consecutive failure, capped at
    6 h) so a broken source is retried politely, not every tick.

    INSERT ... ON CONFLICT DO NOTHING against the partial unique index, so a
    source already queued/running is a silent no-op. Returns jobs inserted.
    """
    return conn.execute(_ENQUEUE_SQL).rowcount


def claim_job(conn: psycopg.Connection) -> dict | None:
    """Claim the oldest queued job, or None when the queue is empty.

    SELECT ... FOR UPDATE SKIP LOCKED, mark status='running', set started_at —
    all inside the caller's transaction so a crashed worker releases the row.
    Returns {"job_id": int, "source_id": str} or None.
    """
    row = conn.execute(_CLAIM_SQL).fetchone()
    if row is None:
        return None
    return {"job_id": row[0], "source_id": row[1]}


def finish_job(
    conn: psycopg.Connection,
    job_id: int,
    status: str,
    message: str | None = None,
) -> None:
    """Mark a claimed job done|failed with finished_at=now()."""
    if status not in ("done", "failed"):
        raise ValueError(f"finish_job status must be done|failed, got {status!r}")
    conn.execute(
        "UPDATE ops.job_queue SET status = %s, finished_at = now(), message = %s "
        "WHERE job_id = %s",
        (status, message, job_id),
    )
