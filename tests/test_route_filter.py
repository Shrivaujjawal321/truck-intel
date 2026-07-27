"""Route-based map filtering: the drawn count must equal what the layer claims.

Boss caught the failure this file guards against on 2026-07-26: the map drew
2,981 general POI while the mechanic table held 11,759 rows, and nothing in the
UI or the API said the two numbers were different things. These tests assert the
invariant rather than the incident — every filtered layer reports both counts,
and the tile SQL really applies the predicate.

Run: uv run pytest tests/test_route_filter.py
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from api import common
from api.main import app
from api.routes_tiles import LAYERS, _cluster_sql, _filter_clause, _tile_sql
from truckintel.route_assign import ON_ROUTE_M

# Layers Boss asked to be truck-route-only (2026-07-26).
SERVICE_LAYERS = ("fuel_stations", "mechanic_shops")


def _get(path: str):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.get(path)

    return asyncio.run(go())


def _db_available() -> bool:
    try:
        with common.connect_ro() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostGIS unreachable")


# ------------------------------------------------------------------ unit tests
def test_service_layers_are_route_filtered():
    """Fuel and mechanics must never render off-network rows."""
    for name in SERVICE_LAYERS:
        assert LAYERS[name].row_filter, f"{name} lost its route filter"
        assert "on_route_5km" in LAYERS[name].row_filter


def test_general_poi_is_not_a_map_layer():
    """core.businesses was removed from the map on purpose — keep it out."""
    assert "businesses" not in LAYERS
    assert all(spec.table != "core.businesses" for spec in LAYERS.values())


def test_filter_clause_is_empty_for_unfiltered_layers():
    assert _filter_clause(LAYERS["bridges"]) == ""


def test_filter_clause_is_alias_qualified():
    """The predicate is concatenated into queries that alias the table `t`."""
    clause = _filter_clause(LAYERS["mechanic_shops"])
    assert clause.startswith(" AND t.")


def test_filter_reaches_both_query_shapes():
    """A predicate applied only to the detail query would leak off-route rows
    into the low-zoom density grid, where they are hardest to notice."""
    spec = LAYERS["mechanic_shops"]
    assert spec.row_filter in _tile_sql(spec, 12)
    assert spec.row_filter in _cluster_sql(spec, 5)


def test_no_layer_combines_a_filter_with_a_generalized_table():
    """A gen table may not carry the filter column, so the filter would silently
    stop applying below its zoom. routes_tiles.py asserts this at import; this
    keeps the guarantee visible in the suite too."""
    for name, spec in LAYERS.items():
        assert not (spec.row_filter and spec.gen_table), name


def test_on_route_buffer_matches_the_corridor_service_buffer():
    """The map filter and the per-route service list must agree on "reachable".

    If these drift, a shop shows on the national map but not in the route's
    service list (or the reverse) — same shop, same road, two answers.
    """
    from truckintel.corridor import SERVICE_BUFFER_M
    assert ON_ROUTE_M == SERVICE_BUFFER_M


# -------------------------------------------------------------- DB-backed tests
@needs_db
def test_inventory_reports_drawn_and_held_separately():
    inv = _get("/v1/viewer/inventory").json()
    by_id = {d["id"]: d for d in inv["datasets"]}
    for name in SERVICE_LAYERS:
        d = by_id[name]
        # The incident in one assertion: a filtered layer must never present its
        # table count as the number on the map.
        assert d["rows"] < d["rows_total"], name
        assert d["row_filter"], name
    assert inv["total_rows"] <= inv["total_rows_held"]


@needs_db
def test_inventory_drawn_count_matches_a_direct_count():
    """`rows` is verifiable against the database, not a derived guess."""
    inv = _get("/v1/viewer/inventory").json()
    by_id = {d["id"]: d for d in inv["datasets"]}
    for name in SERVICE_LAYERS:
        spec = LAYERS[name]
        actual = common.q_all(
            f"SELECT count(*) AS n FROM {spec.table} WHERE {spec.row_filter}"
        )[0]["n"]
        assert by_id[name]["rows"] == actual, name


@needs_db
def test_breakdown_sums_to_the_drawn_count_not_the_table_count():
    """A per-state panel adding up to more than the dots on screen is the same
    lie in a different shape."""
    inv = {d["id"]: d for d in _get("/v1/viewer/inventory").json()["datasets"]}
    for name in SERVICE_LAYERS:
        b = _get(f"/v1/viewer/breakdown/{name}").json()
        assert sum(s["n"] for s in b["states"]) == inv[name]["rows"], name


@needs_db
def test_every_drawn_service_row_is_actually_near_a_truck_route():
    """The filter is only meaningful if the underlying distance is real."""
    for name in SERVICE_LAYERS:
        spec = LAYERS[name]
        bad = common.q_all(
            f"SELECT count(*) AS n FROM {spec.table} "
            f"WHERE {spec.row_filter} AND (route_dist_m IS NULL "
            f"                             OR route_dist_m > {ON_ROUTE_M})"
        )[0]["n"]
        assert bad == 0, f"{name} draws {bad} rows that are not on the network"


@needs_db
def test_fuel_layer_keeps_untagged_diesel_and_drops_only_known_negatives():
    """OSM leaves diesel untagged on ~93% of stations. Requiring has_diesel=true
    would delete the layer over a metadata gap, so untagged rows must stay and
    only explicit `false` may be excluded."""
    drawn_untagged = common.q_all(
        "SELECT count(*) AS n FROM osm.fuel_stations "
        "WHERE on_route_5km AND has_diesel IS NULL "
        "  AND hgv_access IS NOT FALSE"
    )[0]["n"]
    assert drawn_untagged > 0, "untagged-diesel stations were wrongly filtered out"

    drawn_denied = common.q_all(
        "SELECT count(*) AS n FROM osm.fuel_stations "
        f"WHERE {LAYERS['fuel_stations'].row_filter} "
        "  AND (has_diesel IS FALSE OR hgv_access IS FALSE)"
    )[0]["n"]
    assert drawn_denied == 0, "stations tagged no-diesel / no-HGV are on the map"


@needs_db
def test_categories_panel_describes_the_mechanic_layer():
    """It used to report restaurants and cafes; it must describe what is drawn."""
    cats = _get("/v1/viewer/categories").json()
    assert cats["of_layer"] == "mechanic_shops"
    assert cats["on_route_only"] is True
    inv = {d["id"]: d for d in _get("/v1/viewer/inventory").json()["datasets"]}
    assert sum(c["n"] for c in cats["categories"]) == inv["mechanic_shops"]["rows"]


@needs_db
def test_removed_poi_layer_is_still_reported_as_held():
    """Removing it from the map must not make the data invisible."""
    inv = _get("/v1/viewer/inventory").json()
    biz = [d for d in inv["datasets"] if d["table"] == "core.businesses"]
    assert biz and biz[0]["mapped"] is False
    assert biz[0]["rows"] > 0


@needs_db
def test_removed_poi_layer_has_no_tile_endpoint():
    r = _get("/v1/tiles/businesses/10/236/413.mvt")
    assert r.status_code == 404
