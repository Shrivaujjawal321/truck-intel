-- truck-intel Gate 6 — LIVENESS.
--
-- WHY THIS EXISTS
-- ---------------
-- Gate 5 (truckintel/quality.py) answers "how much do I trust this RECORD?"
-- via confidence = 0.35*T + 0.25*F + 0.20*C + 0.20*A. It is a good answer to
-- the wrong question for a place-of-business.
--
-- Freshness there decays from observed_at — the vintage of the FACT. For a
-- bridge that is exactly right: the 2025 NBI row is a true statement about a
-- bridge that is still standing. For a truck stop it is not, because the
-- record can be perfectly well-formed, fully populated, federally sourced,
-- and describe a lot that was bulldozed in 2021.
--
-- The two questions are independent:
--
--     conf_fresh   "how old is this statement?"
--     liveness     "is the thing it describes still there?"
--
-- Boss put it plainly on 2026-08-17: "mechanic ki shop close hui vo nhi
-- dikhani chiye hame". Nothing in the schema could express that. A closed
-- shop and an open shop were byte-identical rows.
--
-- THE MEASUREMENT THAT FORCED THIS
-- --------------------------------
-- Vintage audit, 2026-08-17 (see the conversation log for the full table):
--
--     core.bridges        2025   fresh — annual NBI, safety-critical, fine
--     core.tunnels        2025   fresh
--     core.truck_routes   2018   legally anchored to 1 June 1991 by
--                                23 CFR 658 App. A; ~0.1% of 186,112 miles
--                                changed 2018-2026. Old but CORRECT.
--     core.parking_sites  2019   Jason's Law survey. BTS has published
--                                nothing newer; the FeatureServer itself
--                                still says "compiled on April 09, 2019".
--     core.mechanic_shops 2026-07-22   Overture release vintage
--     core.businesses     2026-07-22   Overture release vintage
--
-- The three stale layers are exactly the three whose subjects are BUSINESSES
-- — things a human opens and closes — and none of the three had any way to
-- say so. That is the gap this file closes.
--
-- WHAT IT DOES NOT DO (binding honesty)
-- -------------------------------------
-- This does not detect closure. Nothing free does. It records, per source,
-- WHEN something was last asserted to exist, and scores that honestly. A
-- place nobody has confirmed since 2019 is reported as `unknown`, not as
-- closed — absence of evidence is not evidence of absence, and a driver
-- routed away from a truck stop that is actually open is also a failure.
--
-- The one exception is a POSITIVE closure assertion from a source that
-- carries one (FSQ date_closed, OSM disused:/was: lifecycle prefixes). Those
-- set live_state='closed' directly. We do not infer that ourselves.
--
-- Idempotent: safe to re-apply, additive only. Apply with:
--   ./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_liveness.sql
-- (or: make schema-liveness). Requires schema.sql + schema_phase2.sql +
-- schema_wave2.sql first (core.businesses / parking_sites / mechanic_shops).

BEGIN;

CREATE SCHEMA IF NOT EXISTS quality;

-- ============================================================
-- quality.presence — the evidence ledger.
--
-- One row per (entity, source) pair: the source's own testimony that this
-- place existed, and when it last said so.
--
-- WHY A LEDGER AND NOT A COLUMN
-- Every place table is loaded snapshot_swap: the new pull replaces the old
-- table wholesale. That means a row which vanishes from the upstream file is
-- deleted, silently, with no trace that it was ever there. The disappearance
-- — the single most useful free closure signal available to us — was being
-- thrown away on every load.
--
-- This table survives the swap because it is keyed on the entity id, not on
-- the table's physical rows. When a load no longer carries an id we have seen
-- before, missing_since gets stamped and the evidence is kept.
--
-- missing_since is NULLed again if the entity reappears: upstream files do
-- wobble (a bad extract, a partial run), and a place is not closed because
-- one pull hiccuped. The count in observations is what separates "seen once,
-- then gone" from "seen 40 times, then gone".
CREATE TABLE IF NOT EXISTS quality.presence (
    entity_type   TEXT        NOT NULL,   -- parking_sites | mechanic_shops | businesses
    entity_id     TEXT        NOT NULL,   -- site_id / shop_id / business_id
    source_id     TEXT        NOT NULL,   -- who asserted it (ops.sources.source_id)
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,   -- fact vintage, NOT the fetch date
    missing_since TIMESTAMPTZ,            -- set when a run that should have listed it did not
    observations  INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_type, entity_id, source_id)
);

