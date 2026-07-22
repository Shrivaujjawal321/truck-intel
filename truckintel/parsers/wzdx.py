"""Parser: WZDx v4.x RoadEventFeed (GeoJSON FeatureCollection) -> events.

One parser for every WZDx feed in the registry (wzdx_wa, wzdx_mn, ...). The
spec drifted across v4.0 -> v4.2 (field renames, feed_info optionality), so
this parser reads BOTH spellings wherever the spec renamed things and passes
unknown extra fields through in props untouched. Verified against live feeds
2026-07-22: WSDOT v4.2, MN/KS CARS v4.0 (empty feed_info!), AZ511 v4.1.
"""
from __future__ import annotations

import json
from typing import Iterator

# ---------------------------------------------------------------- geometry

def _coord(pt: list) -> str:
    return f"{pt[0]} {pt[1]}"


def _ring_wkt(ring: list) -> str:
    return "(" + ", ".join(_coord(pt) for pt in ring) + ")"


def _geom_wkt(geom: dict | None) -> str | None:
    """GeoJSON geometry -> WKT (EPSG:4326). WZDx uses LineString and Point
    (spec) but real feeds also ship MultiPoint (MN CARS) — all handled.
    Anything else / missing -> None: geometry is never fabricated."""
    if not geom:
        return None
    gtype, coords = geom.get("type"), geom.get("coordinates")
    if not coords:
        return None
    if gtype == "Point":
        return "POINT (" + _coord(coords) + ")"
    if gtype == "MultiPoint":
        return "MULTIPOINT (" + ", ".join(f"({_coord(pt)})" for pt in coords) + ")"
    if gtype == "LineString":
        return "LINESTRING " + _ring_wkt(coords)
    if gtype == "MultiLineString":
        return "MULTILINESTRING (" + ", ".join(_ring_wkt(ln) for ln in coords) + ")"
    if gtype == "Polygon":
        return "POLYGON (" + ", ".join(_ring_wkt(r) for r in coords) + ")"
    if gtype == "MultiPolygon":
        polys = ("(" + ", ".join(_ring_wkt(r) for r in poly) + ")" for poly in coords)
        return "MULTIPOLYGON (" + ", ".join(polys) + ")"
    return None


# ------------------------------------------------------------- v4.x drift

def _feed_id(doc: dict) -> str | None:
    """The feed's self-declared identity, for the event_id prefix.

    v4.1+ feeds carry feed_info.publisher; some v4.0 feeds (MN/KS CARS) ship
    an EMPTY feed_info, so fall back to the first data source id. Returns
    None when the feed says nothing about itself — the per-event
    core_details.data_source_id then fills in (see parse)."""
    feed_info = doc.get("feed_info") or doc.get("road_event_feed_info") or {}
    if feed_info.get("publisher"):
        return str(feed_info["publisher"])
    for ds in feed_info.get("data_sources") or []:
        if ds.get("data_source_id"):
            return str(ds["data_source_id"])
    return None


def _road_event_id(feature: dict, props: dict) -> str | None:
    """v4.x: Feature.id; v4.1 (AZ511) duplicates it as properties.road_event_id;
    pre-v4 kept it in properties — read all spellings."""
    for cand in (feature.get("id"), props.get("road_event_id"),
                 (props.get("core_details") or {}).get("road_event_id")):
        if cand is not None:
            return str(cand)
    return None


def _verified(props: dict, new_key: str, old_key: str) -> bool | None:
    """v4.2 renamed {start,end}_date_accuracy ('verified'/'estimated') to
    is_{start,end}_date_verified (bool). Read both; absent -> None (unknown,
    never fabricated as False)."""
    if new_key in props and props[new_key] is not None:
        return bool(props[new_key])
    accuracy = props.get(old_key)
    if accuracy is None:
        return None
    return accuracy == "verified"


