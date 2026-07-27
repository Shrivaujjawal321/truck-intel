"""Overpass truck-repair loader tests (scripts/osm_overpass.py).

This transport replaced a 2 h 51 m PBF pass with a ~2 minute API call, and the
things that make that trade safe are the things pinned here:

  * a way's representative point comes from `center`, a node's from lat/lon,
    and an element with neither is DROPPED rather than published at (0, 0)
  * tri-state capability flags stay NULL when the tag is absent
  * the vintage comes from Overpass's own osm3s stamp — a response without one
    is refused rather than stamped with now()
  * a busy public endpoint (429/504) is retried and failed over, but a 400 is
    OUR bug and must not be replayed against three donated servers

The osm_id shape is pinned too: `node/123` / `way/456`, identical to the PBF
path, because core.mechanic_shops.osm_match_id joins on it either way.

Run: uv run pytest tests/test_osm_overpass.py
"""
from __future__ import annotations

import importlib.util
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "osm_overpass", REPO / "scripts" / "osm_overpass.py")
ov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ov)

STAMP = datetime(2026, 7, 27, 7, 0, 6, tzinfo=timezone.utc)


# ------------------------------------------------------------------ to_row

def test_node_takes_its_own_position():
    row = ov.to_row({"type": "node", "id": 1, "lat": 39.7, "lon": -75.5,
                     "tags": {"shop": "truck_repair", "name": "Big Rig"}}, STAMP)
    assert row["osm_id"] == "node/1"        # same key shape as the PBF path
    assert (row["lat"], row["lon"]) == (39.7, -75.5)
    assert row["truck_repair"] is True
    assert row["observed_at"] == STAMP


def test_way_takes_its_center():
    row = ov.to_row({"type": "way", "id": 100,
                     "center": {"lat": 40.1, "lon": -74.2},
                     "tags": {"shop": "truck_repair"}}, STAMP)
    assert row["osm_id"] == "way/100"
    assert (row["lat"], row["lon"]) == (40.1, -74.2)


def test_element_without_a_position_is_dropped():
    """Publishing it at (0, 0) would put a truck shop in the Gulf of Guinea and
    it would pass every coordinate sanity check downstream."""
    assert ov.to_row({"type": "way", "id": 5, "tags": {"shop": "truck_repair"}},
                     STAMP) is None


def test_capability_tags_alone_qualify():
    """These sit on shops whose primary tag is car_repair — the whole reason
    OSM is worth querying, since Overture exposes no such flag."""
    row = ov.to_row({"type": "node", "id": 8, "lat": 1.0, "lon": -75.0,
                     "tags": {"shop": "car_repair",
                              "service:vehicle:truck_repair": "yes"}}, STAMP)
    assert row["truck_repair"] is True


def test_unstated_capability_is_unknown_not_false():
    row = ov.to_row({"type": "node", "id": 7, "lat": 1.0, "lon": -75.0,
                     "tags": {"shop": "truck_repair"}}, STAMP)
    assert row["trailer_repair"] is None       # never False
    assert row["hgv_access"] is None


def test_explicit_no_is_false():
    row = ov.to_row({"type": "node", "id": 9, "lat": 1.0, "lon": -75.0,
                     "tags": {"shop": "truck_repair",
                              "service:vehicle:trailer_repair": "no",
                              "hgv": "no"}}, STAMP)
    assert row["trailer_repair"] is False
    assert row["hgv_access"] is False


def test_opening_hours_travels_in_props():
    """There is no opening_hours column; the mechanic join reads props->>.
    If this moves, 34 shops silently lose the only hours they have."""
    row = ov.to_row({"type": "node", "id": 2, "lat": 1.0, "lon": -75.0,
                     "tags": {"shop": "truck_repair",
                              "opening_hours": "Mo-Fr 08:00-18:00"}}, STAMP)
    assert row["props"]["opening_hours"] == "Mo-Fr 08:00-18:00"


def test_state_only_from_a_valid_usps_code():
    assert ov.state_code({"addr:state": "de"}) == "DE"
    assert ov.state_code({"addr:state": "Delaware"}) is None   # no guessing
    assert ov.state_code({}) is None


# -------------------------------------------------------------- fetch/retry

def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "busy", {}, None)


def test_busy_endpoint_fails_over_to_the_next_mirror(monkeypatch):
    calls, waited = [], []

    def fake_once(endpoint, query):
        calls.append(endpoint)
        if len(calls) <= 4:            # exhaust the first mirror's attempts
            raise _http(504)
        return [{"type": "node", "id": 1, "lat": 1.0, "lon": -75.0, "tags": {}}], STAMP

    monkeypatch.setattr(ov, "_fetch_once", fake_once)
    elements, stamp = ov.fetch(mirrors=("A", "B"), sleep=waited.append)
    assert len(elements) == 1 and stamp == STAMP
    assert calls[:4] == ["A"] * 4 and calls[4] == "B"   # moved on, not gave up
    assert waited == list(ov.RETRY_WAITS_S)             # and it backed off


def test_a_bad_request_is_not_replayed_on_three_donated_servers(monkeypatch):
    """400 means OUR query is wrong. Retrying it everywhere is rude and cannot
    possibly succeed."""
    calls = []

    def fake_once(endpoint, query):
        calls.append(endpoint)
        raise _http(400)

    monkeypatch.setattr(ov, "_fetch_once", fake_once)
    with pytest.raises(urllib.error.HTTPError):
        ov.fetch(mirrors=("A", "B", "C"), sleep=lambda _: None)
    assert calls == ["A"]


def test_every_mirror_down_raises_rather_than_publishing_nothing(monkeypatch):
    monkeypatch.setattr(ov, "_fetch_once",
                        lambda e, q: (_ for _ in ()).throw(_http(429)))
    with pytest.raises(RuntimeError, match="every Overpass mirror"):
        ov.fetch(mirrors=("A", "B"), sleep=lambda _: None)


def test_a_response_without_a_vintage_is_refused(monkeypatch):
    """observed_at must be when the data was true in OSM. Falling back to
    now() would make stale data look fresh, which is the one thing the
    freshness SLO cannot detect."""
    class FakeResp:
        def read(self): return b'{"elements": []}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(ov.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    with pytest.raises(ValueError, match="timestamp_osm_base"):
        ov._fetch_once("https://example.invalid", "q")
