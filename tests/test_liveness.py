"""Gate 6 tests — truckintel/liveness.py.

Formula tests are pure, with hand-computed expected values. The DB-backed
tests follow the test_quality.py pattern: everything happens in a scratch
schema built with LIKE core.* INCLUDING ALL, so core.* and quality.* are
never written to, and every ops row the tests create is deleted again.

The cases worth naming, because each one encodes a decision that was argued
rather than assumed:

  test_absence_is_not_closure       a place nobody has confirmed since 2019 is
                                    'unknown', never 'closed'
  test_null_last_seen_scores_zero   an unknown vintage is never charitable
  test_chain_lifts_an_old_row       the whole point of core.chain_sites
  test_single_source_cannot_reach_open
                                    corroboration, not freshness, is the
                                    binding constraint — this is why 98% of
                                    parking sits at 'unknown'
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import needs_db
from truckintel.db import get_conn
from truckintel.liveness import (
    CHAIN_SOURCE_ID,
    HALF_LIFE_DAYS,
    TABLE_LIVENESS,
    bucket,
    compute_liveness,
    corroboration,
    presence_decay,
    refresh_chain_presence,
    refresh_presence,
    rescore_liveness,
    source_breadth,
)

NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)
SCHEMA = "scratch_liveness_test"


# ---------------------------------------------------------------- pure: parts

def test_presence_decay_is_one_at_zero_age():
    assert presence_decay(NOW, 365.0, now=NOW) == pytest.approx(1.0)


def test_presence_decay_halves_at_one_half_life():
    seen = NOW - timedelta(days=365)
    assert presence_decay(seen, 365.0, now=NOW) == pytest.approx(0.5)


def test_null_last_seen_scores_zero():
    """No recorded sighting is not 'probably fine'. Same rule as Gate 5's
    NULL observed_at: an unknown vintage never scores as fresh."""
    assert presence_decay(None, 365.0, now=NOW) == 0.0


def test_future_last_seen_clamps_to_one():
    ahead = NOW + timedelta(days=90)
    assert presence_decay(ahead, 365.0, now=NOW) == pytest.approx(1.0)


def test_source_breadth_saturates():
    assert source_breadth(0) == 0.0
    assert source_breadth(1) == 0.5
    assert source_breadth(2) == 0.8
    assert source_breadth(3) == 1.0
    assert source_breadth(9) == 1.0, "a ninth aggregator is not new information"


def test_source_breadth_rejects_negative():
    with pytest.raises(ValueError):
        source_breadth(-1)


def test_corroboration_is_binary():
    assert corroboration() == 0.0
    assert corroboration(chain_confirmed=True) == 1.0
    assert corroboration(licence_active=True) == 1.0


# ------------------------------------------------------------- pure: formula

def test_formula_matches_hand_computation():
    # 0.50*1.0 + 0.30*0.8 + 0.20*1.0 = 0.94
    s = compute_liveness(1.0, 0.8, 1.0)
    assert s.liveness == 94
    assert (s.live_presence, s.live_sources, s.live_corrob) == (100, 80, 100)
    assert s.live_state == "open"


def test_missing_penalty_applies():
    # 0.50*1.0 + 0.30*0.5 + 0.20*0.0 - 0.30 = 0.35
    assert compute_liveness(1.0, 0.5, 0.0, missing=True).liveness == 35


def test_licence_expired_penalty_applies():
    # 0.50*1.0 + 0.30*0.5 + 0.20*0.0 - 0.25 = 0.40
    assert compute_liveness(1.0, 0.5, 0.0, licence_expired=True).liveness == 40


def test_penalties_stack_and_clamp_at_zero():
    assert compute_liveness(0.0, 0.0, 0.0, missing=True,
                            licence_expired=True).liveness == 0


def test_single_source_cannot_reach_open():
    """The binding constraint is corroboration, not freshness.

    A row seen TODAY by exactly one source, with nothing independent backing
    it, tops out at 0.50 + 0.15 = 65 — 'likely_open'. Reaching 'open' requires
    a second witness. This is deliberate, and it is why 98% of
    core.parking_sites reads 'unknown' rather than being tuned upward: the
    honest fix is another source, not a softer curve.
    """
    best = compute_liveness(1.0, source_breadth(1), 0.0)
    assert best.liveness == 65
    assert best.live_state == "likely_open"


def test_absence_is_not_closure():
    """A 2019 rest area with no current corroboration. Decayed hard, but the
    verdict is 'unknown' — we do not know — never 'closed'. Routing a driver
    away from parking that is actually open is also a failure."""
    p = presence_decay(datetime(2019, 1, 1, tzinfo=timezone.utc),
                       3650.0, now=NOW)
    s = compute_liveness(p, source_breadth(1), 0.0)
    assert s.live_state == "unknown"
    assert s.liveness > 0


def test_chain_lifts_an_old_row():
    """core.chain_sites earns its existence here: the same 2019 row, once the
    operator's own store locator confirms the site, clears 'open'."""
    p_old = presence_decay(datetime(2019, 1, 1, tzinfo=timezone.utc),
                           3650.0, now=NOW)
    without = compute_liveness(p_old, source_breadth(1), 0.0)
    # The chain is a second witness AND a current one, so both S and P move.
    with_chain = compute_liveness(1.0, source_breadth(2), 1.0)
    assert without.live_state == "unknown"
    assert with_chain.live_state == "open"
    assert with_chain.liveness > without.liveness


