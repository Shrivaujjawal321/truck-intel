-- Per-edge restriction limits, so a vehicle profile can constrain the SEARCH
-- rather than be checked after a route is already chosen. Idempotent.
--
-- Run AFTER sql/route_graph.sql + route_noding.sql.
-- Apply: make route-limits
--
-- What binds a truck on an edge, and what does not:
--
--   HEIGHT   Two different NBI items, and using the wrong one invents warnings.
--            * a structure CARRYING this road limits us by its over-deck
--              clearance (items 10 / 53) — usually open sky, so usually nothing
--            * a structure CROSSING ABOVE this road limits us by its
--              underclearance (item 54B) — this is the classic low overpass
--            Tunnels limit us by their own clearance.
--
--   WEIGHT   A rating is not a restriction. An unposted bridge is legally open
--            to legal loads no matter what it rates, so rating alone must not
--            block anything. Only these bind:
--              item 41 = K  -> closed to all traffic, block outright
--              item 41 in (P,R,B,D) -> restricted; the operating rating (item 64,
--                                      metric tons) is the usable limit
--            Measured: posted bridges average 28.3 t against 57.5 t for open
--            ones, so the two populations are genuinely different.
--            Only structures CARRYING this road count — a posted county road on
--            an overpass above us is not our weight limit.
--
--   HAZMAT   Tunnels flagged hazmat_restricted (135 nationally).
--
--   LENGTH / WIDTH  Deliberately absent, and not a gap. 23 CFR 658 forbids any
--            state from imposing a width limit other than 102 in, or a
--            semitrailer length limit below 48 ft, ON THE NATIONAL NETWORK —
--            which is exactly the network this graph is built from. There is no
--            per-edge dataset because federal law makes it uniform. (Checked:
--            osm.ways carries maxlength_in / maxwidth_in columns and has 0 rows
--            populated for either, over a Delaware-only extract.) Oversize loads
--            beyond statutory limits need state permits — out of scope here.

DROP TABLE IF EXISTS route.edge_limits;

-- Normalise NBI free text the way a sign reads: 'I-35 SB' -> 'I35SB'.
CREATE OR REPLACE FUNCTION route.norm_ref(txt text) RETURNS text AS $$
    SELECT regexp_replace(upper(coalesce(txt, '')), '[^A-Z0-9]', '', 'g');
$$ LANGUAGE sql IMMUTABLE;

-- The prefixes a truck-route sign type is written with in NBI free text.
CREATE OR REPLACE FUNCTION route.sign_tokens(sign_type text, sign_num text)
RETURNS text[] AS $$
    SELECT CASE upper(coalesce(sign_type, ''))
        WHEN 'I' THEN ARRAY['I', 'IH', 'INTERSTATE']
        WHEN 'U' THEN ARRAY['US', 'USH', 'USHWY', 'U']
        WHEN 'S' THEN ARRAY['SH', 'SR', 'STATE', 'S']
        WHEN 'C' THEN ARRAY['CR', 'CORD', 'COUNTY', 'C']
        WHEN 'F' THEN ARRAY['FM', 'F']
        WHEN 'M' THEN ARRAY['M']
        WHEN 'N' THEN ARRAY['N']
        WHEN 'O' THEN ARRAY['O']
        WHEN 'R' THEN ARRAY['R']
        WHEN 'E' THEN ARRAY['E']
        WHEN 'T' THEN ARRAY['T']
        ELSE ARRAY[]::text[]
    END;
$$ LANGUAGE sql IMMUTABLE;

-- Does this structure carry the road we are driving?
CREATE OR REPLACE FUNCTION route.carries_route(
    facility text, sign_type text, sign_num text
) RETURNS boolean AS $$
    SELECT CASE
        WHEN facility IS NULL OR facility = '' OR sign_num IS NULL THEN NULL
        ELSE EXISTS (
            SELECT 1
            FROM unnest(route.sign_tokens(sign_type, sign_num)) AS p
            WHERE route.norm_ref(facility) LIKE p || route.norm_ref(sign_num) || '%'
              -- 'I35SB' carries I-35; 'I3512' is a different route.
              AND substring(route.norm_ref(facility)
                            FROM length(p || route.norm_ref(sign_num)) + 1 FOR 1)
                  !~ '^[0-9]$'
        )
    END;
$$ LANGUAGE sql IMMUTABLE;

