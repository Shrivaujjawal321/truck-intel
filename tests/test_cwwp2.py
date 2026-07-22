"""Caltrans CWWP2 chain-control parser tests — pure fixtures, no network, no DB.

cwwp2_cc_d03.json is the live District-3 chain-control feed sampled on the wire
2026-07-23 (230 records). Synthetic cases below exercise the honesty rules:
envelope validation, out-of-service skip, R-0 vs active, missing geometry, and
the upstream-drift guard.

Run: uv run pytest tests/test_cwwp2.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from truckintel.parsers import cwwp2
from truckintel.registry import load_registry

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _wrap(records: list[dict]) -> bytes:
    return json.dumps({"data": [{"cc": r} for r in records]}).encode()


_INSERVICE_R0 = {
    "index": "3-ALP-89-23.6-N-237",
    "recordTimestamp": {"recordDate": "2026-07-22", "recordTime": "11:33:03"},
    "location": {
        "district": "3", "locationName": "Luther Pass", "nearbyPlace": "Markleeville",
        "longitude": "-119.939680", "latitude": "38.787944", "elevation": "7732",
        "direction": "North", "county": "Alpine", "route": "SR-89", "postmile": "23.60",
        "milepost": "31.03",
    },
    "inService": "true",
    "statusData": {
        "statusTimestamp": {"statusDate": "2026-05-05", "statusTime": "05:58:27"},
        "status": "R-0",
        "statusDescription": "No chain controls are in effect at this time.",
    },
}


# ------------------------------------------------------------ live fixture

def test_live_fixture_parses():
    rows = list(cwwp2.parse((FIXTURES / "cwwp2_cc_d03.json").read_bytes()))
    assert rows, "expected in-service chain-control records"
    # every emitted row obeys the 5-key contract
    for r in rows:
        assert set(r) == {"event_id", "kind", "geom_wkt", "observed_at", "props"}
        assert r["kind"] == "chain_control"
        assert r["event_id"].startswith("cwwp2-cc/")
        assert r["props"]["state"] == "CA"
        assert r["props"]["in_service"] is True
        assert r["props"]["active"] in (True, False, None)   # tri-state


# ------------------------------------------------------------ happy path

def test_r0_is_emitted_as_fact_not_absence():
    (row,) = list(cwwp2.parse(_wrap([_INSERVICE_R0])))
    assert row["event_id"] == "cwwp2-cc/3-ALP-89-23.6-N-237"
    assert row["geom_wkt"] == "POINT (-119.93968 38.787944)"
    # R-0 = "no controls" is a real timestamped fact, active=False (not dropped)
    assert row["props"]["active"] is False
    assert row["props"]["status"] == "R-0"
    assert row["props"]["route"] == "SR-89"
    assert row["props"]["county"] == "Alpine"
    # observed_at = statusTimestamp (when the status became effective), Pacific
    assert row["observed_at"].startswith("2026-05-05T05:58:27")
    assert "-07:00" in row["observed_at"] or "-08:00" in row["observed_at"]


def test_active_control_flagged():
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["statusData"]["status"] = "R-2"
    rec["statusData"]["statusDescription"] = "Chains required, 4WD exempt."
    (row,) = list(cwwp2.parse(_wrap([rec])))
    assert row["props"]["active"] is True
    assert row["props"]["status"] == "R-2"


def test_out_of_service_skipped():
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["inService"] = "false"
    # out-of-service = unknown status; must NOT be emitted as R-0/no-controls
    assert list(cwwp2.parse(_wrap([rec]))) == []


def test_missing_geometry_is_none_not_fabricated():
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["location"].pop("longitude")
    (row,) = list(cwwp2.parse(_wrap([rec])))
    assert row["geom_wkt"] is None


def test_junk_coordinate_rejected():
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["location"]["longitude"] = "not-a-number"
    (row,) = list(cwwp2.parse(_wrap([rec])))
    assert row["geom_wkt"] is None


def test_null_island_dropped_not_invented():
    # a no-fix sensor default (0,0) must NOT publish as a real location
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["location"]["longitude"] = "0"
    rec["location"]["latitude"] = "0"
    (row,) = list(cwwp2.parse(_wrap([rec])))
    assert row["geom_wkt"] is None


def test_out_of_us_coordinate_dropped():
    # an out-of-US (in-range) coordinate is geocoding junk for CA data -> None
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["location"]["longitude"] = "10.0"   # off Africa, |lon|<180, |lat|<90
    rec["location"]["latitude"] = "48.0"
    (row,) = list(cwwp2.parse(_wrap([rec])))
    assert row["geom_wkt"] is None


def test_missing_status_is_unknown_not_false():
    # tri-state: an in-service record with no status -> active None (unknown),
    # never a fabricated 'no' for a safety-relevant fact
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["statusData"]["status"] = ""
    (row,) = list(cwwp2.parse(_wrap([rec])))
    assert row["props"]["active"] is None


# ------------------------------------------------------------ envelope guards

def test_non_dict_envelope_raises():
    with pytest.raises(ValueError, match="no 'data' key"):
        list(cwwp2.parse(b"[]"))


def test_data_not_a_list_raises():
    with pytest.raises(ValueError, match="not a list"):
        list(cwwp2.parse(json.dumps({"data": {"cc": {}}}).encode()))


def test_all_records_unrecognizable_raises():
    # items present but none carry a 'cc' wrapper -> upstream drift, refuse
    with pytest.raises(ValueError, match="upstream drift"):
        list(cwwp2.parse(json.dumps({"data": [{"lcs": {}}, {"lcs": {}}]}).encode()))


def test_empty_data_publishes_nothing_without_raising():
    # a genuine empty district (feed valid, zero records) is legitimate
    assert list(cwwp2.parse(json.dumps({"data": []}).encode())) == []


def test_all_out_of_service_is_legitimate_empty():
    # records exist and are recognizable, just none in service -> [] on purpose
    rec = json.loads(json.dumps(_INSERVICE_R0))
    rec["inService"] = "false"
    assert list(cwwp2.parse(_wrap([rec, rec]))) == []


# ------------------------------------------------------------ registry wiring

def test_cwwp2_registry_yamls_valid_and_wired():
    sources = {s["id"]: s for s in load_registry(REPO_ROOT / "registry")}
    for did in ("caltrans_cwwp2_cc_d02", "caltrans_cwwp2_cc_d03",
                "caltrans_cwwp2_cc_d09"):
        assert did in sources, f"{did} missing from registry"
        s = sources[did]
        assert s["parser"] == "cwwp2"
        assert s["kind"] == "live_json"
        assert s["load_pattern"] == "event_lifecycle"
        assert s["auth"] is None                      # keyless
        assert s["gates"]["required_fields"] == ["event_id", "kind"]


def test_cwwp2_yaml_urls_follow_district_pattern():
    reg = REPO_ROOT / "registry"
    for did, dnum in (("d02", "d2"), ("d03", "d3"), ("d09", "d9")):
        doc = yaml.safe_load((reg / f"caltrans_cwwp2_cc_{did}.yaml").read_text())
        assert doc["url"].endswith(f"/data/{dnum}/cc/ccStatus{did.upper()}.json")
