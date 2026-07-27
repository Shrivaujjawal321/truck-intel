"""Parser: NTAD "National Network" (ArcGIS GeoJSON pages, concatenated) ->
truck-DESIGNATED route rows for core.truck_routes.

The fetcher (_fetch_arcgis) always queries where=1=1 and concatenates every
resultOffset page into one GeoJSON FeatureCollection. This parser therefore
receives ALL 478,999 polylines and does the truck-network filter itself:

    KEEP only NN > 0 (454,830 rows — the National Network, truck-legal under
    23 CFR 658 / STAA 1982). DROP NN = 0 (24,169 rows carried in the same
    layer but NOT truck-designated). Publishing an NN=0 row as a truck route
    would be a lie — the whole point of this source is legal truck access.

Geometry: the layer is esriGeometryPolyline; f=geojson yields LineString OR
MultiLineString. Both are normalized to MULTILINESTRING WKT so the column type
core.truck_routes.geom is a single consistent geometry(MultiLineString,4326).

Keys — route_id and route_name/route_ref:
  route_id  = the source integer `ID`, verified unique (478,999 distinct).
              NOT `ROUTEID`: that is a STATE-SCOPED string ('1','H3','93'
              repeat across states) and cannot be a primary key.
  route_ref = the signed reference, SIGNT1 + ' ' + SIGNN1 (e.g. 'I 95').
  route_name= LNAME when the source gives one; LNAME is blank (' ') on ~411k
              rows, so it falls back to route_ref rather than publishing ''.

observed_at = 2018 (every row's own YEAR field), never the download date. The
service description claims a 2020-12-22 vintage but every row carries YEAR=2018;
the record's own field wins (repo precedent: nbi/nti derive vintage from the
record). The other candidate dates are preserved in props.
"""
from __future__ import annotations

import json
from typing import Iterator

from truckintel.parsers.nbi import FIPS_TO_USPS


def _int(value) -> int | None:
    """Coded integer -> int; missing / blank / junk -> None (never fabricated)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value) -> str | None:
    """Trimmed non-empty string, else None. ArcGIS pads blanks (' ')."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _multilinestring_wkt(geom: dict) -> str | None:
    """GeoJSON LineString/MultiLineString -> MULTILINESTRING WKT (EPSG:4326).

    Returns None for anything else, empty, or degenerate (a single point is not
    a line). Never fabricates geometry — an unusable shape becomes geom_wkt=None
    so gate 1 (required_fields) rejects the row honestly rather than publishing
    a null-island line.
    """
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    if not coords:
        return None

    if gtype == "LineString":
        lines = [coords]
    elif gtype == "MultiLineString":
        lines = coords
    else:
        return None

    parts: list[str] = []
    for line in lines:
        pts = [
            f"{float(pt[0])} {float(pt[1])}"
            for pt in line
            if pt and len(pt) >= 2 and pt[0] is not None and pt[1] is not None
        ]
        if len(pts) >= 2:  # a line needs at least two distinct vertices
            parts.append("(" + ", ".join(pts) + ")")
    if not parts:
        return None
    return "MULTILINESTRING(" + ", ".join(parts) + ")"


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per truck-designated (NN>0) route from the merged
    GeoJSON FeatureCollection.

    Keys of each yielded dict:
        route_id      int      source `ID` (unique PK); None -> gate 1 rejects
        route_name    str|None  LNAME, else the signed ref (never '')
        route_ref     str|None  SIGNT1 + ' ' + SIGNN1  (e.g. 'I 95')
        sign_type     str|None  SIGNT1
        sign_num      str|None  SIGNN1
        routeid_state str|None  ROUTEID (state-scoped; reference only)
        nn            int       National Network flag (>0)
        state_fips    int|None  STFIPS
        state         str|None  2-letter USPS (mapped from STFIPS)
        county_fips   int|None  full 5-digit FIPS = STFIPS*1000 + CTFIPS
        fclass        int|None  functional class
        aadt          int|None  annual average daily traffic
        aadt_com      int|None  commercial (truck) AADT
        through_lanes int|None  THROUGH_LA
        geom_wkt      str|None  MULTILINESTRING WKT, EPSG:4326; None -> rejected
        observed_at   str|None  '2018-01-01' from the row YEAR (fact vintage)
        props         dict      full attribute record
    """
    fc = json.loads(raw)
    for feature in fc.get("features", []):
        props = dict(feature.get("properties") or {})

        nn = _int(props.get("NN"))
        if nn is None or nn <= 0:
            continue  # NN=0 (or uncoded) is NOT on the National Network — drop it

        sign_type = _str(props.get("SIGNT1"))
        sign_num = _str(props.get("SIGNN1"))
        route_ref = " ".join(p for p in (sign_type, sign_num) if p) or None
        route_name = _str(props.get("LNAME")) or route_ref

        state_fips = _int(props.get("STFIPS"))
        county_raw = _int(props.get("CTFIPS"))
        county_fips = (
            state_fips * 1000 + county_raw
            if state_fips is not None and county_raw is not None
            else None
        )

        year = _int(props.get("YEAR"))
        observed_at = f"{year}-01-01" if year else None

        yield {
            "route_id": _int(props.get("ID")),
            "route_name": route_name,
            "route_ref": route_ref,
            "sign_type": sign_type,
            "sign_num": sign_num,
            "routeid_state": _str(props.get("ROUTEID")),
            "nn": nn,
            "state_fips": state_fips,
            "state": FIPS_TO_USPS.get(f"{state_fips:02d}") if state_fips is not None else None,
            "county_fips": county_fips,
            "fclass": _int(props.get("FCLASS")),
            "aadt": _int(props.get("AADT")),
            "aadt_com": _int(props.get("AADT_COM")),
            "through_lanes": _int(props.get("THROUGH_LA")),
            "geom_wkt": _multilinestring_wkt(feature.get("geometry")),
            "observed_at": observed_at,
            "props": props,
        }
