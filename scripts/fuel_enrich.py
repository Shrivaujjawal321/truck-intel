#!/usr/bin/env python
"""Give every fuel station its full detail: name, address, phone, website, price.

Boss's ask (2026-07-24): each detail — price of fuel, name of the pump, etc.

Where the detail comes from, and why it is stored the way it is
----------------------------------------------------------------
The OSM extract is thin on contact detail: phone on 8.4% of rows, website on
17.4%. Overture Places carries the same stations with phone on 82.6% and website
on 89.0%. We already know which Overture place each station is (`ov_place_id`,
set by scripts/fuel_verify.py).

The detail is therefore NOT copied into `osm.fuel_stations`. Overture Places is
CDLA-Permissive; `osm.*` is ODbL and share-alike. Copying permissive data into an
ODbL table would drag it under share-alike on redistribution for no benefit. So
the Overture rows live in `core.fuel_places` (permissive schema, per the licence
ruling in RESEARCH_BRIEF §5.2/D3) and are joined at query time through the id we
already hold. Nothing is duplicated and nothing is licence-mixed.

Fuel price — read this before believing any number
--------------------------------------------------
There is no free, legal source of per-pump fuel prices in the US. GasBuddy's
terms forbid it, AAA publishes averages only, and no government feed carries
pump-level prices. Anything claiming otherwise here would be invented.

What IS available, and what this attaches, is the EIA weekly on-highway diesel
price for the station's **region** — one of 11 PADD districts. It answers "what
does diesel cost around here this week", not "what does this pump charge today",
and the column names, the API and the UI all say so.

State-to-PADD mapping is EIA's own, read from
https://www.eia.gov/petroleum/weekly/includes/padds.php

  uv run python scripts/fuel_enrich.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402

# EIA PADD districts. PADD1 is reported both whole and as 1A/1B/1C; the
# subdistrict is the tighter number, so it wins where one exists.
STATE_TO_PADD = {
    # PADD 1A — New England
    **{s: "PADD1A" for s in ("CT", "ME", "MA", "NH", "RI", "VT")},
    # PADD 1B — Central Atlantic
    **{s: "PADD1B" for s in ("DE", "DC", "MD", "NJ", "NY", "PA")},
    # PADD 1C — Lower Atlantic
    **{s: "PADD1C" for s in ("FL", "GA", "NC", "SC", "VA", "WV")},
    # PADD 2 — Midwest
    **{s: "PADD2" for s in ("IL", "IN", "IA", "KS", "KY", "MI", "MN", "MO",
                            "NE", "ND", "OH", "OK", "SD", "TN", "WI")},
    # PADD 3 — Gulf Coast
    **{s: "PADD3" for s in ("AL", "AR", "LA", "MS", "NM", "TX")},
    # PADD 4 — Rocky Mountain
    **{s: "PADD4" for s in ("CO", "ID", "MT", "UT", "WY")},
    # PADD 5 — West Coast. California is reported separately and is the more
    # useful number there; the rest of PADD 5 has its own series.
    "CA": "CA",
    **{s: "PADD5" for s in ("AK", "AZ", "HI", "NV", "OR", "WA")},
}

PROMOTE = """
CREATE SCHEMA IF NOT EXISTS core;
DROP TABLE IF EXISTS core.fuel_places;

-- Overture Places (CDLA-Permissive-2.0) fuel sites, promoted out of staging so
-- they can be joined to the OSM stations without copying anything across the
-- licence boundary.
CREATE TABLE core.fuel_places AS SELECT * FROM staging.overture_fuel;
ALTER TABLE core.fuel_places ADD PRIMARY KEY (place_id);
CREATE INDEX fuel_places_gix ON core.fuel_places USING GIST (geom);
CREATE INDEX fuel_places_state_ix ON core.fuel_places (state);
ANALYZE core.fuel_places;
"""

# Which state a station is in, and how we know. Needed because the price is
# per-state, and the OSM extract records a state on only 31,875 of 108,056 rows.
STATE_RESOLVE = """
DROP TABLE IF EXISTS core.fuel_station_state;

CREATE TABLE core.fuel_station_state AS
SELECT f.osm_id,
       coalesce(f.state, p.state, near.state)::char(2) AS state,
       CASE WHEN f.state IS NOT NULL THEN 'osm'
            WHEN p.state IS NOT NULL THEN 'overture'
            WHEN near.state IS NOT NULL THEN 'nearest truck route'
       END AS state_source
