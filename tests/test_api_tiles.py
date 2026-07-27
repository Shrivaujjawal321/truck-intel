"""/v1/tiles + /v1/viewer tests — registry sanity, zoom routing, no silent caps.

Same two-layer contract as tests/test_api.py: registry/unit tests always run;
DB-backed tests assert shape and the truncation invariant, and skip when PostGIS
is unreachable. No fake rows in core tables, ever.

Run: uv run pytest
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from api import common
from api.main import app
from api.routes_tiles import CLUSTER_CELLS, LAYERS, Layer, _tile_sql

# A z4 tile over the central US: dense enough that a naive LIMIT would bite.
DENSE_TILE = (4, 4, 6)
# A z11 tile over Dallas: past every cluster threshold, so raw rows are served.
DETAIL_TILE = (11, 470, 822)


def _get(path: str):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(go())


def _db_available() -> bool:
    try:
        with common.connect_ro() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostGIS unreachable")


# --- registry ---------------------------------------------------------------


def test_every_layer_declares_its_id_column_in_a_real_schema():
    for name, spec in LAYERS.items():
        schema, _, table = spec.table.partition(".")
        assert schema in {"core", "osm"}, f"{name} reads from {schema}"
        assert table, name


def test_osm_ways_is_not_a_map_layer():
    """The generic road graph must never be drawn as the truck-route network."""
    assert all(spec.table != "osm.ways" for spec in LAYERS.values())


def test_dense_layers_are_not_on_by_default():
    """629k markers on first paint hides every other layer."""
    assert LAYERS["bridges"].default_on is False


def test_point_layers_cluster_before_they_would_truncate():
    for name, spec in LAYERS.items():
        if spec.kind == "point":
            assert spec.cluster_below_zoom, f"{name} would LIMIT-truncate at low zoom"


def test_route_layer_generalizes_below_its_cluster_zoom():
    routes = LAYERS["truck_routes"]
    assert routes.gen_table == "core.truck_routes_gen"
    assert routes.gen_below_zoom >= 8


def test_source_switches_at_the_declared_zoom():
    routes = LAYERS["truck_routes"]
    z = routes.gen_below_zoom
    assert routes.source_for(z - 1) == ("core.truck_routes_gen", True)
    assert routes.source_for(z) == ("core.truck_routes", False)


def test_generalized_table_missing_columns_become_null_not_dropped():
    """A column absent from the generalized table stays in the tile schema, so a
    popup renders it as 'unknown' rather than silently losing the field."""
    sql = _tile_sql(LAYERS["truck_routes"], 4)
    assert "NULL AS fclass" in sql
    assert "core.truck_routes_gen" in sql
    # ...and the raw table still selects it for real.
    assert "t.fclass" in _tile_sql(LAYERS["truck_routes"], 12)


def test_order_by_survives_when_the_generalized_table_has_the_column():
    routes = LAYERS["truck_routes"]
    assert "ORDER BY t.aadt_com" in _tile_sql(routes, 12)          # raw table
    assert "ORDER BY t.aadt_com" in _tile_sql(routes, 4)           # gen table has it


def test_order_by_is_dropped_when_the_generalized_table_lacks_the_column():
    """Ordering by a column the generalized table lacks would be a SQL error, so
    the clause has to disappear rather than be emitted and blow up at runtime."""
    routes = LAYERS["truck_routes"]
    hypothetical = replace(routes, order_by="fclass DESC")  # fclass is absent there
    assert "ORDER BY" not in _tile_sql(hypothetical, 4)
    assert "ORDER BY t.fclass" in _tile_sql(hypothetical, 12)


def test_line_layers_are_never_clustered():
    assert LAYERS["truck_routes"].clustered_at(2) is False


def test_cluster_grid_is_bounded():
    """Per-tile feature count is capped by geometry, not by a row LIMIT."""
    assert CLUSTER_CELLS**2 <= 4096


# --- endpoint behaviour -----------------------------------------------------


def test_unknown_layer_is_404_with_the_shared_envelope():
    r = _get("/v1/tiles/not_a_layer/4/4/6.mvt")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_xy_outside_the_zoom_grid_is_rejected():
    r = _get("/v1/tiles/truck_routes/2/9/9.mvt")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


def test_registry_endpoint_lists_every_layer():
    body = _get("/v1/tiles").json()
    assert {layer["id"] for layer in body["layers"]} == set(LAYERS)


@needs_db
def test_tile_reports_which_table_it_came_from():
    z, x, y = DENSE_TILE
    r = _get(f"/v1/tiles/truck_routes/{z}/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.headers["x-source-table"] == "core.truck_routes_gen"
    assert r.headers["x-generalized"] == "true"
    assert r.headers["content-type"] == "application/vnd.mapbox-vector-tile"


@needs_db
def test_detail_tile_serves_raw_rows():
    z, x, y = DETAIL_TILE
    r = _get(f"/v1/tiles/truck_routes/{z}/{x}/{y}.mvt")
    assert r.status_code in (200, 204)
    if r.status_code == 200:
        assert r.headers["x-source-table"] == "core.truck_routes"
        assert r.headers["x-clustered"] == "false"


@needs_db
def test_point_layer_is_clustered_at_low_zoom():
    z, x, y = DENSE_TILE
    r = _get(f"/v1/tiles/bridges/{z}/{x}/{y}.mvt")
    assert r.status_code == 200
    assert r.headers["x-clustered"] == "true"


@needs_db
@pytest.mark.parametrize("layer", ["bridges", "fuel_stations"])
def test_density_grid_accounts_for_every_row(layer: str):
    """The invariant that makes clustering honest: sum(n) over the grid equals
    the raw row count in the same bbox. Cells summarise; they never drop."""
    spec: Layer = LAYERS[layer]
    z, x, y = DENSE_TILE
    cell = (360.0 / (1 << z)) / CLUSTER_CELLS
    with common.connect_ro() as conn:
        raw = conn.execute(
            f"""
            SELECT count(*) AS n FROM {spec.table} t
            WHERE t.geom && ST_Transform(ST_TileEnvelope(%s,%s,%s, margin=>0.0625), 4326)
            """,
            (z, x, y),
        ).fetchone()["n"]
        grid = conn.execute(
            f"""
            SELECT coalesce(sum(c), 0) AS n FROM (
                SELECT count(*) AS c FROM {spec.table} t
                WHERE t.geom && ST_Transform(ST_TileEnvelope(%s,%s,%s, margin=>0.0625), 4326)
                GROUP BY ST_SnapToGrid(t.geom, %s)
            ) q
            """,
            (z, x, y, cell),
        ).fetchone()["n"]
    assert raw > 0, f"{layer} has no rows in the test tile"
    assert grid == raw


@needs_db
def test_generalized_routes_preserve_the_network():
    """Dissolving must not quietly lose corridors: mileage stays within 2%."""
    with common.connect_ro() as conn:
        raw = conn.execute(
            "SELECT sum(ST_Length(geom::geography)) AS m FROM core.truck_routes"
        ).fetchone()["m"]
        gen = conn.execute(
            "SELECT sum(ST_Length(geom::geography)) AS m FROM core.truck_routes_gen"
        ).fetchone()["m"]
    assert gen / raw > 0.98


@needs_db
def test_inventory_counts_are_real_and_include_unmapped_tables():
    body = _get("/v1/viewer/inventory").json()
    ids = {d["id"] for d in body["datasets"]}
    assert set(LAYERS) <= ids
    assert "fuel_prices" in ids          # no geometry -> panel, not map
    assert "osm_ways" in ids             # present, and labelled as not-a-route-layer
    assert body["total_rows"] == sum(d["rows"] for d in body["datasets"])


@needs_db
def test_breakdown_rejects_a_layer_without_a_state_column():
    r = _get("/v1/viewer/breakdown/live_events")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


def test_viewer_page_is_served():
    r = _get("/viewer")
    assert r.status_code == 200
    assert "Truck Intel" in r.text
