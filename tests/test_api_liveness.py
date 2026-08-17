"""Gate 6 on the HTTP surface (api/liveness_filter.py, routes_places,
routes_parking, routes_tiles).

tests/test_liveness.py proves the SCORER. This file proves the thing a driver
actually meets: what the API hides, what it shows, and what it admits to.

The behaviour under test is one asymmetric rule, and both halves matter:
  - a place a source ASSERTED closed must not come back by default
  - a place that merely scores badly, or was never scored at all, MUST come
    back — badged, not filtered. Silently dropping it would publish absence of
    evidence as evidence of absence, which is the failure liveness.py exists
    to refuse.

Layers: pure-function tests on the WHERE builder (no DB), rendering tests on
canned rows (monkeypatched common.q_all), and a registry test on the tile
layers. Nothing here needs PostGIS.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api import common, liveness_filter, routes_parking, routes_places, routes_tiles

BBOX_OK = "-75.9,38.4,-74.9,39.9"

app = FastAPI()
common.install_error_handlers(app)
app.include_router(routes_places.router)
app.include_router(routes_parking.router)


def _get(path: str, **params):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, params=params)

    return asyncio.run(go())


# ------------------------------------------------------- the WHERE builder

def test_default_excludes_only_asserted_closure():
    clauses, params, notes = liveness_filter.where("b")
    assert clauses == ["b.live_state IS DISTINCT FROM %s"]
    assert params == ["closed"]
    # IS DISTINCT FROM, not <>: `live_state <> 'closed'` is NULL for an
    # unscored row and would silently drop every one of them.
    assert "IS DISTINCT FROM" in clauses[0]
    assert any("include_closed=true" in n for n in notes)


def test_default_does_not_threshold_on_score():
    """The default must not encode a liveness cutoff of any kind.

    'unknown' and 'likely_closed' are SCORES. Filtering on them would convert
    "nobody has re-confirmed this since 2019" into "this is gone".
    """
    clauses, params, _ = liveness_filter.where("b")
    assert len(clauses) == 1
    assert all(not isinstance(p, int) for p in params)
    assert "liveness" not in clauses[0]


def test_include_closed_drops_the_clause_and_says_so():
    clauses, params, notes = liveness_filter.where("b", include_closed=True)
    assert clauses == [] and params == []
    assert any("asserted CLOSED are included" in n for n in notes)


def test_min_liveness_is_opt_in_and_admits_what_it_costs():
    clauses, params, notes = liveness_filter.where("p", min_liveness=70)
    assert "p.liveness >= %s" in clauses
    assert 70 in params
    # The note must name the collateral damage, not just the threshold.
    joined = " ".join(notes)
    assert "NULL" in joined and "unknown" in joined


def test_alias_is_honoured_so_joined_queries_stay_unambiguous():
    clauses, _, _ = liveness_filter.where("zz", min_liveness=1)
    assert all(c.startswith("zz.") for c in clauses)


# ------------------------------------------------------ rendering the block

_SCORED = {
    "liveness": 92, "live_state": "open", "live_presence": 88,
    "live_sources": 80, "live_corrob": 100,
    "live_reasons": ["chain_confirmed:loves"],
    "last_seen_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
    "last_seen_src": "chain_sites",
}
_UNSCORED = {
    "liveness": None, "live_state": None, "live_presence": None,
    "live_sources": None, "live_corrob": None, "live_reasons": None,
    "last_seen_at": None, "last_seen_src": None,
}


def test_components_travel_with_the_score():
    """"Why is this 92?" must be answerable from the response alone."""
    p = liveness_filter.props(dict(_SCORED))
    assert p["liveness"] == 92 and p["live_state"] == "open"
    assert p["liveness_components"] == {
        "presence": 88, "sources": 80, "corroboration": 100}
    assert p["liveness_reasons"] == ["chain_confirmed:loves"]
    assert p["last_seen_src"] == "chain_sites"


def test_unscored_renders_unknown_never_zero_or_closed():
    p = liveness_filter.props(dict(_UNSCORED))
    assert p["liveness"] == "unknown"
    assert p["live_state"] == "unknown"
    assert p["live_state"] != "closed"          # unscored is NOT dead
    assert p["liveness_components"] == {
        "presence": "unknown", "sources": "unknown", "corroboration": "unknown"}
    assert p["liveness_reasons"] == []          # NULL array -> [], not None
    assert p["last_seen_at"] == "unknown"


def test_zero_corroboration_is_data_and_survives():
    """0 is a real measurement — 'nothing corroborates this' — not a NULL."""
    p = liveness_filter.props({**_SCORED, "live_corrob": 0, "liveness": 0})
    assert p["liveness"] == 0
    assert p["liveness_components"]["corroboration"] == 0


# ------------------------------------------------------------ /v1 responses

_PLACE_BASE = {
    "business_id": "biz_00000000000000aa", "name": "Love's Travel Stop",
    "category": "truck_stop", "brand": "Love's", "address": None,
    "city": None, "state": None, "zip": None, "address_norm": None,
    "phone": None, "website": None, "present_in": ["overture", "fsq"],
    "def": None, "confidence": 78, "conf_trust": 65, "conf_fresh": 95,
    "conf_complete": 60, "conf_agree": 100, "flags": [],
    "source_id": "businesses_conflate", "run_id": 42,
    "ingested_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
    "observed_at": None,
    "gj": '{"type":"Point","coordinates":[-75.7,39.6]}',
}

_PARKING_BASE = {
    "site_id": 1903, "kind": "rest_area", "name": "WS I-80 EB Frankfort",
    "state": "IL", "truck_spaces": 6,
    "gj": '{"type":"Point","coordinates":[-75.7,39.6]}',
    "source_id": "ntad_parking", "run_id": 7,
    "ingested_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
    "observed_at": datetime(2019, 1, 1, tzinfo=timezone.utc),
    "confidence": 61, "attribution": "BTS",
}


@pytest.fixture()
def spy_db(monkeypatch):
    """Capture the SQL+params the route builds, and serve canned rows back."""
    captured: dict = {}
    rows: list[dict] = []

    def fake_q_all(sql, params=None):
        captured["sql"], captured["params"] = sql, params
        return [dict(r) for r in rows]

    monkeypatch.setattr(common, "q_all", fake_q_all)
    captured["rows"] = rows
    return captured


@pytest.mark.parametrize(
    "path,extra",
    [("/v1/places", {}), ("/v1/parking", {})],
)
def test_closed_predicate_is_in_the_sql_by_default(spy_db, path, extra):
    _get(path, bbox=BBOX_OK, **extra)
    assert "live_state IS DISTINCT FROM" in spy_db["sql"]
    assert "closed" in spy_db["params"]


@pytest.mark.parametrize("path", ["/v1/places", "/v1/parking"])
def test_include_closed_removes_the_predicate(spy_db, path):
    _get(path, bbox=BBOX_OK, include_closed="true")
    assert "live_state IS DISTINCT FROM" not in spy_db["sql"]
    assert "closed" not in (spy_db["params"] or [])


@pytest.mark.parametrize("path", ["/v1/places", "/v1/parking"])
def test_default_exclusion_is_always_disclosed(spy_db, path):
    """A filtered response that does not say it is filtered is a lie by count."""
    body = _get(path, bbox=BBOX_OK).json()
    assert any("closed" in n for n in body["filter_notes"])
    assert "liveness" in body["note"]


@pytest.mark.parametrize("path", ["/v1/places", "/v1/parking"])
def test_min_liveness_out_of_range_is_rejected(spy_db, path):
    resp = _get(path, bbox=BBOX_OK, min_liveness=101)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_param"


def test_places_feature_carries_liveness_beside_confidence(spy_db):
    spy_db["rows"].append({**_PLACE_BASE, **_SCORED})
    props = _get("/v1/places", bbox=BBOX_OK).json()["features"][0]["properties"]
    # The two scores answer different questions and must both be visible.
    assert props["confidence"] == 78          # the RECORD
    assert props["liveness"] == 92            # the SUBJECT
    assert props["live_state"] == "open"
    assert props["liveness_components"]["corroboration"] == 100


def test_parking_keeps_unknown_rows_with_their_2019_vintage(spy_db):
    """The Jason's-Law case: old, uncorroborated, still returned and badged."""
    spy_db["rows"].append({
        **_PARKING_BASE,
        "liveness": 44, "live_state": "unknown", "live_presence": 59,
        "live_sources": 50, "live_corrob": 0, "live_reasons": [],
        "last_seen_at": datetime(2019, 1, 1, tzinfo=timezone.utc),
        "last_seen_src": "ntad_parking",
    })
    body = _get("/v1/parking", bbox=BBOX_OK).json()
    assert body["count"] == 1                  # NOT filtered away
    props = body["features"][0]["properties"]
    assert props["live_state"] == "unknown"
    assert props["last_seen_at"].startswith("2019")
    assert props["truck_spaces"] == 6


