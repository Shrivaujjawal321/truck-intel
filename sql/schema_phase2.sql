-- truck-intel Phase-2 foundation schema (MASTER_PLAN §5.1 naming map, §10 Phase 2;
-- design/storage.md tables translated to the §3.1 canonical rulings:
-- snapshot-swap lineage instead of SCD2, INCHES for clearances, NULL = unknown).
-- Idempotent: safe to re-apply, additive only. Apply with:
--   ./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_phase2.sql
-- (or: make schema-phase2). Requires sql/schema.sql applied first.

BEGIN;

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS osm;      -- ODbL-isolated OSM mirrors, loaded in wave 2
CREATE SCHEMA IF NOT EXISTS quality;

-- ============================================================
-- ops.sources — Phase-2 registry keys (both OPTIONAL in YAML)
--   parser: module name in truckintel/parsers/ (engine falls back to its
--           hardcoded MVP map when NULL)
--   target: schema-qualified snapshot_swap table; validated at sync time
--           against the §5.1 allow-list (registry.SNAPSHOT_TARGETS) — never
--           an unvalidated identifier
-- schedule_minutes / slo_hours become nullable: NULL schedule = event-driven
-- source (never enqueued by the tick; enqueued explicitly, e.g. the post-swap
-- rescore hook). NULL slo = no freshness SLO applies.
-- ============================================================
ALTER TABLE ops.sources ADD COLUMN IF NOT EXISTS parser TEXT;
ALTER TABLE ops.sources ADD COLUMN IF NOT EXISTS target TEXT;
ALTER TABLE ops.sources ALTER COLUMN schedule_minutes DROP NOT NULL;
ALTER TABLE ops.sources ALTER COLUMN slo_hours DROP NOT NULL;

