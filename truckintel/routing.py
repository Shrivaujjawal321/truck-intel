"""Shortest path over the truck-designated network.

The graph is `route.nodes` / `route.edges`, built from `core.truck_routes` — the
NTAD National Network. `osm.ways` is not in it. That is the whole point: a path
this module returns cannot include a road that is not truck-designated, because
no such road exists in the graph it searches.

Three honesty properties, each enforced here rather than left to the caller:

1. **Unreachable is reported, not approximated.** Nodes carry a `component` id.
   If pickup and drop land in different components there is no truck-legal path,
   and `route()` raises `NoTruckPath` instead of returning a nearest-miss.
2. **Synthetic connectors are counted.** Edges of kind `synthetic_connector` are
   inferred gap-closures (<= 50 m), not published geometry. Every result reports
   how many it used and how much distance they contributed.
3. **Off-network access distance is separate.** Pickup/drop rarely sit on a truck
   route. The distance from the requested point to where it joins the network is
   returned as `access_m`, never folded into the driving distance.

Search is A* with a haversine heuristic, which is admissible on this graph
(great-circle distance can never exceed distance along the roads), so the first
time the goal is popped the path is optimal.
"""
from __future__ import annotations

import heapq
import math
import threading
from dataclasses import dataclass, field
from typing import Iterable

from truckintel.db import get_conn

EARTH_R_M = 6_371_008.8


class RoutingError(Exception):
    """Base for routing failures the API turns into a typed error envelope."""

    code = "routing_error"


class GraphNotBuilt(RoutingError):
    code = "graph_not_built"


class NoNearbyRoute(RoutingError):
    """The requested point is not near any truck route."""

    code = "no_truck_route_nearby"


class NoTruckPath(RoutingError):
    """Both ends are on the network, but not on a connected part of it."""

    code = "no_truck_path"


class NoCompliantPath(RoutingError):
    """A truck path exists, but not one this vehicle may legally use."""

    code = "no_compliant_path"


# 23 CFR 658: no state may impose a width limit other than 102 in, or a
# semitrailer length limit below 48 ft, on the National Network. This graph IS
# the National Network, so a standard STAA vehicle is legal on every edge of it
# by regulation. Length and width are therefore validated against the statutory
# limits rather than looked up per edge — there is no per-edge dataset because
# the law removes the need for one.
STAA_MAX_WIDTH_IN = 102
STAA_MIN_SEMITRAILER_FT = 48
# Federal gross weight limit on the Interstate System.
FEDERAL_MAX_GROSS_LB = 80_000


@dataclass(frozen=True)
class VehicleProfile:
    """The truck. Every limit here is checked against the graph before a path is
    returned, so compliance is a property of the search, not a later report."""

    height_in: float | None = None      # e.g. 162 for 13'6"
    weight_lb: float | None = None      # gross
    length_ft: float | None = None      # semitrailer length
    width_in: float | None = None
    hazmat: bool = False

    def statutory_warnings(self) -> list[str]:
        """Dimensions the National Network does NOT guarantee. Returned rather
        than raised: it is legal to run them, but only under a state permit,
        and this system cannot plan a permit route."""
        out: list[str] = []
        if self.width_in and self.width_in > STAA_MAX_WIDTH_IN:
            out.append(
                f"width {self.width_in:g}\" exceeds the 102\" every state must "
                f"allow on the National Network (23 CFR 658.15) — oversize permit "
                f"territory, not routable from free data"
            )
        if self.length_ft and self.length_ft > 53:
            out.append(
                f"semitrailer {self.length_ft:g} ft is beyond the lengths states "
                f"must allow (48 ft floor, 53 ft in general practice) — permit "
                f"territory"
            )
        if self.weight_lb and self.weight_lb > FEDERAL_MAX_GROSS_LB:
            out.append(
                f"gross {self.weight_lb:,.0f} lb exceeds the federal 80,000 lb "
                f"Interstate limit — overweight permit territory; posted-bridge "
                f"avoidance below still applies"
            )
        return out

    @property
    def constrains_search(self) -> bool:
        return bool(self.height_in or self.weight_lb or self.hazmat)


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


@dataclass
class Snap:
    """Where an arbitrary lat/lon joins the truck network."""

    lon: float
    lat: float
    edge_id: int
    source: int
    target: int
    edge_length_m: float
    dist_from_source_m: float   # along the edge
    dist_to_target_m: float     # along the edge
    access_m: float             # off-network: point -> the road
    component: int
    snapped_lon: float
    snapped_lat: float


