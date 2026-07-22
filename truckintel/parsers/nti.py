"""Parser: NTAD National Tunnel Inventory (ArcGIS GeoJSON pages, concatenated)
-> tunnel rows.

The fetcher concatenates all resultOffset pages into one GeoJSON
FeatureCollection so the raw artifact is one file, not many fragments.

UNITS — verified live 2026-07-22 against known tunnels: SNTI items G1/G2 on
this service are US customary FEET, not meters. Holland Tunnel G2 = 12.5,
Lincoln = 13.0, Eisenhower = 14.6, Eisenhower length_g1 = 8,856 — all correct
in feet, impossible in meters (12.5 m would be a 492-inch clearance).
research/tunnels.md's NBI-style "coded meters" assumption is refuted by the
data; min_vert_clearance_in = feet * 12. There is no 99.99-style sentinel in
the live layer (range 6..32.5 plus one implausible 135); values <= 0 or
unparseable become honest None. Implausible-but-coded values (the 135 ft
Baltimore Harbor row) pass through UNFIXED — we never silently correct a
source value; the quality track flags them.

FIELD NAMES — ArcGIS truncates GeoJSON property names to 31 chars (the layer
alias keeps the full SNTI name): min_vert_clearance_over_tunnel_roadway_g2
arrives as 'min_vert_clearance_over_tunnel_'. _get() accepts both spellings so
an untruncated future republish keeps parsing.
"""
from __future__ import annotations

import json
from typing import Iterator

from truckintel.parsers.nbi import FIPS_TO_USPS

_FT_TO_IN = 12.0

# SNTI restriction items (coded 0 = no, 1 = yes). Kept as coded flags — the
# honest limit of the federal source; detailed class/quantity rules live in
# data/curated/tunnel_rules.yaml (research/tunnels.md §2).
_RESTRICTION_ITEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("L10", ("height_restriction_l10",)),
    ("L11", ("hazardous_material_restriction_",
             "hazardous_material_restriction_l11")),
    ("L12", ("other_restrictions_l12",)),
)


def _get(props: dict, *names: str):
    """First present key wins — truncated ArcGIS name, then full SNTI alias."""
    for name in names:
        if name in props:
            return props[name]
    return None


def _coded_flag(value) -> int | None:
    """SNTI 0/1 coded item -> int, tolerant of string coding; else None."""
    if isinstance(value, bool):  # bools are ints; reject explicitly
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return int(value.strip())
    return None


def _feet_to_inches(value) -> float | None:
    """Coded clearance feet -> inches; <= 0 / missing / junk -> honest None."""
    try:
        feet = float(value)
    except (TypeError, ValueError):
        return None
    if feet <= 0:
        return None
    return round(feet * _FT_TO_IN, 1)


def _feet(value) -> float | None:
    try:
        feet = float(value)
    except (TypeError, ValueError):
        return None
    return feet if feet > 0 else None


def _hazmat_codes(props: dict) -> list[str] | None:
    """['L10=1', 'L11=0', ...] for the coded SNTI restriction items;
    None when nothing is coded (unknown, never 'no restrictions')."""
    codes = []
    for item, names in _RESTRICTION_ITEMS:
        flag = _coded_flag(_get(props, *names))
        if flag is not None:
            codes.append(f"{item}={flag}")
    return codes or None


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per tunnel from the merged GeoJSON FeatureCollection.

    Keys of each yielded dict:
        tunnel_id             str      state FIPS + NTI tunnel number
                                       (state_code_i3 + tunnel_number_i1,
                                       composed like nbi_id; verified unique
                                       across the 580 live records)
        name                  str|None NTI tunnel_name_i2
        state                 str|None 2-letter USPS code (mapped from FIPS)
        lat, lon              float    portal point (geometry, falling back to
                                       portal_latitude_i13/portal_longitude_i14)
        length_ft             float|None  tunnel_length_g1 (already feet)
        min_vert_clearance_in float|None  SNTI G2 feet -> INCHES; None = unknown
        hazmat_restricted     bool|None   L11 coded 1 -> True, 0 -> False,
                                          uncoded -> None (tri-state, never
                                          defaulted to "no")
        hazmat_codes          list|None   coded SNTI flags ['L10=1','L11=1',...];
                                          None = nothing coded (unknown)
        observed_at           str|None ISO date — the record's NTI `year`
                                       (inventory vintage), never the download date
        props                 dict     full attribute record
    """
    fc = json.loads(raw)
    for feature in fc.get("features", []):
        props = dict(feature.get("properties") or {})
        geom = feature.get("geometry") or {}

        if geom.get("type") == "Point" and geom.get("coordinates"):
            lon, lat = geom["coordinates"][:2]
        else:  # attribute portal coords as fallback (same values on the live layer)
            lat = _get(props, "portal_latitude_i13")
            lon = _get(props, "portal_longitude_i14")

        fips = str(props.get("state_code_i3") or "").strip()
        number = str(props.get("tunnel_number_i1") or "").strip()
        year = props.get("year")
        try:
            observed_at = f"{int(year)}-01-01" if year else None
        except (TypeError, ValueError):
            observed_at = None

        yield {
            # Both key components or no key at all: a record missing its FIPS
            # or tunnel number must NOT publish a bare-FIPS / empty-string PK
            # — tunnel_id=None lets gate 1 (gates.required_fields in the
            # registry YAML) reject it with an honest reason.
            "tunnel_id": f"{fips}{number}" if fips and number else None,
            "name": str(props.get("tunnel_name_i2") or "").strip() or None,
            "state": FIPS_TO_USPS.get(fips),
            "lat": lat,
            "lon": lon,
            "length_ft": _feet(_get(props, "tunnel_length_g1")),
            "min_vert_clearance_in": _feet_to_inches(
                _get(props, "min_vert_clearance_over_tunnel_",
                     "min_vert_clearance_over_tunnel_roadway_g2")
            ),
            "hazmat_restricted": (
                bool(flag) if (flag := _coded_flag(
                    _get(props, "hazardous_material_restriction_",
                         "hazardous_material_restriction_l11"))) is not None
                else None
            ),
            "hazmat_codes": _hazmat_codes(props),
            "observed_at": observed_at,
            "props": props,
        }
