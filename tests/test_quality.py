"""Quality ladder tests — gates 3-5 (truckintel/quality.py) and the nightly
runner (scripts/quality_nightly.py).

Formula tests are pure (hand-computed expected values, incl. the worked
examples from design/quality-ai.md §9). DB-backed tests follow the
test_loaders.py pattern: everything happens in a scratch schema built with
LIKE core.* / LIKE quality.* INCLUDING ALL — core and quality tables are
never written to; the runner tests delete every ops row they create.
"""
from __future__ import annotations

import dataclasses
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import needs_db
from truckintel.db import get_conn
from truckintel.quality import (
    REGISTERED_CHECKS,
    TABLE_SCORING,
    agreement,
    completeness,
    compute_confidence,
    dedup,
    fallback_trust,
    freshness,
    rescore_table,
    run_gate4,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "scratch_quality_test"
SRC = "test_quality_src"


def _load_nightly():
    """scripts/ is not a package — load the runner by path."""
    spec = importlib.util.spec_from_file_location(
        "quality_nightly", REPO_ROOT / "scripts" / "quality_nightly.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- gate 5: formula

def test_formula_worked_example_a_nbi_bridge():
    # quality-ai.md §9 example A: fresh complete corroborated NBI bridge.
    s = compute_confidence(0.95, 0.88, 1.0, 1.0)
    assert s.confidence == 95
    assert (s.conf_trust, s.conf_fresh, s.conf_complete, s.conf_agree) == (
        95, 88, 100, 100)


def test_formula_worked_example_b_osm_only_station():
    # §9 example B: OSM-only fuel station, aging, sparse, single-source.
    assert compute_confidence(0.55, 0.68, 0.6, 0.5).confidence == 58


def test_formula_worked_example_c_open_conflict():
    # §9 example C: same bridge as A but with one OPEN conflict: A=0 AND the
    # 0.10 penalty stack deliberately.
    s = compute_confidence(0.95, 0.88, 1.0, 0.0, open_conflicts=1)
    assert s.confidence == 65
    assert s.conf_agree == 0


def test_formula_conflict_penalty_caps_at_020():
    # 5 open conflicts: penalty min(0.20, 0.50) = 0.20.
    # 0.35 + 0.25 + 0.20 + 0 - 0.20 = 0.60
    assert compute_confidence(1.0, 1.0, 1.0, 0.0, open_conflicts=5).confidence == 60


def test_formula_geo_penalty_stacks_with_conflicts():
    # 0.35 + 0.25 + 0.20 + 0 - 0.15 - 0.20 = 0.45
    s = compute_confidence(1.0, 1.0, 1.0, 0.0, geo_suspect=True, open_conflicts=3)
    assert s.confidence == 45


def test_formula_clamps_to_zero_and_hundred():
    assert compute_confidence(0.0, 0.0, 0.0, 0.0, geo_suspect=True).confidence == 0
    assert compute_confidence(1.0, 1.0, 1.0, 1.0).confidence == 100


def test_component_rounding_is_half_away_from_zero():
    # 0.125 * 100 = 12.5 exactly (binary-representable) -> 13, matching SQL
    # round(numeric); Python's built-in banker's round() would give 12.
    assert compute_confidence(0.125, 0.0, 0.0, 0.0).conf_trust == 13


def test_freshness_decay_and_unknown_vintage():
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    assert freshness(None, 548.0, now=now) == 0.0          # unknown ≠ fresh
    assert freshness(now, 548.0, now=now) == 1.0
    assert freshness(now - timedelta(days=548), 548.0, now=now) == pytest.approx(0.5)
    assert freshness(now + timedelta(days=9), 548.0, now=now) == 1.0  # future clamps


def test_completeness_weighted_fill():
    manifest = {"a": 3, "b": 1}
    assert completeness({"a": 1, "b": 1}, manifest) == 1.0
    assert completeness({"a": 1, "b": None}, manifest) == 0.75
    assert completeness({}, manifest) == 0.0
    assert completeness({}, {}) == 1.0  # nothing demanded -> vacuously complete


def test_agreement_wave1_semantics():
    assert agreement(0) == 0.5                      # single-source baseline
    assert agreement(2) == 0.0                      # any open conflict wins
    assert agreement(0, corroborated=True) == 1.0   # reachable in wave 2


def test_fallback_trust_authority_classes():
    assert fallback_trust("nbi_annual") == 0.95
    assert fallback_trust("wzdx_az") == 0.90
    assert fallback_trust("osm_ways") == 0.55
    assert fallback_trust("mystery_feed") == 0.55   # conservative floor


# ---------------------------------------------------------------- gate 3: dedup

def test_dedup_keeps_last_rejects_earlier():
    # quality-ai.md §3.1: within-file duplicates keep the LAST — an upstream
    # file appending a corrected record must publish the correction, not the
    # stale first value. Output keeps the first occurrence's position.
    rows = [{"id": "a", "v": 1}, {"id": "b", "v": 2},
            {"id": "a", "v": 3}, {"id": "a", "v": 4}]
    ok, rejects = dedup(rows, "id")
    assert [r["v"] for r in ok] == [4, 2]           # last occurrence wins
    assert len(rejects) == 2
    assert all(r["reason"] == "duplicate_natural_key" for r in rejects)
    assert [r["raw_record"]["v"] for r in rejects] == [1, 3]  # superseded rows


def test_dedup_missing_key_passes_through():
    # Missing/None natural key is gate 1's rejection, not a duplicate —
    # two keyless rows must not be collapsed onto each other.
    rows = [{"id": None}, {"v": 1}, {"id": "a"}]
    ok, rejects = dedup(rows, "id")
    assert ok == rows and rejects == []


# ---------------------------------------------------------------- DB-backed

CONFLICTS = f"{SCHEMA}.conflicts"


@pytest.fixture
def scratch():
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        for name, like in (("bridges", "core.bridges"),
                           ("tunnels", "core.tunnels"),
                           ("parking", "core.parking_sites"),
                           ("events", "core.live_events"),
                           ("conflicts", "quality.conflicts")):
            conn.execute(f"CREATE TABLE {SCHEMA}.{name} (LIKE {like} INCLUDING ALL)")
    yield
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _scratch_scoring() -> dict:
    return {
        "bridges": dataclasses.replace(TABLE_SCORING["bridges"],
                                       table=f"{SCHEMA}.bridges"),
        "tunnels": dataclasses.replace(TABLE_SCORING["tunnels"],
                                       table=f"{SCHEMA}.tunnels"),
        "parking_sites": dataclasses.replace(TABLE_SCORING["parking_sites"],
                                             table=f"{SCHEMA}.parking"),
        "live_events": dataclasses.replace(TABLE_SCORING["live_events"],
                                           table=f"{SCHEMA}.events"),
    }


def _closed_check():
    """The production bridges_closed_vs_clearance check, retargeted at the
    scratch table — same SQL shape as what runs nightly."""
    check = REGISTERED_CHECKS[0]
    assert check.name == "bridges_closed_vs_clearance" and check.enabled
    return dataclasses.replace(
        check, pairs_sql=check.pairs_sql.replace("core.bridges", f"{SCHEMA}.bridges"))


def _insert_bridge(conn, nbi_id: str, *, posting: str | None = "A",
                   clearance: float | None = 168.0,
                   observed_at: datetime | None = None,
                   flags: list[str] | None = None,
                   full: bool = True) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.bridges "
        "  (nbi_id, name, state, geom, min_vert_clearance_in, operating_rating,"
        "   inventory_rating, posting_status, source_id, run_id, observed_at, flags) "
        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(-75.2, 40.1), 4326), %s, %s,"
        "        %s, %s, %s, 1, %s, %s)",
        (nbi_id,
         f"Bridge {nbi_id}" if full else None,
         "PA" if full else None,
         clearance,
         "32.6" if full else None,
         "19.9" if full else None,
         posting, SRC, observed_at, flags or []),
    )


