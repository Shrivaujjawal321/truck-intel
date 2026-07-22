"""Parser: FHWA NBI annual bulk ZIP (comma-delimited flavor) -> bridge rows.

Units (NBI Coding Guide): clearances are METERS, ratings METRIC TONS — this
parser converts clearance to inches; rating codes pass through unconverted
(they are codes, not signed values, and are labeled as such downstream).
Coordinates (items 16/17) are packed degrees-minutes-seconds — must be parsed
and converted to decimal degrees.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Iterator

# NBI Coding Guide: 99.99 m = "no restriction / not applicable" for clearance
# items; 0 = not coded (e.g. item 54B when 54A = 'N', no feature under).
_CLEARANCE_SENTINEL = 99.99
_CLEARANCE_COLS = ("MIN_VERT_CLR_010", "VERT_CLR_OVER_MT_053", "VERT_CLR_UND_054B")
_M_TO_IN = 39.37007874015748  # 1 m / 0.0254

# State FIPS -> USPS, all 50 + DC + PR (NBI's universe).
FIPS_TO_USPS: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}


def _dms_to_decimal(text: str | None, deg_digits: int) -> float | None:
    """Packed DMS (item 16/17) -> decimal degrees.

    Format per Coding Guide: lat = 8 digits DDMMSSSS, lon = 9 digits DDDMMSSSS,
    last four digits = SS.SS (hundredths of seconds implied; a literal '.' is
    tolerated). All-zeros = not recorded -> None. Sign is NOT applied here.
    """
    if not text:
        return None
    digits = text.strip().replace(".", "")
    if not digits.isdigit() or len(digits) != deg_digits + 6:
        return None
    if int(digits) == 0:
        return None
    deg = int(digits[:deg_digits])
    minutes = int(digits[deg_digits:deg_digits + 2])
    seconds = int(digits[deg_digits + 2:]) / 100.0
    if minutes >= 60 or seconds >= 60 or deg > (90 if deg_digits == 2 else 180):
        return None
    return deg + minutes / 60.0 + seconds / 3600.0


def _meters_to_inches(value: str | None) -> float | None:
    """Coded clearance meters -> inches; sentinel 99.99 / 0 / junk -> None."""
    if not value:
        return None
    try:
        meters = float(value)
    except ValueError:
        return None
    if meters <= 0 or meters == _CLEARANCE_SENTINEL:
        return None
    return round(meters * _M_TO_IN, 1)


def _vintage_year(zf: zipfile.ZipFile, member: zipfile.ZipInfo) -> int:
    """Data vintage = the 4-digit year in the member/archive name (e.g.
    '2025AllRecordsDelimitedAllStates.txt'); member mtime as last resort."""
    for name in (member.filename, *(n for n in zf.namelist() if n != member.filename)):
        m = re.search(r"(19|20)\d{2}", name)
        if m:
            return int(m.group(0))
    return member.date_time[0]


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per bridge from the national delimited ZIP.

    Multi-record structures are merged: the national file carries one record
    per inventory route intersecting a structure — item 5A '1' = route ON the
    structure, '2'/'A'..'Z' = routes UNDER it (2025 file: 743,398 records for
    631,301 structures; duplicates are NOT adjacent, so the merge is keyed).
    The ON-record is the base row when present (a rail bridge over a highway
    has only under-records — kept: those are exactly the low-clearance
    hazards); min_vert_clearance_in is the min across ALL of the structure's
    records, because under-route clearances live on the under-records.
    Holds one dict per structure in memory (~631k) — same order as the row
    list the engine materializes for the gates; streaming is a Phase-2 change.

    Keys of each yielded dict:
        nbi_id                  str    state FIPS + structure number (natural key)
        name                    str|None  facility carried / feature crossed
        state                   str    2-letter USPS code (mapped from FIPS)
        lat, lon                float  decimal degrees (converted from DMS items 16/17)
        min_vert_clearance_in   float|None  inches, min of items 10/53/54 across
                                            the structure's records where coded;
                                            None when not applicable/unknown
        operating_rating        str|None  item 64 code (metric tons)
        inventory_rating        str|None  item 66 code
        posting_status          str|None  item 41 code (open/posted/closed)
        observed_at             str    ISO date — the file's data vintage
                                       (submission year), never the download date
        props                   dict   full cleaned record of the base record,
                                       plus _record_types (all item-5A codes seen)
    """
    zf = zipfile.ZipFile(io.BytesIO(raw))
    members = [i for i in zf.infolist()
               if i.filename.lower().endswith((".txt", ".csv")) and not i.is_dir()]
    if not members:
        raise ValueError("no delimited .txt/.csv member in NBI ZIP")
    member = max(members, key=lambda i: i.file_size)
    observed_at = f"{_vintage_year(zf, member)}-01-01"

    bridges: dict[str, dict] = {}

    # Comma-delimited with single-quote text qualifier (research/bridges.md §1).
    text = io.TextIOWrapper(zf.open(member), encoding="latin-1", newline="")
    for row in csv.DictReader(text, quotechar="'"):
        props = {k: (v.strip() or None) for k, v in row.items() if k is not None}

        fips = (props.get("STATE_CODE_001") or "").strip()
        if len(fips) == 3:       # older vintages pack FIPS + FHWA region digit
            fips = fips[:2]
        structure = props.get("STRUCTURE_NUMBER_008") or ""

        lat = _dms_to_decimal(props.get("LAT_016"), deg_digits=2)
        lon = _dms_to_decimal(props.get("LONG_017"), deg_digits=3)

        clearances = [inches for col in _CLEARANCE_COLS
                      if (inches := _meters_to_inches(props.get(col))) is not None]

        name_parts = [p for p in (props.get("FACILITY_CARRIED_007"),
                                  props.get("FEATURES_DESC_006A")) if p]

        record_type = props.get("RECORD_TYPE_005A") or "?"
        props["_record_types"] = [record_type]
        rec = {
            "nbi_id": f"{fips}{structure.strip()}",
            "name": " / ".join(name_parts) or None,
            "state": FIPS_TO_USPS.get(fips),
            "lat": lat,
            "lon": -lon if lon is not None else None,  # NBI lon is unsigned degrees WEST
            "min_vert_clearance_in": min(clearances) if clearances else None,
            "operating_rating": props.get("OPERATING_RATING_064"),
            "inventory_rating": props.get("INVENTORY_RATING_066"),
            "posting_status": props.get("OPEN_CLOSED_POSTED_041"),
            "observed_at": observed_at,
            "props": props,
        }

        prior = bridges.get(rec["nbi_id"])
        if prior is None:
            bridges[rec["nbi_id"]] = rec
            continue
        # Base row: the ON-record wins; among equals, first record wins.
        base, extra = (rec, prior) if (
            record_type == "1" and prior["props"]["_record_types"][0] != "1"
        ) else (prior, rec)
        coded = [c for c in (base["min_vert_clearance_in"],
                             extra["min_vert_clearance_in"]) if c is not None]
        base["min_vert_clearance_in"] = min(coded) if coded else None
        base["props"]["_record_types"] = sorted(
            {*base["props"]["_record_types"], *extra["props"]["_record_types"]})
        bridges[rec["nbi_id"]] = base

    yield from bridges.values()