CREATE INDEX IF NOT EXISTS presence_entity_ix
    ON quality.presence (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS presence_missing_ix
    ON quality.presence (entity_type, missing_since)
    WHERE missing_since IS NOT NULL;

-- ============================================================
-- core.chain_sites — national chains' OWN store locators, persisted.
--
-- WHY THIS TABLE EXISTS AT ALL
-- All The Places (CC0) republishes each chain's own store-locator scrape.
-- scripts/mechanic_list.py --chains has been fetching five truck-relevant
-- spiders every single day since 2026-07-27 and loading them into a
-- `CREATE TEMP TABLE atp ... ON COMMIT DROP` — stamping opening_hours onto
-- mechanic_shops and then discarding the rest.
--
-- Measured from the 2026-08-17 05:00 run, that discarded payload was:
--     loves_us                      731 features
--     pilot_flying_j                722
--     travelcenters_of_america_us   362
--     fleetpride_us                 494
--     penske                       1763
--
-- 1,815 truck stops from Love's + Pilot + TA alone, refreshed daily, against
-- core.parking_sites' 1,915 rows of 2019 survey data. The freshest truck-stop
-- evidence in the system was being created and destroyed in the same
-- transaction, every morning, for three weeks.
--
-- WHY IT IS THE STRONGEST FREE SIGNAL
-- A chain's own store locator is not a third-party observation — it is the
-- operator stating which of its branches are open, because that page exists
-- to send customers there. No aggregator lag, no survey cycle. If Love's
-- stops listing a location, Love's closed it.
--
-- LIMIT, stated up front: chains only. Independent truck stops and one-bay
-- shops — roughly half the market — get nothing from this table. They fall
-- back to presence decay and to the state licence registries.
--
-- LICENCE: All The Places is CC0 (public domain dedication). Safe to store
-- and to serve, unlike the AAA feed.
CREATE TABLE IF NOT EXISTS core.chain_sites (
    chain_site_id TEXT PRIMARY KEY,        -- '<spider>:<atp ref>' — stable across runs
    spider        TEXT        NOT NULL,    -- All The Places spider name
    brand         TEXT,
    name          TEXT,
    street        TEXT,
    city          TEXT,
    state         CHAR(2),
    phone         TEXT,
    website       TEXT,
    opening_hours TEXT,
    open_24h      BOOLEAN,                 -- tri-state: NULL = not published
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL,
    geom          geometry(Point,4326) NOT NULL,
    observed_at   TIMESTAMPTZ NOT NULL,    -- the ATP run's own timestamp, never now()
    run_id        BIGINT      NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chain_sites_geom_gix ON core.chain_sites USING GIST (geom);
CREATE INDEX IF NOT EXISTS chain_sites_spider_ix ON core.chain_sites (spider);

-- ============================================================
-- Liveness columns on the three business-backed place tables.
--
-- Same discipline as Gate 5: every component is STORED, so "why is this 32?"
-- is always answerable from the row itself without re-running the scorer.
--
--   last_seen_at   most recent moment ANY source still asserted existence
--   last_seen_src  which source that was (so the claim is attributable)
--   liveness       0-100, the blended score
--   live_state     the bucket a UI should render
--   live_presence  component P — decay since last_seen_at
--   live_sources   component S — how many sources currently assert it
--   live_corrob    component A — an authoritative CURRENT confirmation
--                                (chain store locator / unexpired licence)
--   live_reasons   the human-readable audit trail, e.g.
--                  {chain_confirmed:loves_us, licence_active:NY}
--
-- live_state vocabulary (deliberately five, not two):
--   'open'           >= 75   something authoritative confirms it now
--   'likely_open'    >= 50
--   'unknown'        >= 25   old data, nothing current — SAY SO, do not guess
--   'likely_closed'  <  25
--   'closed'                 a source positively asserted closure
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['core.parking_sites',
                             'core.mechanic_shops',
                             'core.businesses']
    LOOP
        EXECUTE format($f$
            ALTER TABLE %s
              ADD COLUMN IF NOT EXISTS last_seen_at  TIMESTAMPTZ,
              ADD COLUMN IF NOT EXISTS last_seen_src TEXT,
              ADD COLUMN IF NOT EXISTS liveness      SMALLINT,
              ADD COLUMN IF NOT EXISTS live_state    TEXT,
              ADD COLUMN IF NOT EXISTS live_presence SMALLINT,
              ADD COLUMN IF NOT EXISTS live_sources  SMALLINT,
              ADD COLUMN IF NOT EXISTS live_corrob   SMALLINT,
              ADD COLUMN IF NOT EXISTS live_reasons  TEXT[] NOT NULL DEFAULT '{}'
        $f$, t);

        -- The bucket vocabulary is enforced, not merely documented: a typo in
        -- the scorer that wrote 'lively' would otherwise reach the API and be
        -- rendered to a driver.
        EXECUTE format($f$
            ALTER TABLE %s DROP CONSTRAINT IF EXISTS %s_live_state_ck
        $f$, t, split_part(t, '.', 2));
        EXECUTE format($f$
            ALTER TABLE %s ADD CONSTRAINT %s_live_state_ck
              CHECK (live_state IS NULL OR live_state IN
                     ('open','likely_open','unknown','likely_closed','closed'))
        $f$, t, split_part(t, '.', 2));

        EXECUTE format($f$
            CREATE INDEX IF NOT EXISTS %s_live_state_ix ON %s (live_state)
        $f$, split_part(t, '.', 2), t);
    END LOOP;
END $$;

-- ============================================================
-- The two synthetic sources this gate introduces. ops.source_runs has an FK
-- to ops.sources, so both must exist before either script records a run.
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status, authority_class, base_trust, trust)
VALUES
    ('chain_sites',
     'National truck-chain store locators via All The Places (CC0)',
     'All The Places / the chains themselves', 'bulk_http', 'snapshot_swap',
     1440, 72, TRUE, 'verified', 'curated', 0.85, 0.85),
    ('liveness',
     'Gate 6 liveness rescore (derived — presence ledger + corroboration)',
     'truck-intel', 'derived', 'derived',
     1440, 48, TRUE, 'verified', 'curated', 0.85, 0.85)
ON CONFLICT (source_id) DO NOTHING;

COMMIT;
