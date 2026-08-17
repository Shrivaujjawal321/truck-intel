"""GET /v1/parking — truck parking sites."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common, liveness_filter

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
    include_closed: bool = Query(
        default=False,
        description="include sites a source asserted CLOSED (default: hidden)"),
    min_liveness: int | None = Query(
        default=None, ge=0, le=100,
        description="drop rows scored below this (also drops unscored rows)"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Parking sites in a bbox with capacity (truck_spaces) + vintage badge.
    NULL capacity renders 'unknown', never 0.

    Liveness (Gate 6) travels with every row. Note that most of this layer
    scores 'unknown': the capacity survey is 2019-vintage and almost nothing
    re-confirms a public rest area. That is the honest state — a rest area
    nobody has re-surveyed is still, overwhelmingly, a rest area — so those
    rows are returned and badged rather than filtered away."""
    box = common.parse_bbox(bbox)
    live_where, live_params, filter_notes = liveness_filter.where(
        "p", include_closed=include_closed, min_liveness=min_liveness)
    where = ["ST_Intersects(p.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"]
    where.extend(live_where)
    rows = common.q_all(
        f"""
        SELECT p.site_id, p.kind, p.name, p.state, p.truck_spaces,
               ST_AsGeoJSON(p.geom) AS gj, p.source_id, p.run_id,
               p.ingested_at, p.observed_at, p.confidence,
               {liveness_filter.select_cols('p')},
               COALESCE(s.attribution_text, %s) AS attribution
        FROM core.parking_sites AS p
        LEFT JOIN ops.sources AS s USING (source_id)
        WHERE {' AND '.join(where)}
        ORDER BY p.site_id
        LIMIT %s OFFSET %s
        """,
        # Order follows the statement, not the clause: the attribution
        # placeholder sits in the SELECT list, ahead of the WHERE.
        [_ATTRIBUTION_FALLBACK, *box, *live_params, limit, offset],
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
                **liveness_filter.props(r),
            },
        )
        for r in rows
    ]
    return common.feature_collection(
        features,
        note=liveness_filter.note(),
        filter_notes=filter_notes,
        limit=limit,
        offset=offset,
    )