@dataclass
class RouteResult:
    edge_ids: list[int]
    node_path: list[int]
    distance_m: float           # on-network driving distance
    access_m: float             # pickup access + drop access, never mixed in
    connector_count: int
    connector_m: float
    origin: Snap
    destination: Snap
    explored: int = 0
    partial_first: tuple[int, float, float] | None = field(default=None)
    partial_last: tuple[int, float, float] | None = field(default=None)
    profile: "VehicleProfile | None" = field(default=None)
    edges_excluded: int = 0                       # forbidden to this vehicle
    exclusion_examples: list[str] = field(default_factory=list)
    structures_unknown_clearance: int = 0         # passed, but height not recorded


class TruckGraph:
    """In-memory adjacency over route.edges. Loaded once, read many.

    ~457k edges: small enough to hold, and holding it is what makes a
    cross-country query answer in well under a second without a routing daemon.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self.adj: dict[int, list[tuple[int, float, int]]] = {}
        self.xy: dict[int, tuple[float, float]] = {}
        self.component: dict[int, int] = {}
        self.connector_edges: set[int] = set()
        self.edge_count = 0
        # edge_id -> (min_clearance_in | None, max_weight_lb | None,
        #             closed, hazmat_blocked). Absent means nothing constrains it.
        self.limits: dict[int, tuple[float | None, float | None, bool, bool]] = {}
        self.has_limits = False

    # --- loading ------------------------------------------------------------

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load()
            self._loaded = True

    def _load(self) -> None:
        adj: dict[int, list[tuple[int, float, int]]] = {}
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('route.edges')")
                if cur.fetchone()[0] is None:
                    raise GraphNotBuilt(
                        "route.edges does not exist — run `make route-graph`"
                    )
                cur.execute("""
                    SELECT n.node_id, ST_X(n.geom), ST_Y(n.geom), c.component
                    FROM route.nodes n
                    LEFT JOIN route.node_component c USING (node_id)
                """)
                for node_id, x, y, comp in cur:
                    self.xy[node_id] = (x, y)
                    self.component[node_id] = comp
                    adj[node_id] = []

                cur.execute(
                    "SELECT edge_id, source, target, length_m, kind FROM route.edges"
                )
                for edge_id, src, tgt, length, kind in cur:
                    length = float(length)
                    # Undirected: the National Network carries no one-way flags,
                    # so claiming a direction would be inventing data.
                    adj[src].append((tgt, length, edge_id))
                    adj[tgt].append((src, length, edge_id))
                    if kind == "synthetic_connector":
                        self.connector_edges.add(edge_id)
                    self.edge_count += 1

                cur.execute("SELECT to_regclass('route.edge_limits')")
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        "SELECT edge_id, min_clearance_in, max_weight_lb, "
                        "closed, hazmat_blocked FROM route.edge_limits "
                        "WHERE min_clearance_in IS NOT NULL "
                        "   OR max_weight_lb IS NOT NULL "
                        "   OR closed OR hazmat_blocked"
                    )
                    for edge_id, clr, wt, closed, hz in cur:
                        self.limits[edge_id] = (
                            float(clr) if clr is not None else None,
                            float(wt) if wt is not None else None,
                            bool(closed), bool(hz),
                        )
                    self.has_limits = True
        self.adj = adj

    def blocked_for(self, edge_id: int, profile: VehicleProfile) -> str | None:
        """Why this vehicle may not use this edge, or None if it may.

        Only recorded limits block. An edge with no recorded clearance is NOT
        treated as impassable — 74% of NBI bridges have none, so doing that would
        delete most of the network. The route response reports how many such
        structures it passed, which is the honest form of that uncertainty.
        """
        limit = self.limits.get(edge_id)
        if limit is None:
            return None
        clearance, max_weight, closed, hazmat = limit
        if closed:
            return "structure closed to all traffic"
        if profile.hazmat and hazmat:
            return "hazmat prohibited through this tunnel"
        if (profile.height_in and clearance is not None
                and clearance < profile.height_in):
            return f"clearance {clearance:.0f}\" below vehicle {profile.height_in:g}\""
        if (profile.weight_lb and max_weight is not None
                and max_weight < profile.weight_lb):
            return f"posted {max_weight:,.0f} lb below vehicle {profile.weight_lb:,.0f} lb"
        return None

    # --- snapping -----------------------------------------------------------

    _SNAP_SQL = """
        WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS g),
        near AS (
            SELECT e.edge_id, e.source, e.target, e.geom, e.length_m, c.component
            FROM route.edges e
            JOIN route.node_component c ON c.node_id = e.source, p
            WHERE e.kind = 'truck_route'
            ORDER BY e.geom <-> p.g
            LIMIT %(k)s
        ),
        -- Guarantee the mainland is always an option. The nearest edge overall can
        -- sit on a disconnected stub (measured: downtown OKC snaps to component 107
        -- while Oklahoma has 8,614 nodes on the mainland), and without this the
        -- router would refuse a route that plainly exists 1 km away.
        mainland AS (
            SELECT m.edge_id, m.source, m.target, m.geom, m.length_m, 1 AS component
            FROM route.mainland_edges m, p
            ORDER BY m.geom <-> p.g
            LIMIT 1
        ),
        pool AS (SELECT * FROM near UNION ALL SELECT * FROM mainland)
        SELECT DISTINCT ON (component)
               edge_id, source, target, length_m, component,
               ST_Distance(geom::geography, p.g::geography)          AS access_m,
               ST_LineLocatePoint(geom, ST_ClosestPoint(geom, p.g))  AS frac,
               ST_X(ST_ClosestPoint(geom, p.g))                      AS sx,
               ST_Y(ST_ClosestPoint(geom, p.g))                      AS sy
        FROM pool, p
        ORDER BY component, ST_Distance(geom::geography, p.g::geography)
    """

    def snap_candidates(
        self, lon: float, lat: float, max_access_m: float = 25_000, k: int = 60
    ) -> list[Snap]:
        """Best way onto the network per connected component, nearest first.

        Nearest *edge* rather than nearest node: nodes are only every ~0.4 mile,
        so nearest-node snapping would silently move a pickup a third of a mile
        before routing even starts. One candidate per component, because the
        component is what decides whether a pair can be connected at all.
        """
        with get_conn() as conn:
            rows = conn.execute(self._SNAP_SQL, {"lon": lon, "lat": lat, "k": k}).fetchall()

        out: list[Snap] = []
        for edge_id, source, target, length_m, component, access_m, frac, sx, sy in rows:
            length_m, access_m, frac = float(length_m), float(access_m), float(frac)
            if access_m > max_access_m:
                continue
            out.append(Snap(
                lon=lon, lat=lat, edge_id=edge_id, source=source, target=target,
                edge_length_m=length_m,
                dist_from_source_m=frac * length_m,
                dist_to_target_m=(1.0 - frac) * length_m,
                access_m=access_m, component=component,
                snapped_lon=float(sx), snapped_lat=float(sy),
            ))
        if not out:
            raise NoNearbyRoute(
                f"no truck route within {max_access_m / 1000:.0f} km of this point"
            )
        out.sort(key=lambda s: s.access_m)
        return out

    def snap(self, lon: float, lat: float, max_access_m: float = 25_000) -> Snap:
        """Closest way onto the network, ignoring connectivity."""
        return self.snap_candidates(lon, lat, max_access_m)[0]

    def choose_connected_pair(
        self, origins: list[Snap], destinations: list[Snap]
    ) -> tuple[Snap, Snap]:
        """Cheapest pair of access points that share a component.

        "Cheapest" is total off-network access distance: a driver will happily
        drive an extra kilometre to join the highway that actually goes there
        rather than start on a stub that goes nowhere.
        """
        by_component = {d.component: d for d in destinations}
        best: tuple[float, Snap, Snap] | None = None
        for o in origins:
            d = by_component.get(o.component)
            if d is None:
                continue
            total = o.access_m + d.access_m
            if best is None or total < best[0]:
                best = (total, o, d)
        if best is None:
            o, d = origins[0], destinations[0]
            raise NoTruckPath(
                "pickup and drop are on unconnected parts of the truck network — "
                f"pickup reaches component {o.component}, drop reaches "
                f"component {d.component}; no truck-designated path joins them"
            )
        return best[1], best[2]

    def route_between(
        self, origin_lon: float, origin_lat: float,
        dest_lon: float, dest_lat: float, max_access_m: float = 25_000,
        profile: VehicleProfile | None = None,
    ) -> RouteResult:
        """Point-to-point: snap both ends, pick a connected pair, then search."""
        origins = self.snap_candidates(origin_lon, origin_lat, max_access_m)
        destinations = self.snap_candidates(dest_lon, dest_lat, max_access_m)
        o, d = self.choose_connected_pair(origins, destinations)
        return self.route(o, d, profile=profile)

    # --- search -------------------------------------------------------------

    def route(self, origin: Snap, destination: Snap,
              profile: VehicleProfile | None = None) -> RouteResult:
        self.ensure_loaded()
        profile = profile or VehicleProfile()

        # Same edge: no graph search needed, and searching would give a worse
        # answer (it would force a detour out to a node and back).
        if origin.edge_id == destination.edge_id:
            why = self.blocked_for(origin.edge_id, profile)
            if why:
                raise NoCompliantPath(
                    f"pickup and drop are on the same road segment, and it is "
                    f"not usable by this vehicle: {why}"
                )
            d = abs(origin.dist_from_source_m - destination.dist_from_source_m)
            return RouteResult(
                edge_ids=[origin.edge_id], node_path=[], distance_m=d,
                access_m=origin.access_m + destination.access_m,
                connector_count=0, connector_m=0.0,
                origin=origin, destination=destination, explored=0,
            )

        if origin.component != destination.component:
            raise NoTruckPath(
                "pickup and drop are on unconnected parts of the truck network — "
                "no truck-designated path exists between them"
            )

        # Multi-source / multi-goal: either end of the snapped edge may be the
        # right way on and off, so both are seeded with their partial cost.
        starts = {
            origin.source: origin.dist_from_source_m,
            origin.target: origin.dist_to_target_m,
        }
        goals = {
            destination.source: destination.dist_from_source_m,
            destination.target: destination.dist_to_target_m,
        }
        gx = [self.xy[n] for n in goals]

        def h(node: int) -> float:
            x, y = self.xy[node]
            return min(haversine_m(x, y, tx, ty) for tx, ty in gx)

        dist: dict[int, float] = {}
        prev: dict[int, tuple[int, int]] = {}
        heap: list[tuple[float, float, int]] = []
        for node, cost in starts.items():
            dist[node] = cost
            heapq.heappush(heap, (cost + h(node), cost, node))

        best_goal: int | None = None
        best_total = math.inf
        explored = 0
        constrained = profile.constrains_search
        avoided: dict[int, str] = {}

        while heap:
            f, g, node = heapq.heappop(heap)
            if g > dist.get(node, math.inf):
                continue
            if f >= best_total:
                break                      # nothing left can beat the goal we hold
            explored += 1
            if node in goals:
                total = g + goals[node]
                if total < best_total:
                    best_total, best_goal = total, node
            for nbr, w, edge_id in self.adj[node]:
                if constrained:
                    why = self.blocked_for(edge_id, profile)
                    if why is not None:
                        avoided[edge_id] = why
                        continue          # this vehicle may not use this edge
                ng = g + w
                if ng < dist.get(nbr, math.inf):
                    dist[nbr] = ng
                    prev[nbr] = (node, edge_id)
                    heapq.heappush(heap, (ng + h(nbr), ng, nbr))

        if best_goal is None:
            if constrained and avoided:
                raise NoCompliantPath(
                    f"a truck route exists between these points, but none this "
                    f"vehicle may use — {len(avoided):,} segment(s) on every "
                    f"candidate path are restricted (e.g. {next(iter(avoided.values()))})"
                )
            # Components said reachable, so this is a graph/label mismatch, not
            # a normal "no path" — say which, rather than blaming the user's input.
            raise NoTruckPath(
                "no path found although both ends are labelled reachable — "
                "the component labels may be stale (re-run scripts/route_components.py)"
            )

        edge_ids: list[int] = []
        node_path: list[int] = [best_goal]
        cur = best_goal
        while cur in prev:
            cur, edge_id = prev[cur]
            edge_ids.append(edge_id)
            node_path.append(cur)
        edge_ids.reverse()
        node_path.reverse()

        connectors = [e for e in edge_ids if e in self.connector_edges]
        connector_m = 0.0
        if connectors:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT coalesce(sum(length_m), 0) FROM route.edges "
                    "WHERE edge_id = ANY(%s)",
                    (connectors,),
                ).fetchone()
                connector_m = float(row[0])

        unknown_clearance = 0
        if edge_ids:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT coalesce(sum(structures_unknown_clearance), 0) "
                    "FROM route.edge_limits WHERE edge_id = ANY(%s)",
                    (edge_ids,),
                ).fetchone()
                unknown_clearance = int(row[0]) if row else 0

        return RouteResult(
            edge_ids=edge_ids,
            node_path=node_path,
            distance_m=best_total,
            access_m=origin.access_m + destination.access_m,
            connector_count=len(connectors),
            connector_m=connector_m,
            origin=origin,
            destination=destination,
            explored=explored,
            partial_first=(origin.edge_id, origin.dist_from_source_m, origin.dist_to_target_m),
            partial_last=(destination.edge_id, destination.dist_from_source_m,
                          destination.dist_to_target_m),
            profile=profile,
            edges_excluded=len(avoided),
            exclusion_examples=sorted(set(avoided.values()))[:5],
            structures_unknown_clearance=unknown_clearance,
        )


_GRAPH: TruckGraph | None = None
_GRAPH_LOCK = threading.Lock()


def get_graph() -> TruckGraph:
    """Process-wide singleton, so the graph is paid for once."""
    global _GRAPH
    if _GRAPH is None:
        with _GRAPH_LOCK:
            if _GRAPH is None:
                _GRAPH = TruckGraph()
    _GRAPH.ensure_loaded()
    return _GRAPH


def path_geometry_sql(edge_ids: Iterable[int]) -> tuple[str, dict]:
    """SQL returning the merged path geometry, for corridor analysis and drawing."""
    return (
        """
        SELECT ST_AsGeoJSON(ST_LineMerge(ST_Collect(geom))) AS geojson,
               ST_Collect(geom) AS geom
        FROM route.edges WHERE edge_id = ANY(%(ids)s)
        """,
        {"ids": list(edge_ids)},
    )
