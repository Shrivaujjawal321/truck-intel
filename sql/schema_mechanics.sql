-- core.mechanic_* — the truck-mechanic layer.
--
-- This DDL lived inside scripts/mechanic_list.py as two Python strings until
-- 2026-08-19, which meant a database built from the repo alone never had these
-- tables. Nothing noticed, because sql/schema_liveness.sql — which references
-- core.mechanic_shops — was itself missing from every apply path until
-- 2026-08-18. The moment it was added, CI built a clean container, applied the
-- schema in order and failed on:
--
--     ERROR:  relation "core.mechanic_shops" does not exist
--
-- which is exactly what a clean-container CI is for, and exactly the failure
-- shape ci.yml's own header describes for schema_tracking.sql.
--
-- scripts/mechanic_list.py's ensure_schema() now reads THIS file, so there is
-- one definition rather than two that can drift.

CREATE TABLE IF NOT EXISTS core.mechanic_shops (
    shop_id        TEXT PRIMARY KEY,          -- Overture id
    name           TEXT,
    category_src   TEXT,                      -- Overture category slug
    category       TEXT,                      -- our label
    brand          TEXT,
    lat            DOUBLE PRECISION,
    lon            DOUBLE PRECISION,
    geom           geometry(Point, 4326),
    address        TEXT,
    city           TEXT,
    state          CHAR(2),
    zip            TEXT,
    phone          TEXT,
    website        TEXT,
    email          TEXT,
    socials        TEXT[],
    src_confidence DOUBLE PRECISION,          -- Overture confidence 0-1
    n_sources      INTEGER,                   -- RAW Overture source-dataset count
    source_names   TEXT[],
    -- independence (computed in --verify; see INDEPENDENCE note below)
    source_orgs    TEXT[],                    -- source_names collapsed to owning orgs
    n_independent  SMALLINT,                  -- distinct orgs — the honest count
    -- route enrichment (from core.truck_routes)
    route_id       BIGINT,
    route_ref      TEXT,
    route_name     TEXT,
    route_dist_m   INTEGER,
    on_route_5km   BOOLEAN,
    gmaps_url      TEXT,
    -- verification (computed in --verify)
    phone_valid    BOOLEAN,
    phone_state_ok BOOLEAN,
    coord_ok       BOOLEAN,
    cluster_dup    BOOLEAN,
    spam_flag      BOOLEAN,
    verification_status TEXT,                 -- verified | probable | unverified
    confidence     INTEGER,                   -- 0-100
    flags          TEXT[] DEFAULT '{}',
    -- state licence join (--licence): the only GENUINELY independent vote we
    -- can get, because every Overture* pipeline belongs to one organisation.
    licence_verified BOOLEAN,                 -- NULL = state not covered, never "no"
    licence_id     TEXT,
    licence_state  CHAR(2),
    licence_expiry DATE,
    licence_expired BOOLEAN,
    licence_rule   TEXT,                      -- which match rule fired
    -- hours (--chains): permissive chain feeds + OSM, never scraped from Google
    opening_hours  TEXT,                      -- OSM opening_hours syntax
    open_24h       BOOLEAN,
    hours_source   TEXT,                      -- alltheplaces | osm
    chain_brand    TEXT,
    -- OSM corroboration (--osm-match): independent of Overture AND of the
    -- state registries; ODbL stays in osm.*, only the match flag lands here.
    osm_match_id   TEXT,
    osm_match_m    INTEGER,
    observed_at    TIMESTAMPTZ,               -- Overture release vintage
    props          JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS mechanic_shops_geom_gix ON core.mechanic_shops USING GIST (geom);
CREATE INDEX IF NOT EXISTS mechanic_shops_state_ix ON core.mechanic_shops (state);

-- Mirror of the state licence registries we can legally bulk-download. Kept as
-- its own table (not merged into mechanic_shops) because it is a REGISTRY, not
-- a shop list: 54k NY rows cover every vehicle-repair class, so joining is a
-- verification act, never a discovery one (DEEP_DIVE §4).
CREATE TABLE IF NOT EXISTS core.mechanic_licences (
    licence_key   TEXT PRIMARY KEY,           -- '<state>/<licence_id>/<n>'
    state         CHAR(2) NOT NULL,
    licence_id    TEXT,
    name          TEXT,
    name_norm     TEXT,                       -- normalised for matching
    address       TEXT,
    addr_norm     TEXT,                       -- normalised for matching
    city          TEXT,
    zip           TEXT,
    licence_type  TEXT,
    expiry        DATE,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    geom          geometry(Point, 4326),
    source_url    TEXT,
    observed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS mechanic_licences_geom_gix ON core.mechanic_licences USING GIST (geom);
CREATE INDEX IF NOT EXISTS mechanic_licences_norm_ix ON core.mechanic_licences (state, name_norm);

-- Fill history: one row per metric per refresh. Exists to answer the question
-- a snapshot cannot — "did today's run actually LEARN anything?" Without it a
-- daily refresh that silently stopped finding new detail would look identical
-- to one that is working.
CREATE TABLE IF NOT EXISTS core.mechanic_fill_history (
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metric      TEXT NOT NULL,
    filled      INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    PRIMARY KEY (snapshot_at, metric)
);

-- Per-state coverage denominator from Census County Business Patterns. Answers
-- the one question a shop count alone cannot: is South Dakota EMPTY, or merely
-- UNLISTED? (DEEP_DIVE §5.)
CREATE TABLE IF NOT EXISTS core.mechanic_coverage (
    state          CHAR(2) PRIMARY KEY,
    cbp_year       SMALLINT NOT NULL,
    cbp_estab_811111 INTEGER,                 -- General automotive repair
    cbp_estab_811310 INTEGER,                 -- Commercial/industrial machinery repair
    shops          INTEGER NOT NULL,
    shops_on_route INTEGER NOT NULL,
    route_miles    DOUBLE PRECISION,
    miles_per_shop DOUBLE PRECISION,
    capture_rate   DOUBLE PRECISION,          -- shops / cbp_estab_811111
    verdict        TEXT,                      -- thin_data | real_scarcity | ok
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- migrate
ALTER TABLE core.mechanic_shops
  ADD COLUMN IF NOT EXISTS source_orgs      TEXT[],
  ADD COLUMN IF NOT EXISTS n_independent    SMALLINT,
  ADD COLUMN IF NOT EXISTS licence_verified BOOLEAN,
  ADD COLUMN IF NOT EXISTS licence_id       TEXT,
  ADD COLUMN IF NOT EXISTS licence_state    CHAR(2),
  ADD COLUMN IF NOT EXISTS licence_expiry   DATE,
  ADD COLUMN IF NOT EXISTS licence_expired  BOOLEAN,
  ADD COLUMN IF NOT EXISTS licence_rule     TEXT,
  ADD COLUMN IF NOT EXISTS opening_hours    TEXT,
  ADD COLUMN IF NOT EXISTS open_24h         BOOLEAN,
  ADD COLUMN IF NOT EXISTS hours_source     TEXT,
  ADD COLUMN IF NOT EXISTS chain_brand      TEXT,
  ADD COLUMN IF NOT EXISTS osm_match_id     TEXT,
  ADD COLUMN IF NOT EXISTS osm_match_m      INTEGER;

ALTER TABLE core.mechanic_licences
  ADD COLUMN IF NOT EXISTS addr_norm TEXT;
CREATE INDEX IF NOT EXISTS mechanic_licences_addr_ix
  ON core.mechanic_licences (state, addr_norm);
