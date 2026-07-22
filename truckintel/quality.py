"""Quality ladder gates 3-5 (design/quality-ai.md §2 §7-9, MASTER_PLAN §7).

Three deterministic pieces, no AI anywhere in this module:

- Gate 3  — dedup(): within-source natural-key dedup. Pure function shaped
  exactly like validate.gate1_schema / gate2_coords so the engine can adopt it
  between gate 2 and the registry gates.
  ENGINE WIRE-IN (documented here, applied by the integrator — engine.py is
  foundation-owned): in engine._execute step 5, after gate2_coords:

      ok_rows, rejects3 = quality.dedup(ok_rows, natural_key_field)
      rejects = rejects1 + rejects2 + rejects3

  where natural_key_field comes from a per-source map (nbi_annual -> 'nbi_id',
  ntad_parking -> 'site_id', nti_tunnels -> 'tunnel_id', event feeds ->
  'event_id'). eia_diesel's key is composite (region, product, week_of) — its
  upsert loader is already idempotent on that key, so it needs no gate 3.

- Gate 4  — run_gate4(): config-driven cross-source consistency checks writing
  quality.conflicts. Conflicts are persisted, idempotent on the partial unique
  index (still-open re-detections are no-ops), and auto-closed when a re-check
  no longer finds the pair (quality-ai.md §7.3).

- Gate 5  — compute_confidence() (pure, unit-testable) + rescore_table() (the
  same formula pushed down to one SQL UPDATE per table):

      confidence = 100 * clamp01(0.35*T + 0.25*F + 0.20*C + 0.20*A - penalties)

  All four components are STORED per row as SMALLINT 0-100 (conf_trust,
  conf_fresh, conf_complete, conf_agree) so "why 65?" is always answerable.

Honesty notes (binding):
- F comes from observed_at (fact vintage), NEVER the fetch date. A NULL
  observed_at is an UNKNOWN vintage: F = 0.0 + flag 'vintage_unknown' — an
  unknown-age fact must not score as fresh.
- Active live events skip freshness decay (quality-ai.md §9 exclusions:
  expiry beats decay) — they score with F = 1.0 while active; expired ones
  are lifecycle-managed and not rescored at all.
- A (agreement) is honestly limited in wave 1: every table is single-source
  today, so there is nothing to corroborate — baseline A = 0.5 ("no one to
  disagree with"), dropping to 0.0 when the record has an OPEN conflict.
  A = 1.0 (corroborated within tolerance) becomes reachable only when the
  wave-2 osm.* mirrors land and the cross-source checks below are enabled.
- Trust can only degrade from its authority-class base (quality-ai.md §8);
  the nightly job reads ops.sources.trust/base_trust and falls back to the
  authority mapping below. Unknown sources get the community floor (0.55) —
  conservative by construction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

import psycopg

from truckintel.loaders import _IDENT_RE, _split_target

# ---------------------------------------------------------------------------
# Gate 3 — within-source dedup on the natural key
# ---------------------------------------------------------------------------


def dedup(rows: Iterable[dict], key_field: str) -> tuple[list[dict], list[dict]]:
    """Within-source natural-key dedup: keep the LAST occurrence (quality-ai.md
    §3.1 — government files append corrected rows for the same key, so the
    later row supersedes), reject the earlier ones with reason
    'duplicate_natural_key' (ready for quality.rejects, same
    (ok_rows, rejects) shape as gates 1-2). The kept row occupies the FIRST
    occurrence's position so output order stays stable.

    Rows with a missing/None key pass through untouched — a missing required
    key is gate 1's rejection, not a duplicate; and collapsing distinct
    keyless rows onto each other would fabricate a duplication that is not in
    the data.
    """
    seen: dict = {}  # key -> index of the kept row in ok_rows
    ok_rows: list[dict] = []
    rejects: list[dict] = []
    for row in rows:
        key = row.get(key_field)
        if key is None:
            ok_rows.append(row)
        elif key in seen:
            idx = seen[key]
            rejects.append(
                {"reason": "duplicate_natural_key", "raw_record": ok_rows[idx]}
            )
            ok_rows[idx] = row  # later occurrence supersedes (keep-the-last)
        else:
            seen[key] = len(ok_rows)
            ok_rows.append(row)
    return ok_rows, rejects


# ---------------------------------------------------------------------------
# Trust — per-source, by authority class (quality-ai.md §8)
# ---------------------------------------------------------------------------

# Base trust by authority class. Keys cover both the short spellings used in
# ops.sources.authority_class (schema.sql comment) and the long spellings in
# quality-ai.md §8's table.
AUTHORITY_BASE_TRUST: dict[str, float] = {
    "federal": 0.95, "federal_authoritative": 0.95,
    "state": 0.90, "state_authoritative": 0.90,
    "curated": 0.85, "curated_manual": 0.85,
    "open_aggregate": 0.65,
    "community": 0.55,
}

# Conservative floor for sources with no class assigned anywhere: community
# level. Trust must never be guessed upward.
DEFAULT_TRUST = 0.55

# source_id -> authority class fallback for the live registry (ops.sources
# carries authority_class/base_trust columns but the wave-1 registry YAMLs do
# not populate them yet — this map is the honest interim, reviewed in git).
_FEDERAL_SOURCE_IDS = frozenset(
    {"nbi_annual", "nti_tunnels", "ntad_parking", "nws_alerts", "eia_diesel"}
)


def fallback_trust(source_id: str) -> float:
    """Authority-class base trust for a source_id with no DB-assigned trust."""
    if source_id in _FEDERAL_SOURCE_IDS:
        return AUTHORITY_BASE_TRUST["federal"]
    if source_id.startswith("wzdx_"):        # state DOT 511 / WZDx programs
        return AUTHORITY_BASE_TRUST["state"]
    if source_id.startswith("osm"):          # wave-2 OSM mirrors
        return AUTHORITY_BASE_TRUST["community"]
    return DEFAULT_TRUST


def source_trust_map(conn: psycopg.Connection) -> dict[str, float]:
    """source_id -> effective trust. Precedence: ops.sources.trust (nightly
    degraded value) > base_trust > authority_class mapping > fallback map."""
    out: dict[str, float] = {}
    for sid, trust, base, cls in conn.execute(
        "SELECT source_id, trust, base_trust, authority_class FROM ops.sources"
    ).fetchall():
        value = trust if trust is not None else base
        if value is None:
            value = AUTHORITY_BASE_TRUST.get(cls or "", fallback_trust(sid))
        out[sid] = float(value)
    return out


# ---------------------------------------------------------------------------
# Gate 4 — cross-source consistency framework (quality-ai.md §7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsistencyCheck:
    """One registered tolerance check. pairs_sql must select
    (entity_id TEXT, value_a TEXT, value_b TEXT, delta NUMERIC) — only the
    rows that VIOLATE the tolerance (the conflicts to open)."""
    name: str
    entity_type: str          # quality.conflicts.entity_type ('bridges', ...)
    field: str                # the disputed field
    source_a: str
    source_b: str
    pairs_sql: str
    enabled: bool = True
    note: str = ""


# The wave-1 roster. quality-ai.md §7.3's first cross-source pair (NBI measured
# vs OSM maxheight) needs osm.ways, which is EMPTY until wave 2 — so it ships
# registered but DISABLED, and the runnable-today check is the NBI
# self-consistency pair: a bridge coded CLOSED (item 41 = 'K') that still
# presents an operational clearance value. Analogous to §7.3's
# "posted > measured -> data-error red flag": the record contradicts itself,
# a human should look, and routing must not treat the clearance as usable.
REGISTERED_CHECKS: tuple[ConsistencyCheck, ...] = (
    ConsistencyCheck(
        name="bridges_closed_vs_clearance",
        entity_type="bridges",
        field="posting_status_vs_clearance",
        source_a="nbi_annual",
        source_b="nbi_annual",   # self-consistency: both sides are NBI
        pairs_sql=(
            "SELECT nbi_id AS entity_id, "
            "       'posting_status=' || posting_status AS value_a, "
            "       'min_vert_clearance_in=' || min_vert_clearance_in AS value_b, "
            "       NULL::numeric AS delta "
            "FROM core.bridges "
            "WHERE posting_status = 'K' AND min_vert_clearance_in IS NOT NULL"
        ),
        note="NBI item 41 'K' = closed; a closed bridge presenting a usable "
             "clearance is a within-source contradiction (wave-1 runnable).",
    ),
    ConsistencyCheck(
        name="bridges_nbi_vs_osm_maxheight",
        entity_type="bridges",
        field="min_vert_clearance_in",
        source_a="nbi_annual",
        source_b="osm_ways",
        pairs_sql=(
            "SELECT b.nbi_id AS entity_id, "
            "       'nbi=' || b.min_vert_clearance_in AS value_a, "
            "       'osm=' || w.maxheight_in AS value_b, "
            "       abs(b.min_vert_clearance_in - w.maxheight_in) AS delta "
            "FROM core.bridges b "
            "JOIN osm.ways w "
            "  ON ST_DWithin(b.geom::geography, w.geom::geography, 50) "
            "WHERE b.min_vert_clearance_in IS NOT NULL "
            "  AND w.maxheight_in IS NOT NULL "
            "  AND abs(b.min_vert_clearance_in - w.maxheight_in) > 6"
        ),
        enabled=False,
        note="DISABLED until wave 2: osm.ways is empty, and per quality-ai.md "
             "§7.3 the match needs the same-road name/ref refinement on top of "
             "<=50 m before this can open conflicts (tolerance 6 in — a posted "
             "sign legitimately reads below the measured steel).",
    ),
)


def run_gate4(
    conn: psycopg.Connection,
    checks: Iterable[ConsistencyCheck] | None = None,
    *,
    conflicts_table: str = "quality.conflicts",
) -> dict[str, tuple[int, int]]:
    """Run every ENABLED check: open new conflicts (idempotent on the partial
    unique index — a still-open re-detection is a no-op) and auto-close open
    conflicts the re-check no longer finds (quality-ai.md §7.3: "a conflict
    that disappears after a source update auto-closes").

    Returns {check_name: (opened, closed)}. Runs in the caller's transaction.
    """
    cschema, ctable = _split_target(conflicts_table)
    qualified = f'"{cschema}"."{ctable}"'
    results: dict[str, tuple[int, int]] = {}
    for check in (REGISTERED_CHECKS if checks is None else checks):
        if not check.enabled:
            continue
        scope = {
            "etype": check.entity_type, "field": check.field,
            "sa": check.source_a, "sb": check.source_b,
        }
        opened = conn.execute(
            f"INSERT INTO {qualified} "
            "    (entity_type, entity_id, field, value_a, source_a, "
            "     value_b, source_b, delta) "
            "SELECT %(etype)s, p.entity_id, %(field)s, p.value_a, %(sa)s, "
            "       p.value_b, %(sb)s, p.delta "
            f"FROM ({check.pairs_sql}) p "
            "ON CONFLICT (entity_type, entity_id, field, source_a, source_b) "
            "    WHERE status = 'open' DO NOTHING",
            scope,
        ).rowcount
        closed = conn.execute(
            f"UPDATE {qualified} SET status = 'closed', closed_at = now() "
            "WHERE status = 'open' AND entity_type = %(etype)s "
            "  AND field = %(field)s AND source_a = %(sa)s AND source_b = %(sb)s "
            f"  AND entity_id NOT IN (SELECT p.entity_id FROM ({check.pairs_sql}) p)",
            scope,
        ).rowcount
        results[check.name] = (opened, closed)
    return results


# ---------------------------------------------------------------------------
# Gate 5 — confidence formula (quality-ai.md §9), pure reference implementation
# ---------------------------------------------------------------------------

W_TRUST, W_FRESH, W_COMPLETE, W_AGREE = 0.35, 0.25, 0.20, 0.20
GEO_PENALTY = 0.15            # flagged offroad / coordinate-suspect (§4 C3)
CONFLICT_PENALTY = 0.10       # per open conflict...
CONFLICT_PENALTY_CAP = 0.20   # ...capped (stacks with A=0 deliberately)

# Flags that trigger the geo penalty (the 'offroad' flag arrives with the
# wave-2 TIGER near-road check; the plumbing is live now).
GEO_SUSPECT_FLAGS: tuple[str, ...] = ("offroad", "coord_suspect")

# Flags this module owns on every rescore (recomputed each run; any other
# flag on the row is preserved untouched).
_MANAGED_FLAGS: tuple[str, ...] = ("conflict_open", "vintage_unknown")

# Record-freshness half-lives in days (quality-ai.md §6 table).
HALF_LIFE_DAYS = {
    "bridges": 548.0,          # annual publication x 1.5
    "tunnels": 548.0,          # NTI is annual like NBI
    "parking_sites": 1095.0,   # survey-era data; slow decay from an old base
}


@dataclass(frozen=True)
class ConfidenceScore:
    confidence: int
    conf_trust: int
    conf_fresh: int
    conf_complete: int
    conf_agree: int


def clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def _pct(x: float) -> int:
    """0..1 -> 0..100, rounding half AWAY from zero — matches SQL
    round(numeric) so the Python reference and the SQL push-down agree."""
    return int(math.floor(x * 100.0 + 0.5))


def freshness(
    observed_at: datetime | None,
    half_life_days: float,
    *,
    now: datetime | None = None,
) -> float:
    """F = 0.5^(age_days / half_life), age from observed_at (fact vintage).
    NULL observed_at = unknown vintage -> 0.0 (never scored as fresh).
    A future observed_at clamps to age 0 (F = 1.0) — never > 1."""
    if observed_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def completeness(row: Mapping, manifest: Mapping[str, int]) -> float:
    """C = weighted fill of the 'important' manifest fields (quality-ai.md §5):
    sum(weight of non-null fields) / sum(all weights)."""
    total = sum(manifest.values())
    if total == 0:
        return 1.0  # nothing demanded -> vacuously complete
    return sum(w for col, w in manifest.items() if row.get(col) is not None) / total


def agreement(open_conflicts: int, *, corroborated: bool = False) -> float:
    """A per quality-ai.md §9: 0.0 with any OPEN conflict; 1.0 when
    corroborated by >=2 independent sources within tolerance; 0.5 baseline
    (single-source — no one to disagree with). Wave-1 honesty: nothing is
    corroborated yet, so corroborated=False everywhere until the wave-2
    cross-source matches land."""
    if open_conflicts > 0:
        return 0.0
    return 1.0 if corroborated else 0.5


def compute_confidence(
    trust: float,
    fresh: float,
    complete: float,
    agree: float,
    *,
    geo_suspect: bool = False,
    open_conflicts: int = 0,
) -> ConfidenceScore:
    """The §9 formula, exact:
    confidence = round(100 * clamp01(0.35*T + 0.25*F + 0.20*C + 0.20*A
                                     - P_geo - P_conflict))
    with P_geo = 0.15 when coordinate-suspect and P_conflict = 0.10 per open
    conflict capped at 0.20. Components are returned 0-100 for storage."""
    penalties = (GEO_PENALTY if geo_suspect else 0.0) + min(
        CONFLICT_PENALTY_CAP, CONFLICT_PENALTY * open_conflicts
    )
    raw = (W_TRUST * trust + W_FRESH * fresh + W_COMPLETE * complete
           + W_AGREE * agree - penalties)
    return ConfidenceScore(
        confidence=_pct(clamp01(raw)),
        conf_trust=_pct(trust),
        conf_fresh=_pct(fresh),
        conf_complete=_pct(complete),
        conf_agree=_pct(agree),
    )


# ---------------------------------------------------------------------------
# Gate 5 — per-table SQL push-down (the nightly / post-swap rescore)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableScoring:
    """How one entity table is rescored. completeness weights are v1 config
    (quality-ai.md §5: weights encode trucker value; structure is the
    commitment, exact numbers are tunable)."""
    entity_type: str                 # quality.conflicts.entity_type value
    table: str                       # default physical table (schema.table)
    pk_cols: tuple[str, ...]
    entity_id_sql: str               # TEXT expr over alias x -> conflicts.entity_id
    half_life_days: float | None     # None = lifecycle-managed: F=1.0 (active rows)
    completeness: Mapping[str, int]  # column -> weight ('important' manifest)
    where: str = "TRUE"              # which rows get scored


TABLE_SCORING: dict[str, TableScoring] = {
    "bridges": TableScoring(
        entity_type="bridges", table="core.bridges",
        pk_cols=("nbi_id",), entity_id_sql="x.nbi_id",
        half_life_days=HALF_LIFE_DAYS["bridges"],
        completeness={"min_vert_clearance_in": 3, "posting_status": 2,
                      "operating_rating": 1, "inventory_rating": 1,
                      "name": 1, "state": 1},
    ),
    "tunnels": TableScoring(
        entity_type="tunnels", table="core.tunnels",
        pk_cols=("tunnel_id",), entity_id_sql="x.tunnel_id",
        half_life_days=HALF_LIFE_DAYS["tunnels"],
        completeness={"min_vert_clearance_in": 3, "hazmat_restricted": 2,
                      "hazmat_codes": 2, "length_ft": 1, "name": 1, "state": 1},
    ),
    "parking_sites": TableScoring(
        entity_type="parking_sites", table="core.parking_sites",
        pk_cols=("site_id",), entity_id_sql="x.site_id",
        half_life_days=HALF_LIFE_DAYS["parking_sites"],
        completeness={"truck_spaces": 3, "name": 2, "state": 1},
    ),
    # Active live events only: expiry beats decay (§9 exclusions), so F=1.0
    # while active; soft-closed events are lifecycle-managed, never rescored.
    "live_events": TableScoring(
        entity_type="live_events", table="core.live_events",
        pk_cols=("source_id", "event_id"),
        entity_id_sql="x.source_id || ':' || x.event_id",
        half_life_days=None,
        completeness={"geom": 2, "observed_at": 1},
        where="x.soft_closed_at IS NULL",
    ),
}


def _check_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _quoted(target: str) -> str:
    schema, table = _split_target(target)  # validates both identifiers
    return f'"{schema}"."{table}"'


def rescore_table(
    conn: psycopg.Connection,
    name: str,
    *,
    table: str | None = None,
    conflicts_table: str = "quality.conflicts",
    trust_map: Mapping[str, float] | None = None,
    scoring: Mapping[str, TableScoring] | None = None,
) -> int:
    """Recompute confidence + the four stored components + managed flags for
    every scored row of one table, in ONE SQL UPDATE (the same formula as
    compute_confidence — the tests hold the two implementations together).

    Only rows whose stored score/flags actually change are written (IS
    DISTINCT FROM guard) so the nightly re-run doesn't rewrite 630k identical
    rows. Returns rows updated. Runs in the caller's transaction.

    table/conflicts_table/trust_map are overridable for scratch-schema tests;
    production callers pass just the name.
    """
    cfg = (TABLE_SCORING if scoring is None else scoring)[name]
    target = _quoted(table or cfg.table)
    conflicts = _quoted(conflicts_table)
    if trust_map is None:
        trust_map = source_trust_map(conn)

    pk = [_check_ident(c) for c in cfg.pk_cols]
    pk_select = ", ".join(f'x."{c}" AS _pk_{i}' for i, c in enumerate(pk))
    pk_carry = ", ".join(f"_pk_{i}" for i in range(len(pk)))
    pk_join = " AND ".join(f't."{c}" = comp._pk_{i}' for i, c in enumerate(pk))

    manifest = {_check_ident(c): int(w) for c, w in cfg.completeness.items()}
    total_w = sum(manifest.values()) or 1
    complete_sql = "(" + " + ".join(
        f'CASE WHEN x."{c}" IS NOT NULL THEN {w} ELSE 0 END'
        for c, w in manifest.items()
    ) + f")::numeric / {total_w}"

    if cfg.half_life_days is None:
        fresh_sql = "1.0::numeric"
    else:
        fresh_sql = (
            "CASE WHEN x.observed_at IS NULL THEN 0.0 ELSE "
            "power(0.5, GREATEST(extract(epoch FROM (now() - x.observed_at)), 0)"
            f" / 86400.0 / {float(cfg.half_life_days)}) END::numeric"
        )

    params: dict = {
        "etype": cfg.entity_type,
        "geo": list(GEO_SUSPECT_FLAGS),
        "managed": list(_MANAGED_FLAGS),
        "default_trust": DEFAULT_TRUST,
    }
    if trust_map:
        values_sql = ", ".join(
            f"(%(ts{i})s, %(tv{i})s::numeric)" for i in range(len(trust_map))
        )
        for i, (sid, tv) in enumerate(sorted(trust_map.items())):
            params[f"ts{i}"], params[f"tv{i}"] = sid, tv
        trust_join = (
            f"LEFT JOIN (VALUES {values_sql}) tm(source_id, trust) "
            "ON tm.source_id = x.source_id"
        )
        trust_sql = "COALESCE(tm.trust, %(default_trust)s)::numeric"
    else:
        trust_join = ""
        trust_sql = "%(default_trust)s::numeric"

    sql = f"""
UPDATE {target} AS t
SET conf_trust    = comp.tr_pct,
    conf_fresh    = comp.fr_pct,
    conf_complete = comp.co_pct,
    conf_agree    = comp.ag_pct,
    confidence    = comp.conf,
    flags         = comp.new_flags
FROM (
    SELECT {pk_carry},
           round(t_c * 100)::smallint AS tr_pct,
           round(f_c * 100)::smallint AS fr_pct,
           round(c_c * 100)::smallint AS co_pct,
           round(a_c * 100)::smallint AS ag_pct,
           round(100 * LEAST(1.0, GREATEST(0.0,
               {W_TRUST} * t_c + {W_FRESH} * f_c + {W_COMPLETE} * c_c
               + {W_AGREE} * a_c - pen)))::smallint AS conf,
           base_flags
             || CASE WHEN n_open > 0
                     THEN ARRAY['conflict_open'] ELSE ARRAY[]::text[] END
             || CASE WHEN no_vintage
                     THEN ARRAY['vintage_unknown'] ELSE ARRAY[]::text[] END
             AS new_flags
    FROM (
        SELECT {pk_select},
               {trust_sql} AS t_c,
               {fresh_sql} AS f_c,
               {complete_sql} AS c_c,
               CASE WHEN oc.n_open > 0 THEN 0.0 ELSE 0.5 END::numeric AS a_c,
               (CASE WHEN x.flags && %(geo)s::text[]
                     THEN {GEO_PENALTY} ELSE 0 END
                + LEAST({CONFLICT_PENALTY_CAP},
                        {CONFLICT_PENALTY} * oc.n_open))::numeric AS pen,
               oc.n_open AS n_open,
               (x.observed_at IS NULL) AS no_vintage,
               ARRAY(SELECT f FROM unnest(x.flags) f
                     WHERE f <> ALL (%(managed)s::text[])) AS base_flags
        FROM {target} x
        {trust_join}
        CROSS JOIN LATERAL (
            SELECT count(*)::int AS n_open
            FROM {conflicts} c
            WHERE c.status = 'open'
              AND c.entity_type = %(etype)s
              AND c.entity_id = ({cfg.entity_id_sql})
        ) oc
        WHERE {cfg.where}
    ) y
) comp
WHERE {pk_join}
  AND (t.confidence    IS DISTINCT FROM comp.conf
    OR t.conf_trust    IS DISTINCT FROM comp.tr_pct
    OR t.conf_fresh    IS DISTINCT FROM comp.fr_pct
    OR t.conf_complete IS DISTINCT FROM comp.co_pct
    OR t.conf_agree    IS DISTINCT FROM comp.ag_pct
    OR t.flags         IS DISTINCT FROM comp.new_flags)
"""
    # NOTE on agreement wave-1: a_c is 0.5-or-0.0 only. The corroborated=1.0
    # branch (agreement()) becomes reachable when wave-2 cross-source matching
    # lands; at that point this SELECT gains a corroboration join.
    return conn.execute(sql, params).rowcount
