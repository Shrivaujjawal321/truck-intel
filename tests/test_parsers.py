"""Parser unit tests — pure fixtures, no network, no DB.

Fixtures are synthetic but format-faithful: NBI header + quoting checked
against the real 2025AllRecordsDelimitedAllStates.txt; NTAD/NWS/EIA fixtures
mirror live samples fetched 2026-07-22.

Run: uv run pytest
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from truckintel.parsers import eia, nbi, ntad_parking, nws

FIXTURES = Path(__file__).parent / "fixtures"

# Real header of the 2025 national delimited file (verified on the wire).
NBI_HEADER = (
    "STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A,ROUTE_PREFIX_005B,"
    "SERVICE_LEVEL_005C,ROUTE_NUMBER_005D,DIRECTION_005E,HIGHWAY_DISTRICT_002,"
    "COUNTY_CODE_003,PLACE_CODE_004,FEATURES_DESC_006A,CRITICAL_FACILITY_006B,"
    "FACILITY_CARRIED_007,LOCATION_009,MIN_VERT_CLR_010,KILOPOINT_011,"
    "BASE_HWY_NETWORK_012,LRS_INV_ROUTE_013A,SUBROUTE_NO_013B,LAT_016,LONG_017,"
    "DETOUR_KILOS_019,TOLL_020,MAINTENANCE_021,OWNER_022,FUNCTIONAL_CLASS_026,"
    "YEAR_BUILT_027,OPEN_CLOSED_POSTED_041,VERT_CLR_OVER_MT_053,"
    "VERT_CLR_UND_REF_054A,VERT_CLR_UND_054B,OPR_RATING_METH_063,"
    "OPERATING_RATING_064,INV_RATING_METH_065,INVENTORY_RATING_066"
).split(",")


def _nbi_zip(rows: list[dict], member: str = "2025AllRecordsDelimitedAllStates.txt") -> bytes:
    """Build a mini in-memory NBI delimited ZIP with the real header."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=NBI_HEADER, quotechar="'", restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr(member, buf.getvalue())
    return zbuf.getvalue()


NBI_ROW_DE = {
    "STATE_CODE_001": "10",
    "STRUCTURE_NUMBER_008": "1450 052       ",
    "FEATURES_DESC_006A": "BRANDYWINE CREEK",
    "FACILITY_CARRIED_007": "MAIN ST",
    "MIN_VERT_CLR_010": "99.99",       # sentinel: no restriction on route
    "LAT_016": "39120500",             # 39d 12m 05.00s
    "LONG_017": "075301200",           # 075d 30m 12.00s (unsigned degrees WEST)
    "OPEN_CLOSED_POSTED_041": "A",
    "VERT_CLR_OVER_MT_053": "4.11",    # meters
    "VERT_CLR_UND_REF_054A": "H",
    "VERT_CLR_UND_054B": "4.27",       # meters
    "OPERATING_RATING_064": "44.5",
    "INVENTORY_RATING_066": "32.7",
}

NBI_ROW_PR = {
    "STATE_CODE_001": "72",
    "STRUCTURE_NUMBER_008": "PR0001",
    "FEATURES_DESC_006A": "I-95, RAMP A",   # embedded comma: exercises the quote char
    "FACILITY_CARRIED_007": "PR-52",
    "MIN_VERT_CLR_010": "99.99",
    "LAT_016": "18151030",
    "LONG_017": "066070200",
    "OPEN_CLOSED_POSTED_041": "P",
    "VERT_CLR_OVER_MT_053": "99.99",
    "VERT_CLR_UND_REF_054A": "N",
    "VERT_CLR_UND_054B": "0",          # 0 with ref N = not coded
    "OPERATING_RATING_064": "18.1",
    "INVENTORY_RATING_066": "12.7",
}

NBI_ROW_BAD_COORDS = {
    "STATE_CODE_001": "01",
    "STRUCTURE_NUMBER_008": "BAD001",
    "LAT_016": "99999999",             # minutes/seconds out of range
    "LONG_017": "000000000",           # all zeros = not recorded
    "MIN_VERT_CLR_010": "99.99",
    "VERT_CLR_OVER_MT_053": "99.99",
    "VERT_CLR_UND_054B": "0",
}


# ------------------------------------------------------------------ NBI units

def test_dms_lat_hand_computed():
    # 39d 12m 05.00s = 39 + 12/60 + 5/3600
    assert nbi._dms_to_decimal("39120500", deg_digits=2) == pytest.approx(
        39 + 12 / 60 + 5 / 3600, abs=1e-9
    )


