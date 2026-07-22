"""GET /v1/parking — truck parking sites."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common

router = APIRouter()

_ATTRIBUTION_FALLBACK = "Bureau of Transportation Statistics (BTS), US DOT"
# HONEST: NTAD amenity/capacity data dates to the ~2019 Jason's Law survey era;
# re-downloading it today does not make it current, and observed_at says so.
_VINTAGE = (
    "capacity and amenities from the ~2019 Jason's Law survey era — "
    "not re-verified at download time"
)


@router.get("/v1/parking")
def list_parking(
    bbox: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Parking sites in a bbox with capacity (truck_spaces) + vintage badge.
    NULL capacity renders 'unknown', never 0."""
    box = common.parse_bbox(bbox)
    rows = common.q_all(
        """
        SELECT p.site_id, p.kind, p.name, p.state, p.truck_spaces,
               ST_AsGeoJSON(p.geom) AS gj, p.source_id, p.run_id,
               p.ingested_at, p.observed_at, p.confidence,
               COALESCE(s.attribution_text, %s) AS attribution
        FROM core.parking_sites AS p
        LEFT JOIN ops.sources AS s USING (source_id)
        WHERE ST_Intersects(p.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
        ORDER BY p.site_id
        LIMIT %s OFFSET %s
        """,
        [_ATTRIBUTION_FALLBACK, *box, limit, offset],
    )
    features = [
        common.feature(
            r["site_id"],
            r["gj"],
            {
                "site_id": r["site_id"],
                "kind": r["kind"],
                "name": common.unknown(r["name"]),
                "state": common.unknown(r["state"]),
                "truck_spaces": common.unknown(r["truck_spaces"]),
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
