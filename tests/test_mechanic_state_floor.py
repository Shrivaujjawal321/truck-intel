"""The per-state publish guard on the mechanic pull — pure, no DB, no network.

scripts/mechanic_list.py --pull TRUNCATEs core.mechanic_shops and reloads it.
Before 2026-08-18 nothing stood between a scan that silently returned a
partial result and a live table rebuilt from it: a half-mirrored parquet store
or a bad region filter would publish a near-empty national dataset, and the
run recorded no failure because it recorded nothing at all.

_state_floor_violations is the gate. It is a pure function precisely so it can
be pinned here, and these tests pin BOTH halves of it — the absolute floor and
the self-calibrating drop check — because the drop check alone cannot catch a
first run and the floor alone cannot catch a state falling 100 -> 4.

Run: uv run pytest tests/test_mechanic_state_floor.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mechanic_list = _load("mechanic_list")
_violations = mechanic_list._state_floor_violations
_STATES = mechanic_list._US_STATES
FLOOR = mechanic_list.STATE_MIN_ROWS_FLOOR
BASELINE = mechanic_list.STATE_MIN_BASELINE_ROWS
RATIO = mechanic_list.STATE_MAX_DROP_RATIO


def _rows(counts: dict[str, int]) -> list[tuple]:
    """Overture pull tuples, which the guard reads at column index 8."""
    out = []
    for state, n in counts.items():
        for _ in range(n):
            row = [None] * 16
            row[8] = state
            out.append(tuple(row))
    return out


def _healthy(n: int = 50) -> dict[str, int]:
    return {s: n for s in _STATES}


# ------------------------------------------------------------- the happy path

def test_a_full_pull_trips_nothing():
    assert _violations(_rows(_healthy()), {}) == []


def test_first_ever_pull_has_no_baseline_and_still_passes():
    """prev_counts is empty on a fresh database. The drop check must not fire
    on absent history — only the absolute floor applies."""
    assert _violations(_rows(_healthy(FLOOR)), {}) == []


# ------------------------------------------------------------ absolute floor

def test_a_state_scanned_to_zero_is_flagged():
    counts = _healthy()
    counts["WY"] = 0
    problems = _violations(_rows(counts), {})
    assert len(problems) == 1
    assert problems[0].startswith("WY: 0 <")


def test_the_floor_boundary_is_inclusive():
    """Exactly FLOOR passes; one below does not. Pinned because an off-by-one
    here silently disables the guard for the thinnest states."""
    counts = _healthy()
    counts["VT"] = FLOOR
    assert _violations(_rows(counts), {}) == []
    counts["VT"] = FLOOR - 1
    assert len(_violations(_rows(counts), {})) == 1


def test_every_state_missing_is_reported_per_state():
    """A scan that returns nothing at all should name every state, not one."""
    problems = _violations([], {})
    assert len(problems) == len(_STATES)


# ------------------------------------------- self-calibrating drop vs history

def test_a_collapse_against_its_own_history_is_flagged():
    counts = _healthy()
    counts["CA"] = 4                      # above the floor, so only the ratio catches it
    problems = _violations(_rows(counts), {**{s: 50 for s in _STATES}, "CA": 100})
    assert len(problems) == 1
    assert "drop from" in problems[0] and problems[0].startswith("CA:")


def test_the_drop_boundary_is_half_of_the_previous_count():
    prev = {s: 50 for s in _STATES}
    counts = _healthy()
    counts["TX"] = int(50 * RATIO)        # exactly half -> not a violation
    assert _violations(_rows(counts), prev) == []
    counts["TX"] = int(50 * RATIO) - 1
    assert len(_violations(_rows(counts), prev)) == 1


def test_a_thin_baseline_is_too_little_signal_to_judge():
    """Below BASELINE rows of history, a swing is noise, not evidence — the
    ratio check must stay quiet or it fires forever on small states."""
    prev = {s: 50 for s in _STATES} | {"DC": BASELINE - 1}
    counts = _healthy()
    counts["DC"] = FLOOR                  # a huge relative drop, tiny absolute one
    assert _violations(_rows(counts), prev) == []


# ------------------------------------------------------------- normalisation

def test_rows_outside_the_us_are_not_counted_as_states():
    """Foreign or junk state values must not be able to satisfy a US state's
    floor — otherwise a mis-filtered scan looks full while being wrong."""
    counts = _healthy()
    counts["ZZ"] = 5000
    counts["MT"] = 0
    problems = _violations(_rows(counts), {})
    assert len(problems) == 1 and problems[0].startswith("MT:")


def test_state_values_are_normalised_before_counting():
    counts = {s: 50 for s in _STATES if s != "ca"}
    counts["ca"] = 50                     # lowercase from upstream
    assert _violations(_rows(counts), {}) == []
