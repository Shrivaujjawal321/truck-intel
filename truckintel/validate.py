"""Validation gates 1-2 (plan §7). Deterministic, replayable, between staging
and core. Rejects carry a reason and the raw record; we NEVER silently 'fix'
a coordinate — flag or reject only.
"""
from __future__ import annotations

import re
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


# WKT type prefix, up to and including the opening paren: 'LINESTRING(',
# 'POLYGON ((', 'MULTILINESTRING Z ('. EMPTY geometries have no paren and so
# do not match — they carry no coordinates to check.
_WKT_HEAD_RE = re.compile(r"^\s*([A-Za-z]+)\s*(?:ZM|Z|M)?\s*\(", re.IGNORECASE)


def wkt_coords(wkt: str) -> list[tuple[float, float]] | None:
    """Every (lon, lat) vertex in an EPSG:4326 WKT geometry, or None if the
    string is not parseable as WKT with coordinates.

    Type-agnostic by construction: WKT vertices are always `x y [z [m]]`
    groups separated by commas, whatever the nesting, so stripping the
    parentheses and splitting on commas yields the vertex list for POINT,
    LINESTRING, POLYGON, and every MULTI*/COLLECTION form alike. Extra Z/M
    ordinates are ignored — gate 2 judges horizontal position only.
    """
    if not isinstance(wkt, str):
        return None
    head = _WKT_HEAD_RE.match(wkt)
    if head is None:
        return None
    body = wkt[head.end() - 1:].replace("(", " ").replace(")", " ")
    coords: list[tuple[float, float]] = []
    for chunk in body.split(","):
        parts = chunk.split()
        if len(parts) < 2:
            return None
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        coords.append((lon, lat))
    return coords or None


def _judge_coords(coords: list[tuple[float, float]]) -> str | None:
    """Shared verdict for one or many vertices: a reject reason, or None if
    the geometry passes.

    Multi-vertex rule (the honest generalisation of the point rule):
    * EVERY vertex must be in range and off null island — one (0,0) or
      |lat| > 90 vertex means the producer emitted junk, whatever the rest
      looks like.
    * AT LEAST ONE vertex must fall in a US box. A work-zone LineString or an
      NWS polygon may legitimately cross into Canada, Mexico, or offshore
      water; a feed that lands entirely outside the US is a source error.
      Measured before adopting this rule (2026-07-23): 2,180 cached NWS
      alert rows + 13,710 cached WZDx rows, zero rejects — the CONUS box is
      generous enough to hold the marine zones that worried us. The residual
      case is an alert lying WHOLLY offshore beyond the boxes (e.g. a Gulf
      zone south of 24.4 N); that would be rejected, not dropped silently —
      it lands in quality.rejects with reason 'coords_not_in_us', which is
      the signal to widen US_BBOXES rather than to loosen the gate.
    * Swap detection stays all-or-nothing: only when EVERY vertex would fall
      in a US box with the axes swapped is 'latlon_swapped' the right story.
      Still never auto-fixed — a wrong silent swap puts a closure in the
      wrong state.
    """
    for lon, lat in coords:
        if (lat == 0.0 and lon == 0.0) or abs(lat) > 90.0 or abs(lon) > 180.0:
            return "coords_out_of_range"
    if any(_in_any_us_box(lon, lat) for lon, lat in coords):
        return None
    if all(_in_any_us_box(lat, lon) for lon, lat in coords):
        return "latlon_swapped"
    return "coords_not_in_us"


def gate2_coords(rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Gate 2 — coordinate sanity on rows with 'lat'/'lon' and/or 'geom_wkt'.

    Checks, in order:
    1. junk: null island (0,0), |lat| > 90, |lon| > 180
       -> reject reason 'coords_out_of_range'
    2. in-US: (lon, lat) inside any US_BBOXES box -> pass
    3. swap detection: if (lon, lat) fails but (lat, lon) WOULD pass, the feed
       swapped its axes -> reject reason 'latlon_swapped'. NEVER auto-fix —
       a silent swap 'fix' that is wrong puts a bridge in the wrong state.
    4. anything else -> reject reason 'coords_not_in_us'

    Both coordinate carriers are checked. loaders.py accepts either a
    'lat'/'lon' pair or a 'geom_wkt' string, so validating only the former
    left every LineString/Polygon row (wzdx work zones, nws alert polygons,
    any future geometry feed) to reach core unvalidated — the bypass that let
    a parser bug publish null-island geometry in July 2026. A row carrying
    both is checked on both; the point verdict is reported first.
    Unparseable WKT -> reject reason 'geom_unparseable' (never guessed at).
    Rows with neither carrier (fuel prices, zone-only alerts) pass through.

    Returns (ok_rows, rejects) shaped like gate1_schema.
    """
    ok_rows: list[dict] = []
    rejects: list[dict] = []
    for row in rows:
        # A geometry string present in the row is the authoritative carrier:
        # geometry feeds (wzdx, nws) often carry no point at all, and where a
        # row has both, the point is a derived centroid of the same shape.
        # `geom_wkt: None` means 'this feed has the column, this row has no
        # geometry' — loaders store NULL, so there is nothing to judge.
        geom_raw = row.get("geom_wkt")
        reason: str | None = None

        if geom_raw is not None:
            coords = wkt_coords(geom_raw)
            reason = ("geom_unparseable" if coords is None
                      else _judge_coords(coords))
        elif "lat" in row and "lon" in row:
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except (TypeError, ValueError):
                reason = "coords_out_of_range"
            else:
                reason = _judge_coords([(lon, lat)])
        # else: no coordinate carrier (fuel prices, zone-only alerts) -> pass.

        if reason is None:
            ok_rows.append(row)
        else:
            rejects.append(_reject(reason, row))
    return ok_rows, rejects
