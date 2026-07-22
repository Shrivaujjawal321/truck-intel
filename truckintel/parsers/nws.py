"""Parser: NWS active alerts (api.weather.gov GeoJSON FeatureCollection) -> events."""
from __future__ import annotations

import json
from typing import Iterator


def _ring_wkt(ring: list) -> str:
    return "(" + ", ".join(f"{pt[0]} {pt[1]}" for pt in ring) + ")"


def _geom_wkt(geom: dict | None) -> str | None:
    """GeoJSON Polygon/MultiPolygon -> WKT (EPSG:4326). Anything else -> None —
    geometry is never fabricated (zone-only alerts stay geometry-less)."""
    if not geom:
        return None
    gtype, coords = geom.get("type"), geom.get("coordinates")
    if not coords:
        return None
    if gtype == "Polygon":
        return "POLYGON (" + ", ".join(_ring_wkt(r) for r in coords) + ")"
    if gtype == "MultiPolygon":
        polys = ("(" + ", ".join(_ring_wkt(r) for r in poly) + ")" for poly in coords)
        return "MULTIPOLYGON (" + ", ".join(polys) + ")"
    return None


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per active alert.

    Keys of each yielded dict:
        event_id     str        CAP alert identifier (feed's own id; upsert key)
        kind         str        always 'weather_alert' in MVP
        geom_wkt     str|None   WKT POLYGON/MULTIPOLYGON in EPSG:4326; None when
                                the alert carries zone references only (honest
                                NULL — no geometry is fabricated from zones in MVP)
        observed_at  str        ISO timestamp — the alert's sent/issued time,
                                never the fetch time
        props        dict       severity, headline, event type, onset, expires,
                                area description, full CAP properties
    """
    fc = json.loads(raw)
    # Envelope validation — same rule as parsers/wzdx.py: an event_lifecycle
    # publish of [] soft-closes every active alert, so a 200-with-error-JSON
    # body (api.weather.gov problem+json, proxy/maintenance envelopes) must
    # raise, never read as "zero active alerts". A FeatureCollection with an
    # empty features array IS a legitimate zero-alert state.
    if not isinstance(fc, dict) or "features" not in fc:
        raise ValueError(
            "not an NWS alerts FeatureCollection (no 'features' key) — "
            "refusing to treat an unrecognized envelope as an empty feed"
        )
    features = fc["features"]
    if not isinstance(features, list):
        raise ValueError(
            f"NWS 'features' is {type(features).__name__}, not a list — "
            "upstream drift, refusing to publish"
        )
    for feature in features:
        props = dict(feature.get("properties") or {})
        yield {
            "event_id": props.get("id") or feature.get("id"),
            "kind": "weather_alert",
            "geom_wkt": _geom_wkt(feature.get("geometry")),
            "observed_at": props.get("sent") or props.get("effective"),
            "props": props,
        }