def test_closed_assertion_overrides_everything():
    """A source that positively says 'closed' outranks any amount of decay
    arithmetic — including a perfect score in every component."""
    s = compute_liveness(1.0, 1.0, 1.0, closed_asserted=True)
    assert s.liveness == 0
    assert s.live_state == "closed"
    # Components are still stored: the row must explain why it was overridden.
    assert s.live_presence == 100


@pytest.mark.parametrize("score,expected", [
    (100, "open"), (75, "open"), (74, "likely_open"), (50, "likely_open"),
    (49, "unknown"), (25, "unknown"), (24, "likely_closed"), (0, "likely_closed"),
])
def test_bucket_boundaries(score, expected):
    assert bucket(score) == expected


def test_every_configured_table_has_a_half_life():
    for name, cfg in TABLE_LIVENESS.items():
        assert cfg.half_life_days > 0 or cfg.half_life_sql
        assert cfg.presence_sources_sql
        assert cfg.entity_type == name


def test_half_life_ordering_encodes_turnover():
    """Restaurants churn faster than shops, which churn faster than travel
    centres. If this ever inverts, someone tuned a number without the model."""
    assert (HALF_LIFE_DAYS["businesses"]
            < HALF_LIFE_DAYS["mechanic_shops"]
            < HALF_LIFE_DAYS["parking_sites"])


# --------------------------------------------------------------- DB-backed

@pytest.fixture()
def scratch():
    """A scratch copy of core.parking_sites + the real quality.presence rows
    scoped to a test-only entity_type, torn down afterwards."""
    etype = "test_parking"
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(
            f"CREATE TABLE {SCHEMA}.parking_sites "
            f"(LIKE core.parking_sites INCLUDING ALL)")
        conn.execute(f"DELETE FROM quality.presence WHERE entity_type = '{etype}'")
    yield SCHEMA, etype
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"DELETE FROM quality.presence WHERE entity_type = '{etype}'")


@needs_db
def test_rescore_marks_uncorroborated_rows_unknown(scratch):
    schema, etype = scratch
    import dataclasses
    cfg = dataclasses.replace(
        TABLE_LIVENESS["parking_sites"],
        entity_type=etype, table=f"{schema}.parking_sites")
    with get_conn() as conn:
        conn.execute(f"""
            INSERT INTO {schema}.parking_sites
                (site_id, kind, name, geom, source_id, run_id, observed_at)
            VALUES ('t1', 'public_rest_area', 'Old Rest Area',
                    ST_SetSRID(ST_MakePoint(-97.0, 35.0), 4326),
                    'ntad_parking', 1, '2019-01-01Z')
        """)
        seen, gone = refresh_presence(conn, cfg)
        assert seen == 1 and gone == 0
        assert rescore_liveness(conn, cfg) == 1
        state, score, reasons = conn.execute(
            f"SELECT live_state, liveness, live_reasons "
            f"FROM {schema}.parking_sites WHERE site_id = 't1'").fetchone()
    assert state == "unknown", "a 2019 row with one source is unknown, not closed"
    assert 0 < score < 50
    assert "closure_asserted" not in reasons


