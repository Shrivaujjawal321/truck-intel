"""Tests for scripts/freshness_check.py: the SLO staleness alarm and the
dead-worker reaper. This is the timer that is supposed to notice when any of
~26 unattended scheduled jobs stops working -- a broken alarm fails silently
by definition, so it gets the same DB-backed discipline as the rest of ops.

Every row created here belongs to a throwaway `test_freshness_*` source and
is deleted in teardown; real ops.sources / ops.source_runs rows are never
touched. reap_stale() has no source_id filter (by design -- it reaps ANY
dead worker in the whole table), so the reap test additionally refuses to
run at all if a pre-existing stale 'running' row is found outside its own
fixture -- see _other_stale_running_rows/_jobs below.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from tests.conftest import needs_db
from truckintel.db import get_conn

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


freshness_check = _load("freshness_check")

pytestmark = needs_db

SRC_FRESH = "test_freshness_src"
SRC_REAP = "test_freshness_reap_src"
SLO_HOURS = 24


# --------------------------------------------------------- freshness flagging

@pytest.fixture
def freshness_source():
    """Throwaway enabled source, consumed only via a mocked load_slos()."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, kind, load_pattern, "
            "schedule_minutes, slo_hours, enabled) "
            "VALUES (%s, 'freshness test source', 'live_json', 'event_lifecycle', "
            "60, %s, TRUE) ON CONFLICT (source_id) DO UPDATE SET enabled = TRUE",
            (SRC_FRESH, SLO_HOURS),
        )
    yield
    with get_conn() as conn:
        conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC_FRESH,))
        conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC_FRESH,))


def _seed_last_success(age: timedelta) -> None:
    """Replace SRC_FRESH's run history with a single successful run `age` old."""
    with get_conn() as conn:
        conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC_FRESH,))
        conn.execute(
            "INSERT INTO ops.source_runs (source_id, status, started_at, finished_at) "
            "VALUES (%s, 'success', now() - make_interval(secs => %s), "
            "        now() - make_interval(secs => %s))",
            (SRC_FRESH, age.total_seconds(), age.total_seconds()),
        )


def test_freshness_check_flags_stale_source_past_slo(freshness_source, monkeypatch, capsys):
    """A source whose last successful run is older than its SLO is flagged
    (exit 1, printed to stderr); one comfortably inside the window is not
    (exit 0). The boundary is checked 10 minutes either side of the SLO edge
    rather than at exact equality -- `main()` samples Python's clock a few
    milliseconds after the DB clock stamped `finished_at`, so an
    exactly-equal boundary would be flaky by a hair, not by the code being
    wrong.
    """
    # Isolate from the rest of the live ops schema: only SRC_FRESH's SLO is
    # checked (no registry files, no real derived-source rows from
    # ops.sources), and the dead-worker reaper -- covered by its own test
    # below -- never runs here.
    monkeypatch.setattr(freshness_check, "load_slos", lambda: {SRC_FRESH: SLO_HOURS})
    monkeypatch.setattr(freshness_check, "reap_stale", lambda: None)
    monkeypatch.setattr(sys, "argv", ["freshness_check.py"])

    # comfortably fresh: 1h old against a 24h SLO
    _seed_last_success(timedelta(hours=1))
    rc = freshness_check.main()
    out, err = capsys.readouterr()
    assert rc == 0
    assert "all sources fresh" in out
    assert f"ok {SRC_FRESH}" in out
    assert SRC_FRESH not in err

    # boundary, inside: 10 minutes under the SLO -- must NOT be flagged
    _seed_last_success(timedelta(hours=SLO_HOURS) - timedelta(minutes=10))
    rc = freshness_check.main()
    out, err = capsys.readouterr()
    assert rc == 0
    assert f"ok {SRC_FRESH}" in out
    assert SRC_FRESH not in err

    # boundary, outside: 10 minutes over the SLO -- must be flagged
    _seed_last_success(timedelta(hours=SLO_HOURS) + timedelta(minutes=10))
    rc = freshness_check.main()
    out, err = capsys.readouterr()
    assert rc == 1
    assert f"FRESHNESS VIOLATION {SRC_FRESH}: last success" in err
    assert "exceeds SLO" in err

    # comfortably stale: 48h old
    _seed_last_success(timedelta(hours=48))
    rc = freshness_check.main()
    out, err = capsys.readouterr()
    assert rc == 1
    assert f"FRESHNESS VIOLATION {SRC_FRESH}: last success" in err