# -------------------------------------------------- gate 4: conflicts writer

@needs_db
def test_gate4_opens_idempotently_and_autocloses(scratch):
    with get_conn() as conn:
        _insert_bridge(conn, "K1", posting="K", clearance=150.0)
        _insert_bridge(conn, "K2", posting="K", clearance=None)   # no contradiction
        _insert_bridge(conn, "A1", posting="A", clearance=170.0)  # open bridge, fine

    check = _closed_check()
    with get_conn() as conn:
        first = run_gate4(conn, [check], conflicts_table=CONFLICTS)
    assert first == {"bridges_closed_vs_clearance": (1, 0)}

    with get_conn() as conn:
        row = conn.execute(
            f"SELECT entity_type, entity_id, field, value_a, source_a, value_b,"
            f"       source_b, status FROM {CONFLICTS}"
        ).fetchone()
    assert row == ("bridges", "K1", "posting_status_vs_clearance",
                   "posting_status=K", "nbi_annual",
                   "min_vert_clearance_in=150.0", "nbi_annual", "open")

    # Re-check while still violating: idempotent, no duplicate opened.
    with get_conn() as conn:
        second = run_gate4(conn, [check], conflicts_table=CONFLICTS)
    assert second == {"bridges_closed_vs_clearance": (0, 0)}

    # Source data fixed -> the conflict auto-closes (§7.3), closed_at stamped.
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {SCHEMA}.bridges SET min_vert_clearance_in = NULL "
            "WHERE nbi_id = 'K1'")
    with get_conn() as conn:
        third = run_gate4(conn, [check], conflicts_table=CONFLICTS)
    assert third == {"bridges_closed_vs_clearance": (0, 1)}
    with get_conn() as conn:
        status, closed_at = conn.execute(
            f"SELECT status, closed_at FROM {CONFLICTS}").fetchone()
    assert status == "closed" and closed_at is not None