def test_place_detail_never_hides_a_closed_row(spy_db):
    """Ask for a specific id and you get it, whatever its state.

    Hiding it would answer "does this place exist in your data?" with a 404,
    which is a different and worse lie than reporting the closure.
    """
    spy_db["rows"].append({
        **_PLACE_BASE, "props": {}, **_SCORED,
        "liveness": 0, "live_state": "closed",
        "live_reasons": ["closed_asserted:fsq"],
    })
    body = _get("/v1/places/biz_00000000000000aa").json()
    assert body["properties"]["live_state"] == "closed"
    assert "live_state IS DISTINCT FROM" not in spy_db["sql"]


# --------------------------------------------------------------- map layers

@pytest.mark.parametrize("layer", ["parking_sites", "mechanic_shops"])
def test_place_layers_hide_asserted_closures(layer):
    spec = routes_tiles.LAYERS[layer]
    assert routes_tiles.LIVE_FILTER in (spec.row_filter or "")
    # And the badge must reach the popup, which renders spec.props verbatim.
    assert "live_state" in spec.props and "liveness" in spec.props


def test_mechanic_layer_keeps_its_route_buffer_too():
    """Gate 6 rides ALONG the 5 km filter; it must not replace it."""
    rf = routes_tiles.LAYERS["mechanic_shops"].row_filter
    assert "on_route_5km" in rf and routes_tiles.LIVE_FILTER in rf


def test_live_filter_does_not_threshold():
    assert "liveness" not in routes_tiles.LIVE_FILTER
    assert "IS DISTINCT FROM" in routes_tiles.LIVE_FILTER
