#!/usr/bin/env python
"""Verify that the fuel stations we list actually exist.

Boss's ask (2026-07-24): only fuel stations that are really there.

`osm.fuel_stations` holds 108,056 US rows extracted from OpenStreetMap. OSM is
crowd-sourced: most of it is right, some of it is stale, and nothing in the
extract says which is which. This job attaches evidence to every row.

**The independence question, settled with data.** Corroborating OSM with a source
that is itself derived from OSM proves nothing — the mechanic pipeline scored
every shop as multi-source precisely because `Overture` and `Overture-signals`
were counted as two. So the source datasets behind Overture *Places* were checked
directly before relying on them:

    Overture-signals 18,486 · Overture 3,351 · meta 1,554 · Microsoft 732
    AllThePlaces 539 · Foursquare 408 · DAC 118          (one parquet part, US)

OSM does not appear. Overture Places is built from Meta / Microsoft / Foursquare /
AllThePlaces — OSM feeds Overture's *base* and *transportation* themes, not
Places. So a Places record IS independent evidence about an OSM station, provided
we ignore Overture's own two pipelines and require one of the outside datasets.

Signals, all free and legal:
  INDEPENDENT   an Overture Place of a fuel category within MATCH_M, carrying at
                least one non-Overture source dataset. This is the existence proof.
  BRAND         `brand:wikidata` on the OSM node — a real, identifiable chain
  FRESH         OSM `check_date` — a mapper stood there and confirmed it
  CONTACT       phone or website recorded
  ADDRESS       street + city recorded
  COORD         inside the US, not null island, not in a duplicate pile

Nothing is deleted. A station that fails every check is labelled `unverified`
and kept, because "we could not confirm this" is not the same as "it is not there".

  uv run python scripts/fuel_verify.py --pull    # Overture fuel -> staging
  uv run python scripts/fuel_verify.py --score   # match + score in place
  (default: both)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402

LOCAL_DIR = REPO / "data" / "overture_places"

FUEL_CATS = ("gas_station", "truck_gas_station", "truck_stop", "fuel_dock")

# Overture's own pipelines. Present on nearly every row, and not independent
# evidence of anything — the same double-counting the mechanic scores fell for.
OVERTURE_OWN = ("Overture", "Overture-signals")

# Fuel sites are large. The OSM node is often the pump canopy and the Overture
# point the storefront or the parcel centroid, so an exact-coordinate match would
# reject real pairs. 150 m is wide enough for one site, tight enough that two
# separate stations rarely collide (US stations average far further apart).
MATCH_M = 150

# A mapper's on-the-ground check goes stale. Three years is the window used here.
FRESH_YEARS = 3

STAGING_DDL = """
CREATE SCHEMA IF NOT EXISTS staging;
DROP TABLE IF EXISTS staging.overture_fuel;
CREATE TABLE staging.overture_fuel (
    place_id     text PRIMARY KEY,
    name         text,
    brand        text,
    category     text,
    confidence   double precision,
    datasets     text[],
    -- Contact and address detail. Overture fills these far better than the OSM
    -- extract does (phone 82.6% vs 8.4%, website 89.0% vs 17.4%), which is what
    -- makes scripts/fuel_enrich.py worth running.
    address      text,
    city         text,
    state        text,
    zip          text,
    phone        text,
    website      text,
    email        text,
    socials      text[],
    operating_status text,
    lat          double precision,
    lon          double precision,
    geom         geometry(Point, 4326)
);
"""

VERIFY_COLS = """
ALTER TABLE osm.fuel_stations
    ADD COLUMN IF NOT EXISTS ov_place_id          text,
    ADD COLUMN IF NOT EXISTS ov_match_m           double precision,
    ADD COLUMN IF NOT EXISTS ov_datasets          text[],
    ADD COLUMN IF NOT EXISTS independent_sources  int,
    ADD COLUMN IF NOT EXISTS brand_verified       boolean,
    ADD COLUMN IF NOT EXISTS osm_check_date       date,
    ADD COLUMN IF NOT EXISTS has_contact          boolean,
    ADD COLUMN IF NOT EXISTS has_address          boolean,
    ADD COLUMN IF NOT EXISTS coord_ok             boolean,
    ADD COLUMN IF NOT EXISTS verification_status  text,
    ADD COLUMN IF NOT EXISTS verify_confidence    smallint,
    ADD COLUMN IF NOT EXISTS verified_at          timestamptz;
