"""API tests. Two layers:
- validation + envelope tests: no DB needed, always run
- DB-backed shape tests: skip gracefully when PostGIS is unreachable

Tables may legitimately be empty (connectors land separately), so DB-backed
tests assert response *shape*, never row counts. No fake rows are ever
inserted into core tables — real data only, even in tests.

Run: uv run pytest
"""
from __future__ import annotations

import asyncio

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from api import common
from api.main import app
from api.routes_live import _zones

BBOX_OK = "-75.5,39.5,-73.5,41.5"  # 2x2 deg around NYC/Philadelphia
BBOX_4X4 = "-80.0,36.0,-76.0,40.0"  # exactly at the cap: allowed
BBOX_HUGE = "-80,35,-70,45"  # 10x10: rejected

GEO_PATHS = ["/v1/bridges", "/v1/parking", "/v1/live/weather-alerts"]


def _get(path: str, **params):
    """GET against the app in-process (httpx AsyncClient + ASGITransport)."""

    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, params=params)

    return asyncio.run(go())


def _err_code(resp) -> str:
    """Assert the one error envelope shape, return the stable code."""
    body = resp.json()
    assert set(body) == {"error"}, f"not the envelope: {body}"
    assert set(body["error"]) == {"code", "message"}
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)
    return body["error"]["code"]


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

@pytest.mark.parametrize("path", GEO_PATHS)
def test_bbox_required(path):
    resp = _get(path)
    assert resp.status_code == 400
    assert _err_code(resp) == "invalid_bbox"


@pytest.mark.parametrize("path", GEO_PATHS)
def test_bbox_too_large(path):
    resp = _get(path, bbox=BBOX_HUGE)
    assert resp.status_code == 400
    assert _err_code(resp) == "bbox_too_large"


@pytest.mark.parametrize(
    "bad",
    [
        "abc",  # not numbers
        "1,2,3",  # wrong arity
        "1,2,3,4,5",  # wrong arity
        "10,40,-10,41",  # min_lon > max_lon
        "-200,40,-199,41",  # longitude out of range
        "-75,95,-74,96",  # latitude out of range
    ],
)
def test_bbox_malformed(bad):
    resp = _get("/v1/bridges", bbox=bad)
    assert resp.status_code == 400
    assert _err_code(resp) == "invalid_bbox"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 1001},
        {"limit": 0},
        {"offset": -1},
        {"max_clearance_lt_in": -3},
        {"max_clearance_lt_in": "tall"},
    ],
)
def test_invalid_param(params):
    resp = _get("/v1/bridges", bbox=BBOX_OK, **params)
    assert resp.status_code == 400
    assert _err_code(resp) == "invalid_param"


def test_unknown_route_uses_envelope():
    resp = _get("/v1/nope")
    assert resp.status_code == 404
    assert _err_code(resp) == "not_found"


def test_db_down_is_upstream_unavailable(monkeypatch):
    def boom():
        raise psycopg.OperationalError("connection refused (simulated)")

    monkeypatch.setattr(common, "connect_ro", boom)
    resp = _get("/v1/bridges", bbox=BBOX_OK)
    assert resp.status_code == 503
    assert _err_code(resp) == "upstream_unavailable"


