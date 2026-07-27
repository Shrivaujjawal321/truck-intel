"""Assign every point in a table its nearest *truck-designated* route.

Why this module exists
---------------------
Two tables need the same answer — "which truck route is this on, and how far
off it?" — and the answer must be computed the same way for both, or a fuel
station 4 km off I-80 and a mechanic 4 km off I-80 would disagree about being
"on route". The SQL below was proven on `core.mechanic_shops` (11,759 rows);
this module is that query with the table and key made parameters so fuel
stations get an identical measurement rather than a second implementation.

The route network is `core.truck_routes` — the NTAD National Network, NN=1 —
and nothing else. `osm.ways` is the generic road graph and is never used here:
a distance to the nearest residential street is not a distance to a truck route.

Why the query is shaped this way
--------------------------------
* The KNN operator `<->` runs on plain geometry so `truck_routes_geom_gix` is
  usable. Ordering by the geography cast directly cannot use the index.
* Degrees are not metres, and a degree of longitude is much shorter in Maine
  than in Texas, so the nearest-in-degrees segment is not always the nearest in
  reality. The index therefore supplies 10 *candidates* and true geography
  distance picks the winner among them.
* A LATERAL in an UPDATE's FROM clause cannot reference the update target, so
  the correlation lives in its own subquery and is joined back by primary key.

`on_route_m` is straight-line distance to the route geometry, not drive
distance. Callers must not present it as "X km of driving" — it is "this sits
within X m of a truck route", which is what the map filter needs and all it
claims. Same buffer as `corridor.py`'s `service_buffer_m` (5 km) so the map and
the route-side service list agree by construction.
"""
from __future__ import annotations

import psycopg

# One buffer, one definition. corridor.py's service_buffer_m default is 5 km;
# changing this without changing that would make the national map and the
# per-route service list disagree about the same shop.
ON_ROUTE_M = 5000

# How many index candidates get true-distance scored. 10 was enough for
# mechanics: the 10th candidate is already far outside the 5 km buffer in every
# state, so the winner never sits beyond it.
KNN_CANDIDATES = 10


def add_route_columns(pg: psycopg.Connection, table: str) -> None:
    """Idempotently add the route-assignment columns to `table`."""
    pg.execute(
        f"""
        ALTER TABLE {table}
          ADD COLUMN IF NOT EXISTS route_id     BIGINT,
          ADD COLUMN IF NOT EXISTS route_ref    TEXT,
          ADD COLUMN IF NOT EXISTS route_name   TEXT,
          ADD COLUMN IF NOT EXISTS route_dist_m INTEGER,
          ADD COLUMN IF NOT EXISTS on_route_5km BOOLEAN
        """
    )


def assign_nearest_route(
    pg: psycopg.Connection,
    table: str,
    id_col: str,
    *,
    on_route_m: int = ON_ROUTE_M,
    candidates: int = KNN_CANDIDATES,
) -> int:
    """Set route_id/ref/name/dist_m/on_route_5km on every row of `table`.

    Returns the number of rows updated. Rows with a NULL geom are left alone —
    their route columns stay NULL, which reads as "unknown", not as "off route".

    `table` and `id_col` are developer-supplied identifiers from this repo, not
    request input; they are interpolated because an identifier cannot be bound
    as a parameter.
    """
    cur = pg.execute(
        f"""
        UPDATE {table} s SET
          route_id     = x.route_id,
          route_ref    = x.route_ref,
          route_name   = x.route_name,
          route_dist_m = round(x.d)::int,
          on_route_5km = (x.d <= %(on_route_m)s)
        FROM (
          SELECT p.{id_col} AS key, r.route_id, r.route_ref, r.route_name, r.d
          FROM {table} p
          CROSS JOIN LATERAL (
            SELECT k.route_id, k.route_ref, k.route_name, k.d
            FROM (
              SELECT t.route_id, t.route_ref, t.route_name,
                     ST_Distance(t.geom::geography, p.geom::geography) AS d
              FROM core.truck_routes t
              ORDER BY t.geom <-> p.geom
              LIMIT %(candidates)s
            ) k
            ORDER BY k.d
            LIMIT 1
          ) r
          WHERE p.geom IS NOT NULL
        ) x
        WHERE x.key = s.{id_col}
        """,
        {"on_route_m": on_route_m, "candidates": candidates},
    )
    return cur.rowcount
