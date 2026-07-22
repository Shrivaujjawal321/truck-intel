"""GET /v1/live/weather-alerts — active NWS alerts from core.live_events."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common

router = APIRouter()

_ATTRIBUTION_FALLBACK = "National Weather Service (NOAA)"
_VINTAGE = "live NWS alert — observed_at is the alert issue time, not the fetch time"


def _zones(props: dict) -> list[str]:
    """Best-effort zone codes for locating alerts, essential when geometry is
    NULL (NWS zone-only alerts). Checks the shapes the parser may store."""
    geocode = props.get("geocode") or {}
    for cand in (props.get("zones"), geocode.get("UGC"), props.get("affectedZones")):
        if cand:
            return [str(z) for z in cand]
    return []


@router.get("/v1/live/weather-alerts")
def list_weather_alerts(
    bbox: str,
    include_nongeo: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Active (not soft-closed) NWS alerts intersecting the bbox. Alerts
    without polygon geometry are zone-only; they are excluded by default
    (a bbox can't match them) and returned with include_nongeo=true —
    never silently dropped as a design choice, the flag makes it explicit."""
    box = common.parse_bbox(bbox)
    geo_filter = "ST_Intersects(e.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
    if include_nongeo:
        geo_filter = f"({geo_filter} OR e.geom IS NULL)"
    rows = common.q_all(
        f"""
        SELECT e.event_id, ST_AsGeoJSON(e.geom) AS gj, e.first_seen, e.last_seen,
               e.source_id, e.run_id, e.ingested_at, e.observed_at, e.confidence,
               e.props, COALESCE(s.attribution_text, %s) AS attribution
        FROM core.live_events AS e
        LEFT JOIN ops.sources AS s USING (source_id)
        WHERE e.kind = 'weather_alert' AND e.soft_closed_at IS NULL AND {geo_filter}
        ORDER BY e.event_id
        LIMIT %s OFFSET %s
        """,
        [_ATTRIBUTION_FALLBACK, *box, limit, offset],
    )
    features = []
    for r in rows:
        props = r["props"] or {}
        features.append(
            common.feature(
                r["event_id"],
                r["gj"],
                {
                    "event_id": r["event_id"],
                    "event": common.unknown(props.get("event")),
                    "severity": common.unknown(props.get("severity")),
                    "headline": common.unknown(props.get("headline")),
                    "onset": common.unknown(props.get("onset")),
                    "expires": common.unknown(props.get("expires")),
                    "zones": _zones(props),
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "confidence": common.unknown(r["confidence"]),
                    "source_id": r["source_id"],
                    "run_id": r["run_id"],
                    "ingested_at": r["ingested_at"],
                    "observed_at": common.unknown(r["observed_at"]),
                    "vintage": _VINTAGE,
                    "attribution": r["attribution"],
                },
            )
        )
    return common.feature_collection(
        features, limit=limit, offset=offset, include_nongeo=include_nongeo
    )
