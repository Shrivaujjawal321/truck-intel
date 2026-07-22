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
#
# The ops.feed_health circuit breaker (pipeline.md §10.3) composes with the
# backoff — exactly ONE of the two paces any given attempt, so the breaker's
# cooldown_minutes knob is never dead:
# - Circuit CLOSED: BACKOFF spaces individual retries after failed/gated runs
#   (5 min doubling, cap 6 h). It is per-attempt pacing.
# - Circuit OPEN/HALF_OPEN: the BREAKER alone paces recovery — the source is
#   skipped until opened_at + cooldown_minutes elapses, then exactly ONE probe
#   job is allowed (half-open — the partial unique index enforces the "one");
#   a probe success closes the circuit, a probe failure re-opens it with a
#   fresh opened_at, so a dead feed is probed once per cooldown (default 60
#   min — pipeline.md §10.3 "polling drops to once/hour, not zero"). The
#   backoff MUST NOT also gate here: past the 5-failure threshold its window
#   (80, 160, 320, 360 min) always exceeds the cooldown, which would silently
#   stretch recovery probes to ~6-hour spacing and make cooldown_minutes a
#   dead tunable. The breaker also answers "is this feed dead?" in one SELECT.
#
# schedule_minutes IS NULL = event-driven source (e.g. quality_rescore): the
# tick never enqueues it; something enqueues it explicitly (post-swap hook).
_ENQUEUE_SQL = """
INSERT INTO ops.job_queue (source_id)
SELECT s.source_id
FROM ops.sources s
LEFT JOIN ops.feed_health fh ON fh.source_id = s.source_id
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
  AND s.schedule_minutes IS NOT NULL
  AND (ok.last_ok_at IS NULL
       OR ok.last_ok_at < now() - make_interval(mins => s.schedule_minutes))
  AND (CASE
       WHEN fh.state IS NOT NULL AND fh.state <> 'closed'
            AND fh.opened_at IS NOT NULL
       THEN fh.opened_at < now() - make_interval(mins => fh.cooldown_minutes)
       ELSE (bad.last_bad_at IS NULL
             OR bad.last_bad_at < now() - make_interval(mins =>
                    LEAST(360, 5 * (1 << LEAST(GREATEST(bad.streak - 1, 0), 7)::int))))
       END)
ON CONFLICT (source_id) WHERE status IN ('queued', 'running') DO NOTHING
"""

# After the insert: an open circuit whose cooldown elapsed and that now has an
# active job just got its half-open probe — record the transition so ops can
# see it and the engine knows a probe outcome decides open vs closed.
_HALF_OPEN_SQL = """
UPDATE ops.feed_health fh
SET state = 'half_open', updated_at = now()
WHERE fh.state = 'open'
  AND fh.opened_at < now() - make_interval(mins => fh.cooldown_minutes)
  AND EXISTS (SELECT 1 FROM ops.job_queue j
              WHERE j.source_id = fh.source_id
                AND j.status IN ('queued', 'running'))
"""

# The engine worker claims only non-derived jobs; derived jobs (quality_rescore
# and future conflation jobs) wait 'queued' for their own runner — an honest
# "awaiting runner", never a fake 'done' or a spurious 'failed'.
_CLAIM_SQL = """
UPDATE ops.job_queue
SET status = 'running', started_at = now()
WHERE job_id = (
    SELECT j.job_id FROM ops.job_queue j
    JOIN ops.sources s USING (source_id)
    WHERE j.status = 'queued' AND (s.kind = 'derived') = %s
    ORDER BY j.enqueued_at
    FOR UPDATE OF j SKIP LOCKED
    LIMIT 1
)
RETURNING job_id, source_id, started_at
"""

# Ruling §3.1-10 companion (see engine.enqueue_rescore): while a
# quality_rescore job is RUNNING, a concurrent snapshot swap's post-swap
# enqueue is swallowed by the one-active-job partial unique index. The rescore
# runners therefore re-check after finishing: did any snapshot_swap source
# publish successfully at/after this job's claim time? If yes, the swap's
# rescore was (or may have been) lost — re-enqueue one.
_SWAP_SINCE_SQL = """
SELECT 1
FROM ops.source_runs r
JOIN ops.sources s USING (source_id)
WHERE s.load_pattern = 'snapshot_swap'
  AND r.status = 'success'
  AND coalesce(r.finished_at, r.started_at) >= %s
LIMIT 1
"""


def enqueue_due(conn: psycopg.Connection) -> int:
    """Queue a job for every enabled, scheduled source whose last successful
    run is older than its schedule_minutes (or that never ran).

    A trailing streak of failed/gated runs delays the next attempt by an
    exponential backoff (5 min doubling per consecutive failure, capped at
    6 h) so a broken source is retried politely, not every tick.

    Open-circuit sources (ops.feed_health, pipeline.md §10.3) are skipped
    entirely until their cooldown elapses; then one half-open probe job is
    enqueued (the partial unique index caps it at one) and the feed_health row
    is marked 'half_open'. schedule_minutes IS NULL sources are event-driven
    and never enqueued here.

    INSERT ... ON CONFLICT DO NOTHING against the partial unique index, so a
    source already queued/running is a silent no-op. Returns jobs inserted.
    """
    inserted = conn.execute(_ENQUEUE_SQL).rowcount
    conn.execute(_HALF_OPEN_SQL)
    return inserted


def claim_job(conn: psycopg.Connection, *, derived: bool = False) -> dict | None:
    """Claim the oldest queued job, or None when the queue is empty.

    SELECT ... FOR UPDATE SKIP LOCKED, mark status='running', set started_at —
    all inside the caller's transaction so a crashed worker releases the row.
    derived=False (the engine worker) claims only jobs of non-derived sources;
    derived=True is for the derived-job runners (quality track, conflation) to
    claim theirs. Returns {"job_id": int, "source_id": str,
    "started_at": datetime} or None — started_at (the claim time, DB clock)
    lets rescore runners detect swaps that landed while they ran.
    """
    row = conn.execute(_CLAIM_SQL, (derived,)).fetchone()
    if row is None:
        return None
    return {"job_id": row[0], "source_id": row[1], "started_at": row[2]}


def snapshot_swapped_since(conn: psycopg.Connection, since) -> bool:
    """True when any snapshot_swap source recorded a successful publish at or
    after `since` — used by rescore runners to catch a post-swap enqueue that
    the one-active-job index swallowed while their own job was 'running'
    (binding ruling §3.1-10: every successful swap gets a rescore)."""
    return conn.execute(_SWAP_SINCE_SQL, (since,)).fetchone() is not None


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
