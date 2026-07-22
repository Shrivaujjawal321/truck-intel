"""Parser: Caltrans CWWP2 chain-control JSON -> events (core.live_events).

Caltrans' CWWP2 portal publishes per-district JSON with ZERO auth (verified on
the wire 2026-07-22: GET https://cwwp2.dot.ca.gov/data/d3/cc/ccStatusD03.json
returned 230 chain-control records). This is the flagship KEYLESS 511-class
live-ops adapter — most state 511 feeds need an API key + a signed Developer
Access Agreement (Phase-2 wait items); Caltrans does not, so it proves the
live-ops framework end to end without a human-latency dependency.

Chain controls are truck-critical: on CA mountain passes (Donner/I-80, Echo
Summit/US-50, the Sierra 395/89 passes) chains or 4WD can be a legal requirement
in winter. Status codes: R-0 (none in effect) · R-1 (chains or 4WD w/ snow
tires) · R-2 (chains, 4WD exempt) · R-3 (chains required, all vehicles).

Contract — identical 5-key shape as parsers/wzdx.py and parsers/nws.py:
    event_id     str        'cwwp2-cc/<index>' — the record's own stable index
    kind         str        always 'chain_control'
    geom_wkt     str|None   'POINT (lon lat)' EPSG:4326; None if the record has
                            no parseable coordinate — never fabricated
    observed_at  str|None   ISO-8601 of statusTimestamp (when the status became
                            effective), Pacific-localized; the fact's vintage,
                            never the fetch time
    props        dict       state=CA, district, route, direction, county,
                            location_name, status, status_description, active
                            flag, in_service, timestamps + the full raw record

Honesty rules (tri-state):
  * Only in-service records are emitted. An out-of-service sensor means the
    status is UNKNOWN there — emitting it as "R-0 / no controls" would be a
    fabricated 'no', so it is skipped (soft-closes if it was live before).
  * R-0 ("no chain controls in effect") IS emitted: it is a real, timestamped
    operational fact ("chains not required as of HH:MM"), not an absence.
    props['active'] distinguishes chains-required (True) from R-0 (False).
  * Envelope validation raises on any unrecognized shape — a CDN throttle/error
    body served as HTTP 200 must never read as an empty feed (an empty
    event_lifecycle publish soft-closes every live control for the source).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

from truckintel.validate import _in_any_us_box

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _iso_pacific(ts: dict | None, date_key: str, time_key: str) -> str | None:
    """Caltrans timestamps are wall-clock Pacific with no zone. Localize to
    America/Los_Angeles (DST-correct) and return ISO-8601; unparseable -> None
    (a bad timestamp is never guessed)."""
    if not isinstance(ts, dict):
        return None
    date_s, time_s = ts.get(date_key), ts.get(time_key)
    if not date_s or not time_s:
        return None
    try:
        naive = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=_PACIFIC).isoformat()


def _point_wkt(location: dict) -> str | None:
    """location.longitude/latitude (strings) -> 'POINT (lon lat)'. Missing,
    unparseable, null-island, or outside the US -> None: a junk coordinate is
    DROPPED (geometry never invented), never published as a real location.

    The honesty guard lives HERE, at the point geometry is created, on purpose:
    gate2_coords validates only rows carrying 'lat'/'lon' keys, so a geom_wkt
    row would otherwise bypass every coordinate check (null-island sensor
    defaults, out-of-US geocoding errors) and publish invented geometry."""
    try:
        lon = float(location.get("longitude"))
        lat = float(location.get("latitude"))
    except (TypeError, ValueError):
        return None
    if not (lon == lon and lat == lat):          # NaN/inf guard (float('nan') parses)
        return None
    if lat == 0.0 and lon == 0.0:                # null-island: a no-fix placeholder
        return None
    if not _in_any_us_box(lon, lat):             # out-of-US (incl. axis swaps) -> unknown
        return None
    return f"POINT ({lon} {lat})"


def _first_number(*vals) -> str | None:
    for v in vals:
        if v not in (None, ""):
            return str(v)
    return None


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per in-service chain-control record (see module docstring
    for the key contract)."""
    doc = json.loads(raw)
    # Envelope validation — a non-{data:[...]} body (throttle/error JSON served
    # 200) must raise, never publish []: [] soft-closes every live control.
    if not isinstance(doc, dict) or "data" not in doc:
        raise ValueError(
            "not a CWWP2 document (no 'data' key) — refusing to treat an "
            "unrecognized envelope as an empty feed"
        )
    data = doc["data"]
    if not isinstance(data, list):
        raise ValueError(
            f"CWWP2 'data' is {type(data).__name__}, not a list — upstream "
            "drift, refusing to publish"
        )

    seen_records = 0
    yielded = 0
    for item in data:
        rec = item.get("cc") if isinstance(item, dict) else None
        if not isinstance(rec, dict):
            continue  # not a chain-control record wrapper — skip, don't guess
        index = rec.get("index")
        if not index:
            continue  # unidentifiable -> cannot be lifecycle-tracked
        seen_records += 1

        # Only in-service records carry a trustworthy status (tri-state honesty).
        if str(rec.get("inService", "")).lower() != "true":
            continue

        location = dict(rec.get("location") or {})
        status_data = dict(rec.get("statusData") or {})
        status = status_data.get("status")
        # Tri-state (NULL renders "unknown", never a fabricated "no"): a known
        # status -> True (R-1/2/3, chains required) / False (R-0, none); a
        # missing/empty status -> None (unknown), NOT False — chains-required is
        # a safety-relevant fact we must not assert as "no" when we don't know.
        active = None if not status else not str(status).upper().startswith("R-0")

        observed_at = (
            _iso_pacific(status_data.get("statusTimestamp"), "statusDate", "statusTime")
            or _iso_pacific(rec.get("recordTimestamp"), "recordDate", "recordTime")
        )

        props = {
            "state": "CA",
            "district": location.get("district"),
            "route": location.get("route"),
            "route_suffix": location.get("routeSuffix") or None,
            "direction": location.get("direction") or None,
            "county": location.get("county"),
            "location_name": location.get("locationName"),
            "nearby_place": location.get("nearbyPlace"),
            "postmile": _first_number(location.get("postmile")),
            "milepost": _first_number(location.get("milepost")),
            "elevation_ft": _first_number(location.get("elevation")),
            "status": status,
            "status_description": status_data.get("statusDescription"),
            "active": active,
            "in_service": True,
            "record_timestamp": rec.get("recordTimestamp"),
            "status_timestamp": status_data.get("statusTimestamp"),
            "raw": rec,
        }

        yielded += 1
        yield {
            "event_id": f"cwwp2-cc/{index}",
            "kind": "chain_control",
            "geom_wkt": _point_wkt(location),
            "observed_at": observed_at,
            "props": props,
        }

    # Drift guard: records present but none identifiable/in-service can be a
    # legitimate all-out-of-service district, so only raise when we could not
    # read a single 'cc' record at all (envelope drift), matching wzdx.py's
    # "0 of N carried an id" guard. A valid feed with 0 in-service controls
    # publishes [] on purpose (soft-close), which IS correct here.
    if data and seen_records == 0:
        raise ValueError(
            f"0 of {len(data)} CWWP2 items were recognizable chain-control "
            "records ('cc' wrapper missing) — upstream drift, refusing to publish"
        )
