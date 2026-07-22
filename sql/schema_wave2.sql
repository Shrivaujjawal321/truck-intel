-- truck-intel Wave-2 foundation schema (MASTER_PLAN §3.1-4/5/6 rulings, §5
-- naming map, §6 DEF ruling; design/storage.md businesses DDL translated to
-- the canonical snapshot-swap lineage — no SCD2 valid_from/valid_to).
-- Idempotent: safe to re-apply, additive only. Apply with:
--   ./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_wave2.sql
-- (or: make schema-wave2). Requires schema.sql + schema_phase2.sql first.

BEGIN;

-- pg_trgm for the businesses name-similarity index (conflation blocking,
-- quality-ai.md §3.2, and /v1/places fuzzy search). postgis exists already.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS staging;

-- ============================================================
-- core.businesses — conflated truck-relevant POIs (ruling §3.1-4c: Overture +
-- FSQ attributes ONLY — no OSM attribute values are EVER copied in; OSM POIs
-- stay in the osm schema as query-time corroboration, keeping this table
-- permissively licensed: CDLA-Permissive-2.0 + Apache-2.0).
-- Load: the businesses_conflate DERIVED job (ruling §3.1-6) rebuilds the
-- whole table from staging.overture_places + staging.fsq_places, then swaps
-- atomically — never snapshot_swap per source.
--
-- business_id (stable deterministic conflation key, computed by the conflate
-- job — storage.md canonical_key, renamed per the entity-id convention):
--   business_id = 'biz_' || substr(sha256_hex(norm_name || '|' || geohash7), 1, 16)
--   norm_name = lower(name) with every char outside [a-z0-9] removed
--   geohash7  = 7-char geohash (~153 m cell — matches the 150 m conflation
--               blocking radius, quality-ai.md §3.2) of the canonical point
--               (the contributing source with the highest source confidence)
-- Deterministic and replayable: same inputs -> same key across rebuilds.
--
-- category — OUR ~27-slug truck taxonomy (quality-ai.md §10.3; AI job 3 maps
-- the long tail, 'unclassified' is always legal and the safe default):
--   truck_stop fuel_station def_retail truck_repair mobile_repair
--   trailer_repair auto_repair tire_service towing truck_wash truck_parts
--   auto_parts truck_dealer cat_scale weigh_station truck_parking rest_area
--   restaurant fast_food cafe grocery motel hotel medical pharmacy laundry
--   atm_bank | unclassified
--
-- HONESTY on the two GENERAL-vehicle slugs (added 2026-07-22): a stranded
-- tractor can legitimately use a general/brake/transmission/engine shop or a
-- general auto-parts store (fluids, filters, DEF jugs, bulbs, batteries), so
-- we surface them — but under HONEST general labels, NOT relabeled as
-- truck-specific:
--   auto_repair = general vehicle repair (Overture automotive_repair /
--                 brake / transmission / engine / electrical / exhaust;
--                 FSQ "Automotive Repair Shop"). Distinct from the
--                 truck-specific truck_repair / trailer_repair / mobile_repair
--                 — those keep their own slugs and are NEVER collapsed here.
--   auto_parts  = general vehicle parts retail (Overture
--                 automotive_parts_and_accessories; FSQ "Car Parts and
--                 Accessories"). Distinct from the truck-specific truck_parts.
-- We never claim a general shop is truck-specialized; the honest general slug
-- is the truthful label a trucker can act on.
--
-- def — §6 DEF ruling (binding): the ONLY permitted inference in the whole
-- platform. Deterministic brand->DEF config in git may set it to 'inferred'
-- (renders "def": "inferred", never as fact); NULL = unknown, never "no".
-- No other value is legal — the CHECK makes fabrication structurally
-- impossible.
--
-- HONEST: no ratings/reviews/price columns exist (no free legal source);
-- phone/website fill rates are materially below Google's and stay NULL-honest;
-- observed_at = the newest contributing source's own vintage (Overture
-- release date / FSQ date_refreshed), never the load date.
-- ============================================================
CREATE TABLE IF NOT EXISTS core.businesses (
    business_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL
        CONSTRAINT businesses_category_taxonomy CHECK (category IN (
            'truck_stop', 'fuel_station', 'def_retail', 'truck_repair',
            'mobile_repair', 'trailer_repair',
            -- general-vehicle slugs (honest, NOT truck-specific; see header):
            'auto_repair', 'auto_parts',
            'tire_service', 'towing',
            'truck_wash', 'truck_parts', 'truck_dealer', 'cat_scale',
            'weigh_station', 'truck_parking', 'rest_area', 'restaurant',
            'fast_food', 'cafe', 'grocery', 'motel', 'hotel', 'medical',
            'pharmacy', 'laundry', 'atm_bank', 'unclassified')),
    brand         TEXT,                    -- normalized via Name Suggestion Index
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL,
    geom          geometry(Point, 4326) NOT NULL,
    address       TEXT,
    city          TEXT,
    state         CHAR(2),                 -- USPS code
    zip           TEXT,
    address_norm  TEXT,                    -- written by AI job 2 (address residue);
                                           -- re-segmentation of the input ONLY
    phone         TEXT,                    -- NULL-honest, fill rate low
    website       TEXT,
    present_in    TEXT[] NOT NULL          -- multi-source agreement; NEVER 'osm'
        CONSTRAINT businesses_present_in_permissive CHECK (
            present_in <@ ARRAY['overture', 'fsq'] AND cardinality(present_in) >= 1),
    def           TEXT                     -- §6: 'inferred' | NULL(unknown), nothing else
        CONSTRAINT businesses_def_inferred_only CHECK (def IS NULL OR def = 'inferred'),
    -- lineage (ruling §3.1-1; source_id = 'businesses_conflate', the derived
    -- job; per-source blobs live in props under keys 'overture' / 'fsq' —
    -- the documented merged-table exception, MASTER_PLAN §5.2)
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,             -- newest source vintage, never load date
    -- quality (components stored 0-100, quality-ai.md §9; rescored nightly)
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}',
    -- FTS (storage.md: Postgres FTS + pg_trgm, no Elasticsearch)
    search_tsv    tsvector GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(name, '') || ' ' || coalesce(brand, '') || ' ' ||
            coalesce(city, ''))) STORED
);
CREATE INDEX IF NOT EXISTS businesses_geom_gix  ON core.businesses USING GIST (geom);
CREATE INDEX IF NOT EXISTS businesses_tsv_gix   ON core.businesses USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS businesses_name_trgm ON core.businesses USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS businesses_cat_ix    ON core.businesses (category, state);

