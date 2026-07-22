"""GET /v1/live/* — live layers from core.live_events.

- /v1/live/weather-alerts — active NWS alerts
- /v1/live/closures — WZDx work zones, with per-state coverage honesty
- /v1/live/chain-controls — Caltrans CWWP2 chain controls, with district coverage
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

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


# ------------------------------------------------------------------ closures

_WZDX_ATTRIBUTION_FALLBACK = "State DOT WZDx work-zone feeds"
_WZDX_VINTAGE = (
    "live WZDx work zone — observed_at is the event's update/creation time "
    "from the feed, not the fetch time"
)
# Registry convention: WZDx source ids are wzdx_<usps>[_<qualifier>]
# (wzdx_wa, wzdx_tx_austin, ...) — the state code is the coverage unit.
_WZDX_SOURCE_RE = re.compile(r"^wzdx_([a-z]{2})(?:_|$)")

_COVERAGE_NOTE = (
    "covered_states lists states with an enabled WZDx feed that is fresh "
    "(last success within its SLO) and not circuit-broken. No events in a "
    "state NOT listed here means UNKNOWN — not clear roads."
)


def _covered_states(feed_rows: list[dict], now: datetime) -> list[str]:
    """States whose WZDx feed is enabled + fresh + not circuit-broken.

    Honesty rule: coverage is a claim about the PIPELINE, so it is derived
    from ops.sources + ops.source_runs (publish freshness) + ops.feed_health
    (breaker), never from which states happen to have events. A feed that
    never published, went stale past its SLO (last 'success' /
    'skipped_unchanged' run — gated/keyless runs do NOT count), or tripped
    the breaker ('open') drops out — its state reads unknown."""
    covered: set[str] = set()
    for row in feed_rows:
        m = _WZDX_SOURCE_RE.match(row["source_id"])
        if m is None or not row["enabled"] or row["breaker_state"] == "open":
            continue
        last_ok, slo_hours = row["last_success_at"], row["slo_hours"]
        if last_ok is None or slo_hours is None:
            continue
        if (now - last_ok).total_seconds() <= slo_hours * 3600:
            covered.add(m.group(1).upper())
    return sorted(covered)


@router.get("/v1/live/closures")
def list_closures(
    bbox: str,
    active_only: bool = True,
    include_nongeo: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """WZDx work zones intersecting the bbox. active_only=true (default)
    returns only events the source feed still reports (soft_closed_at IS
    NULL); false includes soft-closed history. Events without geometry are
    rare in WZDx but real — include_nongeo=true returns them explicitly,
    same design as weather-alerts. covered_states makes the coverage hole
    honest: WZDx is per-agency, not nationwide."""
    box = common.parse_bbox(bbox)
    filters = ["e.kind = 'work_zone'"]
    if active_only:
        filters.append("e.soft_closed_at IS NULL")
    geo_filter = "ST_Intersects(e.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
    if include_nongeo:
        geo_filter = f"({geo_filter} OR e.geom IS NULL)"
    filters.append(geo_filter)
    rows = common.q_all(
        f"""
        SELECT e.event_id, ST_AsGeoJSON(e.geom) AS gj, e.first_seen, e.last_seen,
               e.soft_closed_at, e.source_id, e.run_id, e.ingested_at,
               e.observed_at, e.confidence, e.props,
               COALESCE(s.attribution_text, %s) AS attribution
        FROM core.live_events AS e
        LEFT JOIN ops.sources AS s USING (source_id)
        WHERE {' AND '.join(filters)}
        ORDER BY e.source_id, e.event_id
        LIMIT %s OFFSET %s
        """,
        [_WZDX_ATTRIBUTION_FALLBACK, *box, limit, offset],
    )
    # Coverage truth comes from the pipeline's own health bookkeeping.
    # last_success_at is derived from ops.source_runs with the SAME statuses
    # scripts/freshness_check.py calls fresh ('success' = data published,
    # 'skipped_unchanged' = re-verified identical) — NOT from
    # ops.feed_health.last_success_at, which the engine also bumps for 'gated'
    # (all rows rejected, nothing published) and 'skipped_no_key' (never
    # fetched) runs: those are healthy CONTACT for the breaker but must never
    # keep a state advertised as covered while zero events flow.
    feed_rows = common.q_all(
        r"""
        SELECT s.source_id, s.enabled, s.slo_hours,
               fh.state AS breaker_state, ok.last_ok_at AS last_success_at
        FROM ops.sources AS s
        LEFT JOIN ops.feed_health AS fh USING (source_id)
        LEFT JOIN LATERAL (
            SELECT max(coalesce(r.finished_at, r.started_at)) AS last_ok_at
            FROM ops.source_runs AS r
            WHERE r.source_id = s.source_id
              AND r.status IN ('success', 'skipped_unchanged')
        ) AS ok ON TRUE
        WHERE s.source_id LIKE %s
        """,
        [r"wzdx\_%"],
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
                    "event_type": common.unknown(props.get("event_type")),
                    "direction": common.unknown(props.get("direction")),
                    "road_names": common.unknown(props.get("road_names")),
                    "description": common.unknown(props.get("description")),
                    "vehicle_impact": common.unknown(props.get("vehicle_impact")),
                    "start_date": common.unknown(props.get("start_date")),
                    "end_date": common.unknown(props.get("end_date")),
                    "is_start_date_verified": common.unknown(
                        props.get("is_start_date_verified")
                    ),
                    "is_end_date_verified": common.unknown(
                        props.get("is_end_date_verified")
                    ),
                    "restrictions": common.unknown(props.get("restrictions")),
                    "active": r["soft_closed_at"] is None,
                    "soft_closed_at": r["soft_closed_at"],  # null = still active
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "confidence": common.unknown(r["confidence"]),
                    "source_id": r["source_id"],
                    "run_id": r["run_id"],
                    "ingested_at": r["ingested_at"],
                    "observed_at": common.unknown(r["observed_at"]),
                    "vintage": _WZDX_VINTAGE,
                    "attribution": r["attribution"],
                },
            )
        )
    return common.feature_collection(
        features,
        limit=limit,
        offset=offset,
        active_only=active_only,
        include_nongeo=include_nongeo,
        covered_states=_covered_states(feed_rows, datetime.now(timezone.utc)),
        coverage_note=_COVERAGE_NOTE,
    )


# ------------------------------------------------------------- chain controls

_CC_ATTRIBUTION_FALLBACK = "California DOT (Caltrans CWWP2)"
_CC_VINTAGE = (
    "live Caltrans chain control — observed_at is when the status became "
    "effective (Pacific), not the fetch time"
)
# Registry convention: Caltrans CWWP2 chain-control ids are
# caltrans_cwwp2_cc_d<NN> — the district is the coverage unit.
_CC_SOURCE_RE = re.compile(r"^caltrans_cwwp2_cc_(d\d{2})$")
_CC_COVERAGE_NOTE = (
    "covered_districts lists Caltrans districts with an enabled chain-control "
    "feed that is fresh (last success within its SLO) and not circuit-broken. "
    "A pass in a district NOT listed here reads UNKNOWN — not 'chains not "
    "required'. Coverage is currently California only (keyless CWWP2); other "
    "states' 511 chain/closure feeds require API keys + a signed DAA."
)


def _covered_districts(feed_rows: list[dict], now: datetime) -> list[str]:
    """Caltrans districts whose CWWP2 feed is enabled + fresh + not broken —
    the same pipeline-derived honesty as _covered_states (never derived from
    which districts happen to have controls)."""
    covered: set[str] = set()
    for row in feed_rows:
        m = _CC_SOURCE_RE.match(row["source_id"])
        if m is None or not row["enabled"] or row["breaker_state"] == "open":
            continue
        last_ok, slo_hours = row["last_success_at"], row["slo_hours"]
        if last_ok is None or slo_hours is None:
            continue
        if (now - last_ok).total_seconds() <= slo_hours * 3600:
            covered.add(m.group(1).upper())
    return sorted(covered)


@router.get("/v1/live/chain-controls")
def list_chain_controls(
    bbox: str,
    chains_required_only: bool = False,
    active_only: bool = True,
    include_nongeo: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Caltrans chain controls intersecting the bbox. Each feature is an
    in-service chain-control point with its current status.

    - active_only=true (default): only points the feed still reports
      (soft_closed_at IS NULL); false includes soft-closed history.
    - chains_required_only=true: only points where chains/4WD are actually
      required now (status not R-0). Default false returns ALL in-service
      points, so R-0 renders as the real fact "no chains required as of HH:MM"
      — an answer, not an absence.

    props.status is the Caltrans code (R-0..R-3); props.active is the derived
    chains-required flag. covered_districts makes the CA-only coverage honest."""
    box = common.parse_bbox(bbox)
    filters = ["e.kind = 'chain_control'"]
    if active_only:
        filters.append("e.soft_closed_at IS NULL")
    if chains_required_only:
        filters.append("(e.props->>'active')::boolean IS TRUE")
    geo_filter = "ST_Intersects(e.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
    if include_nongeo:
        geo_filter = f"({geo_filter} OR e.geom IS NULL)"
    filters.append(geo_filter)
    rows = common.q_all(
        f"""
        SELECT e.event_id, ST_AsGeoJSON(e.geom) AS gj, e.first_seen, e.last_seen,
               e.soft_closed_at, e.source_id, e.run_id, e.ingested_at,
               e.observed_at, e.confidence, e.props,
               COALESCE(s.attribution_text, %s) AS attribution
        FROM core.live_events AS e
        LEFT JOIN ops.sources AS s USING (source_id)
        WHERE {' AND '.join(filters)}
        ORDER BY e.source_id, e.event_id
        LIMIT %s OFFSET %s
        """,
        [_CC_ATTRIBUTION_FALLBACK, *box, limit, offset],
    )
    feed_rows = common.q_all(
        r"""
        SELECT s.source_id, s.enabled, s.slo_hours,
               fh.state AS breaker_state, ok.last_ok_at AS last_success_at
        FROM ops.sources AS s
        LEFT JOIN ops.feed_health AS fh USING (source_id)
        LEFT JOIN LATERAL (
            SELECT max(coalesce(r.finished_at, r.started_at)) AS last_ok_at
            FROM ops.source_runs AS r
            WHERE r.source_id = s.source_id
              AND r.status IN ('success', 'skipped_unchanged')
        ) AS ok ON TRUE
        WHERE s.source_id LIKE %s
        """,
        [r"caltrans\_cwwp2\_cc\_%"],
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
                    "route": common.unknown(props.get("route")),
                    "direction": common.unknown(props.get("direction")),
                    "county": common.unknown(props.get("county")),
                    "location_name": common.unknown(props.get("location_name")),
                    "district": common.unknown(props.get("district")),
                    "status": common.unknown(props.get("status")),
                    "status_description": common.unknown(props.get("status_description")),
                    "chains_required": common.unknown(props.get("active")),
                    "postmile": common.unknown(props.get("postmile")),
                    "elevation_ft": common.unknown(props.get("elevation_ft")),
                    "active": r["soft_closed_at"] is None,
                    "soft_closed_at": r["soft_closed_at"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "confidence": common.unknown(r["confidence"]),
                    "source_id": r["source_id"],
                    "run_id": r["run_id"],
                    "ingested_at": r["ingested_at"],
                    "observed_at": common.unknown(r["observed_at"]),
                    "vintage": _CC_VINTAGE,
                    "attribution": r["attribution"],
                },
            )
        )
    return common.feature_collection(
        features,
        limit=limit,
        offset=offset,
        active_only=active_only,
        chains_required_only=chains_required_only,
        include_nongeo=include_nongeo,
        covered_districts=_covered_districts(feed_rows, datetime.now(timezone.utc)),
        coverage_note=_CC_COVERAGE_NOTE,
    )
