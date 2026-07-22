"""/v1/live/closures tests — validation + coverage honesty + DB-backed shape.

Same two-layer contract as tests/test_api.py: validation/unit tests always
run; DB-backed tests assert response shape only (tables may be empty) and
skip when PostGIS is unreachable. No fake rows in core tables, ever.

Run: uv run pytest
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from api import common
from api.main import app
from api.routes_live import _covered_states

BBOX_OK = "-122.6,47.0,-121.6,47.9"   # 1x0.9 deg around Seattle/Tacoma
BBOX_HUGE = "-120,30,-100,45"          # 20x15: rejected

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _get(path: str, **params):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, params=params)

    return asyncio.run(go())


def _db_available() -> bool:
    try:
        with common.connect_ro() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(), reason="PostGIS unreachable; DB-backed tests skipped"
)


# ---------------------------------------------------------------- validation

def test_bbox_required():
    resp = _get("/v1/live/closures")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_bbox"


def test_bbox_too_large():
    resp = _get("/v1/live/closures", bbox=BBOX_HUGE)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bbox_too_large"


def test_bad_limit_is_invalid_param():
    resp = _get("/v1/live/closures", bbox=BBOX_OK, limit=0)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_param"


# ---------------------------------------------------- coverage honesty unit

def _feed(source_id="wzdx_wa", enabled=True, slo_hours=24,
          breaker_state="closed", last_ok_hours_ago=1.0):
    return {
        "source_id": source_id,
        "enabled": enabled,
        "slo_hours": slo_hours,
        "breaker_state": breaker_state,
        "last_success_at": (
            NOW - timedelta(hours=last_ok_hours_ago)
            if last_ok_hours_ago is not None else None
        ),
    }


def test_covered_fresh_closed_feed():
    assert _covered_states([_feed()], NOW) == ["WA"]


def test_covered_state_from_qualified_source_id():
    assert _covered_states([_feed(source_id="wzdx_tx_austin")], NOW) == ["TX"]


def test_not_covered_when_stale_past_slo():
    assert _covered_states([_feed(last_ok_hours_ago=25.0)], NOW) == []


def test_not_covered_when_breaker_open():
    # feed_health says the circuit is open -> the state honestly drops out
    assert _covered_states([_feed(breaker_state="open")], NOW) == []


def test_half_open_probe_still_counts_as_covered():
    # half_open = recovering, last success still within SLO -> covered
    assert _covered_states([_feed(breaker_state="half_open")], NOW) == ["WA"]


def test_not_covered_when_disabled():
    assert _covered_states([_feed(enabled=False)], NOW) == []


def test_not_covered_when_never_succeeded():
    # no feed_health row yet (LEFT JOIN nulls) -> unknown, never covered
    assert _covered_states(
        [_feed(breaker_state=None, last_ok_hours_ago=None)], NOW
    ) == []


def test_non_wzdx_sources_ignored():
    rows = [_feed(source_id=sid) for sid in ("nws_alerts", "quality_rescore", "wzdxx_zz")]
    assert _covered_states(rows, NOW) == []


def test_covered_states_sorted_deduped():
    rows = [
        _feed(source_id="wzdx_wa"),
        _feed(source_id="wzdx_az"),
        _feed(source_id="wzdx_wa_turnpike"),
    ]
    assert _covered_states(rows, NOW) == ["AZ", "WA"]


# ---------------------------------------------------------------- DB-backed

def _assert_closures_shape(body: dict):
    assert body["type"] == "FeatureCollection"
    assert body["count"] == len(body["features"])
    # coverage honesty is part of the contract, not an extra
    assert isinstance(body["covered_states"], list)
    assert all(isinstance(s, str) and len(s) == 2 for s in body["covered_states"])
    assert "UNKNOWN" in body["coverage_note"]
    for feat in body["features"][:10]:
        assert feat["type"] == "Feature"
        props = feat["properties"]
        for key in ("event_id", "event_type", "road_names", "vehicle_impact",
                    "start_date", "end_date", "source_id", "observed_at",
                    "vintage", "confidence", "attribution", "active"):
            assert key in props, f"missing {key}"
        assert props["confidence"] is not None      # NULL renders "unknown"
        assert "not the fetch time" in props["vintage"]


@needs_db
def test_closures_shape():
    resp = _get("/v1/live/closures", bbox=BBOX_OK)
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_only"] is True              # the default, echoed
    _assert_closures_shape(body)
    # default (bbox-only, active-only): geometry always present, events active
    for feat in body["features"]:
        assert feat["geometry"] is not None
        assert feat["properties"]["active"] is True
        assert feat["properties"]["soft_closed_at"] is None


@needs_db
def test_closures_include_soft_closed_history():
    resp = _get("/v1/live/closures", bbox=BBOX_OK, active_only="false")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_only"] is False
    _assert_closures_shape(body)


@needs_db
def test_closures_include_nongeo():
    resp = _get("/v1/live/closures", bbox=BBOX_OK, include_nongeo="true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["include_nongeo"] is True
    for feat in body["features"]:
        if feat["geometry"] is None:                # honest NULL, labeled unknown
            assert feat["properties"]["road_names"] is not None


@needs_db
def test_closures_pagination():
    resp = _get("/v1/live/closures", bbox=BBOX_OK, limit=1, offset=0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] <= 1 and body["limit"] == 1
