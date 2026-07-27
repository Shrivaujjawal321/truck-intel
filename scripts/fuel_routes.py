#!/usr/bin/env python
"""Put every fuel station on (or off) the truck route network.

Boss's ask (2026-07-26): the map should carry two service layers — fuel and
mechanics — and only where they are actually on a truck route.

`core.mechanic_shops` already carried route columns from its own pull.
`osm.fuel_stations` did not, so its map layer could not be route-filtered at
all. This script adds the same columns and fills them with the same query
(`truckintel.route_assign`), so "on route" means one thing across both layers.

What this does NOT filter on, and why
-------------------------------------
It is tempting to also keep only stations tagged with diesel. Measured against
the real extract, that would be wrong:

    has_diesel   NULL 100,075   true 7,653   false   328
    hgv_access   NULL 103,117   true 2,687   false 2,252

Diesel is *untagged* on 93% of rows and explicitly denied on 0.3%. Requiring
`has_diesel = true` would delete 93% of the layer because of OSM tagging gaps,
not because those pumps lack diesel — a coverage claim about our metadata
masquerading as a fact about the road. So the geometry filter is the only
inclusion test, and the diesel/DEF/HGV tags travel to the UI as three honest
states: yes, no, unknown.

Explicit negatives ARE respected: a station tagged `hgv=no` or
`fuel:diesel=no` is a *known* fact, not a gap, and a truck map should not
route a driver to it. That exclusion is expressed in the map layer's filter,
not here — this script only measures distance.

Cost, measured before running (project rule: size it, then run it):
    500 stations scored in 362 ms  ->  108,056 stations ~ 78 s

Usage:
    uv run python scripts/fuel_routes.py            # add columns + assign
    uv run python scripts/fuel_routes.py --report   # read-only distribution
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402
from truckintel.route_assign import (  # noqa: E402
    ON_ROUTE_M,
    add_route_columns,
    assign_nearest_route,
)

TABLE = "osm.fuel_stations"
ID_COL = "osm_id"


def report() -> None:
    """Print the on/off-route split without writing anything."""
    with get_conn() as pg:
        cols = pg.execute(
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'osm' AND table_name = 'fuel_stations'
              AND column_name = 'on_route_5km'
            """
        ).fetchone()[0]
        if not cols:
            print("[report] route columns not present yet — run without --report")
            return
        row = pg.execute(
            f"""
            SELECT count(*)                                        AS total,
                   count(*) FILTER (WHERE on_route_5km)            AS on_route,
                   count(*) FILTER (WHERE route_id IS NULL)        AS unassigned,
                   count(*) FILTER (WHERE on_route_5km
                                      AND hgv_access IS NOT FALSE
                                      AND has_diesel IS NOT FALSE) AS mappable,
                   round(avg(route_dist_m))                        AS avg_m
            FROM {TABLE}
            """
        ).fetchone()
        total, on_route, unassigned, mappable, avg_m = row
        print(f"[report] {TABLE}")
        print(f"  total                     {total:>7,}")
        print(f"  within {ON_ROUTE_M // 1000} km of truck route  {on_route:>7,}"
              f"  ({on_route / total:.1%})")
        print(f"  drawn on map (also not hgv=no / diesel=no)"
              f"  {mappable:>7,}")
        print(f"  no geometry -> unassigned  {unassigned:>7,}")
        print(f"  mean distance to route     {avg_m:>7,} m")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="print the on/off-route split, write nothing")
    args = ap.parse_args()

    if args.report:
        report()
        return 0

    t0 = time.monotonic()
    with get_conn() as pg:
        add_route_columns(pg, TABLE)
        # Partial index on the predicate the map filter actually uses, so a tile
        # query does not re-test 108k rows to find the ~74k it draws.
        pg.execute(
            "CREATE INDEX IF NOT EXISTS fuel_stations_on_route_ix "
            f"ON {TABLE} (on_route_5km) WHERE on_route_5km"
        )
        print(f"[fuel-routes] assigning nearest truck route to {TABLE}…",
              flush=True)
        n = assign_nearest_route(pg, TABLE, ID_COL)
    print(f"[fuel-routes] {n:,} rows assigned in {time.monotonic() - t0:.1f}s",
          flush=True)
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
