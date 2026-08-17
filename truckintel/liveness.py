"""Gate 6 — LIVENESS: is the place this row describes still there?

Gate 5 (truckintel.quality) scores the RECORD. This scores the SUBJECT.

Those are different questions and conflating them is how a 2019 truck-stop
survey ends up rendered next to a 2025 bridge inventory as though both were
equally true today. The bridge is still standing. The truck stop might be a
Dollar General now.

    conf_fresh   how old is this statement?      -> quality.py
    liveness     is the thing still there?       -> this module

FORMULA (deliberately shaped like Gate 5's, so the two read as one system)

    liveness = round(100 * clamp01(0.50*P + 0.30*S + 0.20*A - penalties))

    P  presence   0.5^(days_since_last_seen / half_life). The half-life is a
                  property of the SUBJECT, not of the source: what governs it
                  is how fast that kind of place turns over — a travel centre
                  is a 20-acre capital asset, a cafe is a lease, a state rest
                  area is public infrastructure. Set per table, and per ROW
                  where one table mixes kinds (see TableLiveness.half_life_sql).
    S  sources    how many distinct sources CURRENTLY assert existence.
                  0 -> 0.0, 1 -> 0.5, 2 -> 0.8, 3+ -> 1.0. Saturating, because
                  the fourth aggregator copying the same Overture extract is
                  not a fourth witness.
    A  corrob     an authoritative CURRENT confirmation — the operator's own
                  store locator, or an unexpired state repair licence. Binary.
                  This is the only component that can move a 2019 row to
                  'open', and it is the reason chain_sites exists.

    penalties     MISSING_PENALTY  0.30  a source that used to list it stopped
                  LICENCE_EXPIRED  0.25  the state says the licence lapsed

WEIGHTS, and why P is not higher: 0.50 on decay alone would make every old
record look dead, which is a different lie from the one we are fixing. A
Jason's-Law truck stop with no current corroboration lands at ~32 — 'unknown'
— which is the honest answer, not 'likely_closed'.

HARD OVERRIDE, not a component: a POSITIVE closure assertion from a source
that carries one (FSQ date_closed, OSM disused:/was: lifecycle prefix) sets
live_state='closed' and liveness=0 outright. We report closure only when a
source says so. We never infer it — see the honesty note below.

BINDING HONESTY
- Absence of evidence is not evidence of absence. A place nobody has
  confirmed since 2019 is 'unknown', never 'closed'. Routing a driver away
  from a truck stop that is open, at 2 a.m., with the clock on their hours of
  service running out, is also a failure — and a less visible one.
- last_seen_at is a FACT vintage (the source's own observation date / release
  timestamp), never the date we fetched it. Same rule as quality.freshness.
  A source with no honest vintage contributes nothing rather than now().
- S counts SOURCES, not rows. Overture and FSQ both ingesting the same
  Foursquare venue is one witness wearing two coats; the presence ledger is
  keyed on source_id precisely so that stays visible.
- Nothing here is AI. It is decay arithmetic over a ledger, and every stored
  component answers "why is this 32?" without re-running the scorer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

import psycopg

from truckintel.quality import _pct, clamp01

# ---------------------------------------------------------------------------
# Weights and penalties
# ---------------------------------------------------------------------------

W_PRESENCE, W_SOURCES, W_CORROB = 0.50, 0.30, 0.20

# ops.sources.source_id under which a chain's own store locator testifies.
# Must match scripts/chain_sites.py's SOURCE_ID and the row seeded by
# sql/schema_liveness.sql.
CHAIN_SOURCE_ID = "chain_sites"

MISSING_PENALTY = 0.30         # vanished from a source that used to carry it
LICENCE_EXPIRED_PENALTY = 0.25  # the state registry says the licence lapsed

# Bucket floors, highest first. Read as: >= 75 is 'open', and so on down.
STATE_FLOORS: tuple[tuple[int, str], ...] = (
    (75, "open"),
    (50, "likely_open"),
    (25, "unknown"),
    (0, "likely_closed"),
)
STATE_CLOSED = "closed"

# Turnover half-lives in days, by entity type. These encode how long a
# business of that kind typically survives without anyone re-confirming it,
# NOT how often we poll.
#
#   parking_sites   1825 (5 yr)  travel centres are capital assets on owned
#                                land; they change brand far more often than
#                                they disappear
#   mechanic_shops   730 (2 yr)  independent shops; BLS puts small-business
#                                survival well under half a decade
#   businesses       365 (1 yr)  restaurants and cafes, the fastest-churning
#                                category we carry
HALF_LIFE_DAYS: dict[str, float] = {
    "parking_sites": 1825.0,
    "mechanic_shops": 730.0,
    "businesses": 365.0,
}

# S is saturating: independent witnesses stop adding information quickly.
SOURCE_BREADTH: dict[int, float] = {0: 0.0, 1: 0.5, 2: 0.8}
SOURCE_BREADTH_MAX = 1.0


@dataclass(frozen=True)
class LivenessScore:
    liveness: int
    live_presence: int
    live_sources: int
    live_corrob: int
    live_state: str


def presence_decay(
    last_seen_at: datetime | None,
    half_life_days: float,
    *,
    now: datetime | None = None,
) -> float:
    """P = 0.5^(age / half_life), age from last_seen_at.

    A NULL last_seen_at means no source has ever been recorded asserting this
    place exists — which is not the same as a place last seen long ago, but
    scores the same way it does in Gate 5: 0.0, never charitably.
    A future last_seen_at clamps to age 0 rather than exceeding 1.
    """
    if last_seen_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - last_seen_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def source_breadth(n_sources: int) -> float:
    """S, saturating at three. See SOURCE_BREADTH for why it is not linear."""
    if n_sources < 0:
        raise ValueError("n_sources must be >= 0")
    return SOURCE_BREADTH.get(n_sources, SOURCE_BREADTH_MAX)


def corroboration(*, chain_confirmed: bool = False, licence_active: bool = False) -> float:
    """A: binary. Either an authority that would know has confirmed this place
    recently, or nobody has. There is no partial credit, because the whole
    point of this component is to be the one signal strong enough to lift an
    old record — half-credit for a weak proxy would defeat that."""
    return 1.0 if (chain_confirmed or licence_active) else 0.0


def bucket(liveness: int, *, closed_asserted: bool = False) -> str:
    """Map a 0-100 score to the vocabulary the API and UI render."""
    if closed_asserted:
        return STATE_CLOSED
    for floor, name in STATE_FLOORS:
        if liveness >= floor:
            return name
    return STATE_FLOORS[-1][1]


def compute_liveness(
    presence: float,
    sources: float,
    corrob: float,
    *,
    missing: bool = False,
    licence_expired: bool = False,
    closed_asserted: bool = False,
) -> LivenessScore:
    """The formula, exact. Components are returned 0-100 for storage so the
    stored row explains its own score.

    closed_asserted short-circuits everything: a source that positively says
    "this is closed" outranks any amount of decay arithmetic, in both
    directions — it is also the only way to reach 0 while still being
    confidently right.
    """
    if closed_asserted:
        return LivenessScore(
            liveness=0,
            live_presence=_pct(clamp01(presence)),
            live_sources=_pct(clamp01(sources)),
            live_corrob=_pct(clamp01(corrob)),
            live_state=STATE_CLOSED,
        )
    penalties = (MISSING_PENALTY if missing else 0.0) + (
        LICENCE_EXPIRED_PENALTY if licence_expired else 0.0
    )
    raw = (
        W_PRESENCE * presence
        + W_SOURCES * sources
        + W_CORROB * corrob
        - penalties
    )
    score = _pct(clamp01(raw))
    return LivenessScore(
        liveness=score,
        live_presence=_pct(clamp01(presence)),
        live_sources=_pct(clamp01(sources)),
        live_corrob=_pct(clamp01(corrob)),
        live_state=bucket(score),
    )


# ---------------------------------------------------------------------------
# Per-table SQL push-down — the same formula, once, in the database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableLiveness:
    """How one place table is liveness-scored.

    licence_expired_col / closed_flag: only mechanic_shops carries a licence
    registry join today, and no table yet carries a positive closure flag —
    FSQ's date_closed lands when fsq_places is repaired. Both are wired here
    so that repair is a config change, not a rewrite.
    """
    entity_type: str
    table: str
    pk_col: str
    half_life_days: float
    # SQL expression over alias x yielding TEXT[] — who asserts this row.
    # Each table records provenance differently and the differences are
    # meaningful, so this is config rather than a convention:
    #   parking_sites   ARRAY[x.source_id]  single-source federal survey
    #   businesses      x.present_in        the conflate job's contributors
    #   mechanic_shops  x.source_orgs       ORGANISATIONS, not feed labels —
    #                   mechanic_list.py rebuilt this on 2026-07-27 precisely
    #                   because an aggregator's own labels are not independent
    #                   witnesses, and S must not be inflated by one company
    #                   appearing under three names.
    presence_sources_sql: str
    # Optional per-ROW half-life, as a SQL expression over alias x returning
    # days. Overrides half_life_days when set.
    #
    # WHY THIS ESCAPE HATCH EXISTS (measured 2026-08-17): core.parking_sites
    # turned out to be 1,878 public_rest_area rows and only 37 truck_stop
    # rows. A state DOT rest area is public infrastructure — closer to a
    # bridge than to a cafe — and decaying it on a commercial-business clock
    # would report thousands of perfectly real rest areas as 'unknown'. One
    # half-life per TABLE was the wrong unit as soon as a table mixed
    # government assets with businesses.
    half_life_sql: str | None = None
    chain_match_m: int = 500        # chain point -> our row, metres
    licence_expired_col: str | None = None
    closed_flag: str | None = None  # element of flags[] that asserts closure


TABLE_LIVENESS: dict[str, TableLiveness] = {
    "parking_sites": TableLiveness(
        entity_type="parking_sites", table="core.parking_sites",
        pk_col="site_id", half_life_days=HALF_LIFE_DAYS["parking_sites"],
        presence_sources_sql="ARRAY[x.source_id]",
        # Jason's-Law parking is 98% state-run rest areas (1,878 of 1,915,
        # measured 2026-08-17). Those are capital infrastructure on public
        # land: they get defunded occasionally, they do not go out of
        # business. At 3650d a 2019 rest area holds P=59 and scores 44 —
        # 'unknown' — against 35/32 on the commercial clock. Both land in the
        # same bucket today because the binding constraint is corroboration,
        # not decay: a single-source row with A=0 cannot exceed 65 however
        # fresh it is. Fixing that needs a SECOND source for rest areas
        # (OSM highway=rest_area via Overpass), not a softer half-life.
        # The 37 commercial truck_stop rows decay on the business clock.
        half_life_sql=("CASE WHEN x.kind = 'public_rest_area' "
                       "THEN 3650.0 ELSE 1825.0 END"),
        # 500 m: a travel centre parcel is large and our 2019 survey point and
        # the chain's own pin are rarely the same coordinate. At that radius
        # on an interstate exit the only candidate IS the truck stop — the
        # same reasoning mechanic_list.py's --chains join already uses.
        chain_match_m=500,
        closed_flag="closed",
    ),
    "mechanic_shops": TableLiveness(
        entity_type="mechanic_shops", table="core.mechanic_shops",
        pk_col="shop_id", half_life_days=HALF_LIFE_DAYS["mechanic_shops"],
        presence_sources_sql="x.source_orgs",
        chain_match_m=500,
        licence_expired_col="licence_expired",
        closed_flag="closed",
    ),
    "businesses": TableLiveness(
        entity_type="businesses", table="core.businesses",
        pk_col="business_id", half_life_days=HALF_LIFE_DAYS["businesses"],
        presence_sources_sql="x.present_in",
        # 150 m: matches the conflation blocking radius (quality-ai.md §3.2).
        # A restaurant is not a 20-acre parcel; 500 m would marry a cafe to
        # the truck stop across the road and call it chain-confirmed.
        chain_match_m=150,
        closed_flag="closed",
    ),
}

_IDENT_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
)


def _check_ident(name: str) -> str:
    """Identifiers reach SQL by interpolation (psycopg cannot parameterise a
    column name), so they are whitelisted rather than trusted. Same guard as
    loaders._IDENT_RE, spelled locally to keep this module's contract obvious.
    """
    if not name or set(name) - _IDENT_OK:
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def refresh_presence(conn: psycopg.Connection, cfg: TableLiveness) -> tuple[int, int]:
    """Fold the table's CURRENT contents into the ledger. Returns (seen, gone).

    Every place table is loaded snapshot_swap, so a row dropped upstream is
    simply gone — no tombstone, no trace it was ever there. That deletion is
    the single most useful free closure signal we have and it was being
    discarded on every load. This reads the table before the next swap and
    writes the testimony somewhere that survives it.

    Rows with a NULL observed_at contribute nothing rather than now(): an
    unknown vintage laundered into today's date would make the oldest records
    in the system score as the freshest. Same rule as quality.freshness.

    `observations` increments only when the vintage actually ADVANCES. This
    job runs nightly against a table that changes monthly; counting each
    re-read as a sighting would turn 30 nights of looking at the same 2019
    survey into 30 independent confirmations of it.
    """
    table = _check_ident(cfg.table)
    pk = _check_ident(cfg.pk_col)
    seen = conn.execute(
        f"""
        INSERT INTO quality.presence
            (entity_type, entity_id, source_id, first_seen_at, last_seen_at)
        SELECT %(etype)s, x.{pk}::text, s.source_id, x.observed_at, x.observed_at
        FROM {table} x
        CROSS JOIN LATERAL unnest({cfg.presence_sources_sql}) AS s(source_id)
        WHERE x.observed_at IS NOT NULL
          AND s.source_id IS NOT NULL
        ON CONFLICT (entity_type, entity_id, source_id) DO UPDATE SET
            last_seen_at  = GREATEST(quality.presence.last_seen_at,
                                     EXCLUDED.last_seen_at),
            missing_since = NULL,
            observations  = quality.presence.observations
                            + CASE WHEN EXCLUDED.last_seen_at
                                        > quality.presence.last_seen_at
                                   THEN 1 ELSE 0 END
        """,
        {"etype": cfg.entity_type},
    ).rowcount

    # An entity id that is no longer in the table at all: upstream stopped
    # carrying it. Stamped, not deleted — the evidence is the point.
    #
    # Scoped to non-chain sources deliberately. chain_sites presence is
    # spatial and is retired by refresh_chain_presence, which knows whether
    # the chain feed actually ran; treating a skipped chain run as a
    # disappearance would manufacture closures.
    gone = conn.execute(
        f"""
        UPDATE quality.presence p
        SET missing_since = now()
        WHERE p.entity_type = %(etype)s
          AND p.source_id  <> %(chain_sid)s
          AND p.missing_since IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM {table} x WHERE x.{pk}::text = p.entity_id
          )
        """,
        {"etype": cfg.entity_type, "chain_sid": CHAIN_SOURCE_ID},
    ).rowcount
    return (seen, gone)


def refresh_chain_presence(conn: psycopg.Connection, cfg: TableLiveness) -> int:
    """Record the chains' own store locators as testimony about OUR rows.

    This is what actually moves a 2019 Jason's-Law truck stop to 'open'. The
    spatial match makes Love's statement "store 5 is open" into evidence about
    core.parking_sites' row for that same lot, with the CHAIN's vintage —
    2026-08-11 — rather than the survey's 2019.

    It counts as a distinct source in the ledger, which is honest: the chain
    is a genuinely independent witness from BTS, not a relabelling of it.
    """
    table = _check_ident(cfg.table)
    pk = _check_ident(cfg.pk_col)
    return conn.execute(
        f"""
        INSERT INTO quality.presence
            (entity_type, entity_id, source_id, first_seen_at, last_seen_at)
        SELECT %(etype)s, x.{pk}::text, %(chain_sid)s, cs.observed_at,
               cs.observed_at
        FROM {table} x
        CROSS JOIN LATERAL (
            SELECT c.observed_at
            FROM core.chain_sites c
            WHERE ST_DWithin(c.geom::geography, x.geom::geography, {int(cfg.chain_match_m)})
            ORDER BY c.geom::geography <-> x.geom::geography
            LIMIT 1
        ) cs
        WHERE x.geom IS NOT NULL
        ON CONFLICT (entity_type, entity_id, source_id) DO UPDATE SET
            last_seen_at  = GREATEST(quality.presence.last_seen_at,
                                     EXCLUDED.last_seen_at),
            missing_since = NULL,
            observations  = quality.presence.observations
                            + CASE WHEN EXCLUDED.last_seen_at
                                        > quality.presence.last_seen_at
                                   THEN 1 ELSE 0 END
        """,
        {"etype": cfg.entity_type, "chain_sid": CHAIN_SOURCE_ID},
    ).rowcount


def rescore_liveness(
    conn: psycopg.Connection,
    cfg: TableLiveness,
    *,
    now_sql: str = "now()",
) -> int:
    """Push the formula down to one UPDATE. Returns rows touched.

    Reads quality.presence (the ledger), core.chain_sites (the operator's own
    word) and, where configured, the licence columns already on the row.

    Written as a single statement on purpose: scoring 629k-row-class tables
    row-by-row in Python was measured at minutes; this is one pass.
    """
    table = _check_ident(cfg.table)
    pk = _check_ident(cfg.pk_col)
    hl = cfg.half_life_sql or f"{float(cfg.half_life_days)}"
    match_m = int(cfg.chain_match_m)

    # Three-valued on purpose. licence_expired is NULL for every shop outside
    # the two states with a registry (NY, NJ), and "no registry covers this
    # shop" must not read as "licence is fine" — so expired and active are
    # computed separately rather than as each other's negation.
    if cfg.licence_expired_col:
        lic = _check_ident(cfg.licence_expired_col)
        licence_expired_sql = f"COALESCE(x.{lic}, FALSE)"
        licence_active_sql = f"(x.{lic} IS NOT NULL AND NOT x.{lic})"
    else:
        licence_expired_sql = "FALSE"
        licence_active_sql = "FALSE"

    closed_sql = "(%(closed_flag)s = ANY(x.flags))" if cfg.closed_flag else "FALSE"

    sql = f"""