def _road_names(core: dict, props: dict) -> list | None:
    """v4: core_details.road_names (list); pre-v4: road_name (singular)."""
    names = core.get("road_names") or props.get("road_names")
    if names:
        return list(names)
    single = core.get("road_name") or props.get("road_name")
    return [single] if single else None


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per road event.

    Keys of each yielded dict (same contract as parsers/nws.py):
        event_id     str        '<feed id>/<road event id>' — the feed's
                                self-declared identity prefixes the event id so
                                ids stay readable across feeds; DB uniqueness
                                is (source_id, event_id) regardless
        kind         str        always 'work_zone'
        geom_wkt     str|None   WKT in EPSG:4326 (LineString/Point/Polygon +
                                Multi* variants); None only when the feed
                                genuinely omits geometry — never fabricated
        observed_at  str|None   the EVENT's update_date (else creation_date) —
                                the fact's vintage, never the fetch time
        props        dict       full feature properties passthrough + core
                                details flattened + drift-normalized keys
                                (road_names, is_*_date_verified, ...)
    """
    doc = json.loads(raw)
    # Envelope validation — an event_lifecycle publish of [] soft-closes every
    # active event for the source, so "not a feed" must NEVER read as "empty
    # feed". CDN-fronted 511 endpoints serve throttle/maintenance JSON with
    # HTTP 200 ({"status": "error", ...}); only a real FeatureCollection with
    # a features ARRAY (possibly empty — zero work zones is legitimate) may
    # publish. Anything else raises -> the run records 'failed', prior events
    # stay live, the breaker counts the contact.
    if not isinstance(doc, dict) or "features" not in doc:
        raise ValueError(
            "not a WZDx FeatureCollection (no 'features' key) — refusing to "
            "treat an unrecognized envelope as an empty feed"
        )
    features = doc["features"]
    if not isinstance(features, list):
        raise ValueError(
            f"WZDx 'features' is {type(features).__name__}, not a list — "
            "upstream drift, refusing to publish"
        )
    feed_id = _feed_id(doc)
    yielded = 0
    for feature in features:
        raw_props = dict(feature.get("properties") or {})
        core = dict(raw_props.get("core_details") or {})
        road_event_id = _road_event_id(feature, raw_props)
        if road_event_id is None:
            continue  # an event we cannot identify cannot be lifecycle-tracked

        # v4.x: dates live in core_details; pre-v4 kept them in properties.
        observed_at = (
            core.get("update_date") or raw_props.get("update_date")
            or core.get("creation_date") or raw_props.get("creation_date")
        )

        # Full passthrough first (unknown/extra fields survive), then overlay
        # the drift-normalized keys the API reads. Absent stays absent/None —
        # NULL renders unknown downstream, never 'no'.
        props = dict(raw_props)
        props.update(
            {
                "feed_id": feed_id,
                "road_event_id": road_event_id,
                "event_type": core.get("event_type") or raw_props.get("event_type"),
                "data_source_id": core.get("data_source_id"),
                "direction": core.get("direction") or raw_props.get("direction"),
                "road_names": _road_names(core, raw_props),
                "description": core.get("description") or raw_props.get("description"),
                "vehicle_impact": raw_props.get("vehicle_impact")
                or core.get("vehicle_impact"),
                "start_date": raw_props.get("start_date") or core.get("start_date"),
                "end_date": raw_props.get("end_date") or core.get("end_date"),
                "is_start_date_verified": _verified(
                    raw_props, "is_start_date_verified", "start_date_accuracy"
                ),
                "is_end_date_verified": _verified(
                    raw_props, "is_end_date_verified", "end_date_accuracy"
                ),
            }
        )

        yielded += 1
        yield {
            # No feed self-identity (empty feed_info) -> the event's own data
            # source id is the honest next-best prefix (MN/KS CARS case).
            "event_id": f"{feed_id or core.get('data_source_id') or 'wzdx'}"
                        f"/{road_event_id}",
            "kind": "work_zone",
            "geom_wkt": _geom_wkt(feature.get("geometry")),
            "observed_at": observed_at,
            "props": props,
        }
    # Id-field drift guard: individually unidentifiable events are skipped
    # (an event we cannot identify cannot be lifecycle-tracked), but a feed
    # where EVERY feature lost its id is upstream drift — publishing [] would
    # soft-close every active event while the run reads 'success'.
    if features and yielded == 0:
        raise ValueError(
            f"0 of {len(features)} WZDx features carried a recognizable road "
            "event id — upstream id-field drift, refusing to publish"
        )