-- Synthetic derived source for the post-swap confidence rescore job
-- (quality track implements the job runner; the engine only enqueues it).
-- kind='derived' is registry-less by design: sync_sources never disables
-- derived rows, and the engine worker never claims their jobs.
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    ('quality_rescore',
     'Synthetic: post-snapshot-swap confidence rescore',
     'truck-intel quality track',
     'derived', 'derived', NULL, NULL, TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING;

-- Synthetic derived source for the nightly quality ladder, seeded HERE (not
-- only lazily by quality_nightly.py's first run) so its 36 h freshness SLO is
-- alertable from day zero: on a fresh deploy where the truckintel-quality
-- timer was never enabled, scripts/freshness_check.py must report
-- "no successful run ever" for quality_nightly exactly like a dead feed
-- (binding ruling §3.1-D3) — without this seed the SLO row does not exist and
-- the check reads "all sources fresh" forever. slo_hours mirrors
-- quality_nightly.NIGHTLY_SLO_HOURS.
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    ('quality_nightly',
     'Synthetic: nightly quality ladder (gate 4 + confidence rescore)',
     'truck-intel quality track',
     'derived', 'derived', NULL, 36, TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING;

-- ============================================================
-- ops.feed_health — per-source circuit breaker (pipeline.md §10.3, §11).
-- Written by the engine on every finished run; read by jobs.enqueue_due.
-- State machine:
--   closed    — normal; consecutive_failures counts 'failed' runs
--   open      — >= 5 consecutive failures; enqueue_due skips the source until
--               opened_at + cooldown_minutes elapses
--   half_open — cooldown elapsed and ONE probe job enqueued (the partial
--               unique index on ops.job_queue enforces the "one"); probe
--               success -> closed + reset, probe failure -> open again with a
--               fresh opened_at
-- Composition with the exponential backoff in jobs.enqueue_due: the two AND
-- together. Backoff spaces individual retries (5 min doubling, cap 6 h);
-- the breaker hard-stops scheduling after sustained failure, gives ops a
-- queryable state ('is this feed dead?'), and meters recovery to one probe.
-- ============================================================
CREATE TABLE IF NOT EXISTS ops.feed_health (
    source_id            TEXT PRIMARY KEY REFERENCES ops.sources ON DELETE CASCADE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    state                TEXT NOT NULL DEFAULT 'closed',  -- closed | open | half_open
    opened_at            TIMESTAMPTZ,                     -- when the circuit last opened
    cooldown_minutes     INTEGER NOT NULL DEFAULT 60,     -- per-source tunable
    last_success_at      TIMESTAMPTZ,
    last_failure_at      TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- core.tunnels — FHWA National Tunnel Inventory (~500 rows), snapshot_swap.
-- research/tunnels.md: NTI natural key = state FIPS + tunnel number
-- (state_code_i3 + tunnel_number_i1); clearance from
-- min_vert_clearance_over_tunnel_roadway_g2 (FEET -> INCHES (SNTI G1/G2 are US customary feet, live-verified 2026-07-22), plan §5 units
-- ruling, matching core.bridges.min_vert_clearance_in).
-- HONEST: hazmat_codes are the SNTI coded flags only — detailed
-- class/quantity/escort rules have no machine-readable national source; the
-- hand-curated rules file is referenced via rules_curated_ref.
-- ============================================================
CREATE TABLE IF NOT EXISTS core.tunnels (
    tunnel_id             TEXT PRIMARY KEY,        -- state FIPS + NTI tunnel number
    name                  TEXT,                    -- NTI tunnel_name_i2
    state                 CHAR(2),                 -- USPS code
    geom                  geometry(Point, 4326) NOT NULL,
    length_ft             NUMERIC(8,1),            -- NULL = unknown
    min_vert_clearance_in NUMERIC(6,1),            -- inches; NULL = unknown (renders "unknown")
    hazmat_restricted     BOOLEAN,                 -- NULL = unknown, never "no"
    hazmat_codes          TEXT[],                  -- SNTI coded restriction flags; NULL = unknown
    rules_curated_ref     TEXT,                    -- key into data/curated/tunnel_rules.yaml
                                                   -- (PANYNJ, MDTA, VDOT, MassDOT...); NULL = codes only
    -- lineage
    source_id             TEXT NOT NULL,
    run_id                BIGINT NOT NULL,
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at           TIMESTAMPTZ,             -- NTI publication vintage, never fetch date
    -- quality (components stored 0-100, quality-ai.md §9; scored by the quality track)
    confidence            SMALLINT,
    conf_trust            SMALLINT,
    conf_fresh            SMALLINT,
    conf_complete         SMALLINT,
    conf_agree            SMALLINT,
    flags                 TEXT[] NOT NULL DEFAULT '{}',
    props                 JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS tunnels_geom_gix ON core.tunnels USING GIST (geom);
CREATE INDEX IF NOT EXISTS tunnels_state_ix ON core.tunnels (state);

-- ============================================================
-- osm.* — unconflated ODbL mirrors (ruling §3.1-4a: OSM lives ONLY in this
-- schema, joined at query time). Empty now; wave 2 loads them via snapshot_swap.
-- ============================================================

-- osm.ways — osmium-filtered HIGHWAYS ONLY from the Geofabrik US PBF
-- (ruling §3.1-5); the conflation input for NBI->OSM matching.
CREATE TABLE IF NOT EXISTS osm.ways (
    way_id        BIGINT PRIMARY KEY,              -- OSM way id
    highway       TEXT NOT NULL,                   -- motorway | trunk | primary | ...
    name          TEXT,
    ref           TEXT,                            -- route ref, e.g. 'I 95'
    maxheight_in  NUMERIC(6,1),                    -- parsed maxheight tag; NULL = untagged (unknown)
    maxweight_lb  NUMERIC(9,0),                    -- parsed maxweight tag; NULL = untagged
    hgv           TEXT,                            -- raw hgv tag value ('yes','no','designated',...)
    geom          geometry(LineString, 4326) NOT NULL,
    -- lineage
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,                     -- PBF extract timestamp
    -- quality
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}'      -- full truck-relevant tag map
);
CREATE INDEX IF NOT EXISTS osm_ways_geom_gix ON osm.ways USING GIST (geom);
CREATE INDEX IF NOT EXISTS osm_ways_highway_ix ON osm.ways (highway);

-- osm.fuel_stations — amenity=fuel (~109k US). Tri-state booleans by decree:
-- NULL = tag absent = unknown, never "no" (storage.md DEF honesty rule).
CREATE TABLE IF NOT EXISTS osm.fuel_stations (
    osm_id        TEXT PRIMARY KEY,                -- 'node/123' | 'way/456' (ids collide across types)
    name          TEXT,
    brand         TEXT,
    state         CHAR(2),
    has_diesel    BOOLEAN,
    hgv_access    BOOLEAN,                         -- hgv=yes / fuel:HGV_diesel
    has_def       BOOLEAN,                         -- fuel:adblue — sparse; usually NULL (unknown)
    geom          geometry(Point, 4326) NOT NULL,
    -- lineage
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,
    -- quality
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS osm_fuel_geom_gix ON osm.fuel_stations USING GIST (geom);

-- osm.rest_areas — highway=rest_area / services
CREATE TABLE IF NOT EXISTS osm.rest_areas (
    osm_id        TEXT PRIMARY KEY,
    name          TEXT,
    state         CHAR(2),
    geom          geometry(Point, 4326) NOT NULL,
    -- lineage
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,
    -- quality
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS osm_rest_areas_geom_gix ON osm.rest_areas USING GIST (geom);

-- osm.truck_repair — shop=truck_repair, plus car-repair shops that declare a
-- truck/trailer capability via service:vehicle:*. OSM is the ONLY independent
-- truck-specific corroboration available: every Overture source feed belongs
-- to one of three organisations, and the state licence registries cover all
-- vehicle repair without a truck flag. ODbL stays here in osm.* and is joined
-- at query time — core.mechanic_shops only ever receives a match FLAG.
CREATE TABLE IF NOT EXISTS osm.truck_repair (
    osm_id        TEXT PRIMARY KEY,
    name          TEXT,
    brand         TEXT,
    state         CHAR(2),
    truck_repair  BOOLEAN,                     -- shop=truck_repair or service:vehicle:truck_repair=yes
    trailer_repair BOOLEAN,                    -- service:vehicle:trailer_repair=yes
    hgv_access    BOOLEAN,
    geom          geometry(Point, 4326) NOT NULL,
    -- lineage
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,
    -- quality
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}'  -- full tag dict; opening_hours lives here
);
CREATE INDEX IF NOT EXISTS osm_truck_repair_geom_gix ON osm.truck_repair USING GIST (geom);

-- osm.weigh_points — amenity=weighbridge / highway=weigh_station
CREATE TABLE IF NOT EXISTS osm.weigh_points (
    osm_id        TEXT PRIMARY KEY,
    name          TEXT,
    state         CHAR(2),
    geom          geometry(Point, 4326) NOT NULL,
    -- lineage
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,
    -- quality
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS osm_weigh_points_geom_gix ON osm.weigh_points USING GIST (geom);

-- ============================================================
-- quality.conflicts — gate-4 cross-source disagreements (quality-ai.md §7).
-- Persisted, never resolve-and-forget: display takes highest authority,
-- routing takes most restrictive; open conflicts feed confidence penalties.
-- entity_id is TEXT (our entity PKs are natural TEXT keys, e.g. nbi_id).
-- ============================================================
CREATE TABLE IF NOT EXISTS quality.conflicts (
    conflict_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type TEXT NOT NULL,           -- 'bridges' | 'tunnels' | 'restrictions' | ...
    entity_id   TEXT NOT NULL,           -- natural key in that core table
    field       TEXT NOT NULL,           -- e.g. 'min_vert_clearance_in'
    value_a     TEXT,
    source_a    TEXT,
    value_b     TEXT,
    source_b    TEXT,
    delta       NUMERIC,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | closed | human_resolved
    opened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);
-- Re-checking a still-open conflict every run must be idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS conflicts_open_uq
    ON quality.conflicts (entity_type, entity_id, field, source_a, source_b)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS conflicts_entity_ix
    ON quality.conflicts (entity_type, entity_id) WHERE status = 'open';

-- ============================================================
-- quality.ai_decisions — full audit of every model verdict (quality-ai.md §10).
-- Cache key (job, input_hash, decided_by); human rows override ai rows.
-- ============================================================
CREATE TABLE IF NOT EXISTS quality.ai_decisions (
    decision_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job            TEXT NOT NULL,        -- 'poi_dedup' | 'address_norm' | ...
    input_hash     TEXT NOT NULL,        -- sha256 of the canonicalized input
    model          TEXT,
    prompt_version TEXT,
    input          JSONB,
    verdict        JSONB,
    rationale      TEXT,
    decided_by     TEXT NOT NULL DEFAULT 'ai',   -- ai | human
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job, input_hash, decided_by)
);

-- ============================================================
-- Quality-column backfill (plan §5.2: confidence + components + flags on EVERY
-- entity table). schema.sql already carries them on the MVP tables; these
-- idempotent ALTERs guarantee the full set on any environment that predates it.
-- ============================================================
ALTER TABLE core.bridges       ADD COLUMN IF NOT EXISTS confidence SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_trust SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_fresh SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_complete SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_agree SMALLINT,
                               ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE core.parking_sites ADD COLUMN IF NOT EXISTS confidence SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_trust SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_fresh SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_complete SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_agree SMALLINT,
                               ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE core.live_events   ADD COLUMN IF NOT EXISTS confidence SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_trust SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_fresh SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_complete SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_agree SMALLINT,
                               ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE core.fuel_prices   ADD COLUMN IF NOT EXISTS confidence SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_trust SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_fresh SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_complete SMALLINT,
                               ADD COLUMN IF NOT EXISTS conf_agree SMALLINT,
                               ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';

COMMIT;
