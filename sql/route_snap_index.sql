-- Fast "nearest MAINLAND truck route" lookup for snapping. Idempotent.
--
-- Run AFTER sql/route_graph.sql and scripts/route_components.py.
--
-- Why a separate table instead of a component column on route.edges: a KNN
-- search (`ORDER BY geom <-> point`) can only use a GIST index, and a GIST index
-- cannot be filtered by a column living in another table. Denormalising the
-- component onto route.edges was measured at >10 min (full-table rewrite, WAL
-- bound). Materialising just the mainland edges is a fraction of that, adds no
-- bloat to route.edges, and is trivially rebuilt.
--
-- Why it exists at all: the nearest truck route to a point is often a
-- disconnected stub. Downtown Oklahoma City snaps to component 107 while
-- Oklahoma has 8,614 nodes on the mainland — without a mainland option the
-- router refuses a route that plainly exists about a kilometre away.
--
-- Apply:  make route-snap-index
-- Rebuild after components change: REFRESH MATERIALIZED VIEW route.mainland_edges;

DROP MATERIALIZED VIEW IF EXISTS route.mainland_edges;

CREATE MATERIALIZED VIEW route.mainland_edges AS
SELECT e.edge_id, e.source, e.target, e.length_m, e.geom
FROM route.edges e
JOIN route.node_component c ON c.node_id = e.source
WHERE e.kind = 'truck_route' AND c.component = 1;

CREATE UNIQUE INDEX mainland_edges_pk   ON route.mainland_edges (edge_id);
CREATE INDEX        mainland_edges_gix  ON route.mainland_edges USING GIST (geom);

ANALYZE route.mainland_edges;
