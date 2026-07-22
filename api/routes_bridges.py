"""GET /v1/bridges — the signature low-bridge query."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common

router = APIRouter()

# Fallback only until the registry sync populates ops.sources.attribution_text.
_ATTRIBUTION_FALLBACK = "Federal Highway Administration (FHWA), US DOT"
_VINTAGE = (
    "NBI annual snapshot — observed_at is the inventory vintage, "
    "never the download date"
)


@router.get("/v1/bridges")
def list_bridges(
    bbox: str,
    max_clearance_lt_in: float | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Bridges in a bbox, optionally only those with min vertical clearance
    below the given inches. GeoJSON out; every feature carries source, vintage
    (observed_at) and confidence (NULL = unknown in MVP). bbox='minLon,minLat,
    maxLon,maxLat', capped at 4x4 degrees.
    """
    box = common.parse_bbox(bbox)
    where = ["ST_Intersects(b.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"]
    params: list = [_ATTRIBUTION_FALLBACK, *box]
    if max_clearance_lt_in is not None:
        # NULL clearance is *unknown*, not "below the limit" — excluded on purpose.
        where.append("b.min_vert_clearance_in < %s")
        params.append(max_clearance_lt_in)
    rows = common.q_all(
        f"""
        SELECT b.nbi_id, b.name, b.state, ST_AsGeoJSON(b.geom) AS gj,
               b.min_vert_clearance_in::float8 AS min_vert_clearance_in,
               b.operating_rating, b.inventory_rating,
               b.posting_status, b.source_id, b.run_id, b.ingested_at,
               b.observed_at, b.confidence,
               COALESCE(s.attribution_text, %s) AS attribution
        FROM core.bridges AS b
        LEFT JOIN ops.sources AS s USING (source_id)
        WHERE {" AND ".join(where)}
        ORDER BY b.nbi_id
        LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    )
    features = [
        common.feature(
            r["nbi_id"],
            r["gj"],
            {
                "nbi_id": r["nbi_id"],
                "name": common.unknown(r["name"]),
                "state": common.unknown(r["state"]),
                "min_vert_clearance_in": common.unknown(r["min_vert_clearance_in"]),
                "operating_rating": common.unknown(r["operating_rating"]),
                "inventory_rating": common.unknown(r["inventory_rating"]),
                "posting_status": common.unknown(r["posting_status"]),
                "confidence": common.unknown(r["confidence"]),
                "source_id": r["source_id"],
                "run_id": r["run_id"],
                "ingested_at": r["ingested_at"],
                "observed_at": common.unknown(r["observed_at"]),
                "vintage": _VINTAGE,
                "attribution": r["attribution"],
            },
        )
        for r in rows
    ]
    return common.feature_collection(features, limit=limit, offset=offset)
