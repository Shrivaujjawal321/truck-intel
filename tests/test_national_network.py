"""Unit tests for the NTAD National Network parser (truck route spine).

Synthetic FeatureCollection bytes — no network. A live smoke test against the
real FeatureServer is gated on RUN_LIVE=1 so CI stays offline-deterministic.
"""
from __future__ import annotations

import json
import os

import pytest

from truckintel.parsers import national_network as nn


def _fc(*features) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


def _feature(props, geom):
    return {"type": "Feature", "properties": props, "geometry": geom}


_LINE = {"type": "LineString", "coordinates": [[-157.8132, 21.3926], [-157.8134, 21.3904]]}


def test_nn_positive_is_kept_and_mapped():
    raw = _fc(_feature(
        {"ID": 12353, "NN": 1, "ROUTEID": "H3", "SIGNT1": "I", "SIGNN1": "3",
         "LNAME": " ", "STFIPS": 15, "CTFIPS": 3, "YEAR": 2018,
         "FCLASS": 1, "AADT": 27100, "AADT_COM": 494, "THROUGH_LA": 4},
        _LINE,
    ))
    rows = list(nn.parse(raw))
    assert len(rows) == 1
    r = rows[0]
    assert r["route_id"] == 12353
    assert r["nn"] == 1
    assert r["route_ref"] == "I 3"
    assert r["route_name"] == "I 3"          # LNAME was blank -> falls back to ref
    assert r["routeid_state"] == "H3"
    assert r["state"] == "HI"                # STFIPS 15
    assert r["state_fips"] == 15
    assert r["county_fips"] == 15003         # STFIPS*1000 + CTFIPS
    assert r["aadt_com"] == 494
    assert r["observed_at"] == "2018-01-01"  # from YEAR, never download date
    assert r["geom_wkt"].startswith("MULTILINESTRING((")


def test_nn_zero_is_dropped():
    """The 24,169 NN=0 rows are in the file but NOT truck-designated."""
    raw = _fc(_feature({"ID": 5, "NN": 0, "SIGNT1": "SR", "SIGNN1": "1"}, _LINE))
    assert list(nn.parse(raw)) == []


def test_uncoded_nn_is_dropped():
    raw = _fc(_feature({"ID": 6, "NN": None, "SIGNT1": "US", "SIGNN1": "1"}, _LINE))
    assert list(nn.parse(raw)) == []


def test_real_lname_wins_over_ref():
    raw = _fc(_feature(
        {"ID": 7, "NN": 1, "SIGNT1": "US", "SIGNN1": "101", "LNAME": "El Camino Real"},
        _LINE,
    ))
    r = list(nn.parse(raw))[0]
    assert r["route_ref"] == "US 101"
    assert r["route_name"] == "El Camino Real"


def test_multilinestring_geometry_normalized():
    geom = {"type": "MultiLineString",
            "coordinates": [[[-100.0, 40.0], [-100.1, 40.1]],
                            [[-100.2, 40.2], [-100.3, 40.3]]]}
    raw = _fc(_feature({"ID": 8, "NN": 1}, geom))
    r = list(nn.parse(raw))[0]
    assert r["geom_wkt"].count("(") == 3      # MULTILINESTRING( (..),(..) )
    assert r["geom_wkt"].startswith("MULTILINESTRING((-100")


def test_degenerate_geometry_is_none_not_fabricated():
    """A single-point 'line' is unusable -> geom_wkt None -> gate 1 rejects."""
    geom = {"type": "LineString", "coordinates": [[-100.0, 40.0]]}
    raw = _fc(_feature({"ID": 9, "NN": 1}, geom))
    assert list(nn.parse(raw))[0]["geom_wkt"] is None


def test_missing_id_yields_none_pk():
    """No source ID -> route_id None so gate 1 (required_fields) rejects it."""
    raw = _fc(_feature({"NN": 1, "SIGNT1": "I", "SIGNN1": "5"}, _LINE))
    assert list(nn.parse(raw))[0]["route_id"] is None


def test_props_preserved():
    raw = _fc(_feature({"ID": 10, "NN": 1, "VERSION": "2020.01.10", "YEAR": 2018}, _LINE))
    r = list(nn.parse(raw))[0]
    assert r["props"]["VERSION"] == "2020.01.10"   # other candidate vintages kept in props


@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1",
                    reason="live NTAD FeatureServer smoke test (set RUN_LIVE=1)")
def test_live_national_network_smoke():
    """One page from the real service parses and every yielded row is NN>0."""
    import urllib.request
    url = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
           "NTAD_National_Network/FeatureServer/0/query"
           "?where=1%3D1&outFields=*&resultRecordCount=50&f=geojson")
    raw = urllib.request.urlopen(url, timeout=60).read()
    rows = list(nn.parse(raw))
    assert rows, "live service returned no parseable rows"
    assert all(r["nn"] > 0 for r in rows)
    assert all(r["geom_wkt"] is None or r["geom_wkt"].startswith("MULTILINESTRING")
               for r in rows)
