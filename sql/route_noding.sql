-- Node the truck graph: split edges where another edge's endpoint lands on their
-- interior. Re-runnable — running it again when nothing is unnoded is a no-op.
--
-- Run AFTER sql/route_graph.sql, and re-run scripts/route_components.py +
-- sql/route_snap_index.sql afterwards, because both depend on the topology.
--
-- The problem this fixes: the NTAD network is published as geometry, not as a
-- graph. Segments meet visually but do not always share a node — measured, 1,053
-- of 1,053 sampled dead ends sat within ~11 m of another edge without sharing a
-- node with it. A router cannot turn at a junction that is not a node, so
-- Dallas -> Oklahoma City came back 312 mi via US-81 instead of 205 mi on I-35:
-- not a longer road, a road the graph could not turn onto.
--
-- Scope is small and precise: 3,394 interior incidences across 3,287 edges, at a
-- 0.00001 degree (~1.1 m) tolerance. Tight on purpose — a loose tolerance would
-- weld together roads that merely pass near each other, e.g. at an overpass.
--
-- Nothing is invented here. No geometry is moved and no connection is added that
-- the published geometry does not already assert; the edges are only cut at
-- points that already lie on them.

BEGIN;

-- 1. Where does a node land in the middle of an edge it does not belong to?
CREATE TEMP TABLE splits ON COMMIT DROP AS
SELECT e.edge_id,
       n.node_id,
       ST_LineLocatePoint(e.geom, n.geom) AS frac
FROM route.edges e
JOIN route.nodes n ON ST_DWithin(e.geom, n.geom, 0.00001)
WHERE e.kind = 'truck_route'
  AND n.node_id <> e.source
  AND n.node_id <> e.target
  AND ST_LineLocatePoint(e.geom, n.geom) > 0.000001
  AND ST_LineLocatePoint(e.geom, n.geom) < 0.999999;

-- 2. Cut list per affected edge: its own two ends plus every interior hit.
CREATE TEMP TABLE cuts ON COMMIT DROP AS
SELECT DISTINCT ON (edge_id, frac) edge_id, frac, node_id
FROM (
    SELECT edge_id, 0.0::double precision AS frac, NULL::bigint AS node_id
    FROM (SELECT DISTINCT edge_id FROM splits) a
    UNION ALL
    SELECT edge_id, 1.0, NULL FROM (SELECT DISTINCT edge_id FROM splits) b
    UNION ALL
    SELECT edge_id, frac, node_id FROM splits
) u
ORDER BY edge_id, frac, node_id NULLS LAST;

-- 3. Consecutive cut pairs become the replacement pieces.
CREATE TEMP TABLE pieces ON COMMIT DROP AS
SELECT edge_id, frac AS f0, node_id AS n0,
       lead(frac)    OVER w AS f1,
       lead(node_id) OVER w AS n1
FROM cuts
WINDOW w AS (PARTITION BY edge_id ORDER BY frac);

-- 4. Emit the pieces, inheriting every attribute of the edge they came from.
--    A piece that starts at 0 keeps the original source node; one that ends at 1
--    keeps the original target; interior boundaries use the node that caused the
--    split, which is what actually joins the two roads.
CREATE TEMP TABLE new_edges ON COMMIT DROP AS
SELECT (SELECT max(edge_id) FROM route.edges) + row_number() OVER () AS edge_id,
       e.route_id,
       coalesce(p.n0, e.source) AS source,
       coalesce(p.n1, e.target) AS target,
       ST_LineSubstring(e.geom, p.f0, p.f1)::geometry(LineString, 4326) AS geom,
       ST_Length(ST_LineSubstring(e.geom, p.f0, p.f1)::geography) AS length_m,
       e.kind, e.route_name, e.route_ref, e.sign_type, e.sign_num, e.state,
       e.fclass, e.aadt, e.aadt_com, e.through_lanes
FROM pieces p
JOIN route.edges e ON e.edge_id = p.edge_id
WHERE p.f1 IS NOT NULL
  AND p.f1 > p.f0
  AND ST_Length(ST_LineSubstring(e.geom, p.f0, p.f1)::geography) > 0.01;

DELETE FROM route.edges WHERE edge_id IN (SELECT DISTINCT edge_id FROM splits);

INSERT INTO route.edges (
    edge_id, route_id, source, target, geom, length_m, kind,
    route_name, route_ref, sign_type, sign_num, state,
    fclass, aadt, aadt_com, through_lanes
)
SELECT edge_id, route_id, source, target, geom, length_m, kind,
       route_name, route_ref, sign_type, sign_num, state,
       fclass, aadt, aadt_com, through_lanes
FROM new_edges;

-- 5. Degrees moved: nodes that were mid-edge are now real junctions.
UPDATE route.nodes n SET degree = d.deg
FROM (
    SELECT node_id, count(*)::int AS deg
    FROM (SELECT source AS node_id FROM route.edges
          UNION ALL SELECT target FROM route.edges) x
    GROUP BY node_id
) d
WHERE d.node_id = n.node_id AND n.degree IS DISTINCT FROM d.deg;

COMMIT;

ANALYZE route.edges;
ANALYZE route.nodes;