@needs_db
def test_chain_presence_promotes_a_matched_site(scratch):
    """End to end: the same stale row, with a chain site 100 m away, becomes
    'open' and says why."""
    schema, etype = scratch
    import dataclasses
    cfg = dataclasses.replace(
        TABLE_LIVENESS["parking_sites"],
        entity_type=etype, table=f"{schema}.parking_sites")
    with get_conn() as conn:
        conn.execute(f"""
            INSERT INTO {schema}.parking_sites
                (site_id, kind, name, geom, source_id, run_id, observed_at)
            VALUES ('t2', 'truck_stop', 'Stale Stop',
                    ST_SetSRID(ST_MakePoint(-97.0, 35.0), 4326),
                    'ntad_parking', 1, '2019-01-01Z')
        """)
        conn.execute("""
            INSERT INTO core.chain_sites
                (chain_site_id, spider, brand, name, lat, lon, geom,
                 observed_at, run_id)
            VALUES ('test_spider:zz', 'test_spider', 'TestBrand', 'Test Stop',
                    35.0009, -97.0, ST_SetSRID(ST_MakePoint(-97.0, 35.0009), 4326),
                    now(), 1)
            ON CONFLICT (chain_site_id) DO NOTHING
        """)
        try:
            refresh_presence(conn, cfg)
            assert refresh_chain_presence(conn, cfg) == 1
            rescore_liveness(conn, cfg)
            state, score, reasons, src = conn.execute(
                f"SELECT live_state, liveness, live_reasons, last_seen_src "
                f"FROM {schema}.parking_sites WHERE site_id = 't2'").fetchone()
        finally:
            conn.execute("DELETE FROM core.chain_sites "
                         "WHERE chain_site_id = 'test_spider:zz'")
    assert state == "open"
    assert score >= 75
    assert src == CHAIN_SOURCE_ID
    assert any(r.startswith("chain_confirmed:") for r in reasons)


@needs_db
def test_vanished_row_is_stamped_not_deleted(scratch):
    """The disappearance IS the signal. Deleting the ledger row would throw
    away the only free closure evidence a snapshot_swap load produces."""
    schema, etype = scratch
    import dataclasses
    cfg = dataclasses.replace(
        TABLE_LIVENESS["parking_sites"],
        entity_type=etype, table=f"{schema}.parking_sites")
    with get_conn() as conn:
        conn.execute(f"""
            INSERT INTO {schema}.parking_sites
                (site_id, kind, name, geom, source_id, run_id, observed_at)
            VALUES ('t3', 'truck_stop', 'Doomed',
                    ST_SetSRID(ST_MakePoint(-96.0, 34.0), 4326),
                    'ntad_parking', 1, now())
        """)
        refresh_presence(conn, cfg)
        # The next upstream pull no longer carries it.
        conn.execute(f"DELETE FROM {schema}.parking_sites WHERE site_id = 't3'")
        _, gone = refresh_presence(conn, cfg)
        row = conn.execute(
            "SELECT missing_since FROM quality.presence "
            "WHERE entity_type = %s AND entity_id = 't3'", (etype,)).fetchone()
    assert gone == 1
    assert row is not None and row[0] is not None


@needs_db
def test_reappearance_clears_missing_since(scratch):
    """Upstream extracts wobble. One bad pull is not a closure."""
    schema, etype = scratch
    import dataclasses
    cfg = dataclasses.replace(
        TABLE_LIVENESS["parking_sites"],
        entity_type=etype, table=f"{schema}.parking_sites")
    ins = f"""
        INSERT INTO {schema}.parking_sites
            (site_id, kind, name, geom, source_id, run_id, observed_at)
        VALUES ('t4', 'truck_stop', 'Flaky',
                ST_SetSRID(ST_MakePoint(-95.0, 33.0), 4326),
                'ntad_parking', 1, now())
    """
    with get_conn() as conn:
        conn.execute(ins)
        refresh_presence(conn, cfg)
        conn.execute(f"DELETE FROM {schema}.parking_sites WHERE site_id = 't4'")
        refresh_presence(conn, cfg)
        conn.execute(ins)          # it comes back
        refresh_presence(conn, cfg)
        missing = conn.execute(
            "SELECT missing_since FROM quality.presence "
            "WHERE entity_type = %s AND entity_id = 't4'", (etype,)).fetchone()[0]
    assert missing is None