def test_dms_lon_hand_computed():
    # 075d 30m 12.00s = 75.503333...; sign applied by parse(), not here
    assert nbi._dms_to_decimal("075301200", deg_digits=3) == pytest.approx(
        75 + 30 / 60 + 12 / 3600, abs=1e-9
    )


def test_dms_fractional_seconds():
    # 31d 06m 10.94s (real Alabama row from the 2025 file)
    assert nbi._dms_to_decimal("31061094", deg_digits=2) == pytest.approx(
        31 + 6 / 60 + 10.94 / 3600, abs=1e-9
    )


@pytest.mark.parametrize(
    "text",
    ["99999999", "00000000", "", None, "1234", "39B20500", "39610500"],
)  # bad minutes / zeros / short / junk / minutes=61
def test_dms_invalid_is_none(text):
    assert nbi._dms_to_decimal(text, deg_digits=2) is None


def test_meters_to_inches():
    assert nbi._meters_to_inches("4.11") == pytest.approx(161.8)  # 4.11 m = 161.81 in
    assert nbi._meters_to_inches("4.27") == pytest.approx(168.1)


def test_clearance_sentinel_is_none():
    assert nbi._meters_to_inches("99.99") is None
    assert nbi._meters_to_inches("0") is None
    assert nbi._meters_to_inches("") is None
    assert nbi._meters_to_inches("N") is None


def test_fips_mapping():
    assert nbi.FIPS_TO_USPS["10"] == "DE"
    assert nbi.FIPS_TO_USPS["11"] == "DC"
    assert nbi.FIPS_TO_USPS["72"] == "PR"
    assert len(nbi.FIPS_TO_USPS) == 52  # 50 states + DC + PR


# ------------------------------------------------------------ NBI end-to-end

def test_nbi_parse_end_to_end():
    rows = list(nbi.parse(_nbi_zip([NBI_ROW_DE, NBI_ROW_PR, NBI_ROW_BAD_COORDS])))
    assert len(rows) == 3
    de, pr, bad = rows

    assert de["nbi_id"] == "101450 052"          # FIPS + stripped structure number
    assert de["state"] == "DE"
    assert de["lat"] == pytest.approx(39.201389, abs=1e-6)
    assert de["lon"] == pytest.approx(-75.503333, abs=1e-6)  # west = negative
    # min(4.11, 4.27) m -> inches; item 10 sentinel excluded
    assert de["min_vert_clearance_in"] == pytest.approx(161.8)
    assert de["posting_status"] == "A"
    assert de["operating_rating"] == "44.5"
    assert de["inventory_rating"] == "32.7"
    assert de["observed_at"] == "2025-01-01"      # file vintage, not today
    assert de["name"] == "MAIN ST / BRANDYWINE CREEK"
    assert de["props"]["YEAR_BUILT_027"] is None  # cleaned: empty -> None

    assert pr["state"] == "PR"
    assert pr["min_vert_clearance_in"] is None    # all clearances sentinel/0
    assert pr["props"]["FEATURES_DESC_006A"] == "I-95, RAMP A"  # quote char honored

    assert bad["lat"] is None and bad["lon"] is None  # never fabricated


def test_nbi_vintage_from_member_name():
    raw = _nbi_zip([NBI_ROW_DE], member="NBI2031.txt")
    assert next(nbi.parse(raw))["observed_at"] == "2031-01-01"


NBI_ROW_MERGE_UNDER = {
    "STATE_CODE_001": "01",
    "STRUCTURE_NUMBER_008": "MERGE01",
    "RECORD_TYPE_005A": "2",           # route UNDER the structure
    "FEATURES_DESC_006A": "US-31",
    "FACILITY_CARRIED_007": "SPRING CREEK RD",
    "MIN_VERT_CLR_010": "4.20",        # the low under-route clearance
    "LAT_016": "33301500",
    "LONG_017": "086451000",
    "VERT_CLR_OVER_MT_053": "99.99",
    "VERT_CLR_UND_054B": "0",
}

NBI_ROW_MERGE_ON = {
    "STATE_CODE_001": "01",
    "STRUCTURE_NUMBER_008": "MERGE01",
    "RECORD_TYPE_005A": "1",           # route ON the structure -> base row
    "FEATURES_DESC_006A": "SPRING CREEK RD",
    "FACILITY_CARRIED_007": "US-31",
    "MIN_VERT_CLR_010": "99.99",
    "LAT_016": "33301500",
    "LONG_017": "086451000",
    "OPEN_CLOSED_POSTED_041": "A",
    "VERT_CLR_OVER_MT_053": "6.10",
    "VERT_CLR_UND_054B": "4.85",
}


