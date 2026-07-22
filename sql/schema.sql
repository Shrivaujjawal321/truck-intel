-- truck-intel MVP schema (MASTER_PLAN §5, MVP subset per §11).
-- Idempotent: safe to re-apply. Apply with:
--   ./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema.sql

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS ops;      -- registry, queue, run audit
CREATE SCHEMA IF NOT EXISTS staging;  -- per-source scratch tables, created per run
CREATE SCHEMA IF NOT EXISTS core;     -- what the app reads
CREATE SCHEMA IF NOT EXISTS osm;      -- ODbL-isolated OSM mirrors (empty in MVP, Phase 2)
CREATE SCHEMA IF NOT EXISTS quality;  -- rejects (MVP); conflicts + ai_decisions in Phase 2

-- ============================================================
-- ops.sources — synced from registry/*.yaml (git is the source of truth)
-- ============================================================
CREATE TABLE IF NOT EXISTS ops.sources (
    source_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    owner            TEXT,
    url              TEXT,
    kind             TEXT NOT NULL,      -- bulk_http | arcgis | live_json | api_keyed
    load_pattern     TEXT NOT NULL,      -- snapshot_swap | event_lifecycle | upsert
    schedule_minutes INTEGER NOT NULL,
    slo_hours        INTEGER NOT NULL,   -- freshness SLO: alert if no success within this window
    license          TEXT,
    attribution_text TEXT,
    gates            JSONB NOT NULL DEFAULT '{}',  -- {"min_rows": ..., "max_row_delta_pct": ...}
    auth             JSONB,              -- {"env": "EIA_API_KEY"} or NULL
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    verify_status    TEXT NOT NULL DEFAULT 'verified',  -- verified | uncertain | broken
    -- Trust plumbing (ruling §3.1-2). Scoring formula is Phase 2.
    authority_class  TEXT,               -- federal | state | curated | open_aggregate | community
    base_trust       NUMERIC(3,2),
    trust            NUMERIC(3,2),
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- ops.source_runs — one row per fetch attempt (success, skip, or failure)
-- ============================================================
CREATE TABLE IF NOT EXISTS ops.source_runs (
    run_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES ops.sources,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         TEXT NOT NULL,        -- running | success | skipped_unchanged |
                                         -- skipped_no_key | gated | failed
    rows_in        INTEGER,
    rows_published INTEGER,
    rows_rejected  INTEGER,
    message        TEXT,
    raw_sha256     TEXT,                 -- hash of the raw file in data/raw/ (replayable)
    http_status    INTEGER
);
CREATE INDEX IF NOT EXISTS source_runs_source_ix
    ON ops.source_runs (source_id, started_at DESC);

-- ============================================================
-- ops.job_queue — FOR UPDATE SKIP LOCKED work queue
-- ============================================================
CREATE TABLE IF NOT EXISTS ops.job_queue (
    job_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES ops.sources,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    message     TEXT
);
-- Ruling §3.1-10: partial unique index, NOT UNIQUE(source_id, status).
-- Exactly one queued-or-running job per source; unlimited completed history.
CREATE UNIQUE INDEX IF NOT EXISTS job_queue_one_active
    ON ops.job_queue (source_id) WHERE status IN ('queued', 'running');

-- ============================================================
-- core tables. Conventions (plan §5.2), applied to every core table:
--   lineage:  source_id, run_id, ingested_at, observed_at
--             (observed_at = when the fact was true in the world, NEVER the
--              download date; NULL = unknown)
--   quality:  confidence + 4 components, all NULL in MVP — the columns exist
--             now, the scoring formula lands in Phase 2 (plan §11, by rule)
--   flags TEXT[], props JSONB (full cleaned source record)
-- No FK from core to ops: snapshot_swap rebuilds table objects atomically;
-- lineage integrity is the engine's job, checked by audit queries.
-- ============================================================

-- core.bridges — FHWA NBI, snapshot_swap, ~624k rows
CREATE TABLE IF NOT EXISTS core.bridges (
    nbi_id                TEXT PRIMARY KEY,        -- state FIPS + NBI structure number
    name                  TEXT,                    -- facility carried / feature crossed
    state                 CHAR(2),                 -- USPS code
    geom                  geometry(Point, 4326) NOT NULL,
    min_vert_clearance_in NUMERIC(6,1),            -- inches, converted from NBI meters;
                                                   -- NULL = unknown (renders "unknown")
    operating_rating      TEXT,                    -- NBI item 64 code (metric tons) — codes,
    inventory_rating      TEXT,                    -- NBI item 66 code    not signed values
    posting_status        TEXT,                    -- NBI item 41 code: open / posted / closed
    -- lineage
    source_id             TEXT NOT NULL,
    run_id                BIGINT NOT NULL,
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at           TIMESTAMPTZ,
    -- quality (columns only in MVP)
    confidence            SMALLINT,
    conf_trust            SMALLINT,
    conf_fresh            SMALLINT,
    conf_complete         SMALLINT,
    conf_agree            SMALLINT,
    flags                 TEXT[] NOT NULL DEFAULT '{}',
    props                 JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS bridges_geom_gix ON core.bridges USING GIST (geom);
CREATE INDEX IF NOT EXISTS bridges_state_ix ON core.bridges (state);

-- core.parking_sites — NTAD Truck Stop Parking, snapshot_swap, ~1,915 rows.
-- HONEST: observed_at is the ~2019 Jason's Law survey era, not the download date.
CREATE TABLE IF NOT EXISTS core.parking_sites (
    site_id       TEXT PRIMARY KEY,                -- NTAD natural key
    kind          TEXT NOT NULL,                   -- truck_stop | public_rest_area |
                                                   -- weigh_station | parking_lot
    name          TEXT,
    state         CHAR(2),
    truck_spaces  SMALLINT,                        -- NULL = unknown, never 0-as-unknown
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
CREATE INDEX IF NOT EXISTS parking_sites_geom_gix ON core.parking_sites USING GIST (geom);

-- core.live_events — NWS alerts (MVP), event_lifecycle.
-- Upsert on (source_id, event_id); vanished events get soft_closed_at set,
-- rows are never deleted by the poller (90-day archive job is Phase 2).
CREATE TABLE IF NOT EXISTS core.live_events (
    event_id       TEXT NOT NULL,                  -- the feed's own id (NWS CAP id)
    source_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,                  -- weather_alert (MVP)
    geom           geometry(Geometry, 4326),       -- polygon/multipolygon; NULL when the
                                                   -- feed gives zone references only
    first_seen     TIMESTAMPTZ NOT NULL,
    last_seen      TIMESTAMPTZ NOT NULL,           -- poller heartbeat
    soft_closed_at TIMESTAMPTZ,                    -- NULL = active
    -- lineage
    run_id         BIGINT NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at    TIMESTAMPTZ,                    -- alert issue time, not fetch time
    -- quality
    confidence     SMALLINT,
    conf_trust     SMALLINT,
    conf_fresh     SMALLINT,
    conf_complete  SMALLINT,
    conf_agree     SMALLINT,
    flags          TEXT[] NOT NULL DEFAULT '{}',
    props          JSONB NOT NULL DEFAULT '{}',    -- severity, headline, onset/expires, ...
    PRIMARY KEY (source_id, event_id)
);
CREATE INDEX IF NOT EXISTS live_events_geom_gix
    ON core.live_events USING GIST (geom) WHERE soft_closed_at IS NULL;
CREATE INDEX IF NOT EXISTS live_events_active_ix
    ON core.live_events (kind) WHERE soft_closed_at IS NULL;

-- core.fuel_prices — EIA weekly regional averages, upsert, native time series.
-- HONEST: regional weekly estimates, never station-level pump prices
-- (no free legal station-level source exists — plan §2 honest gaps).
CREATE TABLE IF NOT EXISTS core.fuel_prices (
    region        TEXT NOT NULL,                   -- 'US', 'PADD1', 'PADD1A', ..., 'CA'
    product       TEXT NOT NULL,                   -- 'diesel' (MVP)
    week_of       DATE NOT NULL,
    price_usd_gal NUMERIC(5,3) NOT NULL,
    -- lineage
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,                     -- the survey week, not fetch time
    -- quality
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (region, product, week_of)
);

-- ============================================================
-- quality.rejects — every gate rejection, replayable
-- ============================================================
CREATE TABLE IF NOT EXISTS quality.rejects (
    reject_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   TEXT NOT NULL,
    run_id      BIGINT NOT NULL,
    reason      TEXT NOT NULL,       -- e.g. 'missing_required:lat', 'latlon_swapped'
    raw_record  JSONB NOT NULL,
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rejects_run_ix ON quality.rejects (run_id);

COMMIT;