-- ============================================================
-- staging.overture_places / staging.fsq_places — permanent DDL, per-run
-- scratch (§5.1: truncated at the start of every conflate run, no history).
-- The DuckDB extract steps land raw-ish rows here; the conflate job blocks,
-- scores, merges, and rebuilds core.businesses from them.
-- ============================================================
CREATE TABLE IF NOT EXISTS staging.overture_places (
    source_record_id TEXT,                 -- Overture GERS id
    name             TEXT,
    brand            TEXT,
    category_source  TEXT,                 -- raw Overture category slug
    category         TEXT,                 -- mapped truck-taxonomy slug; NULL = unmapped
    lat              DOUBLE PRECISION,
    lon              DOUBLE PRECISION,
    address          TEXT,
    city             TEXT,
    state            CHAR(2),
    zip              TEXT,
    phone            TEXT,
    website          TEXT,
    src_confidence   NUMERIC,              -- Overture per-place confidence 0-1
    observed_at      TIMESTAMPTZ,          -- Overture release vintage
    run_id           BIGINT,
    props            JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS staging_overture_rec_ix
    ON staging.overture_places (source_record_id);

CREATE TABLE IF NOT EXISTS staging.fsq_places (
    source_record_id TEXT,                 -- FSQ fsq_place_id
    name             TEXT,
    brand            TEXT,
    category_source  TEXT,                 -- raw FSQ category label/id
    category         TEXT,                 -- mapped truck-taxonomy slug; NULL = unmapped
    lat              DOUBLE PRECISION,
    lon              DOUBLE PRECISION,
    address          TEXT,
    city             TEXT,
    state            CHAR(2),
    zip              TEXT,
    phone            TEXT,
    website          TEXT,
    date_refreshed   DATE,                 -- FSQ freshness signal (observed_at basis)
    date_closed      DATE,                 -- dead-business filter; NULL = not closed
    observed_at      TIMESTAMPTZ,
    run_id           BIGINT,
    props            JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS staging_fsq_rec_ix
    ON staging.fsq_places (source_record_id);

-- ============================================================
-- Derived-source seeds (ruling §3.1-6; same pattern as quality_rescore /
-- quality_nightly in schema_phase2.sql). kind='derived' rows are registry-less
-- by design: sync_sources never disables them, the engine worker never claims
-- their jobs — engine._DERIVED_RUNNERS dispatches them to their scripts.
-- schedule_minutes NULL = event-driven (enqueued manually / by a weekly
-- timer later, never by the tick). slo_hours 400 (~16.7 days) is a generous
-- freshness SLO for weekly/monthly rebuild cadences.
-- ============================================================
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    ('osm_pois',
     'Derived: OSM POI mirrors (fuel/rest/weigh) from Geofabrik US PBF -> osm.*',
     'truck-intel wave-2 OSM track',
     'derived', 'derived', NULL, 400, TRUE, 'verified'),
    ('osm_ways',
     'Derived: osmium-filtered highways from Geofabrik US PBF -> osm.ways (§3.1-5)',
     'truck-intel wave-2 OSM track',
     'derived', 'derived', NULL, 400, TRUE, 'verified'),
    ('businesses_conflate',
     'Derived: Overture + FSQ conflation -> core.businesses rebuild (§3.1-6)',
     'truck-intel wave-2 businesses track',
     'derived', 'derived', NULL, 400, TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING;

COMMIT;