# --------------------------------------------------------------- dead-worker reap

@pytest.fixture
def reap_source():
    """Throwaway enabled source; teardown deletes every row this test
    created (ops.feed_health cascades off ops.sources)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, kind, load_pattern, "
            "schedule_minutes, slo_hours, enabled) "
            "VALUES (%s, 'reap test source', 'live_json', 'event_lifecycle', "
            "60, 24, TRUE) ON CONFLICT (source_id) DO UPDATE SET enabled = TRUE",
            (SRC_REAP,),
        )
    yield
    with get_conn() as conn:
        conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (SRC_REAP,))
        conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC_REAP,))
        conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC_REAP,))


def _other_stale_running_rows() -> int:
    """ops.source_runs rows reap_stale() would touch that are NOT ours.
    reap_stale() has no source_id filter -- it reaps every dead worker in
    the table -- so the test must refuse to run rather than risk mutating a
    real stuck production run."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT count(*) FROM ops.source_runs WHERE status = 'running' "
            "AND started_at < now() - make_interval(hours => %s) AND source_id <> %s",
            (freshness_check.STALE_RUNNING_HOURS, SRC_REAP),
        ).fetchone()[0]


def _other_stale_running_jobs() -> int:
    """Same guard for ops.job_queue, which reap_stale() re-queues in the
    same unfiltered pass."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT count(*) FROM ops.job_queue WHERE status = 'running' "
            "AND started_at < now() - make_interval(hours => %s) AND source_id <> %s",
            (freshness_check.STALE_RUNNING_HOURS, SRC_REAP),
        ).fetchone()[0]


def test_reap_stale_marks_dead_worker_failed_and_feeds_breaker(reap_source):
    """An ops.source_runs row left 'running' past STALE_RUNNING_HOURS (a
    worker that died mid-run and never wrote a terminal status) is reaped:
    marked 'failed' with a finished_at and an explanatory message. The
    failure is folded into ops.feed_health exactly the way a normal failed
    run is (engine._finish_run's savepoint-guarded record_feed_health call)
    -- pre-seeded at 4 consecutive failures, the reap (the 5th) must open
    the circuit, the same threshold/transition test_phase2_foundation's
    test_breaker_opens_half_opens_and_closes exercises for a live run.
    """
    hours = freshness_check.STALE_RUNNING_HOURS
    other_runs = _other_stale_running_rows()
    other_jobs = _other_stale_running_jobs()
    assert other_runs == 0 and other_jobs == 0, (
        f"{other_runs} source_runs / {other_jobs} job_queue row(s) outside "
        f"this fixture are already 'running' and older than {hours}h. "
        "reap_stale() has no source_id filter, so calling it here would "
        "reap real rows -- refusing to run rather than touch them "
        "(this itself may be worth a look: a genuinely stuck production "
        "worker older than the reap horizon)."
    )

    with get_conn() as conn:
        run_id = conn.execute(
            "INSERT INTO ops.source_runs (source_id, status, started_at) "
            "VALUES (%s, 'running', now() - make_interval(hours => %s)) "
            "RETURNING run_id",
            (SRC_REAP, hours + 1),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ops.feed_health (source_id, consecutive_failures, state) "
            "VALUES (%s, 4, 'closed')",
            (SRC_REAP,),
        )

    freshness_check.reap_stale()

    with get_conn() as conn:
        status, finished_at, message = conn.execute(
            "SELECT status, finished_at, message FROM ops.source_runs "
            "WHERE run_id = %s", (run_id,),
        ).fetchone()
        failures, state, opened_at = conn.execute(
            "SELECT consecutive_failures, state, opened_at FROM ops.feed_health "
            "WHERE source_id = %s", (SRC_REAP,),
        ).fetchone()

    assert status == "failed"
    assert finished_at is not None
    assert "reaped" in message and "stale running" in message

    # same threshold-5 open transition a normal 'failed' run would produce
    assert (failures, state) == (5, "open")
    assert opened_at is not None
