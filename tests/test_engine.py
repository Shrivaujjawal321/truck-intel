"""Engine tests: parser mapping (pure) + run_source outcomes on the live dev DB.

No network — polite_get is stubbed. Publishes go to a scratch schema; every
ops/quality row created here is deleted in teardown.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import date

import pytest

from tests.conftest import needs_db
from truckintel import engine, loaders
from truckintel.db import get_conn
from truckintel.politeness import PoliteResult

SRC = "test_engine_src"
SCHEMA = "scratch_engine_test"
PARSER_MOD = "truckintel.parsers.test_engine_parser"


# ---------------------------------------------------------------- pure

def test_parser_module_mapping_matches_pipeline_md():
    assert engine._PARSER_MODULE == {
        "nbi_annual": "nbi",
        "ntad_parking": "ntad_parking",
        "nws_alerts": "nws",
        "eia_diesel": "eia",
    }
    for module in engine._PARSER_MODULE.values():
        mod = importlib.import_module(f"truckintel.parsers.{module}")
        assert callable(mod.parse)


def test_every_registry_kind_has_a_raw_extension():
    assert set(engine._RAW_EXT) == {"bulk_http", "arcgis", "live_json", "api_keyed"}


def test_candidate_urls_probe_newer_vintages_first():
    from datetime import date as _date

    url = "https://www.fhwa.dot.gov/bridge/nbi/2025allstatesallrecsdel.zip"
    cands = engine._candidate_urls(url)
    assert cands[-1] == url  # configured URL is always the fallback
    assert all("2025" not in c for c in cands[:-1])  # probes are newer years only
    assert f"{_date.today().year + 1}allstatesallrecsdel.zip" in cands[0]
    # a URL without a year never probes
    assert engine._candidate_urls("https://api.weather.gov/alerts/active") == [
        "https://api.weather.gov/alerts/active"
    ]


def test_redact_masks_secret_query_params():
    msg = ("HTTPSConnectionPool(host='api.eia.gov'): Max retries exceeded with url: "
           "/v2/petroleum/pri/gnd/data/?api_key=SECRET123&offset=0")
    red = engine._redact(msg)
    assert "SECRET123" not in red and "api_key=REDACTED" in red


# ---------------------------------------------------------------- DB-backed

def _last_run(source_id: str, after_run_id: int) -> tuple | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT run_id, status, rows_in, rows_published, rows_rejected, "
            "       raw_sha256, http_status, message, finished_at "
            "FROM ops.source_runs WHERE source_id = %s AND run_id > %s "
            "ORDER BY run_id DESC LIMIT 1",
            (source_id, after_run_id),
        ).fetchone()


def _max_run_id() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT coalesce(max(run_id), 0) FROM ops.source_runs").fetchone()[0]


@needs_db
def test_eia_without_key_records_skipped_no_key(monkeypatch):
    from truckintel.registry import load_registry, sync_sources

    with get_conn() as conn:
        sync_sources(conn, load_registry("registry"))
    monkeypatch.setenv("EIA_API_KEY", "")  # empty on purpose (.env rule)
    mark = _max_run_id()
    try:
        engine.run_source("eia_diesel")  # returns without network or crash
        run = _last_run("eia_diesel", mark)
        assert run is not None
        assert run[1] == "skipped_no_key"
        assert "EIA_API_KEY" in run[7]
        assert run[8] is not None  # finished_at set
    finally:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM ops.source_runs WHERE source_id = 'eia_diesel' AND run_id > %s",
                (mark,),
            )


@needs_db
def test_run_source_end_to_end_success_skip_and_gate(monkeypatch, tmp_path):
    """Full spine on a throwaway source: fetch (stubbed) -> raw zone -> parse ->
    gates -> event_lifecycle publish into a scratch table -> run rows."""
    monkeypatch.setenv("TRUCKINTEL_RAW_DIR", str(tmp_path))

    # throwaway source + scratch publish target
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.events (LIKE core.live_events INCLUDING ALL)")
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, url, kind, load_pattern, "
            "schedule_minutes, slo_hours, gates, enabled) "
            "VALUES (%s, 'engine test source', 'https://example.invalid/feed', "
            "'live_json', 'event_lifecycle', 10, 1, '{\"min_rows\": 1}', TRUE) "
            "ON CONFLICT (source_id) DO NOTHING",
            (SRC,),
        )

    # fake parser: one good event + one reject (missing event_id)
    mod = types.ModuleType(PARSER_MOD)

    def parse(raw: bytes):
        doc = json.loads(raw)
        yield {"event_id": doc["id"], "kind": "weather_alert", "geom_wkt": None,
               "observed_at": "2026-07-22T00:00:00+00:00", "props": {"n": doc["n"]}}
        yield {"kind": "weather_alert", "observed_at": "2026-07-22T00:00:00+00:00"}

    mod.parse = parse
    monkeypatch.setitem(sys.modules, PARSER_MOD, mod)
    monkeypatch.setitem(engine._PARSER_MODULE, SRC, "test_engine_parser")
    monkeypatch.setitem(engine._REQUIRED_FIELDS, SRC, ("event_id", "kind", "observed_at"))

    payload = {"holder": b'{"id": "ev1", "n": 1}'}
    monkeypatch.setattr(
        engine, "polite_get",
        lambda url, **kw: PoliteResult(200, payload["holder"], None, None, False),
    )
    monkeypatch.setattr(
        engine, "event_lifecycle_upsert",
        lambda conn, rows, *, source_id, run_id: loaders.event_lifecycle_upsert(
            conn, rows, source_id=source_id, run_id=run_id, target=f"{SCHEMA}.events"
        ),
    )

    mark = _max_run_id()
    try:
        # run 1: success — raw archived, reject recorded, event published
        engine.run_source(SRC)
        run = _last_run(SRC, mark)
        assert run[1] == "success"
        assert (run[2], run[3], run[4]) == (2, 1, 1)  # rows_in / published / rejected
        assert run[6] == 200 and run[5] is not None
        raw_file = tmp_path / SRC / date.today().isoformat() / f"{run[5][:16]}.json"
        assert raw_file.read_bytes() == payload["holder"]
        assert (raw_file.parent / f"{run[5][:16]}.meta.json").is_file()
        with get_conn() as conn:
            reason = conn.execute(
                "SELECT reason FROM quality.rejects WHERE source_id = %s AND run_id = %s",
                (SRC, run[0]),
            ).fetchone()[0]
            published = conn.execute(
                f"SELECT event_id, source_id, run_id FROM {SCHEMA}.events"
            ).fetchall()
        assert reason == "missing_required:event_id"
        assert published == [("ev1", SRC, run[0])]

        # run 2: identical payload -> skipped_unchanged, nothing re-published
        engine.run_source(SRC)
        run2 = _last_run(SRC, run[0])
        assert run2[1] == "skipped_unchanged" and run2[5] == run[5]

        # run 3: new payload but min_rows gate trips -> gated, publish aborted
        with get_conn() as conn:
            conn.execute(
                "UPDATE ops.sources SET gates = '{\"min_rows\": 5}' WHERE source_id = %s",
                (SRC,),
            )
        payload["holder"] = b'{"id": "ev2", "n": 2}'
        engine.run_source(SRC)
        run3 = _last_run(SRC, run2[0])
        assert run3[1] == "gated" and run3[3] == 0
        with get_conn() as conn:
            still = conn.execute(f"SELECT event_id FROM {SCHEMA}.events").fetchall()
        assert still == [("ev1",)]  # old data stays live
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM quality.rejects WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@needs_db
def test_all_rows_rejected_gates_instead_of_wiping(monkeypatch, tmp_path):
    """Upstream schema drift (every row rejected) must abort the publish as
    'gated' — an event_lifecycle publish of [] would soft-close every active
    event and report success (the NWS mass-wipe bug)."""
    monkeypatch.setenv("TRUCKINTEL_RAW_DIR", str(tmp_path))
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.events (LIKE core.live_events INCLUDING ALL)")
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, url, kind, load_pattern, "
            "schedule_minutes, slo_hours, gates, enabled) "
            "VALUES (%s, 'engine drift source', 'https://example.invalid/feed', "
            "'live_json', 'event_lifecycle', 10, 1, '{\"min_rows\": 0}', TRUE) "
            "ON CONFLICT (source_id) DO NOTHING",
            (SRC,),
        )
        # a pre-existing active event that a wipe would soft-close
        conn.execute(
            f"INSERT INTO {SCHEMA}.events (event_id, source_id, kind, first_seen, "
            f"last_seen, run_id, props) VALUES ('keep', %s, 'weather_alert', now(), "
            f"now(), 1, '{{}}')",
            (SRC,),
        )

    mod = types.ModuleType(PARSER_MOD)

    def parse(raw: bytes):  # schema drift: observed_at missing on every row
        yield {"event_id": "ev-drift", "kind": "weather_alert"}
        yield {"event_id": "ev-drift2", "kind": "weather_alert"}

    mod.parse = parse
    monkeypatch.setitem(sys.modules, PARSER_MOD, mod)
    monkeypatch.setitem(engine._PARSER_MODULE, SRC, "test_engine_parser")
    monkeypatch.setitem(engine._REQUIRED_FIELDS, SRC, ("event_id", "kind", "observed_at"))
    monkeypatch.setattr(
        engine, "polite_get",
        lambda url, **kw: PoliteResult(200, b'{"drift": true}', None, None, False),
    )
    monkeypatch.setattr(
        engine, "event_lifecycle_upsert",
        lambda conn, rows, *, source_id, run_id: loaders.event_lifecycle_upsert(
            conn, rows, source_id=source_id, run_id=run_id, target=f"{SCHEMA}.events"
        ),
    )

    mark = _max_run_id()
    try:
        engine.run_source(SRC)
        run = _last_run(SRC, mark)
        assert run[1] == "gated" and "all 2 rows rejected" in run[7]
        with get_conn() as conn:
            still_open = conn.execute(
                f"SELECT count(*) FROM {SCHEMA}.events WHERE soft_closed_at IS NULL"
            ).fetchone()[0]
        assert still_open == 1  # the active event was NOT mass-soft-closed
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM quality.rejects WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@needs_db
def test_geometry_valid_pct_gate_trips(monkeypatch, tmp_path):
    """Systematic coordinate drift below the registry threshold aborts the
    publish (MASTER_PLAN §11 geometry-valid >= 98%)."""
    monkeypatch.setenv("TRUCKINTEL_RAW_DIR", str(tmp_path))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, url, kind, load_pattern, "
            "schedule_minutes, slo_hours, gates, enabled) "
            "VALUES (%s, 'engine geom source', 'https://example.invalid/feed', "
            "'live_json', 'event_lifecycle', 10, 1, "
            "'{\"min_rows\": 1, \"geometry_valid_pct\": 98}', TRUE) "
            "ON CONFLICT (source_id) DO NOTHING",
            (SRC,),
        )

    mod = types.ModuleType(PARSER_MOD)

    def parse(raw: bytes):
        yield {"event_id": "good", "kind": "weather_alert",
               "observed_at": "2026-07-22T00:00:00+00:00", "lat": 40.0, "lon": -75.0}
        yield {"event_id": "junk", "kind": "weather_alert",
               "observed_at": "2026-07-22T00:00:00+00:00", "lat": 0.0, "lon": 0.0}

    mod.parse = parse
    monkeypatch.setitem(sys.modules, PARSER_MOD, mod)
    monkeypatch.setitem(engine._PARSER_MODULE, SRC, "test_engine_parser")
    monkeypatch.setitem(engine._REQUIRED_FIELDS, SRC, ("event_id", "kind", "observed_at"))
    monkeypatch.setattr(
        engine, "polite_get",
        lambda url, **kw: PoliteResult(200, b'{"geom": "drift"}', None, None, False),
    )

    mark = _max_run_id()
    try:
        engine.run_source(SRC)
        run = _last_run(SRC, mark)
        assert run[1] == "gated" and "geometry_valid_pct gate" in run[7]
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM quality.rejects WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))


@needs_db
def test_run_source_failure_records_failed_run(monkeypatch):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ops.sources (source_id, name, url, kind, load_pattern, "
            "schedule_minutes, slo_hours, enabled) "
            "VALUES (%s, 'engine fail source', 'https://example.invalid/x', "
            "'live_json', 'event_lifecycle', 10, 1, TRUE) "
            "ON CONFLICT (source_id) DO NOTHING",
            (SRC,),
        )
    monkeypatch.setattr(
        engine, "polite_get",
        lambda url, **kw: PoliteResult(500, b"", None, None, False),
    )
    mark = _max_run_id()
    try:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            engine.run_source(SRC)
        run = _last_run(SRC, mark)
        assert run[1] == "failed" and "HTTP 500" in run[7]
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))
