"""Track A — NTI tunnels: parser, curated-rules file, registry entry, API.

Fixture is synthetic but format-faithful: property names mirror the live BTS
NTAD FeatureServer GeoJSON (31-char-truncated ArcGIS names, verified
2026-07-22), values modeled on real records (Holland 12.5 ft, Eisenhower
14.6 ft — the layer codes FEET, not meters).

API tests mount the router on a standalone app because api/main.py
registration belongs to the integrator, not this track.

Run: uv run pytest
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
from pathlib import Path

import psycopg
import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api import common, routes_tunnels
from truckintel.parsers import nti
from truckintel.parsers.nbi import FIPS_TO_USPS
from truckintel.registry import load_registry

from tests.conftest import needs_db

FIXTURES = Path(__file__).parent / "fixtures"
RULES_PATH = Path(__file__).parent.parent / "data" / "curated" / "tunnel_rules.yaml"

BBOX_OK = "-75.5,39.5,-73.5,41.5"  # 2x2 deg around NYC (Holland/Lincoln country)
BBOX_HUGE = "-80,35,-70,45"


# ------------------------------------------------------------------ units

def test_feet_to_inches():
    assert nti._feet_to_inches(12.5) == pytest.approx(150.0)   # Holland Tunnel
    assert nti._feet_to_inches(14.6) == pytest.approx(175.2)   # Eisenhower
    assert nti._feet_to_inches("13") == pytest.approx(156.0)


def test_clearance_missing_or_junk_is_none():
    assert nti._feet_to_inches(None) is None
    assert nti._feet_to_inches(0) is None
    assert nti._feet_to_inches(-1) is None
    assert nti._feet_to_inches("") is None
    assert nti._feet_to_inches("N") is None


def test_implausible_value_passes_through_unfixed():
    # BHT - Baltimore Harbor Tunnel codes 135 (ft) on the live layer — almost
    # certainly 13.5 mis-keyed. We NEVER silently fix a source value; the
    # quality track flags implausibles.
    assert nti._feet_to_inches(135) == pytest.approx(1620.0)


def test_coded_flag_tri_state():
    assert nti._coded_flag(1) == 1
    assert nti._coded_flag(0) == 0
    assert nti._coded_flag("1") == 1
    assert nti._coded_flag(None) is None
    assert nti._coded_flag(7) is None
    assert nti._coded_flag("N") is None
    assert nti._coded_flag(True) is None  # bools are not SNTI codes


# ------------------------------------------------------------ parser e2e

def _rows():
    return list(nti.parse((FIXTURES / "nti_tunnels.geojson").read_bytes()))


def test_nti_parse_end_to_end():
    rows = _rows()
    assert len(rows) == 5
    holland, allegheny, unknown_bore, eisenhower, bad = rows

    assert holland["tunnel_id"] == "343800B02"       # FIPS + NTI tunnel number
    assert holland["name"] == "Holland Tunnel"
    assert holland["state"] == "NJ"
    assert holland["lat"] == pytest.approx(40.728)
    assert holland["lon"] == pytest.approx(-74.021)
    assert holland["length_ft"] == pytest.approx(8556.0)
    assert holland["min_vert_clearance_in"] == pytest.approx(150.0)  # 12.5 ft
    assert holland["hazmat_restricted"] is True
    assert holland["hazmat_codes"] == ["L10=1", "L11=1", "L12=0"]
    assert holland["observed_at"] == "2025-01-01"    # inventory vintage, not today
    assert holland["props"]["facility_carried_i10"] == "I-78"

    # L11 coded 0 = the owner reported NO restriction — real False, not unknown
    assert allegheny["state"] == "PA"
    assert allegheny["hazmat_restricted"] is False
    assert allegheny["hazmat_codes"] == ["L10=0", "L11=0", "L12=0"]
    assert allegheny["min_vert_clearance_in"] == pytest.approx(195.6)  # 16.3 ft


def test_nti_uncoded_row_is_honest_none():
    unknown_bore = _rows()[2]
    assert unknown_bore["tunnel_id"] == "06TESTBORE01"
    assert unknown_bore["state"] == "CA"
    assert unknown_bore["min_vert_clearance_in"] is None  # unknown, never 0
    assert unknown_bore["hazmat_restricted"] is None      # unknown, never "no"
    assert unknown_bore["hazmat_codes"] is None
    assert unknown_bore["length_ft"] is None
    assert unknown_bore["observed_at"] is None            # no vintage -> no fake date


def test_nti_alias_names_and_attribute_coord_fallback():
    # Feature 4 uses the FULL SNTI alias names (untruncated) and has no
    # geometry — the parser must read both spellings and fall back to the
    # portal_latitude/longitude attributes.
    eisenhower = _rows()[3]
    assert eisenhower["tunnel_id"] == "08F-13-Y"
    assert eisenhower["state"] == "CO"
    assert eisenhower["lat"] == pytest.approx(39.6795)
    assert eisenhower["lon"] == pytest.approx(-105.921)
    assert eisenhower["min_vert_clearance_in"] == pytest.approx(175.2)  # 14.6 ft
    assert eisenhower["hazmat_restricted"] is True


def test_nti_bad_coords_pass_through_for_gate2():
    # (0,0) is gate-2's reject ('coords_out_of_range'), not the parser's —
    # the parser never drops or fixes rows.
    bad = _rows()[4]
    assert bad["lat"] == 0 and bad["lon"] == 0
    assert bad["tunnel_id"] == "01BADCOORD01"


def test_nti_tunnel_ids_unique_in_fixture():
    ids = [r["tunnel_id"] for r in _rows()]
    assert len(ids) == len(set(ids))  # PK contract for snapshot_swap COPY


def test_nti_missing_key_component_yields_none_tunnel_id():
    # A record missing its FIPS or tunnel number must NOT compose a bare-FIPS
    # or empty-string PK — tunnel_id=None so gate 1 (gates.required_fields in
    # registry/nti_tunnels.yaml) rejects it instead of publishing garbage.
    def _one(props):
        fc = {"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-74.0, 40.7]},
            "properties": props,
        }]}
        return next(iter(nti.parse(json.dumps(fc).encode())))

    assert _one({"state_code_i3": "34"})["tunnel_id"] is None       # no number
    assert _one({"tunnel_number_i1": "3800B02"})["tunnel_id"] is None  # no FIPS
    assert _one({})["tunnel_id"] is None                            # neither
    assert _one({"state_code_i3": "34", "tunnel_number_i1": "3800B02"})[
        "tunnel_id"] == "343800B02"


def test_registry_nti_required_fields_cover_the_natural_key():
    src = next(s for s in load_registry() if s["id"] == "nti_tunnels")
    assert "tunnel_id" in src["gates"]["required_fields"]


# ------------------------------------------------------------- registry

def test_registry_nti_tunnels_entry():
    src = next(s for s in load_registry() if s["id"] == "nti_tunnels")
    assert src["kind"] == "arcgis"
    assert src["load_pattern"] == "snapshot_swap"
    assert src["parser"] == "nti"          # also proves the module imports
    assert src["target"] == "core.tunnels"  # §5.1 allow-list member
    assert src["gates"]["min_rows"] == 350
    assert src["gates"]["max_row_delta_pct"] == 10
    assert src["gates"]["geometry_valid_pct"] == 98
    assert src["auth"] is None


# ------------------------------------------------- curated rules file schema

_REQUIRED_RULE_KEYS = {
    "authority", "match", "rule_summary", "source_url",
    "last_reviewed", "review_cadence_months",
}
_VALID_USPS = set(FIPS_TO_USPS.values())


def _load_rules() -> dict:
    doc = yaml.safe_load(RULES_PATH.read_text())
    assert isinstance(doc, dict) and isinstance(doc.get("rules"), dict)
    return doc["rules"]


def test_curated_rules_schema():
    rules = _load_rules()
    assert rules, "curated rules file must not be empty"
    for key, rule in rules.items():
        missing = _REQUIRED_RULE_KEYS - set(rule)
        assert not missing, f"{key}: missing {missing}"
        match = rule["match"]
        assert isinstance(match.get("states"), list) and match["states"]
        assert all(s in _VALID_USPS for s in match["states"]), key
        patterns = match.get("name_patterns")
        assert isinstance(patterns, list) and patterns, key
        # matching is lowercase-substring; patterns must already be lowercase
        assert all(isinstance(p, str) and p == p.lower() for p in patterns), key
        assert str(rule["source_url"]).startswith("http"), key
        reviewed = rule["last_reviewed"]
        if isinstance(reviewed, str):  # quoted in YAML; both forms acceptable
            reviewed = datetime.date.fromisoformat(reviewed)
        assert isinstance(reviewed, datetime.date), key
        cadence = rule["review_cadence_months"]
        assert isinstance(cadence, int) and cadence > 0, key


def test_curated_rules_cover_the_seven_authorities():
    authorities = {r["authority"] for r in _load_rules().values()}
    for fragment in ("PANYNJ", "MTA", "Maryland", "Massachusetts",
                     "Virginia", "Colorado", "Pennsylvania"):
        assert any(fragment in a for a in authorities), fragment


def test_curated_rule_matching():
    match = routes_tunnels._match_rule
    assert match("NJ", "Holland Tunnel")[0] == "panynj_holland"
    assert match("NY", "Lincoln  Tunnel")[0] == "panynj_lincoln"  # live double space
    assert match("NY", "Queens Midtown Tunnel")[0] == "mta_queens_midtown"
    assert match("VA", "Midtown Tunnel EBL")[0] == "vdot_midtown"  # state-scoped
    assert match("VA", "Westbound Midtown Tunnel")[0] == "vdot_midtown"
    assert match("MD", "BHT - Baltimore Harbor Tunnel")[0] == "mdta_baltimore_harbor"
    assert match("MA", "THOMAS P. TIP ONEILL JR TUNNEL - 93NB")[0] == "massdot_oneill"
    assert match("CO", "Johnson Tunnel")[0] == "cdot_ejmt"
    assert match("PA", "Tuscarora Tunnel WB")[0] == "paturnpike_tunnels"
    # state scoping: same-name tunnels elsewhere must NOT inherit rules
    assert match("SD", "Scovel Johnson Tunnel") is None
    assert match("MI", "HOLLAND AIRPORT") is None
    assert match("CA", "Battery Tunnel SB") is None
    assert match(None, "Holland Tunnel") is None
    assert match("NJ", None) is None


def test_curated_rule_embed_shape():
    rule = routes_tunnels._curated_rule("NJ", "Holland Tunnel")
    assert rule["key"] == "panynj_holland"
    assert "match" not in rule           # match block is plumbing, not payload
    assert rule["source_url"].startswith("http")
    assert "last_reviewed" in rule
    assert routes_tunnels._curated_rule("AL", "Bankhead Tunnel") is None


# --------------------------------------------- rules-file resilience + reload

@pytest.fixture
def _rules_sandbox(monkeypatch, tmp_path):
    """Point the router at a scratch rules file with a clean cache; the real
    path + cache are restored afterwards by monkeypatch."""
    path = tmp_path / "tunnel_rules.yaml"
    monkeypatch.setattr(routes_tunnels, "_RULES_PATH", path)
    monkeypatch.setattr(routes_tunnels, "_rules_cache", {"mtime": None, "rules": {}})
    return path


def test_rules_broken_yaml_degrades_to_no_rules(_rules_sandbox):
    # The file is hand-edited quarterly — a syntax typo must degrade to
    # "no curated rule", never a 500 that takes the NTI data down with it.
    _rules_sandbox.write_text("rules: {holland: [unclosed")
    assert routes_tunnels._rules() == {}
    assert routes_tunnels._curated_rule("NJ", "Holland Tunnel") is None


def test_rules_non_mapping_bodies_dropped_valid_ones_survive(_rules_sandbox):
    _rules_sandbox.write_text(
        "rules:\n"
        "  junk: just a string\n"
        "  good:\n"
        "    match: {states: [NJ], name_patterns: [holland]}\n"
        "    authority: Test Authority\n"
    )
    assert set(routes_tunnels._rules()) == {"good"}
    assert routes_tunnels._match_rule("NJ", "Holland Tunnel")[0] == "good"
    # malformed match internals never raise either
    _rules_sandbox.write_text(
        "rules:\n  odd:\n    match: {states: NJ, name_patterns: [[nested]]}\n")
    os.utime(_rules_sandbox, ns=(1, 1))
    assert routes_tunnels._match_rule("NJ", "Holland Tunnel") is None


def test_rules_missing_file_is_empty(_rules_sandbox):
    assert routes_tunnels._rules() == {}


def test_rules_reload_on_file_change(_rules_sandbox):
    # Quarterly manual edits must be served without an API restart.
    _rules_sandbox.write_text(
        "rules:\n  a:\n    match: {states: [NJ], name_patterns: [holland]}\n")
    os.utime(_rules_sandbox, ns=(10**9, 10**9))
    assert set(routes_tunnels._rules()) == {"a"}
    _rules_sandbox.write_text(
        "rules:\n  b:\n    match: {states: [NJ], name_patterns: [holland]}\n")
    os.utime(_rules_sandbox, ns=(2 * 10**9, 2 * 10**9))
    assert set(routes_tunnels._rules()) == {"b"}


# ------------------------------------------------------------------- API

app = FastAPI()
common.install_error_handlers(app)
app.include_router(routes_tunnels.router)


def _get(path: str, **params):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, params=params)

    return asyncio.run(go())


def _err_code(resp) -> str:
    body = resp.json()
    assert set(body) == {"error"}, f"not the envelope: {body}"
    assert set(body["error"]) == {"code", "message"}
    return body["error"]["code"]


def test_tunnels_bbox_required():
    resp = _get("/v1/tunnels")
    assert resp.status_code == 400
    assert _err_code(resp) == "invalid_bbox"


def test_tunnels_bbox_too_large():
    resp = _get("/v1/tunnels", bbox=BBOX_HUGE)
    assert resp.status_code == 400
    assert _err_code(resp) == "bbox_too_large"


@pytest.mark.parametrize(
    "params",
    [
        {"state": "NEWYORK"},
        {"state": "1"},
        {"max_clearance_lt_in": -3},
        {"max_clearance_lt_in": "tall"},
        {"hazmat": "maybe"},
        {"limit": 1001},
        {"offset": -1},
    ],
)
def test_tunnels_invalid_param(params):
    resp = _get("/v1/tunnels", bbox=BBOX_OK, **params)
    assert resp.status_code == 400
    assert _err_code(resp) == "invalid_param"


def test_tunnels_db_down_is_upstream_unavailable(monkeypatch):
    def boom():
        raise psycopg.OperationalError("connection refused (simulated)")

    monkeypatch.setattr(common, "connect_ro", boom)
    resp = _get("/v1/tunnels", bbox=BBOX_OK)
    assert resp.status_code == 503
    assert _err_code(resp) == "upstream_unavailable"


# ---------------------------------------------------------------- DB-backed

@needs_db
def test_tunnels_shape():
    resp = _get("/v1/tunnels", bbox=BBOX_OK)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert body["count"] == len(body["features"])
    for feat in body["features"][:10]:
        assert feat["type"] == "Feature"
        props = feat["properties"]
        for key in ("tunnel_id", "min_vert_clearance_in", "hazmat_restricted",
                    "hazmat_codes", "curated_rule", "source_id", "observed_at",
                    "vintage", "confidence", "attribution"):
            assert key in props, f"missing {key}"
        assert props["confidence"] is not None       # NULL renders "unknown"
        assert props["hazmat_restricted"] is not None  # tri-state renders "unknown"
        if props["curated_rule"] is not None:
            assert props["curated_rule"]["source_url"].startswith("http")


@needs_db
def test_tunnels_clearance_filter():
    resp = _get("/v1/tunnels", bbox=BBOX_OK, max_clearance_lt_in=160)
    assert resp.status_code == 200
    for feat in resp.json()["features"]:
        clearance = feat["properties"]["min_vert_clearance_in"]
        # unknown clearance must never match a "below X" filter
        assert isinstance(clearance, (int, float)) and clearance < 160


@needs_db
@pytest.mark.parametrize("flag,expect", [("true", True), ("false", False)])
def test_tunnels_hazmat_filter_excludes_unknown(flag, expect):
    resp = _get("/v1/tunnels", bbox=BBOX_OK, hazmat=flag)
    assert resp.status_code == 200
    for feat in resp.json()["features"]:
        assert feat["properties"]["hazmat_restricted"] is expect


@needs_db
def test_tunnels_state_filter():
    resp = _get("/v1/tunnels", bbox=BBOX_OK, state="nj")  # case-insensitive
    assert resp.status_code == 200
    for feat in resp.json()["features"]:
        assert feat["properties"]["state"] == "NJ"


@needs_db
def test_tunnel_detail_not_found_envelope():
    resp = _get("/v1/tunnels/no-such-tunnel")
    assert resp.status_code == 404
    assert _err_code(resp) == "not_found"


@needs_db
def test_tunnel_detail_roundtrip():
    listing = _get("/v1/tunnels", bbox=BBOX_OK, limit=1)
    assert listing.status_code == 200
    features = listing.json()["features"]
    if not features:
        pytest.skip("core.tunnels has no rows in this bbox yet")
    tunnel_id = features[0]["properties"]["tunnel_id"]
    resp = _get(f"/v1/tunnels/{tunnel_id}")
    assert resp.status_code == 200
    feat = resp.json()
    assert feat["type"] == "Feature"
    assert feat["properties"]["tunnel_id"] == tunnel_id
    assert isinstance(feat["properties"]["record"], dict)  # full NTI record
    assert feat["properties"]["attribution"]
