"""Conflate decision-gate tests (scripts/conflate_gate.py).

The gate exists to size the full-US conflation BEFORE running it, so it is
only worth anything if its band counts match `run_conflate()`'s own
thresholds exactly. These tests pin that: canned pairs engineered to land in
each band, measured through the real scorer against SCRATCH staging clones
(live staging/core are never touched).

Also pinned: the honest-refusal path (one side empty -> error, not a
fabricated zero) and that the row filter is genuinely inherited from the
production staging statement rather than retyped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.conftest import needs_db
from tests.test_businesses_pipeline import _ins_fsq, _ins_overture
from truckintel.db import get_conn

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "scratch_conflate_gate_test"


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "conflate_gate", REPO_ROOT / "scripts" / "conflate_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


# The insert helpers are written against test_businesses_pipeline's SCHEMA
# constant, so point them at ours for the duration of these tests.
class _SchemaSwap:
    def __enter__(self):
        import tests.test_businesses_pipeline as bpt
        self._mod, self._old = bpt, bpt.SCHEMA
        bpt.SCHEMA = SCHEMA
        return self

    def __exit__(self, *exc):
        self._mod.SCHEMA = self._old
        return False


def _mk_scratch(conn):
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute(f"CREATE TABLE {SCHEMA}.overture_places "
                 "(LIKE staging.overture_places INCLUDING ALL)")
    conn.execute(f"CREATE TABLE {SCHEMA}.fsq_places "
                 "(LIKE staging.fsq_places INCLUDING ALL)")


def _measure_scratch():
    return gate.measure(staging_overture=f"{SCHEMA}.overture_places",
                        staging_fsq=f"{SCHEMA}.fsq_places",
                        progress_every=0)


def test_where_clause_is_inherited_not_retyped():
    """A measurement that filters differently from production measures the
    wrong population — so the filter must come FROM the production statement."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import businesses_pipeline as bp

    assert gate._PROD_WHERE in bp._STAGE_TEMP_SQL
    assert "category IS NOT NULL" in gate._PROD_WHERE
    assert "{closed_guard}" in gate._PROD_WHERE
    # and the light table must still carry every column _PAIRS_SQL reads
    for col in ("name_norm", "brand", "phone", "address", "g", "rid"):
        assert col in gate._LIGHT_TEMP_SQL


@needs_db
def test_refuses_to_measure_when_one_side_is_empty():
    """FSQ empty is exactly today's state. The gate must say so, not report a
    confident zero — a zero here would read as 'safe to run'."""
    with get_conn() as conn:
        _mk_scratch(conn)
        with _SchemaSwap():
            _ins_overture(conn, "o1", "Loves Travel Stop", "truck_stop",
                          39.7, -104.9)
        conn.commit()
    try:
        m = _measure_scratch()
        assert "error" in m
        assert m["_bo"] == 1 and m["_bf"] == 0
        assert "verdict" not in m          # never guesses a verdict blind
    finally:
        with get_conn() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@needs_db
def test_band_counts_match_the_conflate_thresholds():
    """One engineered pair per band, measured through the real scorer."""
    with get_conn() as conn:
        _mk_scratch(conn)
        with _SchemaSwap():
            # merge band: identical name, ~metres apart, same phone
            _ins_overture(conn, "o1", "Loves Travel Stop 291", "truck_stop",
                          39.70000, -104.90000, phone="+1 303 555 0101")
            _ins_fsq(conn, "f1", "Loves Travel Stop 291", "truck_stop",
                     39.70002, -104.90000, phone="3035550101")
            # gray band: similar-but-not-equal name, no corroborating signal
            _ins_overture(conn, "o2", "Bobs Truck Repair Center", "truck_repair",
                          40.10000, -105.10000)
            _ins_fsq(conn, "f2", "Bobs Truck Repair", "truck_repair",
                     40.10060, -105.10000)
            # far apart -> never blocked at all (150 m radius)
            _ins_overture(conn, "o3", "Zenith Tire Service", "tire_service",
                          41.00000, -106.00000)
            _ins_fsq(conn, "f3", "Zenith Tire Service", "tire_service",
                     41.50000, -106.50000)
        conn.commit()
    try:
        m = _measure_scratch()
        assert m["_bo"] == 3 and m["_bf"] == 3
        # the distant pair is excluded by the blocking join, not by the scorer
        assert m["blocked_pairs"] == m["merge_band_pairs"] \
            + m["gray_band_pairs"] + m["distinct_band_pairs"]
        assert m["merge_band_pairs"] == 1, m
        assert m["gray_band_pairs"] == 1, m
        # distinct gray ids are what get sent as the SQL array parameter
        assert m["gray_ids_overture"] == 1
        assert m["gray_ids_fsq"] == 1
        assert m["ram_pressure_units"] == 3      # 1 pair + 1 + 1 gray ids
        assert m["verdict"] == "run_as_is"
    finally:
        with get_conn() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@needs_db
def test_gray_ids_deduplicate_across_pairs():
    """One Overture row ambiguously near TWO FSQ rows is 2 gray pairs but only
    1 gray id — the array-parameter size is the distinct count, and conflating
    the two would overstate the RAM risk."""
    with get_conn() as conn:
        _mk_scratch(conn)
        with _SchemaSwap():
            _ins_overture(conn, "o1", "Bobs Truck Repair Center",
                          "truck_repair", 40.10000, -105.10000)
            _ins_fsq(conn, "f1", "Bobs Truck Repair", "truck_repair",
                     40.10060, -105.10000)
            _ins_fsq(conn, "f2", "Bobs Truck Repair", "truck_repair",
                     40.09940, -105.10000)
        conn.commit()
    try:
        m = _measure_scratch()
        assert m["gray_band_pairs"] == 2, m
        assert m["gray_ids_overture"] == 1, m
        assert m["gray_ids_fsq"] == 2, m
    finally:
        with get_conn() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@needs_db
def test_batch_flush_survives_more_rows_than_one_copy_batch(monkeypatch):
    """The gray buffer flushes mid-stream via COPY while a named cursor is
    open. Shrink the batch so the flush path actually runs more than once —
    a single COPY that silently swallowed later batches would still produce
    plausible-looking counts."""
    monkeypatch.setattr(gate, "_COPY_BATCH", 2)
    with get_conn() as conn:
        _mk_scratch(conn)
        with _SchemaSwap():
            for i in range(6):
                lat = 40.10000 + i * 0.01
                _ins_overture(conn, f"o{i}", "Bobs Truck Repair Center",
                              "truck_repair", lat, -105.10000)
                _ins_fsq(conn, f"f{i}", "Bobs Truck Repair", "truck_repair",
                         lat + 0.0006, -105.10000)
        conn.commit()
    try:
        m = _measure_scratch()
        assert m["gray_band_pairs"] == 6, m
        assert m["gray_ids_overture"] == 6, m
        assert m["gray_ids_fsq"] == 6, m
    finally:
        with get_conn() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_verdict_flips_at_the_advice_threshold():
    """Pure check on the decision rule itself — no DB needed."""
    assert gate.SPILL_ADVICE_THRESHOLD == 5_000_000
