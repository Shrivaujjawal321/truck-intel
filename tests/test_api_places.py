"""GET /v1/places tests (api/routes_places.py).

The router is mounted on a local test app (main.py registration belongs to
the integrator). Layers:
- validation + envelope: no DB
- rendering on canned rows (monkeypatched common.q_all): def marker only when
  'inferred', confidence components, dual attribution (NO OSM — comment in
  the route explains why), filter notes, detail per-source blobs
- DB-backed shape test: skips when PostGIS is unreachable; core.businesses
  may legitimately be empty — shape only, never row counts.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api import common, routes_places
from tests.conftest import needs_db

BBOX_OK = "-75.9,38.4,-74.9,39.9"
BBOX_HUGE = "-80,35,-70,45"

app = FastAPI()
common.install_error_handlers(app)
app.include_router(routes_places.router)


def _get(path: str, **params):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, params=params)

    return asyncio.run(go())


# ---------------------------------------------------------------- validation

def test_bbox_required():
    resp = _get("/v1/places")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_bbox"


def test_bbox_too_large():
    resp = _get("/v1/places", bbox=BBOX_HUGE)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bbox_too_large"


def test_category_must_be_taxonomy_slug():
    resp = _get("/v1/places", bbox=BBOX_OK, category="made_up")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_param"
    assert "truck_stop" in body["error"]["message"]


def test_min_confidence_bounds():
    resp = _get("/v1/places", bbox=BBOX_OK, min_confidence=101)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_param"


# ----------------------------------------------------- rendering (canned DB)

_ROWS = [
    {
        "business_id": "biz_00000000000000aa", "name": "Love's Travel Stop",
        "category": "truck_stop", "brand": "Love's", "address": "100 I-95",
        "city": "Newark", "state": "DE", "zip": "19702", "address_norm": None,
        "phone": "302-555-0100", "website": None,
        "present_in": ["overture", "fsq"], "def": "inferred",
        "confidence": 78, "conf_trust": 65, "conf_fresh": 95,
        "conf_complete": 60, "conf_agree": 100, "flags": [],
        "source_id": "businesses_conflate", "run_id": 42,
        "ingested_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "observed_at": datetime(2026, 6, 17, tzinfo=timezone.utc),
        "gj": '{"type":"Point","coordinates":[-75.7,39.6]}',
        # Gate 6: chain-corroborated, so 'open' — the state only a current
        # authoritative confirmation can reach.
        "liveness": 92, "live_state": "open", "live_presence": 88,
        "live_sources": 80, "live_corrob": 100,
        "live_reasons": ["chain_confirmed:loves"],
        "last_seen_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "last_seen_src": "chain_sites",
    },
    {
        "business_id": "biz_00000000000000bb", "name": "Indie Fuel",
        "category": "fuel_station", "brand": None, "address": None,
        "city": None, "state": None, "zip": None, "address_norm": None,
        "phone": None, "website": None,
        "present_in": ["overture"], "def": None,
        "confidence": None, "conf_trust": None, "conf_fresh": None,
        "conf_complete": None, "conf_agree": None,
        "flags": ["dedup_gray_zone"],
        "source_id": "businesses_conflate", "run_id": 42,
        "ingested_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "observed_at": None,
        "gj": '{"type":"Point","coordinates":[-75.71,39.61]}',
        # Gate 6: never scored. Unscored is NOT closed — it renders 'unknown'
        # and stays on the map. See api/liveness_filter.py.
        "liveness": None, "live_state": None, "live_presence": None,
        "live_sources": None, "live_corrob": None, "live_reasons": [],
        "last_seen_at": None, "last_seen_src": None,
    },
]


@pytest.fixture()
def canned_db(monkeypatch):
    captured = {}

    def fake_q_all(sql, params=None):
        captured["sql"], captured["params"] = sql, params
        return [dict(r) for r in _ROWS]

    monkeypatch.setattr(common, "q_all", fake_q_all)
    return captured


def test_def_marker_only_when_inferred(canned_db):
    resp = _get("/v1/places", bbox=BBOX_OK)
    assert resp.status_code == 200
    props = {f["id"]: f["properties"] for f in resp.json()["features"]}
    loves, indie = props["biz_00000000000000aa"], props["biz_00000000000000bb"]
    # §6: inferred renders WITH its marker...
    assert loves["def"] == "inferred"
    assert "not an observed fact" in loves["def_note"]
    # ...unknown renders as NO def field at all (never "no", never a fact)
    assert "def" not in indie
    assert "def_note" not in indie


def test_attributions_are_overture_and_fsq_never_osm(canned_db):
    body = _get("/v1/places", bbox=BBOX_OK).json()
    joined = " ".join(body["attribution"])
    assert "Overture" in joined and "CDLA" in joined
    assert "Foursquare" in joined and "Apache" in joined
    assert "OpenStreetMap" not in joined  # §3.1-4c: no OSM data in this table
    for f in body["features"]:
        assert f["properties"]["attribution"] == routes_places.ATTRIBUTIONS


def test_confidence_components_and_tristate_rendering(canned_db):
    props = {f["id"]: f["properties"]
             for f in _get("/v1/places", bbox=BBOX_OK).json()["features"]}
    loves, indie = props["biz_00000000000000aa"], props["biz_00000000000000bb"]
    assert loves["confidence"] == 78
    assert loves["confidence_components"] == {
        "trust": 65, "fresh": 95, "complete": 60, "agree": 100}
    assert loves["present_in"] == ["overture", "fsq"]
    # NULLs render 'unknown' (common.py decree) — never 0, never fabricated
    assert indie["confidence"] == "unknown"
    assert indie["confidence_components"]["agree"] == "unknown"
    assert indie["brand"] == "unknown"
    assert indie["observed_at"] == "unknown"
    assert indie["flags"] == ["dedup_gray_zone"]


def test_category_and_min_confidence_filters_parameterized(canned_db):
    resp = _get("/v1/places", bbox=BBOX_OK, category="truck_stop",
                min_confidence=60)
    assert resp.status_code == 200
    assert "b.category = %s" in canned_db["sql"]
    assert "b.confidence >= %s" in canned_db["sql"]
    assert "truck_stop" in canned_db["params"]
    assert 60 in canned_db["params"]
    notes = " ".join(resp.json()["filter_notes"])
    assert "unscored" in notes


def test_q_search_uses_fts_and_trgm(canned_db):
    resp = _get("/v1/places", bbox=BBOX_OK, q="loves")
    assert resp.status_code == 200
    sql = canned_db["sql"]
    assert "plainto_tsquery" in sql
    assert "b.name %" in sql          # pg_trgm fuzzy fallback
    assert "similarity(b.name" in sql  # relevance ordering
    assert canned_db["params"].count("loves") == 3


def test_detail_includes_per_source_blobs(monkeypatch):
    row = dict(_ROWS[0])
    row["props"] = {"overture": {"source_record_id": "g1"},
                    "fsq": {"source_record_id": "f1"}}
    monkeypatch.setattr(common, "q_all", lambda sql, params=None: [row])
    resp = _get("/v1/places/biz_00000000000000aa")
    assert resp.status_code == 200
    body = resp.json()
    assert body["properties"]["sources"]["overture"]["source_record_id"] == "g1"
    assert body["properties"]["sources"]["fsq"]["source_record_id"] == "f1"
    assert body["attribution"] == routes_places.ATTRIBUTIONS


def test_detail_not_found(monkeypatch):
    monkeypatch.setattr(common, "q_all", lambda sql, params=None: [])
    resp = _get("/v1/places/biz_nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ------------------------------------------------------------------ DB shape

@needs_db
def test_db_shape_empty_table_is_fine():
    resp = _get("/v1/places", bbox=BBOX_OK)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert body["count"] == len(body["features"])
    assert body["attribution"] == routes_places.ATTRIBUTIONS
    for f in body["features"]:  # table may be empty; shape only
        p = f["properties"]
        assert p["present_in"] and set(p["present_in"]) <= {"overture", "fsq"}
        assert "confidence_components" in p
        # def is either absent or the inferred marker — never a bare fact
        assert p.get("def") in (None, "inferred")