def test_health_degraded_when_db_down(monkeypatch):
    def boom():
        raise psycopg.OperationalError("connection refused (simulated)")

    monkeypatch.setattr(common, "connect_ro", boom)
    resp = _get("/v1/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


# ------------------------------------------------------------- unit helpers

def test_parse_bbox_ok():
    assert common.parse_bbox("1,2,3,4") == (1.0, 2.0, 3.0, 4.0)


def test_parse_bbox_exactly_4x4_allowed():
    assert common.parse_bbox(BBOX_4X4) == (-80.0, 36.0, -76.0, 40.0)


def test_parse_bbox_error_codes():
    with pytest.raises(common.ApiError) as exc:
        common.parse_bbox("-80,35,-70,45")
    assert exc.value.code == "bbox_too_large"
    with pytest.raises(common.ApiError) as exc:
        common.parse_bbox("a,b,c,d")
    assert exc.value.code == "invalid_bbox"


def test_unknown_rendering():
    assert common.unknown(None) == "unknown"
    assert common.unknown(0) == 0  # 0 is real data, not unknown
    assert common.unknown("") == ""


def test_zones_extraction():
    assert _zones({"geocode": {"UGC": ["CAZ006", "CAZ007"]}}) == ["CAZ006", "CAZ007"]
    assert _zones({"zones": ["TXZ001"]}) == ["TXZ001"]
    assert _zones({"affectedZones": ["https://api.weather.gov/zones/forecast/AZZ543"]}) == [
        "https://api.weather.gov/zones/forecast/AZZ543"
    ]
    assert _zones({}) == []
    assert _zones({"geocode": None}) == []


# ---------------------------------------------------------------- DB-backed

def _assert_feature_collection(body: dict):
    assert body["type"] == "FeatureCollection"
    assert isinstance(body["features"], list)
    assert body["count"] == len(body["features"])
    for feat in body["features"][:10]:
        assert feat["type"] == "Feature"
        props = feat["properties"]
        for key in ("source_id", "observed_at", "vintage", "confidence", "attribution"):
            assert key in props, f"missing {key}"
        assert props["confidence"] is not None  # NULL must render "unknown"


@needs_db
def test_bridges_shape():
    resp = _get("/v1/bridges", bbox=BBOX_OK)
    assert resp.status_code == 200
    _assert_feature_collection(resp.json())


@needs_db
def test_bridges_bbox_at_cap_is_allowed():
    resp = _get("/v1/bridges", bbox=BBOX_4X4)
    assert resp.status_code == 200


@needs_db
def test_bridges_clearance_filter():
    resp = _get("/v1/bridges", bbox=BBOX_OK, max_clearance_lt_in=170)
    assert resp.status_code == 200
    for feat in resp.json()["features"]:
        clearance = feat["properties"]["min_vert_clearance_in"]
        # unknown clearance must never match a "below X" filter
        assert isinstance(clearance, (int, float)) and clearance < 170


@needs_db
def test_bridges_pagination():
    resp = _get("/v1/bridges", bbox=BBOX_OK, limit=1, offset=0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] <= 1 and body["limit"] == 1


@needs_db
def test_parking_shape_and_vintage():
    resp = _get("/v1/parking", bbox=BBOX_OK)
    assert resp.status_code == 200
    body = resp.json()
    _assert_feature_collection(body)
    for feat in body["features"]:
        assert "2019" in feat["properties"]["vintage"]
        assert "truck_spaces" in feat["properties"]


@needs_db
def test_weather_alerts_shape():
    resp = _get("/v1/live/weather-alerts", bbox=BBOX_OK)
    assert resp.status_code == 200
    body = resp.json()
    _assert_feature_collection(body)
    # bbox-only mode never returns geometry-less features
    assert all(f["geometry"] is not None for f in body["features"])


@needs_db
def test_weather_alerts_include_nongeo():
    resp = _get("/v1/live/weather-alerts", bbox=BBOX_OK, include_nongeo="true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["include_nongeo"] is True
    for feat in body["features"]:
        if feat["geometry"] is None:  # zone-only alerts carry their zone codes
            assert isinstance(feat["properties"]["zones"], list)


@needs_db
def test_fuel_prices_kind_label():
    resp = _get("/v1/fuel/prices")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert body["count"] == len(body["items"])
    for item in body["items"]:
        assert item["kind"] == "regional_weekly_estimate"
        assert "attribution" in item and "observed_at" in item


@needs_db
def test_fuel_prices_region_filter():
    resp = _get("/v1/fuel/prices", region="US")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["region"].upper() == "US"


@needs_db
def test_coverage_shape():
    resp = _get("/v1/meta/coverage")
    assert resp.status_code == 200
    body = resp.json()
    assert "generated_at" in body
    assert isinstance(body["sources"], list)
    for src in body["sources"]:
        assert src["slo_status"] in ("ok", "stale", "never_ran")
        for key in ("source_id", "license", "attribution", "row_count", "vintage", "last_run"):
            assert key in src


@needs_db
def test_health_ok():
    resp = _get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["database"] == "up"
    assert "last_run_at" in body and "last_run_age_seconds" in body