def test_nbi_multi_record_structure_merged():
    # Under-record arrives FIRST and non-adjacent (the real 2025 file
    # interleaves structures); the ON-record must still become the base.
    raw = _nbi_zip([NBI_ROW_MERGE_UNDER, NBI_ROW_DE, NBI_ROW_MERGE_ON])
    rows = list(nbi.parse(raw))
    assert len(rows) == 2                          # 3 records -> 2 structures
    merged = next(r for r in rows if r["nbi_id"] == "01MERGE01")
    assert merged["name"] == "US-31 / SPRING CREEK RD"   # base = ON-record
    assert merged["posting_status"] == "A"
    # min across ALL records: under-route 4.20 m beats the base's 4.85/6.10.
    assert merged["min_vert_clearance_in"] == pytest.approx(165.4)
    assert merged["props"]["_record_types"] == ["1", "2"]


# ---------------------------------------------------------------------- NTAD

def test_ntad_parse():
    rows = list(ntad_parking.parse((FIXTURES / "ntad_parking.geojson").read_bytes()))
    assert len(rows) == 3
    grand_bay, jubitz, baldwin = rows

    assert grand_bay["site_id"] == "1"
    assert grand_bay["kind"] == "public_rest_area"
    assert grand_bay["name"] == "Grand Bay Welcome Center"
    assert grand_bay["state"] == "AL"
    assert grand_bay["lat"] == pytest.approx(30.477238, abs=1e-6)
    assert grand_bay["lon"] == pytest.approx(-88.393032, abs=1e-6)
    assert grand_bay["truck_spaces"] == 90
    assert grand_bay["observed_at"] == "2019-01-01"  # survey era, never download date

    assert jubitz["kind"] == "truck_stop"
    assert jubitz["truck_spaces"] is None            # null stays None, never 0

    assert baldwin["truck_spaces"] == 0              # 0 stays 0, never None


# ----------------------------------------------------------------------- NWS

def test_nws_parse():
    rows = list(nws.parse((FIXTURES / "nws_alerts.json").read_bytes()))
    assert len(rows) == 2
    storm, heat = rows

    assert storm["event_id"] == "urn:oid:2.49.0.1.840.0.aaa.001.1"  # CAP id
    assert storm["kind"] == "weather_alert"
    assert storm["geom_wkt"].startswith("POLYGON ((-104.6 41.1, ")
    assert storm["geom_wkt"].endswith("-104.6 41.1))")
    assert storm["observed_at"] == "2026-07-22T01:48:00-06:00"  # sent, not fetch
    assert storm["props"]["severity"] == "Severe"
    assert storm["props"]["headline"] == "Severe Thunderstorm Warning issued July 22"

    # zone-only alert: honest NULL geometry, never fabricated from zone refs
    assert heat["geom_wkt"] is None
    assert heat["props"]["areaDesc"].startswith("Natrona County")


def test_nws_multipolygon_wkt():
    geom = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
            [[[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 2.0]]],
        ],
    }
    wkt = nws._geom_wkt(geom)
    assert wkt == (
        "MULTIPOLYGON (((0.0 0.0, 1.0 0.0, 1.0 1.0, 0.0 0.0)), "
        "((2.0 2.0, 3.0 2.0, 3.0 3.0, 2.0 2.0)))"
    )


# ----------------------------------------------------------------------- EIA

def test_eia_parse_region_mapping():
    rows = list(eia.parse((FIXTURES / "eia_diesel.json").read_bytes()))
    assert [r["region"] for r in rows] == ["US", "CA", "PADD1A"]  # null-value row skipped

    us = rows[0]
    assert us["product"] == "diesel"
    assert us["week_of"] == "2026-07-13"
    assert us["observed_at"] == "2026-07-13"      # the survey week, not fetch date
    assert us["price_usd_gal"] == pytest.approx(3.681)
    assert us["props"]["series"] == "EMD_EPD2D_PTE_NUS_DPG"

    assert rows[1]["price_usd_gal"] == pytest.approx(5.102)  # string value -> float


def test_eia_duoarea_map_covers_diesel_universe():
    for code, label in [("R10", "PADD1"), ("R1Y", "PADD1B"), ("R1Z", "PADD1C"),
                        ("R20", "PADD2"), ("R30", "PADD3"), ("R40", "PADD4"),
                        ("R50", "PADD5"), ("R5XCA", "PADD5_EX_CA")]:
        assert eia.DUOAREA_TO_REGION[code] == label
