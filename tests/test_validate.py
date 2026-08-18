"""Gates 1-2 unit tests — pure functions, no DB, no network."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from truckintel import validate
from truckintel.validate import gate1_schema, gate2_coords


def _reasons(rejects: list[dict]) -> list[str]:
    return [r["reason"] for r in rejects]


# ---------------------------------------------------------------- gate 1

def test_gate1_ok_row_passes():
    rows = [{"nbi_id": "42001", "lat": 40.0, "lon": -75.0}]
    ok, rejects = gate1_schema(rows, ("nbi_id", "lat", "lon"))
    assert ok == rows and rejects == []


def test_gate1_missing_field_rejected_with_reason():
    ok, rejects = gate1_schema([{"lat": 40.0, "lon": -75.0}], ("nbi_id", "lat", "lon"))
    assert ok == []
    assert _reasons(rejects) == ["missing_required:nbi_id"]
    assert rejects[0]["raw_record"] == {"lat": 40.0, "lon": -75.0}


def test_gate1_null_value_counts_as_missing():
    _, rejects = gate1_schema([{"nbi_id": None, "lat": 1, "lon": 1}], ("nbi_id",))
    assert _reasons(rejects) == ["missing_required:nbi_id"]


def test_gate1_unparseable_lat_rejected():
    _, rejects = gate1_schema(
        [{"nbi_id": "x", "lat": "not-a-float", "lon": -75.0}], ("nbi_id", "lat", "lon")
    )
    assert _reasons(rejects) == ["unparseable:lat"]


def test_gate1_numeric_string_lat_is_parseable():
    ok, rejects = gate1_schema([{"nbi_id": "x", "lat": "40.5", "lon": "-75"}],
                               ("nbi_id", "lat", "lon"))
    assert len(ok) == 1 and rejects == []
    assert ok[0]["lat"] == "40.5"  # gate parses, never mutates


def test_gate1_mixed_rows_split_correctly():
    rows = [{"a": 1}, {"b": 2}, {"a": 3}]
    ok, rejects = gate1_schema(rows, ("a",))
    assert ok == [{"a": 1}, {"a": 3}]
    assert _reasons(rejects) == ["missing_required:a"]


# ---------------------------------------------------------------- gate 2

def test_gate2_conus_alaska_hawaii_pass():
    rows = [
        {"lat": 40.71, "lon": -74.00},   # NYC
        {"lat": 61.19, "lon": -149.87},  # Anchorage
        {"lat": 21.31, "lon": -157.86},  # Honolulu
        {"lat": 18.42, "lon": -66.06},   # San Juan PR
    ]
    ok, rejects = gate2_coords(rows)
    assert len(ok) == 4 and rejects == []


def test_gate2_null_island_and_range_junk_rejected():
    rows = [
        {"lat": 0.0, "lon": 0.0},
        {"lat": 95.0, "lon": -75.0},
        {"lat": 40.0, "lon": -200.0},
    ]
    ok, rejects = gate2_coords(rows)
    assert ok == []
    assert _reasons(rejects) == ["coords_out_of_range"] * 3


def test_gate2_swapped_axes_rejected_never_fixed():
    # True point: lat 40.71, lon -74.00 — the feed swapped the axes.
    swapped = {"lat": -74.00, "lon": 40.71}
    ok, rejects = gate2_coords([swapped])
    assert ok == []
    assert _reasons(rejects) == ["latlon_swapped"]
    assert rejects[0]["raw_record"] is swapped  # untouched: NEVER auto-fixed


def test_gate2_out_of_range_wins_over_swap_detection():
    # Swap of a western point puts |lat| > 90 — junk check fires first, per spec order.
    _, rejects = gate2_coords([{"lat": -120.0, "lon": 47.6}])
    assert _reasons(rejects) == ["coords_out_of_range"]


def test_gate2_non_us_rejected():
    _, rejects = gate2_coords([{"lat": 48.85, "lon": 2.35}])  # Paris
    assert _reasons(rejects) == ["coords_not_in_us"]


def test_gate2_rows_without_coords_pass_through():
    rows = [{"region": "US", "product": "diesel", "week_of": "2026-07-20"}]
    ok, rejects = gate2_coords(rows)
    assert ok == rows and rejects == []


# ------------------------------------------- gate 2 on WKT geometry carriers
#
# Regression cover for the geom_wkt bypass: loaders.py accepts either a
# lat/lon pair or a geom_wkt string, but gate2 used to judge only the former,
# so every LineString/Polygon row (wzdx work zones, nws alert polygons) landed
# in core unvalidated. That is how a parser bug published null-island geometry
# in July 2026.

US_LINE = "LINESTRING(-121.5 39.2, -121.4 39.3)"
US_POLY = "POLYGON((-121.5 39.2, -121.4 39.2, -121.4 39.3, -121.5 39.2))"


@pytest.mark.parametrize("wkt, expected", [
    ("POINT(-121.5 39.2)", [(-121.5, 39.2)]),
    (US_LINE, [(-121.5, 39.2), (-121.4, 39.3)]),
    (US_POLY, [(-121.5, 39.2), (-121.4, 39.2), (-121.4, 39.3), (-121.5, 39.2)]),
    # nesting depth is irrelevant — vertices are comma-separated x y groups
    ("MULTIPOLYGON(((-121.5 39.2, -121.4 39.2, -121.4 39.3, -121.5 39.2)))",
     [(-121.5, 39.2), (-121.4, 39.2), (-121.4, 39.3), (-121.5, 39.2)]),
    ("MULTILINESTRING((-121.5 39.2, -121.4 39.3),(-120.0 38.0, -119.9 38.1))",
     [(-121.5, 39.2), (-121.4, 39.3), (-120.0, 38.0), (-119.9, 38.1)]),
    # Z/M ordinates ignored: gate 2 judges horizontal position only
    ("LINESTRING Z (-121.5 39.2 100, -121.4 39.3 120)",
     [(-121.5, 39.2), (-121.4, 39.3)]),
    # not parseable as coordinate-bearing WKT
    ("POLYGON EMPTY", None),
    ("not wkt at all", None),
    ("LINESTRING(-121.5 39.2, oops 39.3)", None),
    ("LINESTRING(-121.5, -121.4)", None),      # single ordinates, not pairs
    ("", None),
    (None, None),
])
def test_wkt_coords(wkt, expected):
    assert validate.wkt_coords(wkt) == expected


def test_gate2_accepts_us_geometry():
    rows = [{"id": 1, "geom_wkt": US_LINE}, {"id": 2, "geom_wkt": US_POLY}]
    ok, rejects = validate.gate2_coords(rows)
    assert len(ok) == 2 and rejects == []


def test_gate2_rejects_null_island_geometry():
    """The exact July-2026 bug: a parser emitting (0,0) geometry used to sail
    straight through gate 2."""
    rows = [{"id": 1, "geom_wkt": "LINESTRING(0 0, 0 0)"}]
    ok, rejects = validate.gate2_coords(rows)
    assert ok == []
    assert rejects[0]["reason"] == "coords_out_of_range"
    assert rejects[0]["raw_record"]["id"] == 1


def test_gate2_rejects_one_bad_vertex_among_good_ones():
    # One junk vertex means the producer emitted junk, whatever the rest says.
    rows = [{"geom_wkt": "LINESTRING(-121.5 39.2, 0 0, -121.4 39.3)"},
            {"geom_wkt": "LINESTRING(-121.5 39.2, -121.4 999.0)"}]
    ok, rejects = validate.gate2_coords(rows)
    assert ok == []
    assert [r["reason"] for r in rejects] == ["coords_out_of_range"] * 2


def test_gate2_geometry_may_cross_the_border():
    """A work zone or alert polygon legitimately crosses into Canada — one
    US vertex is enough. Requiring ALL vertices in-US would drop real data."""
    rows = [{"geom_wkt": "LINESTRING(-95.15 48.9, -95.15 49.8)"}]  # MN -> Canada
    ok, rejects = validate.gate2_coords(rows)
    assert len(ok) == 1 and rejects == []


def test_gate2_rejects_wholly_foreign_geometry():
    rows = [{"geom_wkt": "LINESTRING(2.29 48.85, 2.30 48.86)"}]   # Paris
    ok, rejects = validate.gate2_coords(rows)
    assert ok == [] and rejects[0]["reason"] == "coords_not_in_us"


def test_gate2_detects_swapped_axes_in_geometry():
    # Every vertex would land in-US with the axes swapped -> named honestly,
    # and still never auto-fixed. (Latitudes stay within +/-90 so the
    # out-of-range check, which correctly fires first, does not mask this.)
    rows = [{"geom_wkt": "LINESTRING(39.2 -75.0, 39.3 -75.1)"}]
    ok, rejects = validate.gate2_coords(rows)
    assert ok == [] and rejects[0]["reason"] == "latlon_swapped"
    assert rejects[0]["raw_record"]["geom_wkt"].startswith("LINESTRING(39.2")


def test_gate2_out_of_range_beats_swap_detection_in_geometry():
    # Ordering parity with the point path: a |lat| > 90 vertex is junk, and is
    # reported as junk rather than as a swap.
    rows = [{"geom_wkt": "LINESTRING(39.2 -121.5, 39.3 -121.4)"}]
    ok, rejects = validate.gate2_coords(rows)
    assert ok == [] and rejects[0]["reason"] == "coords_out_of_range"


def test_gate2_rejects_unparseable_geometry_never_guesses():
    rows = [{"geom_wkt": "LINESTRING(-121.5 39.2, garbage)"}]
    ok, rejects = validate.gate2_coords(rows)
    assert ok == [] and rejects[0]["reason"] == "geom_unparseable"


def test_gate2_null_geometry_passes_through():
    # 'this feed has a geometry column, this row has none' -> loaders store
    # NULL; there is nothing to judge, so it is not a reject.
    ok, rejects = validate.gate2_coords([{"id": 1, "geom_wkt": None}])
    assert len(ok) == 1 and rejects == []


def test_gate2_geometry_wins_over_a_derived_point():
    # Where a row carries both, the geometry is the authoritative shape.
    rows = [{"lat": 39.2, "lon": -121.5, "geom_wkt": "LINESTRING(0 0, 0 0)"}]
    ok, rejects = validate.gate2_coords(rows)
    assert ok == [] and rejects[0]["reason"] == "coords_out_of_range"


def test_gate2_point_only_behaviour_is_unchanged():
    rows = [
        {"lat": 39.2, "lon": -121.5},                  # in-US
        {"lat": 0.0, "lon": 0.0},                      # null island
        {"lat": -121.5, "lon": 39.2},                  # |lat| > 90 -> junk
        {"lat": -75.0, "lon": 40.0},                   # swapped, both in range
        {"lat": 48.85, "lon": 2.29},                   # Paris
        {"lat": None, "lon": None},                    # unparseable
        {"region": "PADD1", "price_usd_gal": 3.9},     # no carrier -> passes
        {"lat": 39.2},                                 # half a pair -> passes
    ]
    ok, rejects = validate.gate2_coords(rows)
    assert len(ok) == 3
    assert [r["reason"] for r in rejects] == [
        "coords_out_of_range", "coords_out_of_range", "latlon_swapped",
        "coords_not_in_us", "coords_out_of_range"]


# ---------------------------------- gate 2 vs. real cached feed payloads
#
# The unit cases above prove the RULE; this proves the rule does not reject
# real production data. Tightening gate 2 to cover geom_wkt could have started
# rejecting legitimate NWS marine polygons or cross-border WZDx work zones —
# measured at adoption (2026-07-23): 2,180 NWS + 13,710 WZDx rows, 0 rejects.
# Skips when the raw cache is absent (fresh clone / CI without a fetch).
#
# Bounded to the most recent RAW_SAMPLE files per parser, newest first, as of
# 2026-08-18. It used to parse the entire cache. That was fine at adoption
# (2,180 + 13,710 rows) but data/raw/ is gitignored and unpruned, so by August
# it was 6 GB / 8,560 files and this test no longer finished in 8 minutes —
# it had quietly become the slowest thing in the "fast" pre-commit loop while
# being a no-op in CI, where data/raw/ does not exist at all.
#
# The sample is deterministic (path sort; the layout is <source>/<date>/<file>,
# so newest-last sorts chronologically) — not random, so a failure reproduces.
# Set TRUCKINTEL_RAW_SAMPLE=all for the exhaustive pass when deliberately
# re-validating a gate change against the whole cache.

RAW_ROOT = Path(__file__).resolve().parent.parent / "data" / "raw"
RAW_SAMPLE = os.environ.get("TRUCKINTEL_RAW_SAMPLE", "60")


@pytest.mark.parametrize("parser_name, glob_pat", [
    ("nws", "nws_alerts/*/*.json"),
    ("wzdx", "wzdx_*/*/*.json"),
])
def test_gate2_accepts_all_real_cached_feed_geometry(parser_name, glob_pat):
    import importlib
    files = sorted(p for p in RAW_ROOT.glob(glob_pat)
                   if not p.name.endswith(".meta.json"))
    if not files:
        pytest.skip(f"no cached {parser_name} payloads under {RAW_ROOT}")
    if RAW_SAMPLE != "all":
        files = files[-int(RAW_SAMPLE):]
    parser = importlib.import_module(f"truckintel.parsers.{parser_name}")

    total = 0
    offenders: list[tuple[str, str]] = []
    for path in files:
        try:
            rows = list(parser.parse(path.read_bytes()))
        except Exception:
            continue          # envelope-validation cases are their own tests
        total += len(rows)
        _ok, rejects = gate2_coords(rows)
        offenders += [(r["reason"], str(r["raw_record"].get("geom_wkt"))[:120])
                      for r in rejects]

    assert total > 0, f"cached {parser_name} payloads parsed to zero rows"
    assert offenders == [], (
        f"gate 2 would reject {len(offenders)}/{total} real {parser_name} "
        f"rows — investigate before tightening further: {offenders[:3]}")
