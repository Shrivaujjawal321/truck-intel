"""Validation gates 1-2 (plan §7). Deterministic, replayable, between staging
and core. Rejects carry a reason and the raw record; we NEVER silently 'fix'
a coordinate — flag or reject only.
"""
from __future__ import annotations

from typing import Iterable

# In-US bounding boxes (lon_min, lat_min, lon_max, lat_max), EPSG:4326.
# Coarse by design — gate 2 catches junk and swaps, not survey-grade fit.
# (TIGER polygon point-in-polygon check is the Phase-2 upgrade, plan §7.)
US_BBOXES: tuple[tuple[float, float, float, float], ...] = (
    (-125.0, 24.4, -66.9, 49.4),    # CONUS
    (-179.9, 51.0, -129.9, 71.5),   # Alaska (east of the antimeridian)
    (-160.6, 18.8, -154.7, 22.5),   # Hawaii
    (-67.6, 17.5, -64.4, 18.6),     # Puerto Rico + USVI
)

# Fields gate 1 additionally requires to parse as floats when required.
_FLOAT_FIELDS = ("lat", "lon", "price_usd_gal")


def _reject(reason: str, row: dict) -> dict:
    return {"reason": reason, "raw_record": row}


def gate1_schema(
    rows: Iterable[dict],
    required_fields: tuple[str, ...],
) -> tuple[list[dict], list[dict]]:
    """Gate 1 — required fields present and parseable.

    A row missing any required field (or with an unparseable value, e.g. lat
    that is not a float) is rejected with reason 'missing_required:<field>' /
    'unparseable:<field>'.

    Returns (ok_rows, rejects); each reject is
    {"reason": str, "raw_record": dict} ready for quality.rejects.
    """
    ok_rows: list[dict] = []
    rejects: list[dict] = []
    for row in rows:
        reason = None
        for field in required_fields:
            if field not in row or row[field] is None:
                reason = f"missing_required:{field}"
                break
            if field in _FLOAT_FIELDS:
                try:
                    float(row[field])
                except (TypeError, ValueError):
                    reason = f"unparseable:{field}"
                    break
        if reason is None:
            ok_rows.append(row)
        else:
            rejects.append(_reject(reason, row))
    return ok_rows, rejects


def _in_any_us_box(lon: float, lat: float) -> bool:
    return any(
        lon_min <= lon <= lon_max and lat_min <= lat <= lat_max
        for lon_min, lat_min, lon_max, lat_max in US_BBOXES
    )


def gate2_coords(rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Gate 2 — coordinate sanity on rows with 'lat'/'lon' keys.

    Checks to implement, in order:
    1. junk: null island (0,0), |lat| > 90, |lon| > 180
       -> reject reason 'coords_out_of_range'
    2. in-US: (lon, lat) inside any US_BBOXES box -> pass
    3. swap detection: if (lon, lat) fails but (lat, lon) WOULD pass, the feed
       swapped its axes -> reject reason 'latlon_swapped'. NEVER auto-fix —
       a silent swap 'fix' that is wrong puts a bridge in the wrong state.
    4. anything else -> reject reason 'coords_not_in_us'

    Returns (ok_rows, rejects) shaped like gate1_schema.
    """
    ok_rows: list[dict] = []
    rejects: list[dict] = []
    for row in rows:
        if "lat" not in row or "lon" not in row:
            ok_rows.append(row)  # non-point rows (fuel, zone-only alerts) pass through
            continue
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (TypeError, ValueError):
            rejects.append(_reject("coords_out_of_range", row))
            continue
        if (lat == 0.0 and lon == 0.0) or abs(lat) > 90.0 or abs(lon) > 180.0:
            rejects.append(_reject("coords_out_of_range", row))
        elif _in_any_us_box(lon, lat):
            ok_rows.append(row)
        elif _in_any_us_box(lat, lon):  # would pass with axes swapped
            rejects.append(_reject("latlon_swapped", row))
        else:
            rejects.append(_reject("coords_not_in_us", row))
    return ok_rows, rejects