@needs_db
def test_gate4_disabled_checks_never_run(scratch):
    disabled = dataclasses.replace(_closed_check(), enabled=False)
    with get_conn() as conn:
        _insert_bridge(conn, "K1", posting="K", clearance=150.0)
        assert run_gate4(conn, [disabled], conflicts_table=CONFLICTS) == {}
        assert conn.execute(f"SELECT count(*) FROM {CONFLICTS}").fetchone()[0] == 0


def test_osm_check_registered_but_disabled_until_wave2():
    by_name = {c.name: c for c in REGISTERED_CHECKS}
    osm = by_name["bridges_nbi_vs_osm_maxheight"]
    assert osm.enabled is False           # osm.ways is empty until wave 2
    assert "osm.ways" in osm.pairs_sql
    enabled = [c for c in REGISTERED_CHECKS if c.enabled]
    assert [c.name for c in enabled] == ["bridges_closed_vs_clearance"]


# -------------------------------------------------- gate 5: SQL rescore

TRUST = {SRC: 0.95}


def _bridge_scores(conn, nbi_id: str):
    return conn.execute(
        f"SELECT confidence, conf_trust, conf_fresh, conf_complete, conf_agree,"
        f"       flags FROM {SCHEMA}.bridges WHERE nbi_id = %s", (nbi_id,),
    ).fetchone()


@needs_db
def test_rescore_bridges_matches_python_reference(scratch):
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        _insert_bridge(conn, "FULL", observed_at=now)
        _insert_bridge(conn, "NOVINTAGE", observed_at=None, clearance=None,
                       posting=None, full=False)
        _insert_bridge(conn, "OFFROAD", observed_at=now, flags=["offroad"])
        _insert_bridge(conn, "CONFL", observed_at=now)
        _insert_bridge(conn, "OLD", observed_at=now - timedelta(days=548))
        conn.execute(
            f"INSERT INTO {CONFLICTS} (entity_type, entity_id, field, source_a,"
            f" source_b) VALUES ('bridges', 'CONFL', 'x', %s, %s)", (SRC, SRC))

    with get_conn() as conn:
        n = rescore_table(conn, "bridges", table=f"{SCHEMA}.bridges",
                          conflicts_table=CONFLICTS, trust_map=TRUST)
    assert n == 5

    with get_conn() as conn:
        # FULL: T=0.95 F=1 C=1 A=0.5 -> 0.3325+0.25+0.20+0.10 = 0.8825 -> 88
        assert _bridge_scores(conn, "FULL") == (88, 95, 100, 100, 50, [])
        # NOVINTAGE: only posting/clearance missing AND name/state/ratings
        # missing -> C=0, F=0 (unknown vintage), flag says so.
        # 0.3325+0+0+0.10 = 0.4325 -> 43
        assert _bridge_scores(conn, "NOVINTAGE") == (
            43, 95, 0, 0, 50, ["vintage_unknown"])
        # OFFROAD: FULL minus the 0.15 geo penalty -> 0.7325 -> 73; flag kept.
        assert _bridge_scores(conn, "OFFROAD") == (73, 95, 100, 100, 50, ["offroad"])
        # CONFL: A=0 plus one 0.10 penalty -> 0.3325+0.25+0.20-0.10 = 0.6825 -> 68
        assert _bridge_scores(conn, "CONFL") == (
            68, 95, 100, 100, 0, ["conflict_open"])
        # OLD: age = one half-life -> F=0.5 -> 0.3325+0.125+0.20+0.10 = 0.7575 -> 76
        assert _bridge_scores(conn, "OLD") == (76, 95, 50, 100, 50, [])

    # Python reference and SQL push-down are the same formula.
    assert compute_confidence(0.95, 1.0, 1.0, 0.5).confidence == 88
    assert compute_confidence(0.95, 0.0, 0.0, 0.5).confidence == 43
    assert compute_confidence(0.95, 1.0, 1.0, 0.5, geo_suspect=True).confidence == 73
    assert compute_confidence(0.95, 1.0, 1.0, 0.0, open_conflicts=1).confidence == 68
    assert compute_confidence(0.95, 0.5, 1.0, 0.5).confidence == 76

    # Idempotent: nothing changed, so the DISTINCT guard writes nothing.
    with get_conn() as conn:
        assert rescore_table(conn, "bridges", table=f"{SCHEMA}.bridges",
                             conflicts_table=CONFLICTS, trust_map=TRUST) == 0


