"""GET /v1/tunnels + /v1/tunnels/{tunnel_id} — NTI tunnels with curated rules.

NOT registered in api/main.py by this module — the integrator adds
`routes_tunnels` to the import and `routes_tunnels.router` to the router loop.

Curated-rule embedding: data/curated/tunnel_rules.yaml is matched at request
time by (state, name substring) — see the MATCHING note in that file. The
matched rule (with its source_url + last_reviewed) rides under the feature's
`curated_rule` property; no match -> explicit null (we know there is no
curated rule — that is real absence, not unknown).
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, Query

from api import common

logger = logging.getLogger(__name__)

router = APIRouter()

_ATTRIBUTION_FALLBACK = (
    "Federal Highway Administration (FHWA) / "
    "Bureau of Transportation Statistics (BTS), US DOT"
)
_VINTAGE = (
    "NTI annual snapshot — observed_at is the FHWA inventory vintage year, "
    "never the download date"
)

_RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "curated" / "tunnel_rules.yaml"

_SELECT = """
SELECT t.tunnel_id, t.name, t.state, ST_AsGeoJSON(t.geom) AS gj,
       t.length_ft::float8 AS length_ft,
       t.min_vert_clearance_in::float8 AS min_vert_clearance_in,
       t.hazmat_restricted, t.hazmat_codes,
       t.source_id, t.run_id, t.ingested_at, t.observed_at, t.confidence,
       t.props,
       COALESCE(s.attribution_text, %s) AS attribution
FROM core.tunnels AS t
LEFT JOIN ops.sources AS s USING (source_id)
"""


# mtime-keyed cache: the rules file is hand-edited on a quarterly review
# cadence while truckintel-api.service runs for months — an lru_cache would
# serve superseded safety rules until a process restart. Re-parse only when
# the file changes; malformed content degrades to no-curated-rule (with a log
# line), NEVER a 500 — a typo during the manual review must not take down the
# tunnels API (the federal NTI data is unrelated to the curated layer).
_rules_cache: dict = {"mtime": None, "rules": {}}


def _rules() -> dict:
    """Curated rules keyed by rule id; {} when the file is absent/empty/broken."""
    try:
        mtime = _RULES_PATH.stat().st_mtime_ns
    except OSError:
        _rules_cache.update(mtime=None, rules={})
        return {}
    if _rules_cache["mtime"] == mtime:
        return _rules_cache["rules"]
    rules: dict = {}
    try:
        doc = yaml.safe_load(_RULES_PATH.read_text()) or {}
        raw = doc.get("rules") or {} if isinstance(doc, dict) else {}
        if isinstance(raw, dict):
            # a rule body that is not a mapping is a typo — drop just that rule
            for key, rule in raw.items():
                if isinstance(rule, dict):
                    rules[key] = rule
                else:
                    logger.warning(
                        "tunnel_rules.yaml: rule %r is not a mapping — ignored", key
                    )
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("tunnel_rules.yaml unreadable (%s) — serving no curated rules", exc)
        rules = {}
    _rules_cache.update(mtime=mtime, rules=rules)
    return rules


def _match_rule(state: str | None, name: str | None) -> tuple[str, dict] | None:
    """First rule whose match.states contains the row's state AND any
    match.name_patterns substring occurs (case-insensitive) in the name."""
    if not state or not name:
        return None
    lowered = name.lower()
    for key, rule in _rules().items():
        match = rule.get("match")
        if not isinstance(match, dict):  # match block missing/typo'd -> no match
            continue
        states = match.get("states")
        if not isinstance(states, list) or state.upper() not in states:
            continue
        patterns = match.get("name_patterns")
        if isinstance(patterns, list) and any(
            isinstance(pat, str) and pat in lowered for pat in patterns
        ):
            return key, rule
    return None


def _curated_rule(state: str | None, name: str | None) -> dict | None:
    """Embeddable curated-rule dict (rule fields + key, match block dropped)."""
    hit = _match_rule(state, name)
    if hit is None:
        return None
    key, rule = hit
    return {"key": key, **{k: v for k, v in rule.items() if k != "match"}}


def _properties(r: dict, *, include_record: bool = False) -> dict:
    props = {
        "tunnel_id": r["tunnel_id"],
        "name": common.unknown(r["name"]),
        "state": common.unknown(r["state"]),
        "length_ft": common.unknown(r["length_ft"]),
        "min_vert_clearance_in": common.unknown(r["min_vert_clearance_in"]),
        # tri-state honesty: NULL = unknown, never "no"; False is real data
        "hazmat_restricted": common.unknown(r["hazmat_restricted"]),
        "hazmat_codes": common.unknown(r["hazmat_codes"]),
        "curated_rule": _curated_rule(r["state"], r["name"]),
        "confidence": common.unknown(r["confidence"]),
        "source_id": r["source_id"],
        "run_id": r["run_id"],
        "ingested_at": r["ingested_at"],
        "observed_at": common.unknown(r["observed_at"]),
        "vintage": _VINTAGE,
        "attribution": r["attribution"],
    }
    if include_record:
        props["record"] = r["props"]  # full NTI attribute record (detail only)
    return props


@router.get("/v1/tunnels")
def list_tunnels(
    bbox: str,
    state: str | None = Query(default=None, pattern="^[A-Za-z]{2}$"),
    max_clearance_lt_in: float | None = Query(default=None, gt=0),
    hazmat: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Tunnels in a bbox, optionally filtered by state, by min vertical
    clearance below the given inches, or by hazmat restriction flag. GeoJSON
    out; every feature carries source, vintage (observed_at), confidence and
    the curated authority rule when one exists. bbox='minLon,minLat,maxLon,
    maxLat', capped at 4x4 degrees.
    """
    box = common.parse_bbox(bbox)
    where = ["ST_Intersects(t.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"]
    params: list = [_ATTRIBUTION_FALLBACK, *box]
    if state is not None:
        where.append("t.state = %s")
        params.append(state.upper())
    if max_clearance_lt_in is not None:
        # NULL clearance is *unknown*, not "below the limit" — excluded on purpose.
        where.append("t.min_vert_clearance_in < %s")
        params.append(max_clearance_lt_in)
    if hazmat is not None:
        # tri-state: NULL (unknown) never matches either filter value
        where.append("t.hazmat_restricted IS TRUE" if hazmat
                     else "t.hazmat_restricted IS FALSE")
    rows = common.q_all(
        f"""
        {_SELECT}
        WHERE {" AND ".join(where)}
        ORDER BY t.tunnel_id
        LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    )
    features = [
        common.feature(r["tunnel_id"], r["gj"], _properties(r)) for r in rows
    ]
    return common.feature_collection(features, limit=limit, offset=offset)


@router.get("/v1/tunnels/{tunnel_id}")
def get_tunnel(tunnel_id: str) -> dict:
    """One tunnel by natural key (state FIPS + NTI tunnel number) as a single
    GeoJSON Feature, including the full NTI attribute record under
    properties.record."""
    rows = common.q_all(
        f"{_SELECT} WHERE t.tunnel_id = %s",
        [_ATTRIBUTION_FALLBACK, tunnel_id],
    )
    if not rows:
        raise common.ApiError(
            "not_found", f"no tunnel with tunnel_id {tunnel_id!r}", status=404
        )
    r = rows[0]
    return common.feature(r["tunnel_id"], r["gj"], _properties(r, include_record=True))
