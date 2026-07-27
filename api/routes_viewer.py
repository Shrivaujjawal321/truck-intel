"""The local data viewer: one HTML page plus the summary numbers it shows.

The page itself is static (`api/viewer/index.html`) and talks to
`/v1/tiles/...` for geometry. This module only serves the file and the
inventory/fuel-price panels, which are not map data.
"""
from __future__ import annotations

from pathlib import Path as FsPath

from fastapi import APIRouter
from fastapi.responses import FileResponse

from api import common
from api.routes_tiles import LAYERS

router = APIRouter(tags=["viewer"])

VIEWER_HTML = FsPath(__file__).parent / "viewer" / "index.html"
TRACK_HTML = FsPath(__file__).parent / "viewer" / "track.html"

# Tables we hold but do not draw. Two reasons land here, and both are stated so
# a dataset is never quietly missing: no geometry to draw, or a deliberate
# product decision not to draw it.
NON_SPATIAL = {
    "fuel_prices": ("core.fuel_prices", "Diesel prices (EIA, weekly)"),
    "osm_ways": ("osm.ways", "OSM highway ways (raw graph — not a truck route layer)"),
    "businesses": ("core.businesses",
                   "General POI (restaurants, cafes, ATMs) — held, not mapped"),
}


@router.get("/viewer", include_in_schema=False)
def viewer_page() -> FileResponse:
    return FileResponse(VIEWER_HTML, media_type="text/html")


@router.get("/track", include_in_schema=False)
def track_page() -> FileResponse:
    """The driver's page: turns a phone into a live marker. See track.html."""
    return FileResponse(TRACK_HTML, media_type="text/html")


@router.get("/v1/viewer/inventory")
def inventory() -> dict:
    """Exact row counts per dataset + the newest observed_at we hold for it.

    Honest by construction: a count of 0 is reported as 0, never hidden, and
    `observed_at` is when the fact was true in the world, not download time.
    """
    datasets = []
    for name, spec in LAYERS.items():
        mapped_pred = spec.row_filter or "true"
        row = common.q_all(
            f"SELECT count(*) AS total, "
            f"count(*) FILTER (WHERE {mapped_pred}) AS mapped, "
            f"max(observed_at) AS newest FROM {spec.table}"
        )[0]
        datasets.append(
            {
                "id": name,
                "label": spec.label,
                "table": spec.table,
                "color": spec.color,
                "kind": spec.kind,
                "mapped": True,
                # `rows` stays the count the map actually draws — that is the
                # number a viewer can verify by eye. `rows_total` is what the
                # table holds, and `row_filter` says why they differ. A layer
                # whose map count silently disagreed with its table count is the
                # bug this triple exists to make impossible.
                "rows": row["mapped"],
                "rows_total": row["total"],
                "row_filter": spec.row_filter,
                "newest_observed_at": row["newest"],
            }
        )
    for name, (table, label) in NON_SPATIAL.items():
        row = common.q_all(
            f"SELECT count(*) AS n, max(observed_at) AS newest FROM {table}"
        )[0]
        datasets.append(
            {
                "id": name,
                "label": label,
                "table": table,
                "color": "#94a3b8",
                "kind": "table",
                "mapped": False,
                "rows": row["n"],
                "rows_total": row["n"],
                "row_filter": None,
                "newest_observed_at": row["newest"],
            }
        )
    datasets.sort(key=lambda d: d["rows"], reverse=True)
    return {
        "datasets": datasets,
        # Drawn vs held, both stated. total_rows alone would overstate the map on
        # a filtered layer and understate the holdings on a hidden one.
        "total_rows": sum(d["rows"] for d in datasets),
        "total_rows_held": sum(d["rows_total"] for d in datasets),
    }


@router.get("/v1/viewer/breakdown/{layer}")
def breakdown(layer: str) -> dict:
    """Per-state row counts for one mapped layer — the 'where is my data' answer."""
    spec = LAYERS.get(layer)
    if spec is None:
        raise common.ApiError("not_found", f"unknown layer '{layer}'", status=404)
    if "state" not in spec.props:
        raise common.ApiError(
            "invalid_param", f"layer '{layer}' has no state column", status=400
        )
    # Counts the MAP's rows, not the table's: a per-state breakdown that summed
    # to more than the dots on screen would be the same lie in another shape.
    where = f" WHERE {spec.row_filter}" if spec.row_filter else ""
    rows = common.q_all(
        f"SELECT coalesce(state, 'unknown') AS state, count(*) AS n "
        f"FROM {spec.table}{where} GROUP BY 1 ORDER BY 2 DESC"
    )
    return {"layer": layer, "states": rows, "row_filter": spec.row_filter}


@router.get("/v1/viewer/categories")
def categories() -> dict:
    """What kind of shop the mechanic layer is made of.

    This used to report `core.businesses` — restaurants, cafes and ATMs — which
    told a driver nothing. It now reports the layer the map actually draws, and
    counts only the on-route rows so the panel and the dots agree.
    """
    rows = common.q_all(
        """
        SELECT coalesce(category, 'unknown') AS category, count(*) AS n
        FROM core.mechanic_shops
        WHERE on_route_5km
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    return {"categories": rows, "of_layer": "mechanic_shops", "on_route_only": True}


@router.get("/v1/viewer/fuel-latest")
def fuel_latest() -> dict:
    """Most recent weekly diesel price per region (no geometry — panel, not map)."""
    rows = common.q_all(
        """
        SELECT DISTINCT ON (region, product)
               region, product, week_of, price_usd_gal
        FROM core.fuel_prices
        ORDER BY region, product, week_of DESC
        """
    )
    return {"prices": rows}


@router.get("/v1/viewer/runs")
def runs() -> dict:
    """Last 25 ingest runs — success, skip and failure alike, never faked."""
    rows = common.q_all(
        """
        SELECT run_id, source_id, status, started_at, rows_in, rows_published,
               rows_rejected, left(coalesce(message, ''), 120) AS message
        FROM ops.source_runs
        ORDER BY started_at DESC
        LIMIT 25
        """
    )
    return {"runs": rows}
