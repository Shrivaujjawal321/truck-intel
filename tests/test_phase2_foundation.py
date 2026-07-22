"""Phase-2 foundation: registry parser/target keys, feed-health circuit
breaker (open/half-open/close), and the post-swap rescore hook.

Pure tests never touch the DB; DB-backed tests use the live dev PostGIS and
delete every row they create (core tables are never touched — snapshot
publishes go to a scratch schema).
"""
from __future__ import annotations

import json
import sys
import types

import pytest
import yaml

from tests.conftest import needs_db
from truckintel import engine, jobs, loaders
from truckintel.db import get_conn
from truckintel.politeness import PoliteResult
from truckintel.registry import SNAPSHOT_TARGETS, load_registry, sync_sources

VALID_DOC = {
    "id": "src_p2",
    "name": "A phase-2 source",
    "url": "https://example.gov/data",
    "kind": "arcgis",
    "load_pattern": "snapshot_swap",
    "schedule_minutes": 1440,
    "slo_hours": 48,
}


def _write(tmp_path, name: str, doc: dict) -> None:
    (tmp_path / name).write_text(yaml.safe_dump(doc))


# ------------------------------------------------- registry: parser/target keys

def test_snapshot_allowlist_is_the_5_1_naming_map():
    assert SNAPSHOT_TARGETS == {
        "core.bridges", "core.tunnels", "core.parking_sites",
        "osm.ways", "osm.fuel_stations", "osm.rest_areas", "osm.weigh_points",
    }


def test_parser_and_target_keys_optional_default_none(tmp_path):
    _write(tmp_path, "a.yaml", VALID_DOC)
    src = load_registry(tmp_path)[0]
    assert src["parser"] is None and src["target"] is None


def test_valid_parser_and_target_accepted(tmp_path):
    _write(tmp_path, "a.yaml",
           {**VALID_DOC, "parser": "nws", "target": "core.tunnels"})
    src = load_registry(tmp_path)[0]
    assert src["parser"] == "nws" and src["target"] == "core.tunnels"


def test_unimportable_parser_fails_at_sync_time(tmp_path):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "parser": "no_such_parser_xyz"})
    with pytest.raises(ValueError, match="not importable"):
        load_registry(tmp_path)


@pytest.mark.parametrize("bad", ["../evil", "os.path", "Nws", "nws;drop", ""])
def test_parser_must_be_bare_module_name(tmp_path, bad):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "parser": bad})
    with pytest.raises(ValueError, match="parser"):
        load_registry(tmp_path)


@pytest.mark.parametrize("bad", [
    "core.evil", "public.bridges", 'core."bridges";DROP TABLE ops.sources;--',
    "staging.x",
])
def test_target_outside_allowlist_rejected(tmp_path, bad):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "target": bad})
    with pytest.raises(ValueError, match="allow-list"):
        load_registry(tmp_path)


def test_target_requires_snapshot_swap(tmp_path):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "kind": "live_json",
                                "load_pattern": "event_lifecycle",
                                "target": "core.tunnels"})
    with pytest.raises(ValueError, match="snapshot_swap"):
        load_registry(tmp_path)


# ------------------------------------------------- engine: resolution fallbacks

def test_resolve_parser_registry_wins_then_fallback_map():
    assert engine._resolve_parser_module(
        {"source_id": "nbi_annual", "parser": "custom_mod"}) == "custom_mod"
    assert engine._resolve_parser_module(
        {"source_id": "nbi_annual", "parser": None}) == "nbi"
    with pytest.raises(ValueError, match="no parser configured"):
        engine._resolve_parser_module({"source_id": "unknown_src", "parser": None})


def test_resolve_target_registry_wins_then_fallback_then_allowlist():
    assert engine._resolve_snapshot_target(
        {"source_id": "x", "target": "osm.ways"}) == "osm.ways"
    assert engine._resolve_snapshot_target(
        {"source_id": "nbi_annual", "target": None}) == "core.bridges"
    with pytest.raises(ValueError, match="no snapshot target"):
        engine._resolve_snapshot_target({"source_id": "unknown_src", "target": None})
    # DB-sourced value is re-checked — an unvalidated identifier never reaches SQL
    with pytest.raises(ValueError, match="allow-list"):
        engine._resolve_snapshot_target(
            {"source_id": "x", "target": 'core."b";DROP TABLE ops.sources'})


# ------------------------------------------------- DB-backed

SRC = "test_p2_breaker_src"
SNAP_SRC = "test_p2_snapshot_src"
SCHEMA = "scratch_p2_test"
PARSER_MOD = "truckintel.parsers.test_p2_parser"


def _active_jobs(source_id: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT count(*) FROM ops.job_queue "
            "WHERE source_id = %s AND status IN ('queued', 'running')",
            (source_id,),
        ).fetchone()[0]


def _health(source_id: str) -> tuple | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT consecutive_failures, state, opened_at FROM ops.feed_health "
            "WHERE source_id = %s",
            (source_id,),
        ).fetchone()