@needs_db
def test_rescore_live_events_active_only_composite_pk(scratch):
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {SCHEMA}.events (event_id, source_id, kind, geom,"
            " first_seen, last_seen, run_id, observed_at) VALUES "
            "('E-ACTIVE', %(s)s, 'weather_alert',"
            " ST_SetSRID(ST_MakePoint(-75, 40), 4326), now(), now(), 1, %(o)s)",
            {"s": SRC, "o": now})
        conn.execute(
            f"INSERT INTO {SCHEMA}.events (event_id, source_id, kind, geom,"
            " first_seen, last_seen, soft_closed_at, run_id, observed_at) VALUES "
            "('E-CLOSED', %(s)s, 'weather_alert', NULL, now(), now(), now(), 1,"
            " %(o)s)", {"s": SRC, "o": now})

    with get_conn() as conn:
        n = rescore_table(conn, "live_events", table=f"{SCHEMA}.events",
                          conflicts_table=CONFLICTS, trust_map=TRUST)
    assert n == 1  # active only — soft-closed rows are lifecycle-managed

    with get_conn() as conn:
        active = conn.execute(
            f"SELECT confidence, conf_fresh, conf_complete FROM {SCHEMA}.events"
            " WHERE event_id = 'E-ACTIVE'").fetchone()
        closed = conn.execute(
            f"SELECT confidence FROM {SCHEMA}.events"
            " WHERE event_id = 'E-CLOSED'").fetchone()
    # Active: T=0.95, F=1.0 (expiry beats decay — no staleness formula),
    # C=1 (geom + observed_at), A=0.5 -> 88.
    assert active == (88, 100, 100)
    assert closed == (None,)  # untouched


@needs_db
def test_rescore_unknown_source_gets_conservative_floor(scratch):
    with get_conn() as conn:
        _insert_bridge(conn, "X", observed_at=datetime.now(timezone.utc))
        rescore_table(conn, "bridges", table=f"{SCHEMA}.bridges",
                      conflicts_table=CONFLICTS, trust_map={})  # SRC unmapped
        trust = conn.execute(
            f"SELECT conf_trust FROM {SCHEMA}.bridges WHERE nbi_id = 'X'"
        ).fetchone()[0]
    assert trust == 55  # DEFAULT_TRUST: community-level floor, never guessed up


# -------------------------------------------------- nightly runner

def test_resolve_tables_accepts_logical_physical_and_all():
    qn = _load_nightly()
    assert qn._resolve_tables("bridges") == ("bridges",)
    assert qn._resolve_tables("core.bridges") == ("bridges",)
    assert qn._resolve_tables("all") == ("bridges", "tunnels", "parking_sites")
    with pytest.raises(SystemExit):
        qn._resolve_tables("core.no_such_table")