-- NBI codes clearances in metres with 99.99 meaning "unlimited".
CREATE OR REPLACE FUNCTION route.clr_in(txt text) RETURNS double precision AS $$
    SELECT CASE
        WHEN txt IS NULL OR txt !~ '^[0-9.]+$' THEN NULL
        WHEN txt::float <= 0 OR txt::float = 99.99 THEN NULL
        ELSE round((txt::float * 39.3700787)::numeric, 1)::float
    END;
$$ LANGUAGE sql IMMUTABLE;

-- Everything from here runs in ONE transaction: psql is autocommit, so an
-- ON COMMIT DROP temp table would vanish the instant it was created.
BEGIN;

-- One row per (edge, structure) pair that actually constrains that edge.
CREATE TEMP TABLE edge_constraint ON COMMIT DROP AS
-- Bridges. 30 m: tight enough that a structure on the next carriageway does not
-- bind us, wide enough to catch the one directly overhead.
SELECT e.edge_id,
       CASE WHEN route.carries_route(b.props->>'FACILITY_CARRIED_007',
                                     e.sign_type, e.sign_num)
            THEN LEAST(route.clr_in(b.props->>'MIN_VERT_CLR_010'),
                       route.clr_in(b.props->>'VERT_CLR_OVER_MT_053'))
            ELSE route.clr_in(b.props->>'VERT_CLR_UND_054B')
       END                                                        AS clearance_in,
       -- Conservative on ignorance: if NBI recorded no facility carried, or the
       -- edge has no route number to match against, we cannot tell whether we
       -- drive on this structure. A posted bridge we might be driving on is a
       -- real hazard, so the limit is applied rather than dropped. (Measured: 12
       -- posted structures on 10 unnamed edges nationally — negligible for
       -- connectivity, not negligible for a driver.)
       CASE WHEN route.carries_route(b.props->>'FACILITY_CARRIED_007',
                                     e.sign_type, e.sign_num) IS NOT FALSE
                 AND b.posting_status IN ('P', 'R', 'B', 'D')
                 AND b.operating_rating ~ '^[0-9.]+$'
                 AND b.operating_rating::float > 0
            THEN round(b.operating_rating::float * 2204.62)
       END                                                        AS max_weight_lb,
       (route.carries_route(b.props->>'FACILITY_CARRIED_007',
                            e.sign_type, e.sign_num) IS NOT FALSE
        AND b.posting_status = 'K')                               AS closes_road,
       false                                                      AS hazmat,
       (route.clr_in(b.props->>'MIN_VERT_CLR_010') IS NULL
        AND route.clr_in(b.props->>'VERT_CLR_OVER_MT_053') IS NULL
        AND route.clr_in(b.props->>'VERT_CLR_UND_054B') IS NULL)  AS clearance_unknown,
       b.nbi_id                                                   AS structure_id,
       'bridge'                                                   AS structure_kind
FROM route.edges e
JOIN core.bridges b
  ON b.geom && ST_Expand(e.geom, 30 / 70000.0)
 AND ST_DWithin(e.geom::geography, b.geom::geography, 30)
WHERE e.kind = 'truck_route'

UNION ALL

-- Tunnels: you drive through them, so their clearance and hazmat rules bind.
SELECT e.edge_id,
       t.min_vert_clearance_in::float,
       NULL::double precision,
       false,
       coalesce(t.hazmat_restricted, false),
       (t.min_vert_clearance_in IS NULL),
       t.tunnel_id,
       'tunnel'
FROM route.edges e
JOIN core.tunnels t
  ON t.geom && ST_Expand(e.geom, 30 / 70000.0)
 AND ST_DWithin(e.geom::geography, t.geom::geography, 30)
WHERE e.kind = 'truck_route';

CREATE INDEX edge_constraint_ix ON edge_constraint (edge_id);
ANALYZE edge_constraint;

CREATE TABLE route.edge_limits AS
SELECT edge_id,
       min(clearance_in)                              AS min_clearance_in,
       min(max_weight_lb)                             AS max_weight_lb,
       bool_or(closes_road)                           AS closed,
       bool_or(hazmat)                                AS hazmat_blocked,
       count(*)::int                                  AS structures,
       count(*) FILTER (WHERE clearance_unknown)::int AS structures_unknown_clearance
FROM edge_constraint
GROUP BY edge_id;

ALTER TABLE route.edge_limits ADD PRIMARY KEY (edge_id);
CREATE INDEX edge_limits_clearance_ix ON route.edge_limits (min_clearance_in);
CREATE INDEX edge_limits_weight_ix    ON route.edge_limits (max_weight_lb);
CREATE INDEX edge_limits_blocked_ix   ON route.edge_limits (closed, hazmat_blocked);

COMMIT;

ANALYZE route.edge_limits;
