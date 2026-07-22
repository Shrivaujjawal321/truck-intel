"""Gates 1-2 unit tests — pure functions, no DB, no network."""
from __future__ import annotations

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