@pytest.fixture
def temp_source():
    """Throwaway enabled, always-due source; yields pre-test max job_id so
    teardown deletes every queue row created (feed_health cascades with the
    source row)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, kind, load_pattern, "
            "schedule_minutes, slo_hours, enabled) "
            "VALUES (%s, 'breaker test source', 'live_json', 'event_lifecycle', "
            "1, 1, TRUE) ON CONFLICT (source_id) DO UPDATE SET enabled = TRUE",
            (SRC,),
        )
        max_job = conn.execute(
            "SELECT coalesce(max(job_id), 0) FROM ops.job_queue").fetchone()[0]
    yield max_job
    with get_conn() as conn:
        conn.execute("DELETE FROM ops.job_queue WHERE job_id > %s", (max_job,))
        conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
        conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))


@needs_db
def test_phase2_schema_objects_exist():
    """schema_phase2.sql applied: new tables, ops.sources columns, seed row."""
    with get_conn() as conn:
        for table in ("core.tunnels", "ops.feed_health", "quality.conflicts",
                      "quality.ai_decisions", "osm.ways", "osm.fuel_stations",
                      "osm.rest_areas", "osm.weigh_points"):
            assert conn.execute(
                "SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None, table
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'ops' AND table_name = 'sources'").fetchall()}
        assert {"parser", "target"} <= cols
        seed = conn.execute(
            "SELECT kind, enabled, schedule_minutes FROM ops.sources "
            "WHERE source_id = %s", (engine.RESCORE_SOURCE_ID,)).fetchone()
    assert seed == ("derived", True, None)


@needs_db
def test_breaker_opens_half_opens_and_closes(temp_source):
    # 4 failures: circuit still closed, counter counts
    with get_conn() as conn:
        for _ in range(4):
            engine.record_feed_health(conn, SRC, ok=False)
    failures, state, opened_at = _health(SRC)
    assert (failures, state, opened_at) == (4, "closed", None)

    # 5th failure: circuit opens
    with get_conn() as conn:
        engine.record_feed_health(conn, SRC, ok=False)
    failures, state, opened_at = _health(SRC)
    assert (failures, state) == (5, "open") and opened_at is not None

    # open circuit: the due-and-not-backed-off source is NOT enqueued
    with get_conn() as conn:
        jobs.enqueue_due(conn)
    assert _active_jobs(SRC) == 0

    # cooldown elapsed: exactly ONE probe job, feed marked half_open
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.feed_health SET opened_at = now() - interval '61 minutes' "
            "WHERE source_id = %s", (SRC,))
        jobs.enqueue_due(conn)
    assert _active_jobs(SRC) == 1
    assert _health(SRC)[1] == "half_open"
    with get_conn() as conn:  # partial unique index caps the probe at one
        jobs.enqueue_due(conn)
    assert _active_jobs(SRC) == 1

    # probe fails: circuit re-opens with a FRESH opened_at -> skipped again
    with get_conn() as conn:
        engine.record_feed_health(conn, SRC, ok=False)
        conn.execute(
            "UPDATE ops.job_queue SET status = 'failed', finished_at = now() "
            "WHERE source_id = %s AND status IN ('queued', 'running')", (SRC,))
    failures, state, opened_at = _health(SRC)
    assert (failures, state) == (6, "open")
    with get_conn() as conn:
        fresh = conn.execute(
            "SELECT opened_at > now() - interval '1 minute' FROM ops.feed_health "
            "WHERE source_id = %s", (SRC,)).fetchone()[0]
        # isolate the breaker: wipe the failed run's backoff shadow (none was
        # written — record_feed_health is the only thing we called)
        jobs.enqueue_due(conn)
    assert fresh is True
    assert _active_jobs(SRC) == 0

    # probe success: circuit closes, counter resets, scheduling resumes
    with get_conn() as conn:
        engine.record_feed_health(conn, SRC, ok=True)
    failures, state, opened_at = _health(SRC)
    assert (failures, state, opened_at) == (0, "closed", None)
    with get_conn() as conn:
        jobs.enqueue_due(conn)
    assert _active_jobs(SRC) == 1


@needs_db
def test_finish_run_wires_feed_health(temp_source):
    """_finish_run records failed runs against the breaker and healthy
    statuses (incl. gated — the feed answered, the data was bad) reset it."""
    run_id = engine._start_run(SRC)
    engine._finish_run(run_id, "failed", message="HTTP 500")
    assert _health(SRC)[:2] == (1, "closed")

    run_id = engine._start_run(SRC)
    engine._finish_run(run_id, "gated", message="min_rows gate")
    assert _health(SRC)[:2] == (0, "closed")

    run_id = engine._start_run(SRC)
    engine._finish_run(run_id, "failed", message="timeout")
    run_id = engine._start_run(SRC)
    engine._finish_run(run_id, "success")
    assert _health(SRC)[:2] == (0, "closed")


@needs_db
def test_rescore_enqueue_idempotent_and_never_auto_scheduled(temp_source):
    with get_conn() as conn:  # clean slate for the synthetic source's queue
        conn.execute(
            "DELETE FROM ops.job_queue WHERE source_id = %s "
            "AND status IN ('queued', 'running')", (engine.RESCORE_SOURCE_ID,))

    # schedule_minutes IS NULL: the tick never enqueues the synthetic source
    with get_conn() as conn:
        jobs.enqueue_due(conn)
    assert _active_jobs(engine.RESCORE_SOURCE_ID) == 0

    # explicit hook enqueues once; a second call is a silent no-op
    with get_conn() as conn:
        assert engine.enqueue_rescore(conn) is True
        assert engine.enqueue_rescore(conn) is False
    assert _active_jobs(engine.RESCORE_SOURCE_ID) == 1

    # a missing ops.sources seed row degrades to a no-op WITHOUT poisoning the
    # surrounding transaction (savepoint) — the publish it rides on survives
    with get_conn() as conn:
        assert engine.enqueue_rescore(conn, "no_such_synthetic_xyz") is False
        assert conn.execute("SELECT 1").fetchone() == (1,)  # txn still usable


@needs_db
def test_engine_worker_never_claims_derived_jobs(temp_source):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM ops.job_queue WHERE source_id = %s "
            "AND status IN ('queued', 'running')", (engine.RESCORE_SOURCE_ID,))
        engine.enqueue_rescore(conn)

    with get_conn() as conn:
        for _ in range(100):  # drain every non-derived job
            job = jobs.claim_job(conn)
            if job is None:
                break
            assert job["source_id"] != engine.RESCORE_SOURCE_ID
            jobs.finish_job(conn, job["job_id"], "done", "claimed by test, not run")
        # the derived job is still honestly queued, for its own runner
        derived = jobs.claim_job(conn, derived=True)
        assert derived is not None
        assert derived["source_id"] == engine.RESCORE_SOURCE_ID
        jobs.finish_job(conn, derived["job_id"], "done", "claimed by test, not run")


@needs_db
def test_sync_never_disables_derived_synthetic_sources():
    with get_conn() as conn:
        sync_sources(conn, load_registry("registry"))
        enabled = conn.execute(
            "SELECT enabled FROM ops.sources WHERE source_id = %s",
            (engine.RESCORE_SOURCE_ID,)).fetchone()
    assert enabled == (True,)


@needs_db
def test_snapshot_swap_run_enqueues_rescore(monkeypatch, tmp_path):
    """End-to-end: a snapshot_swap publish (DB-driven parser + target columns)
    fires the rescore hook in the same transaction."""
    monkeypatch.setenv("TRUCKINTEL_RAW_DIR", str(tmp_path))
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(
            f"CREATE TABLE {SCHEMA}.sites (LIKE core.parking_sites INCLUDING ALL)")
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, url, kind, load_pattern, "
            "schedule_minutes, slo_hours, gates, parser, target, enabled) "
            "VALUES (%s, 'p2 snapshot source', 'https://example.invalid/feed', "
            "'live_json', 'snapshot_swap', 1440, 48, '{\"min_rows\": 1}', "
            "'test_p2_parser', 'core.parking_sites', TRUE) "
            "ON CONFLICT (source_id) DO NOTHING",
            (SNAP_SRC,),
        )
        conn.execute(
            "DELETE FROM ops.job_queue WHERE source_id = %s "
            "AND status IN ('queued', 'running')", (engine.RESCORE_SOURCE_ID,))

    mod = types.ModuleType(PARSER_MOD)

    def parse(raw: bytes):
        for doc in json.loads(raw):
            yield {"site_id": doc["id"], "kind": "truck_stop", "name": doc["name"],
                   "lat": 40.0, "lon": -75.0, "props": {}}

    mod.parse = parse
    monkeypatch.setitem(sys.modules, PARSER_MOD, mod)
    monkeypatch.setattr(
        engine, "polite_get",
        lambda url, **kw: PoliteResult(
            200, b'[{"id": "s1", "name": "Stop One"}]', None, None, False),
    )

    seen_targets: list[str] = []

    def redirect_swap(conn, target, rows, *, source_id, run_id):
        seen_targets.append(target)  # resolved from the DB row, allow-listed
        return loaders.snapshot_swap(
            conn, f"{SCHEMA}.sites", rows, source_id=source_id, run_id=run_id)

    monkeypatch.setattr(engine, "snapshot_swap", redirect_swap)

    try:
        engine.run_source(SNAP_SRC)
        assert seen_targets == ["core.parking_sites"]
        with get_conn() as conn:
            published = conn.execute(
                f"SELECT site_id, source_id FROM {SCHEMA}.sites").fetchall()
            status = conn.execute(
                "SELECT status FROM ops.source_runs WHERE source_id = %s "
                "ORDER BY run_id DESC LIMIT 1", (SNAP_SRC,)).fetchone()[0]
        assert status == "success"
        assert published == [("s1", SNAP_SRC)]
        assert _active_jobs(engine.RESCORE_SOURCE_ID) == 1
    finally:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM ops.job_queue WHERE source_id = %s "
                "AND status IN ('queued', 'running')", (engine.RESCORE_SOURCE_ID,))
            conn.execute("DELETE FROM quality.rejects WHERE source_id = %s", (SNAP_SRC,))
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (SNAP_SRC,))
            conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SNAP_SRC,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SNAP_SRC,))
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
