"""Loader tests against the live dev DB, entirely inside a scratch schema built
with LIKE core.* INCLUDING ALL — core tables are never written to or dropped.
"""
from __future__ import annotations

import pytest

from tests.conftest import needs_db
from truckintel.db import get_conn
from truckintel.loaders import (
    EmptyPublishRefused,
    event_lifecycle_upsert,
    fuel_upsert,
    snapshot_swap,
)

SCHEMA = "scratch_loader_test"
SRC = "test_loader_src"

pytestmark = needs_db


@pytest.fixture
def scratch():
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.sites (LIKE core.parking_sites INCLUDING ALL)")
        conn.execute(f"CREATE TABLE {SCHEMA}.events (LIKE core.live_events INCLUDING ALL)")
        conn.execute(f"CREATE TABLE {SCHEMA}.fuel (LIKE core.fuel_prices INCLUDING ALL)")
    yield
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _site(site_id: str, spaces: int | None = 25) -> dict:
    return {
        "site_id": site_id,
        "kind": "truck_stop",
        "name": f"Stop {site_id}",
        "state": "PA",
        "truck_spaces": spaces,
        "lat": 40.1,
        "lon": -75.2,
        "observed_at": "2019-06-01",
        "props": {"survey": "jasons_law_2019"},
    }


# ---------------------------------------------------------------- snapshot_swap

def test_snapshot_swap_publishes_with_lineage_and_geom(scratch):
    with get_conn() as conn:
        n = snapshot_swap(conn, f"{SCHEMA}.sites", [_site("a"), _site("b", spaces=None)],
                          source_id=SRC, run_id=7)
    assert n == 2
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT site_id, source_id, run_id, truck_spaces, "
            f"       round(ST_X(geom)::numeric, 2), round(ST_Y(geom)::numeric, 2), "
            f"       props->>'survey', ingested_at, observed_at "
            f"FROM {SCHEMA}.sites ORDER BY site_id"
        ).fetchall()
    assert len(rows) == 2
    a, b = rows
    assert a[0] == "a" and a[1] == SRC and a[2] == 7
    assert float(a[4]) == -75.20 and float(a[5]) == 40.10
    assert a[6] == "jasons_law_2019"
    assert a[7] is not None and a[8] is not None
    assert b[3] is None  # None capacity stays NULL — never coerced to 0


def test_snapshot_swap_fully_replaces_previous_snapshot(scratch):
    with get_conn() as conn:
        snapshot_swap(conn, f"{SCHEMA}.sites", [_site("a"), _site("b")],
                      source_id=SRC, run_id=1)
    with get_conn() as conn:
        snapshot_swap(conn, f"{SCHEMA}.sites", [_site("c")], source_id=SRC, run_id=2)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT site_id, run_id FROM {SCHEMA}.sites").fetchall()
    assert rows == [("c", 2)]


def test_snapshot_swap_failure_never_touches_live_table(scratch):
    with get_conn() as conn:
        snapshot_swap(conn, f"{SCHEMA}.sites", [_site("a")], source_id=SRC, run_id=1)
    bad = _site("boom")
    bad["site_id"] = None  # violates the PK -> load must fail
    with pytest.raises(Exception):
        with get_conn() as conn:
            snapshot_swap(conn, f"{SCHEMA}.sites", [_site("x"), bad],
                          source_id=SRC, run_id=2)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT site_id, run_id FROM {SCHEMA}.sites").fetchall()
    assert rows == [("a", 1)]  # old snapshot still live, untouched


def test_snapshot_swap_refuses_to_publish_nothing_over_live_data(scratch):
    """The 2026-07-23 failure mode: a load that yields no rows swapped an EMPTY
    table over the live one and returned 0, which callers recorded as success.
    A truncated upstream file or an aborted spool must never delete a dataset
    and report that it worked."""
    with get_conn() as conn:
        snapshot_swap(conn, f"{SCHEMA}.sites", [_site("a"), _site("b")],
                      source_id=SRC, run_id=1)
    with pytest.raises(EmptyPublishRefused):
        with get_conn() as conn:
            snapshot_swap(conn, f"{SCHEMA}.sites", iter([]),
                          source_id=SRC, run_id=2)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT site_id, run_id FROM {SCHEMA}.sites ORDER BY site_id"
        ).fetchall()
    assert rows == [("a", 1), ("b", 1)]  # live snapshot untouched


def test_snapshot_swap_refuses_below_an_explicit_floor(scratch):
    """A partial load is as destructive as an empty one — the floor is what
    separates 'the upstream shrank' from 'the upstream broke'."""
    with get_conn() as conn:
        snapshot_swap(conn, f"{SCHEMA}.sites", [_site(str(i)) for i in range(5)],
                      source_id=SRC, run_id=1)
    with pytest.raises(EmptyPublishRefused) as excinfo:
        with get_conn() as conn:
            snapshot_swap(conn, f"{SCHEMA}.sites", [_site("lonely")],
                          source_id=SRC, run_id=2, min_rows=4)
    assert "min_rows floor of 4" in str(excinfo.value)
    with get_conn() as conn:
        n = conn.execute(f"SELECT count(*) FROM {SCHEMA}.sites").fetchone()[0]
    assert n == 5


def test_snapshot_swap_allows_empty_publish_when_asked_explicitly(scratch):
    """min_rows=0 is the documented opt-out for sources where publishing
    nothing is a real state — it must still work, or callers will reach for
    the guard-removing fix instead."""
    with get_conn() as conn:
        snapshot_swap(conn, f"{SCHEMA}.sites", [_site("a")],
                      source_id=SRC, run_id=1)
    with get_conn() as conn:
        assert snapshot_swap(conn, f"{SCHEMA}.sites", iter([]),
                             source_id=SRC, run_id=2, min_rows=0) == 0
    with get_conn() as conn:
        assert conn.execute(f"SELECT count(*) FROM {SCHEMA}.sites").fetchone()[0] == 0


