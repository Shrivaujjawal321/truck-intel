"""Registry loading/validation (pure) + ops.sources sync (live dev DB)."""
from __future__ import annotations

import pytest
import yaml

from tests.conftest import needs_db
from truckintel.db import get_conn
from truckintel.registry import load_registry, sync_sources

EXPECTED_KEYS = {
    "id", "name", "owner", "url", "kind", "load_pattern", "schedule_minutes",
    "license", "attribution", "slo_hours", "gates", "auth",
}

VALID_DOC = {
    "id": "src_ok",
    "name": "A valid source",
    "url": "https://example.gov/data",
    "kind": "live_json",
    "load_pattern": "event_lifecycle",
    "schedule_minutes": 10,
    "slo_hours": 1,
}


def _write(tmp_path, name: str, doc: dict) -> None:
    (tmp_path / name).write_text(yaml.safe_dump(doc))


# ---------------------------------------------------------------- load_registry

def test_real_registry_loads_and_validates():
    sources = load_registry("registry")
    assert {s["id"] for s in sources} == {"eia_diesel", "nbi_annual", "ntad_parking", "nws_alerts"}
    for s in sources:
        assert set(s) == EXPECTED_KEYS
        assert isinstance(s["gates"], dict)
    eia = next(s for s in sources if s["id"] == "eia_diesel")
    assert eia["auth"] == {"env": "EIA_API_KEY"}
    assert eia["kind"] == "api_keyed" and eia["load_pattern"] == "upsert"


def test_malformed_yaml_fails_loudly(tmp_path):
    (tmp_path / "bad.yaml").write_text("id: [unclosed\nname: 'x")
    with pytest.raises(ValueError, match="malformed YAML"):
        load_registry(tmp_path)


def test_unknown_kind_rejected(tmp_path):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "kind": "ftp_scrape"})
    with pytest.raises(ValueError, match="kind"):
        load_registry(tmp_path)


def test_unknown_load_pattern_rejected(tmp_path):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "load_pattern": "scd2"})
    with pytest.raises(ValueError, match="load_pattern"):
        load_registry(tmp_path)


def test_nonpositive_schedule_rejected(tmp_path):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "schedule_minutes": 0})
    with pytest.raises(ValueError, match="schedule_minutes"):
        load_registry(tmp_path)


def test_auth_without_env_name_fails_loudly(tmp_path):
    _write(tmp_path, "a.yaml", {**VALID_DOC, "auth": {"env": ""}})
    with pytest.raises(ValueError, match="auth.env"):
        load_registry(tmp_path)


def test_auth_env_value_unset_is_allowed(tmp_path, monkeypatch):
    # A *named but unset* env var is the graceful skipped_no_key path — not a sync error.
    monkeypatch.delenv("SOME_UNSET_KEY_XYZ", raising=False)
    _write(tmp_path, "a.yaml", {**VALID_DOC, "auth": {"env": "SOME_UNSET_KEY_XYZ"}})
    sources = load_registry(tmp_path)
    assert sources[0]["auth"] == {"env": "SOME_UNSET_KEY_XYZ"}


def test_duplicate_source_id_rejected(tmp_path):
    _write(tmp_path, "a.yaml", VALID_DOC)
    _write(tmp_path, "b.yaml", VALID_DOC)
    with pytest.raises(ValueError, match="duplicate"):
        load_registry(tmp_path)


def test_empty_registry_dir_rejected(tmp_path):
    with pytest.raises(ValueError, match="no .*yaml"):
        load_registry(tmp_path)


# ---------------------------------------------------------------- sync_sources

GHOST_ID = "test_sync_ghost"


@needs_db
def test_sync_upserts_and_disables_missing_never_deletes():
    real = load_registry("registry")
    ghost = {
        "id": GHOST_ID, "name": "ghost (test row)", "owner": None,
        "url": "https://example.invalid/", "kind": "live_json",
        "load_pattern": "event_lifecycle", "schedule_minutes": 100000,
        "license": None, "attribution": None, "slo_hours": 100000,
        "gates": {}, "auth": None,
    }
    try:
        with get_conn() as conn:
            assert sync_sources(conn, real + [ghost]) == len(real) + 1
        with get_conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM ops.sources WHERE source_id = %s", (GHOST_ID,)
            ).fetchone()
        assert row == (True,)

        # Re-sync without the ghost: it must be disabled, never deleted.
        with get_conn() as conn:
            assert sync_sources(conn, real) == len(real)
        with get_conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM ops.sources WHERE source_id = %s", (GHOST_ID,)
            ).fetchone()
            enabled_real = conn.execute(
                "SELECT count(*) FROM ops.sources WHERE enabled"
            ).fetchone()[0]
        assert row == (False,)  # still resolvable for audit history
        assert enabled_real == len(real)
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s", (GHOST_ID,))
            conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (GHOST_ID,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (GHOST_ID,))


@needs_db
def test_sync_is_idempotent():
    real = load_registry("registry")
    with get_conn() as conn:
        sync_sources(conn, real)
        sync_sources(conn, real)
        n = conn.execute(
            "SELECT count(*) FROM ops.sources WHERE source_id = ANY(%s)",
            ([s["id"] for s in real],),
        ).fetchone()[0]
    assert n == len(real)