UPDATE {table} AS t
SET last_seen_at  = c.last_seen_at,
    last_seen_src = c.last_seen_src,
    live_presence = c.p_pct,
    live_sources  = c.s_pct,
    live_corrob   = c.a_pct,
    liveness      = c.score,
    live_state    = c.state,
    live_reasons  = c.reasons
FROM (
    -- Outermost level does presentation only: bucket the score and assemble
    -- the audit trail. The arithmetic happened once, in `m`.
    SELECT m.{pk},
           m.last_seen_at,
           m.last_seen_src,
           m.p_pct, m.s_pct, m.a_pct,
           CASE WHEN m.closed THEN 0::smallint ELSE m.raw_score END AS score,
           CASE
             WHEN m.closed          THEN 'closed'
             WHEN m.raw_score >= 75 THEN 'open'
             WHEN m.raw_score >= 50 THEN 'likely_open'
             WHEN m.raw_score >= 25 THEN 'unknown'
             ELSE 'likely_closed'
           END AS state,
           (CASE WHEN m.chain_brand IS NOT NULL
                 THEN ARRAY['chain_confirmed:' || m.chain_brand]
                 ELSE ARRAY[]::text[] END
            || CASE WHEN m.lic_active THEN ARRAY['licence_active'] ELSE ARRAY[]::text[] END
            || CASE WHEN m.lic_exp    THEN ARRAY['licence_expired'] ELSE ARRAY[]::text[] END
            || CASE WHEN m.missing    THEN ARRAY['vanished_from_source'] ELSE ARRAY[]::text[] END
            || CASE WHEN m.closed     THEN ARRAY['closure_asserted'] ELSE ARRAY[]::text[] END
            || CASE WHEN m.last_seen_at IS NULL
                    THEN ARRAY['never_confirmed'] ELSE ARRAY[]::text[] END
           ) AS reasons
    FROM (
        -- The formula, evaluated exactly once per row.
        SELECT k.{pk}, k.last_seen_at, k.last_seen_src,
               k.chain_brand, k.missing, k.lic_exp, k.lic_active, k.closed,
               round(k.p * 100)::smallint AS p_pct,
               round(k.s * 100)::smallint AS s_pct,
               round(k.a * 100)::smallint AS a_pct,
               round(100 * LEAST(1.0, GREATEST(0.0,
                   {W_PRESENCE} * k.p + {W_SOURCES} * k.s + {W_CORROB} * k.a
                   - CASE WHEN k.missing THEN {MISSING_PENALTY} ELSE 0.0 END
                   - CASE WHEN k.lic_exp THEN {LICENCE_EXPIRED_PENALTY} ELSE 0.0 END
               )))::smallint AS raw_score
        FROM (
            -- Evidence gathering: the ledger on one side, the operator's own
            -- store locator on the other.
            SELECT x.{pk},
                   pr.last_seen_at,
                   pr.last_seen_src,
                   CASE WHEN pr.last_seen_at IS NULL THEN 0.0 ELSE
                       power(0.5,
                             GREATEST(extract(epoch FROM ({now_sql} - pr.last_seen_at)), 0)
                             / 86400.0 / ({hl}))
                   END::numeric AS p,
                   CASE COALESCE(pr.n_sources, 0)
                        WHEN 0 THEN 0.0 WHEN 1 THEN 0.5 WHEN 2 THEN 0.8
                        ELSE 1.0 END::numeric AS s,
                   CASE WHEN ch.brand IS NOT NULL OR {licence_active_sql}
                        THEN 1.0 ELSE 0.0 END::numeric AS a,
                   ch.brand                    AS chain_brand,
                   COALESCE(pr.missing, FALSE) AS missing,
                   {licence_expired_sql}       AS lic_exp,
                   {licence_active_sql}        AS lic_active,
                   {closed_sql}                AS closed
            FROM {table} x
            LEFT JOIN LATERAL (
                -- n_sources counts only sources that STILL assert existence;
                -- a source with missing_since set is evidence against, and is
                -- picked up separately as the `missing` penalty.
                SELECT max(p.last_seen_at)                           AS last_seen_at,
                       count(*) FILTER (WHERE p.missing_since IS NULL) AS n_sources,
                       bool_or(p.missing_since IS NOT NULL)          AS missing,
                       (array_agg(p.source_id ORDER BY p.last_seen_at DESC))[1]
                                                                     AS last_seen_src
                FROM quality.presence p
                WHERE p.entity_type = %(etype)s
                  AND p.entity_id   = x.{pk}::text
            ) pr ON TRUE
            LEFT JOIN LATERAL (
                SELECT cs.brand
                FROM core.chain_sites cs
                WHERE x.geom IS NOT NULL
                  AND ST_DWithin(cs.geom::geography, x.geom::geography, {match_m})
                ORDER BY cs.geom::geography <-> x.geom::geography
                LIMIT 1
            ) ch ON TRUE
        ) k
    ) m
) c
WHERE t.{pk} = c.{pk}
"""
    params: dict = {"etype": cfg.entity_type}
    if cfg.closed_flag:
        params["closed_flag"] = cfg.closed_flag
    cur = conn.execute(sql, params)
    return cur.rowcount


def record_presence(
    conn: psycopg.Connection,
    entity_type: str,
    source_id: str,
    rows: Mapping[str, datetime],
) -> tuple[int, int]:
    """Write one source's testimony into the ledger.

    `rows` maps entity_id -> the source's own observation vintage.

    Returns (seen, newly_missing). Anything this source previously asserted
    and no longer does gets missing_since stamped; anything that reappears
    has it cleared, because upstream extracts do wobble and one bad pull is
    not a closure.
    """
    if not rows:
        return (0, 0)
    payload = [(entity_type, eid, source_id, ts, ts) for eid, ts in rows.items()]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO quality.presence
                (entity_type, entity_id, source_id, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id, source_id) DO UPDATE SET
                last_seen_at  = GREATEST(quality.presence.last_seen_at,
                                         EXCLUDED.last_seen_at),
                missing_since = NULL,
                observations  = quality.presence.observations + 1
            """,
            payload,
        )
    gone = conn.execute(
        """
        UPDATE quality.presence p
        SET missing_since = now()
        WHERE p.entity_type = %(etype)s
          AND p.source_id   = %(sid)s
          AND p.missing_since IS NULL
          AND NOT (p.entity_id = ANY(%(ids)s))
        """,
        {"etype": entity_type, "sid": source_id, "ids": list(rows.keys())},
    ).rowcount
    return (len(rows), gone)
