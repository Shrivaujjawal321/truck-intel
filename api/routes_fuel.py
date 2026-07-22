"""GET /v1/fuel/prices — EIA weekly regional averages."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common

router = APIRouter()

_ATTRIBUTION_FALLBACK = "U.S. Energy Information Administration (EIA)"
_NOTE = (
    "Regional weekly survey averages — never station-level pump prices "
    "(no free legal station-level source exists; honest gap)."
)


@router.get("/v1/fuel/prices")
def list_fuel_prices(
    region: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Latest week per (region, product) from core.fuel_prices. Every item is
    explicitly labeled "kind": "regional_weekly_estimate". Unknown region ->
    empty items, honestly, not an error."""
    where = ""
    params: list = [_ATTRIBUTION_FALLBACK]
    if region:
        where = "WHERE upper(f.region) = upper(%s)"
        params.append(region)
    rows = common.q_all(
        f"""
        SELECT DISTINCT ON (f.region, f.product)
               f.region, f.product, f.week_of, f.price_usd_gal,
               f.source_id, f.run_id, f.ingested_at, f.observed_at,
               COALESCE(s.attribution_text, %s) AS attribution
        FROM core.fuel_prices AS f
        LEFT JOIN ops.sources AS s USING (source_id)
        {where}
        ORDER BY f.region, f.product, f.week_of DESC
        LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    )
    items = [
        {
            "kind": "regional_weekly_estimate",
            "region": r["region"],
            "product": r["product"],
            "week_of": r["week_of"],
            "price_usd_gal": r["price_usd_gal"],
            "source_id": r["source_id"],
            "run_id": r["run_id"],
            "ingested_at": r["ingested_at"],
            "observed_at": common.unknown(r["observed_at"]),
            "attribution": r["attribution"],
        }
        for r in rows
    ]
    return {"count": len(items), "note": _NOTE, "limit": limit, "offset": offset, "items": items}
