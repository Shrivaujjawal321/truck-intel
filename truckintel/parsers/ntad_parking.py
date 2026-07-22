"""Parser: NTAD Truck Stop Parking (ArcGIS GeoJSON pages, concatenated) -> sites.

The fetcher concatenates all resultOffset pages into one GeoJSON
FeatureCollection so the raw artifact is one file, not many fragments.
"""
from __future__ import annotations

import json
from typing import Iterator

# Honesty rule (registry YAML + plan §11): the amenity data is from the ~2019
# Jason's Law survey era, NOT the download date.
OBSERVED_AT = "2019-01-01"

# The live layer (verified 2026-07-22) has no facility-type field — sites are
# NHS rest stops (`nhs_rest_stop` is the site NAME). Deterministic name rule,
# not inference: only an explicit truck-stop-ish name maps to 'truck_stop'.
_TRUCK_STOP_HINTS = ("truck stop", "truckstop", "travel center", "travel plaza")


def _kind(name: str | None) -> str:
    lowered = (name or "").lower()
    if any(hint in lowered for hint in _TRUCK_STOP_HINTS):
        return "truck_stop"
    return "public_rest_area"


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per parking site.

    Keys of each yielded dict:
        site_id       str        NTAD feature id (natural key)
        kind          str        'truck_stop' | 'public_rest_area' (from facility type)
        name          str|None
        state         str|None   2-letter USPS code
        lat, lon      float      decimal degrees (point geometry)
        truck_spaces  int|None   capacity; None = unknown (NEVER coerce 0/None)
        observed_at   str        ISO date of the ~2019 Jason's Law survey era —
                                 the honesty rule: NOT the download date
        props         dict       full cleaned attribute record
    """
    fc = json.loads(raw)
    for feature in fc.get("features", []):
        props = dict(feature.get("properties") or {})
        geom = feature.get("geometry") or {}

        if geom.get("type") == "Point" and geom.get("coordinates"):
            lon, lat = geom["coordinates"][:2]
        else:  # attribute coords as fallback (same values on the live layer)
            lat, lon = props.get("latitude"), props.get("longitude")

        object_id = props.get("OBJECTID", feature.get("id"))
        name = (props.get("nhs_rest_stop") or "").strip() or None
        state = (props.get("state") or "").strip().upper() or None
        spots = props.get("number_of_spots")

        yield {
            "site_id": str(object_id) if object_id is not None else None,
            "kind": _kind(name),
            "name": name,
            "state": state,
            "lat": lat,
            "lon": lon,
            "truck_spaces": int(spots) if spots is not None else None,
            "observed_at": OBSERVED_AT,
            "props": props,
        }
