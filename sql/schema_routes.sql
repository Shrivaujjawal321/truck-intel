-- ============================================================
-- core.truck_routes — the truck-DESIGNATED route spine.
--
-- Source: BTS/FHWA NTAD "National Network" ArcGIS FeatureServer. These are the
-- highways on which STAA dimensioned trucks (102-inch width, 48-ft semitrailer,
-- twin 28-ft trailers) have federal access under 23 CFR Part 658 Appendix A /
-- the Surface Transportation Assistance Act of 1982. This is the truck network
-- as a matter of LAW — not a filter applied to a general road network.
--
-- SCOPE RULE (owner, non-negotiable): truck routes ONLY. The NTAD layer carries
-- 478,999 polylines; 24,169 of them have NN=0 (present in the file but NOT on
-- the National Network). The parser keeps ONLY NN>0 (454,830) — the NN=0 rows
-- are never published here. See registry/ntad_national_network.yaml.
--
-- HONESTY:
--   observed_at = 2018 (every row's own YEAR field), NEVER the download date.
--   The layer's own metadata says it "should not be used for truck size and
--   weight enforcement purposes or for navigation" — advisory routing only.
--   route_name is often blank at source (LNAME=' ' on ~411k rows) and is then
--   synthesized from the signed ref (SIGNT1 + SIGNN1); route_id is the source
--   integer ID (verified unique), NOT ROUTEID (state-scoped, collides).
-- ============================================================
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.truck_routes (
    route_id      BIGINT PRIMARY KEY,              -- NTAD source `ID` (unique: 478,999 distinct)
    route_name    TEXT,                            -- LNAME if present, else synthesized ref
    route_ref     TEXT,                            -- signed ref, e.g. 'I 95' (SIGNT1 + SIGNN1)
    sign_type     TEXT,                            -- SIGNT1  (I / US / SR / ...)
    sign_num      TEXT,                            -- SIGNN1
    routeid_state TEXT,                            -- ROUTEID — state-scoped, NOT unique (kept for reference)
    nn            SMALLINT NOT NULL,               -- National Network flag; always > 0 here
    state_fips    SMALLINT,                        -- STFIPS
    state         CHAR(2),                         -- USPS code mapped from STFIPS
    county_fips   INTEGER,                         -- full 5-digit FIPS (STFIPS*1000 + CTFIPS) when both present
    fclass        SMALLINT,                        -- functional class (FCLASS)
    aadt          INTEGER,                         -- annual average daily traffic
    aadt_com      INTEGER,                         -- commercial (truck) AADT — a first-class truck signal
    through_lanes SMALLINT,                        -- THROUGH_LA
    geom          geometry(MultiLineString, 4326) NOT NULL,
    -- lineage (every published row carries these — repo honesty rule)
    source_id     TEXT NOT NULL,
    run_id        BIGINT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_at   TIMESTAMPTZ,                     -- 2018 (row YEAR), never the download date
    -- quality (quality ladder; scored later, NULL for now — tri-state honest)
    confidence    SMALLINT,
    conf_trust    SMALLINT,
    conf_fresh    SMALLINT,
    conf_complete SMALLINT,
    conf_agree    SMALLINT,
    flags         TEXT[] NOT NULL DEFAULT '{}',
    props         JSONB NOT NULL DEFAULT '{}'
);

-- Spatial index for display / bbox queries.
CREATE INDEX IF NOT EXISTS truck_routes_geom_gix ON core.truck_routes USING GIST (geom);
-- Geography functional index — the buffer/proximity path (brief §7: ~240x vs
-- planar hand-rolled bbox; ST_DWithin(geom::geography, shop::geography, 5000)).
CREATE INDEX IF NOT EXISTS truck_routes_geog_gix ON core.truck_routes USING GIST ((geom::geography));
CREATE INDEX IF NOT EXISTS truck_routes_ref_ix   ON core.truck_routes (route_ref);
CREATE INDEX IF NOT EXISTS truck_routes_state_ix  ON core.truck_routes (state);
