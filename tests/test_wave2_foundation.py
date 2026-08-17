"""Wave-2 foundation: sql/schema_wave2.sql (core.businesses + staging scratch
+ derived-source seeds) and the generalized engine._DERIVED_RUNNERS dispatch.

Pure tests never touch the DB; DB-backed tests use the live dev PostGIS and
delete every row they create (core.businesses only ever sees a transient test
row that is removed in the same test).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from tests.conftest import needs_db
from truckintel import engine
from truckintel.db import get_conn

REPO = Path(__file__).resolve().parents[1]

DERIVED_SRC = "_test_wave2_derived"


# ------------------------------------------------- allow-list: the contract

def test_derived_runner_allowlist_is_the_exact_contract():
    """The EXACT map, not just membership: this allow-list is what stops an
    arbitrary source_id becoming a subprocess argv, so growing it must be a
    deliberate edit here — never a side effect of a change elsewhere.

    route_rebuild joined on 2026-07-27 with the truck_routes post-swap hook.
    """
    assert engine._DERIVED_RUNNERS == {
        "quality_rescore": ["scripts/quality_nightly.py", "--rescore", "all"],
        # Enqueued by the publish hook when core.truck_routes swaps; --if-stale
        # so an unchanged network is not rebuilt for 50 minutes.
        "route_rebuild": ["scripts/route_rebuild.py", "--if-stale"],
        "osm_pois": ["scripts/osm_extract.py", "--job", "pois"],
        "osm_ways": ["scripts/osm_extract.py", "--job", "ways"],
        # businesses rebuild chain: the two pulls + the conflate. Every seeded
        # kind='derived' source (schema_wave2.sql) has a runner here, so an
        # enqueued job runs rather than failing "no runner".
        "overture_places": ["scripts/businesses_pipeline.py", "--pull-overture"],
        "fsq_places": ["scripts/businesses_pipeline.py", "--pull-fsq", "--fsq-mirror"],
        "businesses_conflate": ["scripts/businesses_pipeline.py", "--conflate"],
    }


def test_every_allowlisted_runner_lives_inside_scripts_dir():
    for source_id, argv in engine._DERIVED_RUNNERS.items():
        script = (engine._REPO_ROOT / argv[0]).resolve()
        assert engine._SCRIPTS_DIR.resolve() in script.parents, (source_id, argv)


def test_runner_env_loads_dotenv_then_snapshots_environ(monkeypatch):
    """The wave-1 gap: derived runners must see .env vars. _runner_env folds
    .env into os.environ (load_dotenv) and hands the subprocess a copy."""
    calls = []

    def fake_dotenv():
        calls.append(True)
        import os
        os.environ["_TEST_WAVE2_DOTENV_VAR"] = "loaded"

    monkeypatch.setattr(engine, "load_dotenv", fake_dotenv)
    env = engine._runner_env()
    monkeypatch.delenv("_TEST_WAVE2_DOTENV_VAR", raising=False)
    assert calls == [True]
    assert env["_TEST_WAVE2_DOTENV_VAR"] == "loaded"


# ------------------------------------------------- DB-backed

def _psql_apply(sql_file: str) -> subprocess.CompletedProcess:
    with open(REPO / sql_file, "rb") as f:
        return subprocess.run(
            ["./scripts/db_psql.sh", "-v", "ON_ERROR_STOP=1"],
            stdin=f, cwd=REPO, capture_output=True, text=True,
        )


@pytest.fixture
def derived_job():
    """Factory: insert a throwaway derived source + a claimed ('running') job
    row, return the job dict engine._run_derived_job expects. Teardown deletes
    everything the tests created."""
    made_sources: list[str] = []

    def make(source_id: str = DERIVED_SRC) -> dict:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ops.sources (source_id, name, kind, load_pattern, "
                "schedule_minutes, slo_hours, enabled) VALUES "
                "(%s, 'wave2 dispatch test', 'derived', 'derived', NULL, NULL, TRUE) "
                "ON CONFLICT (source_id) DO NOTHING", (source_id,))
            row = conn.execute(
                "INSERT INTO ops.job_queue (source_id, status, started_at) "
                "VALUES (%s, 'running', now()) "
                "RETURNING job_id, source_id, started_at", (source_id,)).fetchone()
        made_sources.append(source_id)
        return {"job_id": row[0], "source_id": row[1], "started_at": row[2]}

    yield make
    with get_conn() as conn:
        for sid in made_sources:
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (sid,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s "
                         "AND source_id LIKE '\\_test\\_%%'", (sid,))


def _job_row(job_id: int) -> tuple:
    with get_conn() as conn:
        return conn.execute(
            "SELECT status, message FROM ops.job_queue WHERE job_id = %s",
            (job_id,)).fetchone()


@needs_db
def test_wave2_schema_objects_and_seeds_exist():
    with get_conn() as conn:
        for table in ("core.businesses", "staging.overture_places",
                      "staging.fsq_places"):
            assert conn.execute(
                "SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None, table
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'core' AND table_name = 'businesses'")}
        assert {"business_id", "name", "category", "brand", "lat", "lon", "geom",
                "address", "city", "state", "zip", "address_norm", "phone",
                "website", "present_in", "def", "source_id", "run_id",
                "ingested_at", "observed_at", "confidence", "conf_trust",
                "conf_fresh", "conf_complete", "conf_agree", "flags", "props",
                "search_tsv"} <= cols
        indexes = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'core' AND tablename = 'businesses'")}
        assert {"businesses_geom_gix", "businesses_tsv_gix",
                "businesses_name_trgm", "businesses_cat_ix"} <= indexes
        # Derived-source seeds: event-driven, so NULL schedule_minutes. The SLO
        # budget is NOT uniform, and must not be asserted as though it were:
        # slo_hours has to be >= the cadence that actually fires the source or
        # the budget is unmeetable by construction. osm_pois and businesses
        # moved to monthly timers on 2026-07-28, so schema_wave2.sql widened
        # them to 1080 h (45 d) on 2026-08-04; osm_ways stays at 400 h because
        # it is event-driven off a PBF refresh, not monthly. See the comment
        # above the seed INSERT in sql/schema_wave2.sql.
        expected_slo = {"osm_pois": 1080, "osm_ways": 400,
                        "businesses_conflate": 1080}
        for sid, slo in expected_slo.items():
            seed = conn.execute(
                "SELECT kind, load_pattern, schedule_minutes, slo_hours, enabled "
                "FROM ops.sources WHERE source_id = %s", (sid,)).fetchone()
            assert seed == ("derived", "derived", None, slo, True), sid


@needs_db
def test_schema_wave2_is_idempotent():
    if subprocess.run(["docker", "exec", "truckintel-pg", "true"],
                      capture_output=True).returncode != 0:
        pytest.skip("truckintel-pg container not reachable via docker")
    for attempt in (1, 2):  # additive DDL: re-apply must be a clean no-op
        proc = _psql_apply("sql/schema_wave2.sql")
        assert proc.returncode == 0, (attempt, proc.stderr[-800:])


@needs_db
def test_businesses_constraints_def_present_in_category():
    valid = dict(
        business_id="biz_test_wave2", name="Test Truck Stop",
        category="truck_stop", lat=40.0, lon=-75.0,
        present_in=["overture", "fsq"], def_=None,
        source_id="businesses_conflate", run_id=0,
    )
    insert = (
        "INSERT INTO core.businesses (business_id, name, category, lat, lon, "
        "geom, present_in, def, source_id, run_id) VALUES "
        "(%(business_id)s, %(name)s, %(category)s, %(lat)s, %(lon)s, "
        "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), %(present_in)s, "
        "%(def_)s, %(source_id)s, %(run_id)s)"
    )
    # def: 'inferred' or NULL ONLY (§6 — the one permitted inference)
    with pytest.raises(psycopg.errors.CheckViolation, match="def_inferred_only"):
        with get_conn() as conn:
            conn.execute(insert, {**valid, "def_": "yes"})
    # present_in: Overture/FSQ only — OSM is NEVER conflated in (§3.1-4c)
    with pytest.raises(psycopg.errors.CheckViolation, match="present_in_permissive"):
        with get_conn() as conn:
            conn.execute(insert, {**valid, "present_in": ["osm"]})
    with pytest.raises(psycopg.errors.CheckViolation, match="present_in_permissive"):
        with get_conn() as conn:
            conn.execute(insert, {**valid, "present_in": []})
    # category: our taxonomy slugs only
    with pytest.raises(psycopg.errors.CheckViolation, match="category_taxonomy"):
        with get_conn() as conn:
            conn.execute(insert, {**valid, "category": "yoga_studio"})
    # a valid row (def='inferred') inserts; search_tsv generates; cleaned up
    with get_conn() as conn:
        conn.execute(insert, {**valid, "def_": "inferred"})
        tsv = conn.execute(
            "SELECT search_tsv::text FROM core.businesses WHERE business_id = %s",
            (valid["business_id"],)).fetchone()[0]
        assert "truck" in tsv
        conn.execute("DELETE FROM core.businesses WHERE business_id = %s",
                     (valid["business_id"],))


@needs_db
def test_unknown_derived_source_finishes_failed(derived_job):
    job = derived_job()
    engine._run_derived_job(job)
    status, message = _job_row(job["job_id"])
    assert status == "failed" and "no derived-job runner" in message


@needs_db
def test_runner_outside_scripts_dir_refused(derived_job, tmp_path, monkeypatch):
    """A runner entry resolving outside <repo>/scripts/ is refused — the
    script is NOT executed (marker file proves it), the job fails honestly."""
    marker = tmp_path / "executed.marker"
    evil = tmp_path / "evil_runner.py"
    evil.write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    monkeypatch.setitem(engine._DERIVED_RUNNERS, DERIVED_SRC, [str(evil)])
    job = derived_job()
    engine._run_derived_job(job)
    status, message = _job_row(job["job_id"])
    assert status == "failed" and "outside" in message
    assert not marker.exists()


@needs_db
def test_missing_runner_script_records_honest_failed_run(derived_job, monkeypatch):
    """Allow-listed source whose script the track has not created yet -> the
    job fails with an honest message, never a crash (osm_extract.py and
    businesses_pipeline.py land in later wave-2 tracks)."""
    monkeypatch.setitem(engine._DERIVED_RUNNERS, DERIVED_SRC,
                        ["scripts/_no_such_runner_wave2.py", "--x"])
    job = derived_job()
    engine._run_derived_job(job)
    status, message = _job_row(job["job_id"])
    assert status == "failed" and "missing" in message


@needs_db
def test_dispatch_runs_allowlisted_argv_with_repo_cwd_and_dotenv_env(
        derived_job, monkeypatch):
    """Success path: exact allow-listed argv under sys.executable, cwd=repo
    root, env carries the .env-loaded environment (the wave-1 fix)."""
    monkeypatch.setitem(engine._DERIVED_RUNNERS, DERIVED_SRC,
                        ["scripts/quality_nightly.py", "--fake-flag"])
    monkeypatch.setenv("_TEST_WAVE2_ENV_SENTINEL", "present")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"], captured["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    job = derived_job()
    engine._run_derived_job(job)
    status, _ = _job_row(job["job_id"])
    assert status == "done"
    assert captured["cmd"] == [sys.executable, "scripts/quality_nightly.py",
                               "--fake-flag"]
    assert Path(captured["kwargs"]["cwd"]) == REPO
    assert captured["kwargs"]["env"]["_TEST_WAVE2_ENV_SENTINEL"] == "present"


@needs_db
def test_failed_runner_exit_code_recorded(derived_job, monkeypatch):
    monkeypatch.setitem(engine._DERIVED_RUNNERS, DERIVED_SRC,
                        ["scripts/quality_nightly.py", "--fake-flag"])
    monkeypatch.setattr(
        engine.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, stdout="",
                                                      stderr="boom detail"))
    job = derived_job()
    engine._run_derived_job(job)
    status, message = _job_row(job["job_id"])
    assert status == "failed"
    assert "exited 3" in message and "boom detail" in message
