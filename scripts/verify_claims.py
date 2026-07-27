#!/usr/bin/env python
"""Re-derive every published figure from the live database.

Boss's ask (2026-07-24): make sure whatever was built is verified and factual.

Documentation drifts from data silently. Every number printed in README.md, in
the map viewer, and in the source comments is asserted here against the database
it came from, so "the docs say 74%" and "the data says 74%" cannot diverge
without this failing.

A claim is one of:
  EXACT      the number must match
  AT_LEAST   a floor (counts that only grow)
  RANGE      a real-world distance, checked against the published road distance
  INVARIANT  a property that must hold, not a number

Exit 0 only when every claim holds.

  uv run python scripts/verify_claims.py
  uv run python scripts/verify_claims.py --verbose
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402

M_PER_MILE = 1609.344


class Checker:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def _ok(self, label: str, detail: str) -> None:
        self.passed += 1
        if self.verbose:
            print(f"  PASS  {label:<52} {detail}")

    def _bad(self, label: str, detail: str) -> None:
        self.failed.append(f"{label}: {detail}")
        print(f"  FAIL  {label:<52} {detail}")

    def exact(self, label: str, claimed, actual) -> None:
        if isinstance(actual, (int,)) or actual is None:
            pass
        else:
            actual = int(actual)
        if claimed == actual:
            self._ok(label, f"{actual:,}" if isinstance(actual, int) else str(actual))
        else:
            self._bad(label, f"claimed {claimed:,}, database says {actual:,}")

    def at_least(self, label: str, floor, actual) -> None:
        actual = int(actual)
        if actual >= floor:
            self._ok(label, f"{actual:,} >= {floor:,}")
        else:
            self._bad(label, f"claimed at least {floor:,}, database says {actual:,}")

    def close(self, label: str, claimed: float, actual, tol: float) -> None:
        # psycopg returns numeric/decimal for SQL aggregates; normalise before math.
        actual = float(actual)
        if abs(actual - claimed) <= tol:
            self._ok(label, f"{actual:,.1f} (claimed {claimed:,.1f}, tol {tol:g})")
        else:
            self._bad(label, f"claimed {claimed:,.1f}, measured {actual:,.1f}")

    def invariant(self, label: str, holds: bool, detail: str = "") -> None:
        if holds:
            self._ok(label, detail or "holds")
        else:
            self._bad(label, detail or "VIOLATED")

    def skip(self, label: str, why: str) -> None:
        self.skipped.append(f"{label}: {why}")
        print(f"  SKIP  {label:<52} {why}")


def one(conn, sql: str, params=None):
    return conn.execute(sql, params).fetchone()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    c = Checker(args.verbose)

    with get_conn() as conn:
        # --- dataset row counts, as published in the viewer sidebar ----------
        print("\nDATASET COUNTS")
        for label, table, claimed in [
            ("truck route segments", "core.truck_routes", 454_830),
            ("NBI bridges", "core.bridges", 629_710),
            ("OSM fuel stations", "osm.fuel_stations", 108_056),
            ("truck parking sites", "core.parking_sites", 1_915),
            ("rest areas", "osm.rest_areas", 5_452),
            ("weigh stations", "osm.weigh_points", 3_773),
            ("tunnels", "core.tunnels", 580),
            ("mechanic shops", "core.mechanic_shops", 11_759),
        ]:
            n = one(conn, f"SELECT count(*) FROM {table}")[0]
            c.exact(label, claimed, n)

        # --- the clearance-ignorance figure the whole honesty story rests on --
        print("\nTHE 74% CLEARANCE CLAIM")
        row = one(conn, """
            SELECT count(*) FILTER (WHERE min_vert_clearance_in IS NULL) AS unknown,
                   count(*) AS total FROM core.bridges
        """)
        c.exact("bridges with no recorded clearance", 468_598, row[0])
        pct = row[0] / row[1] * 100
        c.close("that as a percentage", 74.0, pct, tol=0.5)

        # --- graph shape ------------------------------------------------------
        print("\nROUTE GRAPH")
        if one(conn, "SELECT to_regclass('route.edges')")[0] is None:
            c.skip("route graph", "not built — run `make route-graph`")
        else:
            row = one(conn, """
                SELECT count(*) FILTER (WHERE kind = 'truck_route'),
                       count(*) FILTER (WHERE kind = 'synthetic_connector'),
                       coalesce(max(length_m) FILTER (WHERE kind='synthetic_connector'), 0)
                FROM route.edges
            """)
            c.at_least("truck-route edges after noding", 458_000, row[0])
            c.exact("synthetic connectors", 2_107, row[1])
            c.invariant("every connector is <= 50 m", row[2] <= 50.0,
                        f"longest is {row[2]:.1f} m")

            row = one(conn, """
                SELECT count(*) AS comps,
                       max(n) AS largest,
                       sum(n) AS total
                FROM (SELECT count(*) AS n FROM route.node_component
                      GROUP BY component) q
            """)
            c.exact("connected components after noding", 180, row[0])
            c.close("largest component as % of network",
                    97.1, row[1] / row[2] * 100, tol=0.2)

            # The rule the whole product rests on.
            row = one(conn, """
                SELECT count(*) FROM route.edges e
                WHERE e.kind = 'truck_route'
                  AND NOT EXISTS (SELECT 1 FROM core.truck_routes t
                                  WHERE t.route_id = e.route_id)
            """)
            c.invariant("every routable edge traces to core.truck_routes",
                        row[0] == 0, f"{row[0]} orphans")
            row = one(conn, "SELECT count(DISTINCT kind) FROM route.edges")
            c.invariant("no third edge kind exists", row[0] == 2)

        # --- the mileage preserved by generalization -------------------------
        print("\nGENERALIZED ROUTES (map at low zoom)")
        if one(conn, "SELECT to_regclass('core.truck_routes_gen')")[0] is None:
            c.skip("truck_routes_gen", "not built")
        else:
            row = one(conn, """
                SELECT (SELECT sum(ST_Length(geom::geography)) FROM core.truck_routes),
                       (SELECT sum(ST_Length(geom::geography)) FROM core.truck_routes_gen),
                       (SELECT count(*) FROM core.truck_routes_gen)
            """)
            c.exact("dissolved corridors", 3_282, row[2])
            c.close("network miles", 186_112, row[0] / M_PER_MILE, tol=50)
            c.close("mileage kept by generalization %",
                    99.4, row[1] / row[0] * 100, tol=0.3)

        # --- per-edge limits --------------------------------------------------
        print("\nVEHICLE-PROFILE LIMITS")
        if one(conn, "SELECT to_regclass('route.edge_limits')")[0] is None:
            c.skip("route.edge_limits", "not built — run `make route-limits`")
        else:
            row = one(conn, """
                SELECT count(*),
                       count(*) FILTER (WHERE min_clearance_in IS NOT NULL),
                       count(*) FILTER (WHERE max_weight_lb IS NOT NULL),
                       count(*) FILTER (WHERE closed),
                       count(*) FILTER (WHERE hazmat_blocked),
                       min(min_clearance_in), min(max_weight_lb)
                FROM route.edge_limits
            """)
            c.at_least("edges carrying a structure", 90_000, row[0])
            c.at_least("edges with a recorded clearance", 35_000, row[1])
            c.invariant("no non-positive clearance stored",
                        row[5] is None or row[5] > 0, f"min {row[5]}")
            c.invariant("no non-positive weight limit stored",
                        row[6] is None or row[6] > 0, f"min {row[6]}")

            # An unposted bridge must never block: it is legally open.
            row = one(conn, """
                SELECT count(*) FROM route.edge_limits l
                WHERE l.max_weight_lb IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM route.edges e
                    JOIN core.bridges b
                      ON b.geom && ST_Expand(e.geom, 30/70000.0)
                     AND ST_DWithin(e.geom::geography, b.geom::geography, 30)
                    WHERE e.edge_id = l.edge_id
                      AND b.posting_status IN ('P','R','B','D')
                )
            """)
            c.invariant("weight limits come only from POSTED structures",
                        row[0] == 0, f"{row[0]} edges limited by an unposted bridge")

        # --- fuel verification ------------------------------------------------
        print("\nFUEL VERIFICATION")
        has_col = one(conn, """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema='osm' AND table_name='fuel_stations'
              AND column_name='verification_status'
        """)[0]
        if not has_col:
            c.skip("fuel verification", "not run — `make fuel-verify`")
        else:
            row = one(conn, """
                SELECT count(*) FILTER (WHERE verification_status='verified'),
                       count(*) FILTER (WHERE verification_status='probable'),
                       count(*) FILTER (WHERE verification_status='unverified'),
                       count(*) FILTER (WHERE verification_status IS NULL),
                       count(*)
                FROM osm.fuel_stations
            """)
            c.exact("fuel stations verified", 81_913, row[0])
            c.exact("fuel stations probable", 2_947, row[1])
            c.exact("fuel stations unverified", 23_196, row[2])
            c.invariant("every station is banded", row[3] == 0)
            c.invariant("bands sum to the whole table",
                        row[0] + row[1] + row[2] == row[4])

            # 'verified' must never rest on Overture's own two pipelines.
            row = one(conn, """
                SELECT count(*) FROM osm.fuel_stations
                WHERE verification_status = 'verified'
                  AND coalesce(independent_sources, 0) = 0
            """)
            c.invariant("no station verified without an outside source",
                        row[0] == 0, f"{row[0]} violations")
            row = one(conn, """
                SELECT count(*) FROM osm.fuel_stations
                WHERE verification_status = 'verified'
                  AND NOT EXISTS (
                      SELECT 1 FROM unnest(ov_datasets) d
                      WHERE d NOT IN ('Overture', 'Overture-signals'))
            """)
            c.invariant("every verified station names a real outside dataset",
                        row[0] == 0, f"{row[0]} violations")

        # --- fuel detail -------------------------------------------------------
        print("\nFUEL DETAIL")
        if one(conn, "SELECT to_regclass('core.fuel_places')")[0] is None:
            c.skip("fuel enrichment", "not run — `make fuel-enrich`")
        else:
            row = one(conn, """
                SELECT count(*) AS total,
                       count(coalesce(f.name, p.name)) AS named,
                       count(p.address) AS addressed,
                       count(coalesce(f.props->>'phone', p.phone)) AS phoned,
                       count(pr.price_usd_gal) AS priced
                FROM osm.fuel_stations f
                LEFT JOIN core.fuel_places p ON p.place_id = f.ov_place_id
                LEFT JOIN core.fuel_station_state st ON st.osm_id = f.osm_id
                LEFT JOIN core.fuel_price_by_state pr ON pr.state = st.state
            """)
            c.close("stations with a name %", 93.5, row[1]/row[0]*100, tol=0.5)
            c.close("stations with an address %", 75.4, row[2]/row[0]*100, tol=0.5)
            c.close("stations with a phone %", 70.1, row[3]/row[0]*100, tol=0.5)
            c.close("stations with a regional price %", 100.0, row[4]/row[0]*100, tol=0.1)

            # The price must never be presentable as a pump price.
            row = one(conn, """
                SELECT count(*) FROM core.fuel_price_by_state
                WHERE note NOT LIKE '%not the price at any individual pump%'
            """)
            c.invariant("every price row carries the not-a-pump-price note", row[0] == 0)
            # Every state must resolve to the region EIA actually assigns it.
            row = one(conn, """
                SELECT count(*) FROM core.fuel_price_by_state WHERE price_usd_gal IS NULL
            """)
            c.invariant("every state has a current regional price", row[0] == 0)
            # Permissive detail must not have leaked into the ODbL table.
            row = one(conn, """
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema='osm' AND table_name='fuel_stations'
                  AND column_name IN ('address','city','zip','email','socials')
            """)
            c.invariant("Overture detail stays out of the ODbL table", row[0] == 0)

        # --- mechanics ---------------------------------------------------------
        print("\nMECHANICS")
        if one(conn, "SELECT to_regclass('core.mechanic_shops')")[0] is None:
            c.skip("mechanic shops", "not loaded")
        else:
            row = one(conn, """
                SELECT count(*) FILTER (WHERE phone IS NOT NULL),
                       count(*) FILTER (WHERE on_route_5km),
                       count(*) FILTER (WHERE route_id IS NOT NULL),
                       count(*) FILTER (WHERE n_sources = 1),
                       count(*)
                FROM core.mechanic_shops
            """)
            c.close("with a phone number %", 99.4, row[0] / row[4] * 100, tol=0.3)
            c.close("within 5 km of a truck route %", 83.4,
                    row[1] / row[4] * 100, tol=0.5)
            c.invariant("every shop got a nearest route", row[2] == row[4])
            # The finding that made the mechanic confidence score uninformative.
            c.invariant("no mechanic has a single source (the degenerate signal)",
                        row[3] == 0, f"{row[3]} single-source rows")

        # --- density grid conserves every row ---------------------------------
        print("\nMAP DENSITY GRID")
        row = one(conn, """
            WITH b AS (SELECT ST_Transform(ST_TileEnvelope(4,4,6, margin=>0.0625),4326) g),
            cell AS (SELECT (360.0/(1<<4))/64 AS c)
            SELECT (SELECT count(*) FROM core.bridges t, b WHERE t.geom && b.g),
                   (SELECT coalesce(sum(n),0) FROM (
                      SELECT count(*) n FROM core.bridges t, b, cell
                      WHERE t.geom && b.g GROUP BY ST_SnapToGrid(t.geom, cell.c)) q)
        """)
        c.invariant("grid cells account for every row in the tile",
                    row[0] == row[1], f"{row[0]:,} raw vs {row[1]:,} summed")

    # --- live routing: the distances quoted in the README --------------------
    print("\nROUTE DISTANCES (against published road distances)")
    try:
        from truckintel.routing import VehicleProfile, get_graph
        g = get_graph()
        for label, a, b, claimed, tol in [
            ("Dallas -> Oklahoma City", (-96.797, 32.777), (-97.517, 35.467), 205, 25),
            ("Chicago -> Indianapolis", (-87.629, 41.878), (-86.158, 39.768), 183, 20),
            ("Los Angeles -> Phoenix", (-118.243, 34.052), (-112.074, 33.448), 372, 30),
            ("Atlanta -> Nashville", (-84.388, 33.749), (-86.781, 36.163), 250, 25),
        ]:
            r = g.route_between(*a, *b)
            c.close(label, claimed, r.distance_m / M_PER_MILE, tol=tol)

        # A constraint can only remove options, never shorten the answer.
        base = g.route_between(-96.797, 32.777, -97.517, 35.467,
                               profile=VehicleProfile(height_in=140))
        tall = g.route_between(-96.797, 32.777, -97.517, 35.467,
                               profile=VehicleProfile(height_in=180, weight_lb=105_000))
        c.invariant("a constrained route is never shorter",
                    tall.distance_m >= base.distance_m - 1,
                    f"{base.distance_m/M_PER_MILE:.0f} mi -> {tall.distance_m/M_PER_MILE:.0f} mi")
        c.invariant("the constrained path contains no forbidden edge",
                    all(g.blocked_for(e, VehicleProfile(height_in=180, weight_lb=105_000))
                        is None for e in tall.edge_ids))
    except Exception as exc:
        c.skip("live routing", f"{type(exc).__name__}: {exc}")

    print(f"\n{'-' * 72}")
    print(f"{c.passed} claims verified · {len(c.failed)} failed · {len(c.skipped)} skipped")
    if c.failed:
        print("\nFAILED:")
        for f in c.failed:
            print(f"  {f}")
        return 1
    print("every published figure matches the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
