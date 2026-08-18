"""WZDx track tests — parser (v4.0 + v4.2 flavors) and the discovery script.

Pure fixtures, no network, no DB. Fixtures are synthetic but format-faithful:
wzdx_v42.json mirrors the live WSDOT v4.2 feed, wzdx_v40.json mirrors the live
MN/KS CARS v4.0 feeds (empty feed_info, MultiPoint geometry), both sampled on
the wire 2026-07-22. The registry CSV fixture carries the real export headers.

Run: uv run pytest
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from truckintel.parsers import wzdx
from truckintel.registry import load_registry

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_discover():
    """scripts/ is not a package — load the discovery script by path."""
    spec = importlib.util.spec_from_file_location(
        "wzdx_discover", REPO_ROOT / "scripts" / "wzdx_discover.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ parser: v4.2

def test_v42_parse():
    rows = list(wzdx.parse((FIXTURES / "wzdx_v42.json").read_bytes()))
    assert len(rows) == 3
    line, area, point = rows

    # event_id = feed id + '/' + road event id (feed_info.publisher present)
    assert line["event_id"] == "Test State DOT/1001-E"
    assert line["kind"] == "work_zone"
    assert line["geom_wkt"] == (
        "LINESTRING (-122.5 47.2, -122.4 47.25, -122.3 47.3)"
    )
    # observed_at = the EVENT's update_date, never the fetch time
    assert line["observed_at"] == "2026-07-20T10:00:00Z"

    props = line["props"]
    assert props["direction"] == "eastbound"
    assert props["road_names"] == ["I-90"]
    assert props["vehicle_impact"] == "some-lanes-closed"
    assert props["start_date"] == "2026-07-01T00:00:00Z"
    assert props["end_date"] == "2026-09-30T00:00:00Z"
    assert props["is_start_date_verified"] is True
    assert props["is_end_date_verified"] is False
    assert props["restrictions"] == [
        {"type": "reduced-width", "value": 10.0, "unit": "feet"}
    ]
    # unknown extra fields pass through untouched
    assert props["custom_agency_field"] == "passes through untouched"
    assert props["worker_presence"] == {"are_workers_present": True}


def test_v42_polygon_and_creation_date_fallback():
    area = list(wzdx.parse((FIXTURES / "wzdx_v42.json").read_bytes()))[1]
    assert area["geom_wkt"] == (
        "POLYGON ((-122.2 47.6, -122.1 47.6, -122.1 47.7, "
        "-122.2 47.7, -122.2 47.6))"
    )
    # no update_date on the event -> creation_date; still the event's vintage
    assert area["observed_at"] == "2026-07-10T08:30:00Z"
    # neither accuracy nor is_verified spelling present -> honest None
    assert area["props"]["is_start_date_verified"] is None
    assert area["props"]["is_end_date_verified"] is None


def test_v42_point_geometry():
    point = list(wzdx.parse((FIXTURES / "wzdx_v42.json").read_bytes()))[2]
    assert point["geom_wkt"] == "POINT (-121.9 47.85)"


# ------------------------------------------------------------ parser: v4.0

def test_v40_empty_feed_info_falls_back_to_data_source():
    rows = list(wzdx.parse((FIXTURES / "wzdx_v40.json").read_bytes()))
    assert len(rows) == 3
    # feed_info is {} (real MN/KS CARS shape) -> per-event data_source_id
    assert rows[0]["event_id"] == "CARS/CARSx-105193"


def test_v40_accuracy_spelling_normalized():
    first, _, legacy = list(wzdx.parse((FIXTURES / "wzdx_v40.json").read_bytes()))
    # v4.0 start_date_accuracy 'estimated'/'verified' -> normalized bool
    assert first["props"]["is_start_date_verified"] is False
    assert legacy["props"]["is_start_date_verified"] is True
    assert legacy["props"]["is_end_date_verified"] is False


def test_v40_multipoint_wkt():
    first = next(wzdx.parse((FIXTURES / "wzdx_v40.json").read_bytes()))
    assert first["geom_wkt"] == "MULTIPOINT ((-93.400414 44.952156))"


def test_v40_null_geometry_stays_none():
    nogeo = list(wzdx.parse((FIXTURES / "wzdx_v40.json").read_bytes()))[1]
    assert nogeo["geom_wkt"] is None            # never fabricated
    assert nogeo["event_id"] == "CARS/CARSx-200001"
    assert nogeo["observed_at"] == "2026-07-01T00:00:00Z"


def test_v40_legacy_spellings():
    legacy = list(wzdx.parse((FIXTURES / "wzdx_v40.json").read_bytes()))[2]
    # Feature.id absent -> properties.road_event_id (pre-v4 / AZ511 spelling)
    assert legacy["event_id"] == "CARS/CARS5-300002"
    # singular road_name -> normalized road_names list
    assert legacy["props"]["road_names"] == ["US 50"]


def test_unidentifiable_event_is_skipped_among_identifiable():
    # An individually id-less feature is skipped (cannot be lifecycle-tracked)
    # while the rest of the feed still parses.
    raw = (
        b'{"feed_info": {"publisher": "X"}, "features": ['
        b'{"type": "Feature", "properties": {}, "geometry": null},'
        b'{"type": "Feature", "id": "42", "properties": {}, "geometry": null}]}'
    )
    rows = list(wzdx.parse(raw))
    assert [r["event_id"] for r in rows] == ["X/42"]


def test_all_events_unidentifiable_raises_never_publishes_empty():
    # Id-field drift: EVERY feature id-less must raise, because parsing to []
    # would soft-close every active event for the source as a 'success'.
    raw = b'{"feed_info": {"publisher": "X"}, "features": [{"type": "Feature", "properties": {}, "geometry": null}]}'
    with pytest.raises(ValueError, match="id-field drift"):
        list(wzdx.parse(raw))


def test_error_envelope_raises_never_reads_as_empty_feed():
    # HTTP-200 JSON error/maintenance envelope (CDN-fronted 511 endpoints):
    # not a FeatureCollection -> raise, never an empty feed that soft-closes
    # every active work zone.
    raw = b'{"status": "error", "message": "rate limit exceeded"}'
    with pytest.raises(ValueError, match="features"):
        list(wzdx.parse(raw))


def test_features_null_raises():
    raw = b'{"feed_info": {"publisher": "X"}, "features": null}'
    with pytest.raises(ValueError, match="not a list"):
        list(wzdx.parse(raw))


def test_empty_feature_collection_is_a_legitimate_empty_feed():
    # features: [] IS a real zero-work-zones state and must parse to [].
    raw = b'{"feed_info": {"publisher": "X"}, "features": []}'
    assert list(wzdx.parse(raw)) == []


def test_geom_unknown_type_is_none():
    assert wzdx._geom_wkt({"type": "GeometryCollection", "geometries": []}) is None
    assert wzdx._geom_wkt(None) is None
    assert wzdx._geom_wkt({"type": "Point", "coordinates": []}) is None


def test_multilinestring_wkt():
    wkt = wzdx._geom_wkt(
        {
            "type": "MultiLineString",
            "coordinates": [[[-1.0, 1.0], [-2.0, 2.0]], [[-3.0, 3.0], [-4.0, 4.0]]],
        }
    )
    assert wkt == "MULTILINESTRING ((-1.0 1.0, -2.0 2.0), (-3.0 3.0, -4.0 4.0))"


# --------------------------------------------------------------- discovery

@pytest.fixture(scope="module")
def discover_mod():
    return _load_discover()


@pytest.fixture()
def summary_and_out(discover_mod, tmp_path):
    out = tmp_path / "proposed"
    csv_bytes = (FIXTURES / "wzdx_registry.csv").read_bytes()
    return discover_mod.discover(csv_bytes, out), out


def test_discover_proposes_only_open_active_us_feeds(summary_and_out):
    summary, out = summary_and_out
    proposed_ids = sorted(e["source_id"] for e in summary if e["proposed"])
    # WA + the two TX rows; everything else is excluded with a reason
    assert proposed_ids == ["wzdx_tx", "wzdx_tx_austin", "wzdx_wa"]
    for sid in proposed_ids:
        assert (out / f"{sid}.yaml").exists()
    assert (out / "SUMMARY.md").exists()


def test_discover_exclusion_reasons(summary_and_out):
    summary, _ = summary_and_out
    reasons = {e["org"]: e["reason"] for e in summary if not e["proposed"]}
    assert "credential" in reasons["Oklahoma DOT"]           # embedded token
    assert "API key" in reasons["Illinois Tollway"]
    assert "inactive" in reasons["Michigan Department of Transportation"]
    assert "CWZ" in reasons["Massachusetts DOT"]             # non-WZDx schema
    assert "US state" in reasons["CivicLink"]


def test_discover_proposals_are_promotable_yaml(summary_and_out):
    """A proposal must be valid registry YAML so promotion is copy + human
    edits — validated by the real load_registry (parser importable, kind,
    load_pattern, schedule/slo)."""
    _, out = summary_and_out
    sources = load_registry(out)
    by_id = {s["id"]: s for s in sources}
    wa = by_id["wzdx_wa"]
    assert wa["kind"] == "live_json"
    assert wa["load_pattern"] == "event_lifecycle"
    assert wa["parser"] == "wzdx"
    assert wa["schedule_minutes"] == 15 and wa["slo_hours"] == 24
    assert wa["gates"]["min_rows"] == 0
    # the un-approved flag is loud in the raw YAML
    text = (out / "wzdx_wa.yaml").read_text()
    assert "PROPOSED — NOT APPROVED" in text
    assert "UNVERIFIED" in yaml.safe_load(text)["license"]


def test_discover_never_writes_into_registry(discover_mod):
    csv_bytes = (FIXTURES / "wzdx_registry.csv").read_bytes()
    with pytest.raises(ValueError, match="registry/"):
        discover_mod.discover(csv_bytes, REPO_ROOT / "registry")
    with pytest.raises(ValueError, match="registry/"):
        discover_mod.discover(csv_bytes, REPO_ROOT / "registry" / "sub")


def test_discover_empty_csv_fails_loudly(discover_mod, tmp_path):
    with pytest.raises(ValueError, match="zero rows"):
        discover_mod.discover(b"state,url\n", tmp_path / "p")


# ------------------------------------------------- promoted registry YAMLs

def test_promoted_wzdx_yaml_are_valid():
    """The four wave-1 feeds promoted per research/live-ops.md license
    verification load through the real registry validation."""
    by_id = {s["id"]: s for s in load_registry(REPO_ROOT / "registry")}
    # Cadence is pinned per feed, not as one shared number. wzdx_mn moved to
    # hourly on 2026-08-18: it stalls roughly every other attempt (84 of 175
    # runs in a week died on a 60s read timeout, and after polite_get gained a
    # retry the survivors were runs where BOTH attempts hung), and its SLO is
    # 24 h, so 96 polls a day was over-polling a struggling state-DOT server.
    # Keeping the assertion per-feed means the pin still catches an accidental
    # change to the other three.
    for sid, state_url, sched in [
        ("wzdx_wa", "wzdx.wsdot.wa.gov", 15),
        ("wzdx_mn", "mn.carsprogram.org", 60),
        ("wzdx_ks", "ks.carsprogram.org", 15),
        ("wzdx_az", "az511.com", 15),
    ]:
        src = by_id[sid]
        assert state_url in src["url"]
        assert src["kind"] == "live_json"
        assert src["load_pattern"] == "event_lifecycle"
        assert src["parser"] == "wzdx"
        assert src["target"] is None                # event feeds never swap
        assert src["schedule_minutes"] == sched
        assert src["slo_hours"] == 24
        assert src["gates"]["min_rows"] == 0
        assert src["auth"] is None                  # wave 1 = open feeds only
        assert src["license"] and src["attribution"]
