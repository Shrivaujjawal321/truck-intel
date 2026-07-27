-- Low-zoom generalization for the map viewer. Additive and idempotent.
--
-- Why this exists: core.truck_routes is 454,830 *segments* averaging a quarter
-- mile each. A z4 tile covers ~110k of them. Serving that per-tile is slow, and
-- capping it with a LIMIT would silently drop most of the network — the viewer
-- would look complete while showing a fraction. Repo honesty rule: no silent caps.
--
-- Instead, dissolve to one geometry per (state, sign_type, sign_num) corridor —
-- 3,282 rows — and simplify to ~200 m. Below zoom 8 the viewer draws this table
-- (whole network, generalized); at zoom 8+ it draws the raw segments. Nothing is
-- ever dropped; only the level of detail changes.
--
-- Apply:  make schema-viewer      (or: ./scripts/db_psql.sh < sql/viewer_generalized.sql)
-- Refresh after a truck_routes reload:  REFRESH MATERIALIZED VIEW core.truck_routes_gen;

DROP MATERIALIZED VIEW IF EXISTS core.truck_routes_gen;

CREATE MATERIALIZED VIEW core.truck_routes_gen AS
SELECT
    state || ':' || coalesce(sign_type, '?') || ':' || coalesce(sign_num, '?') AS route_id,
    -- One corridor may carry several names/refs across counties; keep one, and
    -- keep the segment count so the popup is honest about what was merged.
    min(route_name)                          AS route_name,
    min(route_ref)                           AS route_ref,
    sign_type,
    sign_num,
    state,
    max(aadt)                                AS aadt,
    max(aadt_com)                            AS aadt_com,
    count(*)::int                            AS segments,
    -- Order matters. ST_Collect over MultiLineStrings returns a
    -- GeometryCollection, and ST_LineMerge silently returns EMPTY for one — so
    -- extract the line parts FIRST, then stitch, then simplify. On IA I-80 that
    -- is 12,809 points -> 75, with 305.0 mi -> 304.5 mi (0.2% length loss).
    ST_Multi(
        ST_SimplifyPreserveTopology(
            ST_LineMerge(ST_CollectionExtract(ST_Collect(geom), 2)),
            0.002   -- degrees, ~200 m: invisible below zoom 8, big win in tile size
        )
    )::geometry(MultiLineString, 4326)       AS geom
FROM core.truck_routes
GROUP BY state, sign_type, sign_num;

CREATE INDEX truck_routes_gen_geom_gix ON core.truck_routes_gen USING GIST (geom);
CREATE UNIQUE INDEX truck_routes_gen_pk ON core.truck_routes_gen (route_id);

ANALYZE core.truck_routes_gen;