"""


def pull() -> int:
    """Scan the local Overture mirror for US fuel places into staging."""
    import duckdb

    parts = sorted(LOCAL_DIR.glob("*.parquet"))
    if not parts:
        raise SystemExit(f"no parquet in {LOCAL_DIR} — run ./scripts/overture_fetch.sh")
    print(f"[pull] scanning {len(parts)} Overture parts for US fuel places…", flush=True)

    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false; SET memory_limit='4GB';")
    con.execute(f"SET temp_directory='{REPO / 'data' / 'duckdb_tmp'}';")
    # The parquet's geometry column is a spatial-extension GEOMETRY, so ST_X/ST_Y
    # read it directly — but the extension has to be loaded first.
    con.execute("INSTALL spatial; LOAD spatial;")
    cats = ", ".join(f"'{c}'" for c in FUEL_CATS)
    rows = con.execute(f"""
        SELECT id,
               names.primary                                  AS name,
               brand.names.primary                            AS brand,
               categories.primary                             AS category,
               confidence,
               list_transform(sources, s -> s.dataset)        AS datasets,
               addresses[1].freeform                          AS address,
               addresses[1].locality                          AS city,
               addresses[1].region                            AS state,
               addresses[1].postcode                          AS zip,
               phones[1]                                      AS phone,
               websites[1]                                    AS website,
               emails[1]                                      AS email,
               socials                                        AS socials,
               operating_status                               AS operating_status,
               ST_Y(geometry)                                 AS lat,
               ST_X(geometry)                                 AS lon
        FROM read_parquet('{LOCAL_DIR}/*.parquet')
        WHERE addresses[1].country = 'US'
          AND categories.primary IN ({cats})
    """).fetchall()
    print(f"[pull] {len(rows):,} US fuel places found", flush=True)

    with get_conn() as pg:
        with pg.cursor() as cur:
            cur.execute(STAGING_DDL)
            with cur.copy(
                "COPY staging.overture_fuel "
                "(place_id, name, brand, category, confidence, datasets, "
                " address, city, state, zip, phone, website, email, socials, "
                " operating_status, lat, lon) FROM STDIN"
            ) as cp:
                for r in rows:
                    cp.write_row(r)
            cur.execute("""
                UPDATE staging.overture_fuel
                SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                WHERE lat IS NOT NULL AND lon IS NOT NULL
            """)
            cur.execute(
                "CREATE INDEX overture_fuel_gix ON staging.overture_fuel USING GIST (geom)"
            )
            cur.execute("ANALYZE staging.overture_fuel")
        pg.commit()
    print(f"[pull] loaded {len(rows):,} into staging.overture_fuel", flush=True)
    return len(rows)


def score() -> None:
    """Match each OSM station to Overture, gather signals, band the result."""
    own = "ARRAY[" + ",".join(f"'{d}'" for d in OVERTURE_OWN) + "]"
    with get_conn() as pg:
        with pg.cursor() as cur:
            cur.execute(VERIFY_COLS)

            print("[score] matching against Overture…", flush=True)
            cur.execute(f"""
                UPDATE osm.fuel_stations f SET
                    ov_place_id = m.place_id,
                    ov_match_m  = round(m.d::numeric, 1),
                    ov_datasets = m.datasets,
                    -- Only datasets that are NOT Overture's own pipelines count as
                    -- independent evidence that this station exists.
                    independent_sources = (
                        SELECT count(*) FROM unnest(m.datasets) AS d
                        WHERE d <> ALL ({own})
                    )
                FROM (
                    SELECT s.osm_id, o.place_id, o.datasets, o.d
                    FROM osm.fuel_stations s
                    CROSS JOIN LATERAL (
                        SELECT k.place_id, k.datasets, k.d
                        FROM (
                            SELECT g.place_id, g.datasets,
                                   ST_Distance(g.geom::geography, s.geom::geography) AS d
                            FROM staging.overture_fuel g
                            WHERE g.geom && ST_Expand(s.geom, {MATCH_M} / 70000.0)
                            ORDER BY g.geom <-> s.geom
                            LIMIT 5
                        ) k
                        WHERE k.d <= {MATCH_M}
                        ORDER BY k.d
                        LIMIT 1
                    ) o
                ) m
                WHERE m.osm_id = f.osm_id
            """)
            print(f"[score] matched {cur.rowcount:,} stations", flush=True)

            print("[score] reading OSM's own signals…", flush=True)
            cur.execute(f"""
                UPDATE osm.fuel_stations SET
                    brand_verified = (props ? 'brand:wikidata'),
                    osm_check_date = CASE
                        WHEN props->>'check_date' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'
                        THEN (props->>'check_date')::date END,
                    has_contact = (props ? 'phone' OR props ? 'website'
                                   OR props ? 'contact:phone' OR props ? 'contact:website'),
                    has_address = (props ? 'addr:street' AND props ? 'addr:city'),
                    -- Continental US + AK/HI/PR envelope, and never null island.
                    coord_ok = (
                        ST_X(geom) BETWEEN -180 AND -64 AND ST_Y(geom) BETWEEN 17 AND 72
                        AND NOT (abs(ST_X(geom)) < 0.01 AND abs(ST_Y(geom)) < 0.01)
                    )
            """)

            print("[score] banding…", flush=True)
            cur.execute(f"""
                UPDATE osm.fuel_stations SET
                    verify_confidence = LEAST(100, (
                        CASE WHEN coalesce(independent_sources, 0) > 0 THEN 45 ELSE 0 END
                      + CASE WHEN coalesce(independent_sources, 0) > 1 THEN 10 ELSE 0 END
                      + CASE WHEN brand_verified THEN 15 ELSE 0 END
                      + CASE WHEN osm_check_date >= (CURRENT_DATE - INTERVAL '{FRESH_YEARS} years')
                             THEN 15 ELSE 0 END
                      + CASE WHEN has_contact THEN 10 ELSE 0 END
                      + CASE WHEN has_address THEN 5 ELSE 0 END
                    )),
                    verified_at = now()
                WHERE TRUE
            """)
            cur.execute("""
                UPDATE osm.fuel_stations SET
                    verification_status = CASE
                        WHEN NOT coalesce(coord_ok, false) THEN 'unverified'
                        -- An outside organisation also puts a fuel station here.
                        WHEN coalesce(independent_sources, 0) > 0 THEN 'verified'
                        -- No outside match, but the station identifies itself
                        -- strongly enough that dismissing it would be wrong.
                        WHEN verify_confidence >= 30 THEN 'probable'
                        ELSE 'unverified'
                    END
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS fuel_verification_ix
                    ON osm.fuel_stations (verification_status)
            """)
            cur.execute("ANALYZE osm.fuel_stations")
        pg.commit()

    with get_conn() as pg:
        rows = pg.execute("""
            SELECT verification_status, count(*),
                   round(avg(verify_confidence)) AS avg_conf,
                   count(*) FILTER (WHERE has_diesel) AS diesel,
                   count(*) FILTER (WHERE hgv_access) AS hgv
            FROM osm.fuel_stations GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()
    print(f"\n{'status':<14}{'stations':>10}{'avg conf':>10}{'diesel':>9}{'truck ok':>10}")
    for status, n, conf, diesel, hgv in rows:
        print(f"{status or 'null':<14}{n:>10,}{conf or 0:>10}{diesel:>9,}{hgv:>10,}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true", help="Overture fuel -> staging")
    ap.add_argument("--score", action="store_true", help="match + score in place")
    a = ap.parse_args()
    do_all = not (a.pull or a.score)
    if a.pull or do_all:
        pull()
    if a.score or do_all:
        score()
    return 0


if __name__ == "__main__":
    sys.exit(main())
