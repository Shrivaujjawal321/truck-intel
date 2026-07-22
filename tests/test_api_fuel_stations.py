"""GET /v1/fuel tests (api/routes_fuel_stations.py).

The router is mounted on a local test app (main.py registration belongs to
the integrator), so these tests run before the route is wired in. Layers:
- validation + envelope: no DB
- rendering tests on canned rows (monkeypatched common.q_all): tri-state
  true/false/null passthrough, filter notes, ODbL attribution, EIA regional
  price embed (state -> PADD, most specific region wins)
- DB-backed shape test: skips when PostGIS is unreachable; osm.fuel_stations
  may legitimately be empty — shape only, never row counts.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api import common, routes_fuel_stations
from tests.conftest import needs_db

BBOX_OK = "-75.9,38.4,-74.9,39.9"  # Delaware-ish
BBOX_HUGE = "-80,35,-70,45"

app = FastAPI()
common.install_error_handlers(app)
app.include_router(routes_fuel_stations.router)


def _get(path: str, **params):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, params=params)

    return asyncio.run(go())


# ---------------------------------------------------------------- validation

def test_bbox_required():
    resp = _get("/v1/fuel")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_bbox"


def test_bbox_too_large():
    resp = _get("/v1/fuel", bbox=BBOX_HUGE)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bbox_too_large"


# ------------------------------------------------- state -> EIA region logic

def test_state_to_regions_covers_50_states_plus_dc():
    m = routes_fuel_stations.STATE_TO_REGIONS
    assert len(m) == 51
    assert m["DE"] == ("PADD1B", "PADD1", "US")   # sub-PADD first
    assert m["TX"] == ("PADD3", "US")
    assert m["CA"] == ("CA", "PADD5", "US")       # EIA publishes CA alone
    assert m["WA"] == ("PADD5_EX_CA", "PADD5", "US")
    # every fallback chain ends at the national number
    assert all(chain[-1] == "US" for chain in m.values())


def test_regional_price_most_specific_wins_and_unknown_is_none():
    prices = {
        "PADD1B": {"region": "PADD1B", "week_of": date(2026, 7, 20),
                   "price_usd_gal": 3.62},
        "US": {"region": "US", "week_of": date(2026, 7, 20),
               "price_usd_gal": 3.71},
    }
    hit = routes_fuel_stations._regional_price("DE", prices)
    assert hit["region"] == "PADD1B"
    assert hit["kind"] == "regional_weekly_estimate"
    # TX has no PADD3 row loaded -> falls back to US
    assert routes_fuel_stations._regional_price("TX", prices)["region"] == "US"
    # unknown state -> None, never a fabricated number
    assert routes_fuel_stations._regional_price(None, prices) is None
    assert routes_fuel_stations._regional_price("DE", {}) is None


# ----------------------------------------------------- rendering (canned DB)

_STATION_ROWS = [
    {
        "osm_id": "node/1", "name": "Truck Alpha", "brand": "Pilot",
        "state": "DE", "has_diesel": True, "hgv_access": True, "has_def": None,
        "opening_hours": "24/7",
        "gj": '{"type":"Point","coordinates":[-75.5,39.7]}',
        "confidence": None, "source_id": "osm_pois", "run_id": 7,
        "ingested_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "observed_at": datetime(2026, 7, 21, 20, 21, 50, tzinfo=timezone.utc),
    },
    {
        "osm_id": "node/3", "name": None, "brand": None,
        "state": None, "has_diesel": None, "hgv_access": None, "has_def": None,
        "opening_hours": None,
        "gj": '{"type":"Point","coordinates":[-75.52,39.72]}',
        "confidence": None, "source_id": "osm_pois", "run_id": 7,
        "ingested_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "observed_at": None,
    },
]

_PRICE_ROWS = [
    {"region": "PADD1B", "week_of": date(2026, 7, 20), "price_usd_gal": 3.62},
    {"region": "US", "week_of": date(2026, 7, 20), "price_usd_gal": 3.71},
]


@pytest.fixture()
def canned_db(monkeypatch):
    """Route common.q_all onto canned rows; capture the station SQL+params."""
    captured = {}

    def fake_q_all(sql, params=None):
        if "core.fuel_prices" in sql:
            return list(_PRICE_ROWS)
        captured["sql"], captured["params"] = sql, params
        return [dict(r) for r in _STATION_ROWS]

    monkeypatch.setattr(common, "q_all", fake_q_all)
    return captured


def test_tristate_rendered_literally_with_attribution(canned_db):
    resp = _get("/v1/fuel", bbox=BBOX_OK)
    assert resp.status_code == 200
    body = resp.json()
    assert body["attribution"] == "© OpenStreetMap contributors"
    assert body["filter_notes"] == []  # no filters -> no exclusion caveats
    props = {f["id"]: f["properties"] for f in body["features"]}
    alpha, bare = props["node/1"], props["node/3"]
    # tri-state booleans: true / false / null — NOT the 'unknown' string
    assert alpha["has_diesel"] is True and alpha["has_def"] is None
    assert bare["has_diesel"] is None and bare["hgv_access"] is None
    # non-boolean NULLs still render 'unknown' per the common.py decree
    assert bare["name"] == "unknown" and bare["observed_at"] == "unknown"
    assert alpha["opening_hours"] == "24/7"
    assert all(p["attribution"] == "© OpenStreetMap contributors"
               for p in props.values())
    # no price=true -> no embed at all
    assert "regional_price" not in alpha


def test_diesel_and_hgv_filters_note_null_exclusion(canned_db):
    resp = _get("/v1/fuel", bbox=BBOX_OK, diesel="true", hgv="true")
    assert resp.status_code == 200
    assert "has_diesel IS TRUE" in canned_db["sql"]
    assert "hgv_access IS TRUE" in canned_db["sql"]
    notes = " ".join(resp.json()["filter_notes"])
    assert "unknown" in notes and "diesel" in notes and "hgv" in notes


def test_brand_filter_is_parameterized(canned_db):
    resp = _get("/v1/fuel", bbox=BBOX_OK, brand="pilot")
    assert resp.status_code == 200
    assert "ILIKE" in canned_db["sql"]
    assert "pilot" in canned_db["params"]  # value travels as a bind parameter


def test_price_embed_is_regional_estimate_never_pump_price(canned_db):
    resp = _get("/v1/fuel", bbox=BBOX_OK, price="true")
    props = {f["id"]: f["properties"] for f in resp.json()["features"]}
    de = props["node/1"]["regional_price"]
    assert de["kind"] == "regional_weekly_estimate"
    assert de["region"] == "PADD1B" and de["price_usd_gal"] == 3.62
    assert "never a station pump price" in de["note"]
    # unknown state -> null embed, honestly
    assert props["node/3"]["regional_price"] is None


# ------------------------------------------------------------------ DB shape

@needs_db
def test_db_shape_empty_table_is_fine():
    resp = _get("/v1/fuel", bbox=BBOX_OK, price="true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert body["count"] == len(body["features"])
    assert body["attribution"] == "© OpenStreetMap contributors"
    for f in body["features"]:  # table may be empty; shape only
        assert f["properties"]["attribution"] == "© OpenStreetMap contributors"
        assert "has_diesel" in f["properties"]