@needs_db
def test_nightly_run_writes_audited_source_run(scratch):
    """Full nightly pass against the scratch schema: seeds the synthetic
    source, runs gate 4 + all four rescores, writes ONE success run row under
    'quality_nightly' (36 h SLO seed) — the D3 ruling: a dead quality job must
    trip the same freshness alert as a dead feed."""
    qn = _load_nightly()
    with get_conn() as conn:
        _insert_bridge(conn, "K1", posting="K", clearance=150.0,
                       observed_at=datetime.now(timezone.utc))
        baseline = conn.execute(
            "SELECT coalesce(max(run_id), 0) FROM ops.source_runs").fetchone()[0]
    try:
        changed = qn.run_nightly(checks=[_closed_check()],
                                 scoring=_scratch_scoring(),
                                 conflicts_table=CONFLICTS)
        assert changed >= 1  # at least the K1 bridge got scored

        with get_conn() as conn:
            runs = conn.execute(
                "SELECT status, rows_published, message FROM ops.source_runs "
                "WHERE source_id = %s AND run_id > %s",
                (qn.NIGHTLY_SOURCE_ID, baseline)).fetchall()
            seed = conn.execute(
                "SELECT kind, schedule_minutes, slo_hours, enabled "
                "FROM ops.sources WHERE source_id = %s",
                (qn.NIGHTLY_SOURCE_ID,)).fetchone()
            conflict_open = conn.execute(
                f"SELECT count(*) FROM {CONFLICTS} WHERE status = 'open'"
            ).fetchone()[0]
            k1 = conn.execute(
                f"SELECT conf_agree, flags FROM {SCHEMA}.bridges "
                "WHERE nbi_id = 'K1'").fetchone()
        assert len(runs) == 1
        status, rows_published, message = runs[0]
        assert status == "success" and rows_published == changed
        assert "gate4" in message and "bridges_closed_vs_clearance" in message
        # Synthetic source seeded exactly as specified: derived, event-driven
        # (never tick-enqueued), 36 h SLO.
        assert seed == ("derived", None, qn.NIGHTLY_SLO_HOURS, True)
        # And the ladder actually chained: gate 4 opened the conflict, gate 5
        # scored it as conflicted in the SAME pass.
        assert conflict_open == 1
        assert k1 == (0, ["conflict_open"])
    finally:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM ops.source_runs WHERE source_id = %s AND run_id > %s",
                (qn.NIGHTLY_SOURCE_ID, baseline))


@needs_db
def test_nightly_failure_is_recorded_never_hidden(scratch):
    qn = _load_nightly()
    with get_conn() as conn:
        baseline = conn.execute(
            "SELECT coalesce(max(run_id), 0) FROM ops.source_runs").fetchone()[0]
    bad_check = dataclasses.replace(
        _closed_check(), pairs_sql="SELECT no_such_col FROM missing_table")
    try:
        with pytest.raises(Exception):
            qn.run_nightly(checks=[bad_check], scoring=_scratch_scoring(),
                           conflicts_table=CONFLICTS)
        with get_conn() as conn:
            status, message = conn.execute(
                "SELECT status, message FROM ops.source_runs "
                "WHERE source_id = %s AND run_id > %s",
                (qn.NIGHTLY_SOURCE_ID, baseline)).fetchone()
        assert status == "failed" and message  # the audit never lies
    finally:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM ops.source_runs WHERE source_id = %s AND run_id > %s",
                (qn.NIGHTLY_SOURCE_ID, baseline))


DERIVED_SRC = "_test_derived_quality"


@needs_db
def test_claim_jobs_dispatches_only_known_derived_sources():
    """claim_jobs drains derived jobs the engine worker never touches; a
    derived source without a runner is finished 'failed' with an honest
    message — never left claimed forever, never faked 'done'."""
    qn = _load_nightly()
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT 1 FROM ops.job_queue WHERE source_id = %s "
            "AND status IN ('queued', 'running')", (qn.RESCORE_SOURCE_ID,),
        ).fetchone()
    if pending:
        pytest.skip("a real quality_rescore job is pending — not consuming it here")
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ops.sources (source_id, name, kind, load_pattern,"
                " schedule_minutes, slo_hours, enabled) VALUES "
                "(%s, 'test derived', 'derived', 'derived', NULL, NULL, TRUE) "
                "ON CONFLICT (source_id) DO NOTHING", (DERIVED_SRC,))
            conn.execute(
                "INSERT INTO ops.job_queue (source_id) VALUES (%s)", (DERIVED_SRC,))
        assert qn.claim_jobs() == 1
        with get_conn() as conn:
            status, message = conn.execute(
                "SELECT status, message FROM ops.job_queue WHERE source_id = %s "
                "ORDER BY job_id DESC LIMIT 1", (DERIVED_SRC,)).fetchone()
        assert status == "failed" and "no derived-job runner" in message
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM ops.job_queue WHERE source_id = %s",
                         (DERIVED_SRC,))
            conn.execute("DELETE FROM ops.sources WHERE source_id = %s",
                         (DERIVED_SRC,))