FROM osm.fuel_stations f
LEFT JOIN core.fuel_places p ON p.place_id = f.ov_place_id
LEFT JOIN LATERAL (
    -- Only for the rows neither source names. The price is a PADD-region
    -- average covering many states, so a border misread almost never changes
    -- the number; leaving 18% of stations with no price at all would.
    SELECT t.state FROM core.truck_routes t
    WHERE f.state IS NULL AND p.state IS NULL
    ORDER BY t.geom <-> f.geom
    LIMIT 1
) near ON true;

ALTER TABLE core.fuel_station_state ADD PRIMARY KEY (osm_id);
CREATE INDEX fuel_station_state_ix ON core.fuel_station_state (state);
ANALYZE core.fuel_station_state;
"""

PRICE_TABLE = """
DROP TABLE IF EXISTS core.fuel_price_by_state;

-- One row per state: the most recent weekly diesel price for the EIA region the
-- state belongs to. A REGIONAL average, never a pump price.
CREATE TABLE core.fuel_price_by_state (
    state         char(2) PRIMARY KEY,
    eia_region    text NOT NULL,
    price_usd_gal numeric,
    week_of       date,
    note          text NOT NULL DEFAULT
        'EIA weekly on-highway diesel average for this EIA region — not the price at any individual pump'
);
"""


def main() -> int:
    with get_conn() as pg:
        with pg.cursor() as cur:
            cur.execute("SELECT to_regclass('staging.overture_fuel')")
            if cur.fetchone()[0] is None:
                raise SystemExit(
                    "staging.overture_fuel missing — run "
                    "`uv run python scripts/fuel_verify.py --pull` first"
                )
            cur.execute("SELECT count(*) FROM staging.overture_fuel")
            staged = cur.fetchone()[0]
            print(f"[enrich] {staged:,} Overture fuel places staged", flush=True)

            print("[enrich] promoting to core.fuel_places…", flush=True)
            cur.execute(PROMOTE)

            print("[enrich] resolving each station's state…", flush=True)
            cur.execute(STATE_RESOLVE)
            cur.execute("""
                SELECT state_source, count(*) FROM core.fuel_station_state
                GROUP BY 1 ORDER BY 2 DESC
            """)
            for src, n in cur.fetchall():
                print(f"           {str(src or 'still unknown'):<22}{n:>8,}", flush=True)

            print("[enrich] building regional diesel prices…", flush=True)
            cur.execute(PRICE_TABLE)
            rows = [(state, region) for state, region in STATE_TO_PADD.items()]
            cur.executemany(
                """
                INSERT INTO core.fuel_price_by_state (state, eia_region,
                                                      price_usd_gal, week_of)
                SELECT %s, %s, p.price_usd_gal, p.week_of
                FROM core.fuel_prices p
                WHERE p.region = %s AND p.product = 'diesel'
                ORDER BY p.week_of DESC
                LIMIT 1
                """,
                [(s, r, r) for s, r in rows],
            )
            cur.execute("SELECT count(*), count(price_usd_gal) FROM core.fuel_price_by_state")
            n_states, n_priced = cur.fetchone()
            print(f"[enrich] {n_priced}/{n_states} states have a current regional price",
                  flush=True)
        pg.commit()

    # --- report what the join actually delivers ---------------------------
    with get_conn() as pg:
        row = pg.execute("""
            SELECT count(*) AS stations,
                   count(f.ov_place_id) AS matched,
                   count(coalesce(f.name, p.name)) AS with_name,
                   count(p.address) AS with_address,
                   count(coalesce(f.props->>'phone', p.phone)) AS with_phone,
                   count(coalesce(f.props->>'website', p.website)) AS with_website,
                   count(p.email) AS with_email,
                   count(f.props->>'opening_hours') AS with_hours,
                   count(pr.price_usd_gal) AS with_region_price
            FROM osm.fuel_stations f
            LEFT JOIN core.fuel_places p ON p.place_id = f.ov_place_id
            LEFT JOIN core.fuel_station_state st ON st.osm_id = f.osm_id
            LEFT JOIN core.fuel_price_by_state pr ON pr.state = st.state
        """).fetchone()

    labels = ["stations", "matched to Overture", "with a name", "with an address",
              "with a phone", "with a website", "with an email",
              "with opening hours", "with a regional price"]
    total = row[0]
    print(f"\n{'field':<26}{'filled':>10}{'of total':>12}")
    for label, n in zip(labels, row):
        print(f"{label:<26}{n:>10,}{n / total * 100:>11.1f}%")

    print("\nopening hours come from OSM only — Overture Places carries no hours field.")
    print("per-pump prices do not exist in any free legal source; the price above is "
          "a regional weekly average.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