def test_snapshot_swap_rejects_unqualified_target(scratch):
    with get_conn() as conn:
        with pytest.raises(ValueError):
            snapshot_swap(conn, "sites", [], source_id=SRC, run_id=1)


def test_snapshot_swap_preserves_index_and_constraint_names(scratch):
    """LIKE ... INCLUDING ALL + RENAME must not leave *_new-named indexes on
    the live table — schema.sql's idempotent re-apply depends on the names."""
    def names() -> set[str]:
        with get_conn() as conn:
            return {r[0] for r in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = %s AND tablename = 'sites'", (SCHEMA,),
            ).fetchall()}

    before = names()
    assert before  # LIKE core.parking_sites copied at least the PK + GIST
    for run_id in (1, 2):  # two swaps: names must survive repeated swaps
        with get_conn() as conn:
            snapshot_swap(conn, f"{SCHEMA}.sites", [_site("a")],
                          source_id=SRC, run_id=run_id)
    assert names() == before
    with get_conn() as conn:
        con_names = {r[0] for r in conn.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = %s::regclass", (f"{SCHEMA}.sites",),
        ).fetchall()}
    assert not {n for n in con_names if "_new" in n}


# ---------------------------------------------------------------- event_lifecycle

POLY = "POLYGON((-75 40,-74 40,-74 41,-75 41,-75 40))"


def _event(event_id: str, geom_wkt: str | None = POLY) -> dict:
    return {
        "event_id": event_id,
        "kind": "weather_alert",
        "geom_wkt": geom_wkt,
        "observed_at": "2026-07-22T06:00:00+00:00",
        "props": {"severity": "Severe"},
    }


def test_event_lifecycle_insert_softclose_reopen(scratch):
    target = f"{SCHEMA}.events"
    # poll 1: two events, one with an honest NULL geometry
    with get_conn() as conn:
        n = event_lifecycle_upsert(conn, [_event("a"), _event("b", geom_wkt=None)],
                                   source_id=SRC, run_id=1, target=target)
    assert n == 2
    with get_conn() as conn:
        rows = dict(conn.execute(
            f"SELECT event_id, soft_closed_at FROM {SCHEMA}.events"
        ).fetchall())
        geom_null = conn.execute(
            f"SELECT geom IS NULL FROM {SCHEMA}.events WHERE event_id = 'b'"
        ).fetchone()[0]
    assert rows == {"a": None, "b": None} and geom_null is True

    # poll 2: b vanished -> soft-closed, never deleted; a's last_seen refreshed
    with get_conn() as conn:
        event_lifecycle_upsert(conn, [_event("a")], source_id=SRC, run_id=2, target=target)
    with get_conn() as conn:
        a_closed, a_bumped = conn.execute(
            f"SELECT soft_closed_at IS NULL, last_seen > first_seen "
            f"FROM {SCHEMA}.events WHERE event_id = 'a'"
        ).fetchone()
        b_closed = conn.execute(
            f"SELECT soft_closed_at IS NOT NULL FROM {SCHEMA}.events WHERE event_id = 'b'"
        ).fetchone()[0]
    assert a_closed is True and a_bumped is True and b_closed is True

    # poll 3: b reappears -> reopened; a soft-closes; row count still 2 (no deletes)
    with get_conn() as conn:
        event_lifecycle_upsert(conn, [_event("b")], source_id=SRC, run_id=3, target=target)
    with get_conn() as conn:
        b_active = conn.execute(
            f"SELECT soft_closed_at IS NULL FROM {SCHEMA}.events WHERE event_id = 'b'"
        ).fetchone()[0]
        total = conn.execute(f"SELECT count(*) FROM {SCHEMA}.events").fetchone()[0]
    assert b_active is True and total == 2


def test_event_lifecycle_empty_poll_closes_all(scratch):
    target = f"{SCHEMA}.events"
    with get_conn() as conn:
        event_lifecycle_upsert(conn, [_event("a")], source_id=SRC, run_id=1, target=target)
    with get_conn() as conn:
        n = event_lifecycle_upsert(conn, [], source_id=SRC, run_id=2, target=target)
    assert n == 0
    with get_conn() as conn:
        open_count = conn.execute(
            f"SELECT count(*) FROM {SCHEMA}.events WHERE soft_closed_at IS NULL"
        ).fetchone()[0]
    assert open_count == 0


# ---------------------------------------------------------------- fuel_upsert

def test_fuel_upsert_is_idempotent_time_series(scratch):
    target = f"{SCHEMA}.fuel"
    week = {
        "region": "US", "product": "diesel", "week_of": "2026-07-20",
        "price_usd_gal": 3.755, "observed_at": "2026-07-20",
        "props": {"series": "EMD_EPD2D_PTE_NUS_DPG"},
    }
    with get_conn() as conn:
        assert fuel_upsert(conn, [week], source_id=SRC, run_id=1, target=target) == 1
    # re-fetch of the same week with a revised price: still one row, updated
    with get_conn() as conn:
        assert fuel_upsert(conn, [{**week, "price_usd_gal": 3.801}],
                           source_id=SRC, run_id=2, target=target) == 1
    # a second week accumulates history
    with get_conn() as conn:
        fuel_upsert(conn, [{**week, "week_of": "2026-07-27", "observed_at": "2026-07-27"}],
                    source_id=SRC, run_id=3, target=target)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT week_of::text, price_usd_gal::float, run_id "
            f"FROM {SCHEMA}.fuel ORDER BY week_of"
        ).fetchall()
    assert rows == [("2026-07-20", 3.801, 2), ("2026-07-27", 3.755, 3)]
