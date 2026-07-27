-- Routable graph over the truck-designated network. Additive and idempotent.
--
-- Source is core.truck_routes ONLY — the NTAD National Network (NN=1). osm.ways
-- is the generic road graph and is never used here: a route this system returns
-- must be legal for a truck, so the graph it searches cannot contain a road that
-- is not truck-designated in the first place.
--
-- Two edge kinds, always distinguishable:
--   truck_route          one NTAD segment, geometry exactly as published
--   synthetic_connector  a straight line closing a gap <= 50 m between a DEAD END
--                        and its nearest node. Measured: 4,906 dead ends exist,
--                        2,339 sit within 50 m of another node, and bridging only
--                        those lifts the largest routable component from 62.7% to
--                        88.7% of the network. Only degree-1 nodes qualify, so this
--                        cannot fuse two roads that merely cross at an overpass.
--                        Every route response reports how many it used.
--
-- Apply:  make route-graph
-- Rebuild after a truck_routes reload: same command (it drops and recreates).

CREATE SCHEMA IF NOT EXISTS route;

-- route.mainland_edges is a MATERIALIZED VIEW over route.edges, created by
-- sql/route_snap_index.sql later in this same rebuild chain. On a virgin
-- database it does not exist and this is a no-op; on a RE-RUN it does, and it
-- blocks the DROP below with:
--
--   ERROR: cannot drop table route.edges because other objects depend on it
--   DETAIL: materialized view route.mainland_edges depends on table route.edges
--
-- So this file only ever worked once. `make route-graph` a second time failed,
-- and so did the first automated route_rebuild — which is how it was found
-- (2026-07-27): nobody had re-run the chain end to end before it was scheduled.
--
-- Dropped EXPLICITLY rather than with DROP TABLE ... CASCADE: cascade would
-- silently remove whatever happens to depend on route.edges, including objects
-- this file has never heard of. Naming the dependency means an unexpected one
-- still stops the rebuild loudly instead of being deleted quietly.
-- route_snap_index.sql recreates it, so nothing is lost.
DROP MATERIALIZED VIEW IF EXISTS route.mainland_edges;

DROP TABLE IF EXISTS route.edges;
DROP TABLE IF EXISTS route.nodes;

-- --- nodes: every distinct segment endpoint ---------------------------------
-- Endpoints coincide exactly in this dataset (verified: 455,414 distinct points
-- from 909,658 endpoint instances), so equality joins are sound — no snapping
-- tolerance is applied here, only at the connector step below.
CREATE TABLE route.nodes AS
WITH ends AS (
    SELECT ST_StartPoint(ST_GeometryN(geom, 1)) AS p
    FROM core.truck_routes WHERE ST_NumGeometries(geom) = 1
    UNION ALL
    SELECT ST_EndPoint(ST_GeometryN(geom, 1))
    FROM core.truck_routes WHERE ST_NumGeometries(geom) = 1
)
SELECT row_number() OVER (ORDER BY ST_X(p), ST_Y(p))::bigint AS node_id,
       p::geometry(Point, 4326)                             AS geom,
       count(*)::int                                        AS degree,
       NULL::int                                            AS component
FROM ends
GROUP BY p;

ALTER TABLE route.nodes ADD PRIMARY KEY (node_id);
CREATE UNIQUE INDEX nodes_geom_btree ON route.nodes (geom);   -- exact equality joins
CREATE INDEX nodes_geom_gix ON route.nodes USING GIST (geom); -- nearest-node lookups
ANALYZE route.nodes;

-- --- edges: one per NTAD segment --------------------------------------------
CREATE TABLE route.edges AS
WITH seg AS (
    SELECT route_id, route_name, route_ref, sign_type, sign_num, state,
           fclass, aadt, aadt_com, through_lanes,
           ST_GeometryN(geom, 1) AS g
    FROM core.truck_routes
    WHERE ST_NumGeometries(geom) = 1
)
SELECT row_number() OVER ()::bigint                     AS edge_id,
       seg.route_id,
       s.node_id                                        AS source,
       t.node_id                                        AS target,
       seg.g::geometry(LineString, 4326)                AS geom,
       ST_Length(seg.g::geography)                      AS length_m,
       'truck_route'::text                              AS kind,
       seg.route_name, seg.route_ref, seg.sign_type, seg.sign_num, seg.state,
       seg.fclass, seg.aadt, seg.aadt_com, seg.through_lanes
FROM seg
JOIN route.nodes s ON s.geom = ST_StartPoint(seg.g)
JOIN route.nodes t ON t.geom = ST_EndPoint(seg.g);

-- --- synthetic connectors: close measured dead-end gaps ---------------------
INSERT INTO route.edges (
    edge_id, route_id, source, target, geom, length_m, kind,
    route_name, route_ref, sign_type, sign_num, state,
    fclass, aadt, aadt_com, through_lanes
)
SELECT (SELECT max(edge_id) FROM route.edges) + row_number() OVER (),
       NULL, node_id, near_id,
       ST_MakeLine(a_geom, b_geom)::geometry(LineString, 4326),
       gap_m, 'synthetic_connector',
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
FROM (
    SELECT DISTINCT ON (least(n.node_id, m.node_id), greatest(n.node_id, m.node_id))
           n.node_id, n.geom AS a_geom, m.node_id AS near_id, m.geom AS b_geom, m.gap_m
    FROM route.nodes n
    CROSS JOIN LATERAL (
        SELECT o.node_id, o.geom,
               ST_Distance(n.geom::geography, o.geom::geography) AS gap_m
        FROM route.nodes o
        WHERE o.node_id <> n.node_id
        ORDER BY n.geom <-> o.geom
        LIMIT 1
    ) m
    WHERE n.degree = 1 AND m.gap_m <= 50
) q;

ALTER TABLE route.edges ADD PRIMARY KEY (edge_id);
CREATE INDEX edges_source_ix ON route.edges (source);
CREATE INDEX edges_target_ix ON route.edges (target);
CREATE INDEX edges_geom_gix  ON route.edges USING GIST (geom);
CREATE INDEX edges_kind_ix   ON route.edges (kind);
ANALYZE route.edges;
