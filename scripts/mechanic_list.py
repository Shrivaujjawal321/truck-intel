#!/usr/bin/env python
"""Route-based truck mechanic list — a focused, verified, enriched deliverable.

Boss's ask (2026-07-23): a list of truck mechanics with enriched detail, as an
HTML file, with the details VERIFIED. Not the full pipeline — that comes later.

Honest scope, per data/projects/truck-intel/research/mechanics/RESEARCH_BRIEF.md:
  SOURCE   Overture Places (CDLA-Permissive-2.0), truck-SPECIFIC categories only:
           truck_repair, trailer_repair, emergency_roadside_service,
           roadside_assistance. General auto_repair is deliberately excluded —
           this is a TRUCK mechanic list.
  FILLED   name, category, address, city, state, zip, lat/lon, phone, website,
           socials, email, generated Google Maps URL, nearest truck route +
           distance (from core.truck_routes).
  ENRICHED further (2026-07-27, closing DEEP_DIVE_2026-07-24.md §9):
           opening hours + chain badge from All The Places (CC0), state
           licence number + expiry from the NY/NJ registries, an OSM
           corroboration flag, and a per-state coverage denominator from
           Census County Business Patterns.
  NULL-HONEST (no free legal source — never fabricated): rating, review count,
           review summary, photos, WhatsApp, live "open now". Shown as "—" /
           "unknown". Hours are shown only where a permissive source has them.
  VERIFIED means (free, structural — no paid API, no ToS breach):
    - Overture's own confidence score
    - phone is NANP-valid AND its area code's state matches the shop's state
    - coordinate is in the US, not null-island, not a duplicate-cluster point
    - INDEPENDENCE: ≥2 organisations that actually observed the place. The
      aggregator's own feed labels do not count — see the independence note
      below for why, and why this had to be rebuilt on 2026-07-27.
    - fake-listing screen: name-spam / state-vs-coordinate mismatch
  Each shop gets verification_status ∈ {verified, probable, unverified} and a
  confidence 0-100, both explained in the HTML.

Subcommands:
  --pull       scan Overture national parquet -> core.mechanic_shops
  --enrich     re-assign nearest truck route in place (no re-pull, no TRUNCATE)
  --licence    mirror NY/NJ licence registries and join them to shops
  --chains     chain badge + opening hours from All The Places
  --osm-match  flag shops an OSM truck-repair POI corroborates
  --verify     compute verification_status + confidence in place
  --cbp        per-state coverage vs the Census CBP denominator
  --html       render the HTML deliverable
  (default: run all of them, in that order)

ORDER MATTERS: --verify reads the licence and OSM flags, because those are the
only genuinely independent votes available. Running --verify before them
understates every shop.

Audit: exactly one ops.source_runs row per invocation (source_id
'mechanic_list', seeded kind='derived' so freshness_check.py and the
circuit breaker can see it). Exit 0 = success, 1 = failure (recorded on the
run row, never silent — see the 2026-08-18 data-critic review, F-01). --pull
additionally refuses to TRUNCATE core.mechanic_shops if any US state comes
back with an implausibly low row count (see STATE_MIN_ROWS_FLOOR below).
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402
from truckintel.route_assign import (  # noqa: E402
    add_route_columns,
    assign_nearest_route,
)

# load_dotenv() BEFORE reading the env var, not in main(): OUT_DIR is a
# module-level constant, so an override in .env would otherwise be evaluated
# before the file was ever read and silently ignored.
load_dotenv()

# Where the HTML/JSON deliverable lands. Overridable because the default used
# to be an absolute path into the author's private notes directory, which is
# meaningless in any other checkout — and this repo is public.
OUT_DIR = Path(os.environ.get("MECHANICS_OUT_DIR", "data/outputs/mechanics"))
OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_REGION = "us-west-2"

# ops.sources identity for this whole pipeline (2026-08-18 data-critic F-01:
# this script wrote zero ops.source_runs rows and was invisible to
# freshness_check.py, the circuit breaker and gates 1-3 — see
# data/reviews/2026-08-18/05-data-critic.md). One source_id covers every
# subcommand combination main() can run, mirroring chain_sites.py /
# osm_extract.py's "exactly one ops.source_runs row per invocation" rule.
SOURCE_ID = "mechanic_list"
# The daily --refresh chain (licence + chains + OSM + verify + cbp + html) is
# the cadence that actually keeps the deliverable live; the monthly --pull is
# allowed to be the stale case some months. 48h gives one missed daily run
# before paging, per the review's explicit recommendation.
SLO_HOURS = 48
TRUCK_CATS = (
    "truck_repair", "truck_repair_and_services_for_businesses",
    "trailer_repair", "emergency_roadside_service", "roadside_assistance",
)
CAT_LABEL = {
    "truck_repair": "Truck repair",
    "truck_repair_and_services_for_businesses": "Truck repair (fleet)",
    "trailer_repair": "Trailer repair",
    "emergency_roadside_service": "Roadside / breakdown",
    "roadside_assistance": "Roadside / breakdown",
}

# NANP US geographic area code -> USPS state. Used ONLY to check phone/state
# consistency (a free, deterministic verification signal — never to fabricate a
# location). Non-geographic codes (800/888/877/866/855/844/833/822 toll-free,
# 900 premium) map to None and are treated as "not state-verifiable".
AREACODE_STATE = {
    # AL
    "205": "AL", "251": "AL", "256": "AL", "334": "AL", "938": "AL",
    # AK
    "907": "AK",
    # AZ
    "480": "AZ", "520": "AZ", "602": "AZ", "623": "AZ", "928": "AZ",
    # AR
    "479": "AR", "501": "AR", "870": "AR",
    # CA
    "209": "CA", "213": "CA", "279": "CA", "310": "CA", "323": "CA",
    "341": "CA", "350": "CA", "408": "CA", "415": "CA", "424": "CA",
    "442": "CA", "510": "CA", "530": "CA", "559": "CA", "562": "CA",
    "619": "CA", "626": "CA", "628": "CA", "650": "CA", "657": "CA",
    "661": "CA", "669": "CA", "707": "CA", "714": "CA", "747": "CA",
    "760": "CA", "805": "CA", "818": "CA", "820": "CA", "831": "CA",
    "840": "CA", "858": "CA", "909": "CA", "916": "CA", "925": "CA",
    "949": "CA", "951": "CA",
    # CO
    "303": "CO", "719": "CO", "720": "CO", "970": "CO", "983": "CO",
    # CT
    "203": "CT", "475": "CT", "860": "CT", "959": "CT",
    # DE
    "302": "DE",
    # DC
    "202": "DC",
    # FL
    "239": "FL", "305": "FL", "321": "FL", "352": "FL", "386": "FL",
    "407": "FL", "448": "FL", "561": "FL", "656": "FL", "689": "FL",
    "727": "FL", "754": "FL", "772": "FL", "786": "FL", "813": "FL",
    "850": "FL", "863": "FL", "904": "FL", "941": "FL", "954": "FL",
    # GA
    "229": "GA", "404": "GA", "470": "GA", "478": "GA", "678": "GA",
    "706": "GA", "762": "GA", "770": "GA", "912": "GA", "943": "GA",
    # HI
    "808": "HI",
    # ID
    "208": "ID", "986": "ID",
    # IL
    "217": "IL", "224": "IL", "309": "IL", "312": "IL", "331": "IL",
    "447": "IL", "464": "IL", "618": "IL", "630": "IL", "708": "IL",
    "730": "IL", "773": "IL", "779": "IL", "815": "IL", "847": "IL",
    "872": "IL",
    # IN
    "219": "IN", "260": "IN", "317": "IN", "463": "IN", "574": "IN",
    "765": "IN", "812": "IN", "930": "IN",
    # IA
    "319": "IA", "515": "IA", "563": "IA", "641": "IA", "712": "IA",
    # KS
    "316": "KS", "620": "KS", "785": "KS", "913": "KS",
    # KY
    "270": "KY", "364": "KY", "502": "KY", "606": "KY", "859": "KY",
    # LA
    "225": "LA", "318": "LA", "337": "LA", "504": "LA", "985": "LA",
    # ME
    "207": "ME",
    # MD
    "227": "MD", "240": "MD", "301": "MD", "410": "MD", "443": "MD",
    "667": "MD",
    # MA
    "339": "MA", "351": "MA", "413": "MA", "508": "MA", "617": "MA",
    "774": "MA", "781": "MA", "857": "MA", "978": "MA",
    # MI
    "231": "MI", "248": "MI", "269": "MI", "313": "MI", "517": "MI",
    "586": "MI", "616": "MI", "679": "MI", "734": "MI", "810": "MI",
    "906": "MI", "947": "MI", "989": "MI",
    # MN
    "218": "MN", "320": "MN", "507": "MN", "612": "MN", "651": "MN",
    "763": "MN", "952": "MN",
    # MS
    "228": "MS", "601": "MS", "662": "MS", "769": "MS",
    # MO
    "314": "MO", "417": "MO", "557": "MO", "573": "MO", "636": "MO",
    "660": "MO", "816": "MO",
    # MT
    "406": "MT",
    # NE
    "308": "NE", "402": "NE", "531": "NE",
    # NV
    "702": "NV", "725": "NV", "775": "NV",
    # NH
    "603": "NH",
    # NJ
    "201": "NJ", "551": "NJ", "609": "NJ", "640": "NJ", "732": "NJ",
    "848": "NJ", "856": "NJ", "862": "NJ", "908": "NJ", "973": "NJ",
    # NM
    "505": "NM", "575": "NM",
    # NY
    "212": "NY", "315": "NY", "329": "NY", "332": "NY", "347": "NY",
    "363": "NY", "516": "NY", "518": "NY", "585": "NY", "607": "NY",
    "631": "NY", "646": "NY", "680": "NY", "716": "NY", "718": "NY",
    "838": "NY", "845": "NY", "914": "NY", "917": "NY", "929": "NY",
    "934": "NY",
    # NC
    "252": "NC", "336": "NC", "472": "NC", "704": "NC", "743": "NC",
    "828": "NC", "910": "NC", "919": "NC", "980": "NC", "984": "NC",
    # ND
    "701": "ND",
    # OH
    "216": "OH", "220": "OH", "234": "OH", "326": "OH", "330": "OH",
    "380": "OH", "419": "OH", "440": "OH", "513": "OH", "567": "OH",
    "614": "OH", "740": "OH", "937": "OH",
    # OK
    "405": "OK", "539": "OK", "580": "OK", "918": "OK",
    # OR
    "458": "OR", "503": "OR", "541": "OR", "971": "OR",
    # PA
    "215": "PA", "223": "PA", "267": "PA", "272": "PA", "412": "PA",
    "445": "PA", "484": "PA", "570": "PA", "582": "PA", "610": "PA",
    "717": "PA", "724": "PA", "814": "PA", "835": "PA", "878": "PA",
    # RI
    "401": "RI",
    # SC
    "803": "SC", "839": "SC", "843": "SC", "854": "SC", "864": "SC",
    # SD
    "605": "SD",
    # TN
    "423": "TN", "615": "TN", "629": "TN", "731": "TN", "865": "TN",
    "901": "TN", "931": "TN",
    # TX
    "210": "TX", "214": "TX", "254": "TX", "281": "TX", "325": "TX",
    "346": "TX", "361": "TX", "409": "TX", "430": "TX", "432": "TX",
    "469": "TX", "512": "TX", "682": "TX", "713": "TX", "726": "TX",
    "737": "TX", "806": "TX", "817": "TX", "830": "TX", "832": "TX",
    "903": "TX", "915": "TX", "936": "TX", "940": "TX", "945": "TX",
    "956": "TX", "972": "TX", "979": "TX",
    # UT
    "385": "UT", "435": "UT", "801": "UT",
    # VT
    "802": "VT",
    # VA
    "276": "VA", "434": "VA", "540": "VA", "571": "VA", "703": "VA",
    "757": "VA", "804": "VA", "826": "VA", "948": "VA",
    # WA
    "206": "WA", "253": "WA", "360": "WA", "425": "WA", "509": "WA",
    "564": "WA",
    # WV
    "304": "WV", "681": "WV",
    # WI
    "262": "WI", "274": "WI", "414": "WI", "534": "WI", "608": "WI",
    "715": "WI", "920": "WI",
    # WY
    "307": "WY",
}


# ------------------------------------------------------------- independence
# Overture publishes a `sources` array naming the feed each record came from,
# and `n_sources` counted those names. That count never fell below 2, so it
# awarded full marks to every row and separated nothing (DEEP_DIVE §3).
#
# Collapsing 'Overture' and 'Overture-signals' into one organisation — the fix
# the deep dive proposed — is necessary but NOT sufficient: measured
# 2026-07-27 it still left 11,551 rows at 2 and 208 at 3, because every row
# also carries a 'meta' or 'Microsoft' name. The real problem is one level
# deeper.
#
# Overture is a CONFLATOR, not a collector. 'Overture' and 'Overture-signals'
# are the aggregator's own labels on a record it assembled; they attest to no
# independent survey of the premises. The actual collection was done by the
# member who donated the record — Meta (business-owner-maintained Pages) or
# Microsoft (Bing listings). Counting the aggregator as a witness to its own
# output is the same error as counting its two pipelines twice, just harder to
# see.
#
# So: aggregator labels get NO vote. Every US truck shop then turns out to
# have exactly ONE real contributor, which is the honest finding — source
# agreement inside Overture carries no information for this dataset at all,
# and independence has to come from outside it. That is precisely what the
# state licence registries and OpenStreetMap are for.
_ORG_PREFIXES = (
    ("overture", "overture"),
    ("microsoft", "microsoft"), ("msft", "microsoft"), ("bing", "microsoft"),
    ("meta", "meta"), ("facebook", "meta"),
)

# Organisations that publish the dataset rather than survey the world. Kept in
# `source_orgs` for provenance; excluded from `n_independent`.
AGGREGATOR_ORGS = frozenset({"overture"})


def source_org(feed_name: str) -> str:
    """Feed name -> owning organisation. Unknown feeds keep their own identity
    (a new donor is a real extra vote until proven otherwise)."""
    n = (feed_name or "").strip().lower()
    for prefix, org in _ORG_PREFIXES:
        if n.startswith(prefix):
            return org
    return n


def independence(source_names, *, licence_ok=False, licence_expired=False,
                 osm_id=None) -> tuple[list[str], int]:
    """(source_orgs, n_independent) for one shop.

    Pure function so the rule that decides what may be called `verified` can
    be tested without a database — this is the claim the whole deliverable
    rests on, and it was wrong once already.
    """
    orgs = {source_org(s) for s in (source_names or []) if s}
    if licence_ok and not licence_expired:
        orgs.add("state_licence")   # a government registry, truly independent
    if osm_id:
        orgs.add("osm")             # a different mapping community entirely
    return sorted(orgs), len(orgs - AGGREGATOR_ORGS)


# ----------------------------------------------------------------- schema
DDL = """
CREATE TABLE IF NOT EXISTS core.mechanic_shops (
    shop_id        TEXT PRIMARY KEY,          -- Overture id
    name           TEXT,
    category_src   TEXT,                      -- Overture category slug
    category       TEXT,                      -- our label
    brand          TEXT,
    lat            DOUBLE PRECISION,
    lon            DOUBLE PRECISION,
    geom           geometry(Point, 4326),
    address        TEXT,
    city           TEXT,
    state          CHAR(2),
    zip            TEXT,
    phone          TEXT,
    website        TEXT,
    email          TEXT,
    socials        TEXT[],
    src_confidence DOUBLE PRECISION,          -- Overture confidence 0-1
    n_sources      INTEGER,                   -- RAW Overture source-dataset count
    source_names   TEXT[],
    -- independence (computed in --verify; see INDEPENDENCE note below)
    source_orgs    TEXT[],                    -- source_names collapsed to owning orgs
    n_independent  SMALLINT,                  -- distinct orgs — the honest count
    -- route enrichment (from core.truck_routes)
    route_id       BIGINT,
    route_ref      TEXT,
    route_name     TEXT,
    route_dist_m   INTEGER,
    on_route_5km   BOOLEAN,
    gmaps_url      TEXT,
    -- verification (computed in --verify)
    phone_valid    BOOLEAN,
    phone_state_ok BOOLEAN,
    coord_ok       BOOLEAN,
    cluster_dup    BOOLEAN,
    spam_flag      BOOLEAN,
    verification_status TEXT,                 -- verified | probable | unverified
    confidence     INTEGER,                   -- 0-100
    flags          TEXT[] DEFAULT '{}',
    -- state licence join (--licence): the only GENUINELY independent vote we
    -- can get, because every Overture* pipeline belongs to one organisation.
    licence_verified BOOLEAN,                 -- NULL = state not covered, never "no"
    licence_id     TEXT,
    licence_state  CHAR(2),
    licence_expiry DATE,
    licence_expired BOOLEAN,
    licence_rule   TEXT,                      -- which match rule fired
    -- hours (--chains): permissive chain feeds + OSM, never scraped from Google
    opening_hours  TEXT,                      -- OSM opening_hours syntax
    open_24h       BOOLEAN,
    hours_source   TEXT,                      -- alltheplaces | osm
    chain_brand    TEXT,
    -- OSM corroboration (--osm-match): independent of Overture AND of the
    -- state registries; ODbL stays in osm.*, only the match flag lands here.
    osm_match_id   TEXT,
    osm_match_m    INTEGER,
    observed_at    TIMESTAMPTZ,               -- Overture release vintage
    props          JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS mechanic_shops_geom_gix ON core.mechanic_shops USING GIST (geom);
CREATE INDEX IF NOT EXISTS mechanic_shops_state_ix ON core.mechanic_shops (state);

-- Mirror of the state licence registries we can legally bulk-download. Kept as
-- its own table (not merged into mechanic_shops) because it is a REGISTRY, not
-- a shop list: 54k NY rows cover every vehicle-repair class, so joining is a
-- verification act, never a discovery one (DEEP_DIVE §4).
CREATE TABLE IF NOT EXISTS core.mechanic_licences (
    licence_key   TEXT PRIMARY KEY,           -- '<state>/<licence_id>/<n>'
    state         CHAR(2) NOT NULL,
    licence_id    TEXT,
    name          TEXT,
    name_norm     TEXT,                       -- normalised for matching
    address       TEXT,
    addr_norm     TEXT,                       -- normalised for matching
    city          TEXT,
    zip           TEXT,
    licence_type  TEXT,
    expiry        DATE,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    geom          geometry(Point, 4326),
    source_url    TEXT,
    observed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS mechanic_licences_geom_gix ON core.mechanic_licences USING GIST (geom);
CREATE INDEX IF NOT EXISTS mechanic_licences_norm_ix ON core.mechanic_licences (state, name_norm);

-- Fill history: one row per metric per refresh. Exists to answer the question
-- a snapshot cannot — "did today's run actually LEARN anything?" Without it a
-- daily refresh that silently stopped finding new detail would look identical
-- to one that is working.
CREATE TABLE IF NOT EXISTS core.mechanic_fill_history (
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metric      TEXT NOT NULL,
    filled      INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    PRIMARY KEY (snapshot_at, metric)
);

-- Per-state coverage denominator from Census County Business Patterns. Answers
-- the one question a shop count alone cannot: is South Dakota EMPTY, or merely
-- UNLISTED? (DEEP_DIVE §5.)
CREATE TABLE IF NOT EXISTS core.mechanic_coverage (
    state          CHAR(2) PRIMARY KEY,
    cbp_year       SMALLINT NOT NULL,
    cbp_estab_811111 INTEGER,                 -- General automotive repair
    cbp_estab_811310 INTEGER,                 -- Commercial/industrial machinery repair
    shops          INTEGER NOT NULL,
    shops_on_route INTEGER NOT NULL,
    route_miles    DOUBLE PRECISION,
    miles_per_shop DOUBLE PRECISION,
    capture_rate   DOUBLE PRECISION,          -- shops / cbp_estab_811111
    verdict        TEXT,                      -- thin_data | real_scarcity | ok
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Columns added after the first national load. The table holds 11,759 rows that
# cost an 11 GB download, so every later change is an ALTER, never a re-CREATE.
MIGRATE = """
ALTER TABLE core.mechanic_shops
  ADD COLUMN IF NOT EXISTS source_orgs      TEXT[],
  ADD COLUMN IF NOT EXISTS n_independent    SMALLINT,
  ADD COLUMN IF NOT EXISTS licence_verified BOOLEAN,
  ADD COLUMN IF NOT EXISTS licence_id       TEXT,
  ADD COLUMN IF NOT EXISTS licence_state    CHAR(2),
  ADD COLUMN IF NOT EXISTS licence_expiry   DATE,
  ADD COLUMN IF NOT EXISTS licence_expired  BOOLEAN,
  ADD COLUMN IF NOT EXISTS licence_rule     TEXT,
  ADD COLUMN IF NOT EXISTS opening_hours    TEXT,
  ADD COLUMN IF NOT EXISTS open_24h         BOOLEAN,
  ADD COLUMN IF NOT EXISTS hours_source     TEXT,
  ADD COLUMN IF NOT EXISTS chain_brand      TEXT,
  ADD COLUMN IF NOT EXISTS osm_match_id     TEXT,
  ADD COLUMN IF NOT EXISTS osm_match_m      INTEGER;

ALTER TABLE core.mechanic_licences
  ADD COLUMN IF NOT EXISTS addr_norm TEXT;
CREATE INDEX IF NOT EXISTS mechanic_licences_addr_ix
  ON core.mechanic_licences (state, addr_norm);
"""


# Two schedules now write core.mechanic_shops: a monthly --pull (3 h) and a
# daily --refresh (minutes). On the 2nd of the month both are due, and the
# refresh's UPDATEs interleaved with the pull's TRUNCATE+reload would produce a
# table that is half old and half new with no way to tell which. Timing them
# apart is not a guarantee — a slow pull outlives any gap. A lock is.
#
# Advisory, not a table lock: it is held for the whole process, released
# automatically if the process dies, and costs nothing. The key is an arbitrary
# constant that only this script uses.
_LOCK_KEY = 0x7CE3_4EC1        # "truck mechanic" — any stable constant


class _Busy(RuntimeError):
    pass


def _acquire_lock():
    """Hold the mechanic-pipeline lock for the life of the returned connection.

    Raises _Busy when another run holds it. The caller exits 0 on that — a
    skipped run because the monthly pull is still going is correct behaviour,
    not a failure, and alerting on it would train Boss to ignore the alert.
    """
    conn = get_conn(autocommit=True)
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,)).fetchone()[0]
    if not got:
        conn.close()
        raise _Busy("another mechanic_list run holds the pipeline lock")
    return conn


def ensure_schema() -> None:
    """Create-if-absent, then migrate. Every subcommand calls this, so a stage
    can be run on a database that has never seen the later stages' columns."""
    with get_conn() as pg:
        pg.execute(DDL)
        pg.execute(MIGRATE)


# ------------------------------------------------------------ run bookkeeping
#
# Idempotent seed, same shape as chain_sites.py / osm_extract.py: a fresh DB
# has no ops.sources row for this pipeline yet, and ops.source_runs has an FK
# to ops.sources, so the seed must run before the first INSERT ever can.
_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s,
     'Derived: route-based truck mechanic list (Overture Places + NY/NJ '
     'licence registries + All The Places + OSM) -> core.mechanic_shops',
     'truck-intel mechanics track', 'derived', 'derived', NULL, %(slo)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""


def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID, "slo": SLO_HOURS})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id", (SOURCE_ID,)
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows_published: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, (message or "")[:1000] or None, rows_published, run_id))


# Every state Overture's US places extract should be able to see (+DC).
# Territories excluded on purpose: pull()'s own query only keeps
# addresses[1].country IN ('US', NULL), so PR/VI/GU rows are rare and not a
# fair comparison against a 50-states-+-DC baseline. Same set osm_extract.py
# and osm_overpass.py already carry for the identical reason.
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY".split()
)

# A structural backstop, not a market estimate — unlike chain_sites.py's
# MIN_ROWS dict (set from a real dated production run, 2026-08-17), there is
# no observed per-state baseline for this pull yet: building one honestly
# means running the 3h monthly scan, which is out of scope for this fix. 3 is
# deliberately tiny: even DC and the thinnest states (WY, VT, ND) sit on
# interstate corridors and have always returned more than a handful of
# truck-specific Overture rows. A state at 0-2 means the scan silently missed
# that state's slice of the parquet store (partial S3 mirror, a country/region
# filter bug) — a national category scan going to true zero in an entire
# state is not a plausible real-world outcome, so this never fires on genuine
# scarcity. TODO once a real post-fix --pull lands: replace with a per-state
# MIN_ROWS dict the same way chain_sites.py did, from the observed counts.
STATE_MIN_ROWS_FLOOR = 3

# Self-calibrating guard against the state's OWN last successful pull —
# chosen because it needs no fabricated ground truth: "roughly half of what
# this exact state had last time" is the same ratio chain_sites.py's MIN_ROWS
# dict comment uses ("roughly half of the 2026-08-17 observed counts"),
# applied relatively instead of as a hardcoded number we have no grounding
# for. Skipped below a 10-row baseline — too little signal to tell a real
# swing from noise in a small state.
STATE_MIN_BASELINE_ROWS = 10
STATE_MAX_DROP_RATIO = 0.5


def _state_floor_violations(rows: list[tuple], prev_counts: dict[str, int]) -> list[str]:
    """States this pull looks broken for, not merely thin. `rows` are the raw
    Overture tuples from pull()'s cursor (state is column index 8);
    `prev_counts` is {state: shop count} from core.mechanic_shops BEFORE the
    TRUNCATE this run is about to issue. Pure function so the rule can be
    tested without a database, same reasoning as independence() above."""
    counts: dict[str, int] = {}
    for r in rows:
        state = (r[8] or "")[:2].upper()
        if state in _US_STATES:
            counts[state] = counts.get(state, 0) + 1
    problems = []
    for state in sorted(_US_STATES):
        n = counts.get(state, 0)
        if n < STATE_MIN_ROWS_FLOOR:
            problems.append(f"{state}: {n} < absolute floor {STATE_MIN_ROWS_FLOOR}")
            continue
        prev = prev_counts.get(state, 0)
        if prev >= STATE_MIN_BASELINE_ROWS and n < prev * STATE_MAX_DROP_RATIO:
            problems.append(
                f"{state}: {n} is a {100 * (1 - n / prev):.0f}% drop from "
                f"last successful pull's {prev}")
    return problems


def _duck():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_region='{OVERTURE_REGION}'")
    return con


def discover_release(con) -> str:
    """Newest release prefix under the bucket; fall back to a known-good one."""
    try:
        rows = con.execute(
            f"SELECT DISTINCT regexp_extract(file, 'release/([^/]+)/', 1) "
            f"FROM glob('s3://{OVERTURE_BUCKET}/release/*/theme=places/type=place/*.parquet') "
            f"AS t(file) ORDER BY 1 DESC LIMIT 1"
        ).fetchall()
        if rows and rows[0][0]:
            return rows[0][0]
    except Exception as e:
        print(f"  release discovery failed ({e}); using fallback", flush=True)
    return "2026-07-22.0"


def pull(release: str | None = None, local_dir: str | None = None) -> int:
    """Scan the Overture places store for truck-specific categories.

    Reading the 10.5 GB store straight off S3 stalls on a home link (observed
    2026-07-24: DuckDB httpfs dropped to ~5 KB/s with sockets in CLOSE-WAIT and
    made no progress for 2.5 h). Mirroring the 16 parquet files locally first
    with resumable parallel curl, then scanning them, is the reliable path —
    same data, same query, no long-lived HTTP range reads.
    """
    con = _duck()
    if local_dir:
        rel = release or Path(local_dir).name
        src = f"read_parquet('{local_dir.rstrip('/')}/*.parquet')"
        where_from = f"local mirror {local_dir}"
    else:
        rel = release or discover_release(con)
        src = (f"read_parquet('s3://{OVERTURE_BUCKET}/release/{rel}"
               "/theme=places/type=place/*.parquet')")
        where_from = f"s3 release {rel}"
    try:
        observed = datetime.strptime(rel[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        # A local mirror dir that isn't named after the release: fall back to the
        # newest release id on S3 rather than stamping a wrong vintage.
        rel = discover_release(con)
        observed = datetime.strptime(rel[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    inlist = ", ".join(f"'{c}'" for c in TRUCK_CATS)
    print(f"[pull] {where_from}: scanning national parquet for truck-specific "
          f"categories (this reads the US places column store — minutes)…",
          flush=True)
    t = time.time()
    # sources[] is a list of structs {property, dataset, record_id, update_time};
    # unnest to count distinct contributing datasets (independence signal).
    cur = con.execute(f"""
        SELECT id,
               names.primary AS name,
               brand.names.primary AS brand,
               categories.primary AS category_src,
               bbox.ymin AS lat, bbox.xmin AS lon,
               addresses[1].freeform AS address,
               addresses[1].locality AS city,
               addresses[1].region AS state,
               addresses[1].postcode AS zip,
               phones[1] AS phone,
               websites[1] AS website,
               emails[1] AS email,
               socials,
               confidence AS src_confidence,
               list_transform(sources, x -> x.dataset) AS source_names
        FROM {src}
        WHERE categories.primary IN ({inlist})
          AND (addresses[1].country = 'US' OR addresses[1].country IS NULL)
    """)
    rows = cur.fetchall()
    print(f"[pull] scan done in {time.time()-t:.0f}s — {len(rows)} rows", flush=True)

    loaded = 0
    ensure_schema()
    with get_conn() as pg:
        # Snapshot BEFORE the truncate — this is the only place the previous
        # run's per-state counts still exist. Raising below (before the
        # TRUNCATE executes) rolls the whole transaction back on exit, so a
        # tripped guard leaves the live table exactly as it was: no manual
        # rollback bookkeeping needed, same "gate aborts, old data stays live"
        # outcome the registry gates give engine.py-driven pulls.
        prev_counts = dict(pg.execute(
            "SELECT state, count(*) FROM core.mechanic_shops "
            "WHERE state IS NOT NULL GROUP BY state").fetchall())
        problems = _state_floor_violations(rows, prev_counts)
        if problems:
            raise RuntimeError(
                f"per-state min_rows guard tripped on {len(problems)} "
                "state(s), refusing to publish (truncate aborted): "
                + "; ".join(problems))
        pg.execute("TRUNCATE core.mechanic_shops")
        with pg.cursor() as cur2:
            for r in rows:
                (gid, name, brand, cat_src, lat, lon, address, city, state, zip_,
                 phone, website, email, socials, conf, source_names) = r
                if lat is None or lon is None:
                    continue
                src_names = sorted(set(s for s in (source_names or []) if s))
                cur2.execute("""
                    INSERT INTO core.mechanic_shops
                      (shop_id, name, category_src, category, brand, lat, lon, geom,
                       address, city, state, zip, phone, website, email, socials,
                       src_confidence, n_sources, source_names, gmaps_url, observed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326),
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (shop_id) DO NOTHING
                """, (
                    gid, name, cat_src, CAT_LABEL.get(cat_src, cat_src), brand,
                    lat, lon, lon, lat,
                    address, city, (state or "")[:2].upper() or None, zip_,
                    phone, website, email, list(socials) if socials else None,
                    conf, len(src_names), src_names or None,
                    f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
                    observed,
                ))
                loaded += 1
    print(f"[pull] loaded {loaded} shops into core.mechanic_shops", flush=True)
    return loaded


def enrich_routes() -> None:
    """Nearest truck route + distance for every shop (KNN, one pass).

    The query lives in `truckintel.route_assign` because fuel stations need the
    identical measurement; see that module for why it is shaped the way it is.
    Keeping one copy is what makes "on route" mean the same thing on both
    layers of the map.
    """
    print("[enrich] assigning nearest truck route (KNN <->)…", flush=True)
    with get_conn() as pg:
        add_route_columns(pg, "core.mechanic_shops")
        n = assign_nearest_route(pg, "core.mechanic_shops", "shop_id")
    print(f"[enrich] route assignment done ({n:,} rows)", flush=True)


def verify() -> None:
    """Structural, free verification -> verification_status + confidence.

    Run this AFTER --licence and --osm-match: those two supply the only
    genuinely independent votes, and the independence count is what decides
    whether a shop may be called `verified` at all.
    """
    ensure_schema()
    print("[verify] scoring…", flush=True)
    with get_conn() as pg:
        with pg.cursor() as cur:
            # duplicate-coordinate clusters (fake-listing signal): >=3 shops at
            # the exact same rounded point.
            cur.execute("""
                SELECT round(lat::numeric,5), round(lon::numeric,5)
                FROM core.mechanic_shops
                GROUP BY 1,2 HAVING count(*) >= 3
            """)
            dup_pts = {(str(a), str(b)) for a, b in cur.fetchall()}

            cur.execute("""SELECT shop_id, name, state, lat, lon, phone,
                                  src_confidence, n_sources, source_names,
                                  licence_verified, licence_expired, osm_match_id
                           FROM core.mechanic_shops""")
            rows = cur.fetchall()

    def phone_digits(p):
        if not p:
            return None
        d = re.sub(r"\D", "", p)
        if len(d) == 11 and d[0] == "1":
            d = d[1:]
        return d if len(d) == 10 else None

    def is_nanp(d):
        # NANP: area code + exchange first digits are 2-9; not N11.
        return bool(d) and d[0] in "23456789" and d[3] in "23456789" and d[1:3] != "11"

    ascii_re = re.compile(r"[A-Za-z]")
    updates = []
    for (sid, name, state, lat, lon, phone, conf, nsrc, snames,
         lic_ok, lic_expired, osm_id) in rows:
        d = phone_digits(phone)
        phone_valid = is_nanp(d) if d else False
        ac = d[:3] if d else None
        phone_state_ok = bool(ac and state and AREACODE_STATE.get(ac) == state)
        coord_ok = (lat is not None and lon is not None
                    and -180 < lon < 0 and 15 < lat < 72
                    and not (abs(lat) < 0.5 and abs(lon) < 0.5))
        cluster_dup = (str(round(lat, 5)), str(round(lon, 5))) in dup_pts if coord_ok else True
        # spam screen: no Latin letters in the name (e.g. 'Вакансии'), or blank.
        spam = not name or not ascii_re.search(name)

        # The honest corroboration count: organisations that INDEPENDENTLY
        # observed this place. The aggregator's own labels are provenance, not
        # corroboration, so they stay in `source_orgs` and out of the count.
        orgs, n_ind = independence(snames, licence_ok=lic_ok,
                                   licence_expired=lic_expired, osm_id=osm_id)

        score = 0
        if conf is not None:
            score += int(min(max(conf, 0), 1) * 40)          # up to 40
        if phone_valid:
            score += 10
        if phone_state_ok:
            score += 15
        if coord_ok and not cluster_dup:
            score += 15
        score += {0: 0, 1: 0, 2: 12}.get(n_ind, 20)          # up to 20
        if spam:
            score -= 40
        if cluster_dup:
            score -= 15
        score = max(0, min(100, score))

        # `verified` now REQUIRES two independent organisations. A shop attested
        # only by Overture's own pipelines is `probable` however good its phone
        # is — which is the honest answer, not a regression.
        if spam or not coord_ok:
            status = "unverified"
        elif score >= 70 and phone_valid and not cluster_dup and n_ind >= 2:
            status = "verified"
        elif score >= 45:
            status = "probable"
        else:
            status = "unverified"

        flags = []
        if cluster_dup:
            flags.append("shared_coordinate")
        if phone and not phone_valid:
            flags.append("phone_malformed")
        if ac and state and AREACODE_STATE.get(ac) and AREACODE_STATE.get(ac) != state:
            flags.append("phone_state_mismatch")
        if spam:
            flags.append("name_nonlatin_or_blank")
        if n_ind <= 1:
            flags.append("single_org_source")
        if lic_ok and not lic_expired:
            flags.append("state_licensed")
        if lic_ok and lic_expired:
            flags.append("licence_expired")
        if osm_id:
            flags.append("osm_corroborated")

        updates.append((phone_valid, phone_state_ok, coord_ok, cluster_dup,
                        spam, orgs, n_ind, status, score, flags, sid))

    with get_conn() as pg:
        with pg.cursor() as cur:
            cur.executemany("""
                UPDATE core.mechanic_shops SET
                  phone_valid=%s, phone_state_ok=%s, coord_ok=%s, cluster_dup=%s,
                  spam_flag=%s, source_orgs=%s, n_independent=%s,
                  verification_status=%s, confidence=%s, flags=%s
                WHERE shop_id=%s
            """, updates)
    print(f"[verify] scored {len(updates)} shops", flush=True)
    _print_bands()


def _print_bands() -> None:
    """Status/independence distribution — printed after every --verify so a
    scoring change is visible in the log, not just in the database."""
    for label, sql in (
        ("status", "SELECT verification_status, count(*), round(avg(confidence)) "
                   "FROM core.mechanic_shops GROUP BY 1 ORDER BY 2 DESC"),
        ("n_independent", "SELECT n_independent, count(*) FROM core.mechanic_shops "
                          "GROUP BY 1 ORDER BY 1"),
    ):
        with get_conn() as pg:
            rows = pg.execute(sql).fetchall()
        print(f"  {label}: " + "  ".join(str(tuple(r)) for r in rows), flush=True)


# ------------------------------------------------------------- name matching
# Joining a registry to a shop list is a name-match problem, and the two sides
# spell the same business differently ("A-1 ALL GERMAN CAR CORP" vs "A1 All
# German Car"). Normalise both through the SAME function — a second, slightly
# different normaliser on one side is how these joins silently under-match.
_CORP_WORDS = {
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "CO", "CORP",
    "CORPORATION", "COMPANY", "THE", "AND", "OF", "DBA",
}


# A registry lists the LEGAL name ("BROADWAY GARAGE OF BETHPAGE INC") where a
# places dataset lists the TRADING name ("Broadway Garage"), so a name-only
# join misses shops that plainly exist in both. The street address is the
# second identifier both sides publish — once the abbreviations are spoken the
# same way. Expansion order matters: longest form first, or 'STREET' becomes
# 'ST' before 'ST' can be left alone.
_STREET_WORDS = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD",
    "DRIVE": "DR", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "LANE": "LN",
    "PLACE": "PL", "COURT": "CT", "PARKWAY": "PKWY", "TURNPIKE": "TPKE",
    "TPK": "TPKE", "ROUTE": "RT", "RTE": "RT", "CIRCLE": "CIR",
    "TERRACE": "TER", "TRAIL": "TRL", "SQUARE": "SQ", "EXPRESSWAY": "EXPY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
# Unit designators and everything after them: "12 MAIN ST STE 4" and
# "12 MAIN ST" are the same building.
_UNIT_RE = re.compile(r"\b(STE|SUITE|UNIT|APT|BLDG|BUILDING|FL|FLOOR|#).*$")
_ORDINAL_RE = re.compile(r"\b(\d+)(ST|ND|RD|TH)\b")


def addr_norm(s: str | None) -> str | None:
    """Street address -> comparable key. None when nothing usable remains.

    PO boxes return None on purpose: NJ lists several facilities at a PO box,
    and matching two shops because they share a mailbox would be a false
    positive dressed as evidence.
    """
    if not s:
        return None
    up = re.sub(r"[^A-Z0-9 ]", " ", s.upper())
    up = re.sub(r"\bP\s*O\s+BOX\b.*$", "", up)
    up = _UNIT_RE.sub("", up)
    up = _ORDINAL_RE.sub(r"\1", up)
    words = [_STREET_WORDS.get(w, w) for w in up.split()]
    out = "".join(words)
    return out or None


def name_norm(s: str | None) -> str | None:
    """Upper-case, '&'->AND, drop corporate suffix words, keep A-Z0-9 only.

    Returns None for a name that normalises to nothing (punctuation-only or
    non-Latin) — a NULL key never matches, which is the correct behaviour;
    an empty-string key would match every other empty-string key.
    """
    if not s:
        return None
    up = re.sub(r"[^A-Z0-9 ]", " ", s.upper().replace("&", " AND "))
    kept = [w for w in up.split() if w not in _CORP_WORDS]
    out = "".join(kept)
    return out or None


# ------------------------------------------------------------ state licences
# What a licence join buys us: an existence-and-currency check from a
# government that is genuinely independent of Overture — the scarcest input in
# the verification stack now that source agreement has been shown to be one
# organisation voting twice (DEEP_DIVE §3/§4).
#
# These registries license ALL vehicle repair, not truck repair. They are a
# VERIFICATION layer, never a discovery one: classifying 54k NY auto shops by
# name to find truck shops is exactly the false-positive trap the brief
# measured. We only ever join them TO shops we already found.
LICENCE_SOURCES = [
    {
        "state": "NY",
        "url": "https://data.ny.gov/resource/nhjr-rpi2.json",
        # NY DMV facility classes. RS = repair shop, RSB = repair shop/body.
        # Dealers (DL*), inspection-only (IS*), dismantlers (DI*),
        # transporters (TRS) and ATV shops are NOT repair licences and are
        # excluded — including them would "verify" a shop against a used-car
        # lot that happens to share its address.
        "type_field": "business_type",
        "keep_types": {"RS", "RSB"},
        "f": {"id": "facility", "name": "facility_name",
              "street": "facility_street", "city": "facility_city",
              "zip": "facility_zip_code", "type": "business_type",
              "expiry": "expiration_date", "geo": "georeference"},
    },
    {
        "state": "NJ",
        # t6tk-mr48, not rggz-cv9v: both report 1,167 rows but rggz-cv9v's
        # columns come back empty over the API, so it is unusable in practice.
        "url": "https://data.nj.gov/resource/t6tk-mr48.json",
        "type_field": None,
        "keep_types": None,
        "f": {"id": "lic_id", "name": "st_nm", "street": "adr_street",
              "city": "adr_city", "zip": "zip", "type": "type",
              "expiry": None, "geo": None},
    },
    # CT (data.ct.gov apne-w8c6, "Licensed Automobile Dealers And Repairers")
    # is DELIBERATELY ABSENT. The deep dive listed it at 138 rows; measured
    # 2026-07-27, all 138 carry license_type = 'MANUFACTURER LICENSE' — the
    # published extract contains no repairers at all despite its title. Adding
    # it would contribute zero matches while implying CT coverage we lack.
]

_SOCRATA_PAGE = 50_000


def _socrata_all(url: str, *, select: str | None = None) -> list[dict]:
    """Every row of a Socrata resource, keyless, paginated. No API token is
    needed at this volume and none is stored — these are public datasets."""
    import urllib.parse
    import urllib.request
    out: list[dict] = []
    offset = 0
    while True:
        q = {"$limit": _SOCRATA_PAGE, "$offset": offset, "$order": ":id"}
        if select:
            q["$select"] = select
        req = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(q)}",
            headers={"User-Agent": "truck-intel/1.0 (public-data verification)"})
        with urllib.request.urlopen(req, timeout=180) as r:
            page = json.loads(r.read())
        out.extend(page)
        if len(page) < _SOCRATA_PAGE:
            return out
        offset += _SOCRATA_PAGE


def _lic_point(val) -> tuple[float | None, float | None]:
    """Socrata point column -> (lat, lon). Absent/!Point -> (None, None)."""
    if isinstance(val, dict) and val.get("type") == "Point":
        c = val.get("coordinates") or []
        if len(c) == 2:
            return float(c[1]), float(c[0])
    return None, None


def fetch_licences() -> int:
    """Mirror every configured state registry into core.mechanic_licences."""
    ensure_schema()
    rows: list[tuple] = []
    for src in LICENCE_SOURCES:
        print(f"[licence] fetching {src['state']} …", flush=True)
        raw = _socrata_all(src["url"])
        kept = 0
        seen: dict[str, int] = {}
        for rec in raw:
            f = src["f"]
            if src["keep_types"] is not None:
                if (rec.get(src["type_field"]) or "").strip().upper() \
                        not in src["keep_types"]:
                    continue
            lic_id = (rec.get(f["id"]) or "").strip() or None
            nm = (rec.get(f["name"]) or "").strip() or None
            # A facility can hold several licences -> repeated ids. Suffix the
            # key rather than dropping rows: the duplicates carry real distinct
            # licence records and the PK must not silently lose them.
            base = f"{src['state']}/{lic_id or 'NA'}"
            seen[base] = seen.get(base, 0) + 1
            key = f"{base}/{seen[base]}"
            exp_raw = rec.get(f["expiry"]) if f["expiry"] else None
            expiry = None
            if exp_raw:
                try:
                    expiry = datetime.fromisoformat(
                        str(exp_raw).replace("Z", "+00:00")).date()
                except ValueError:
                    try:                       # NY also emits MM/DD/YYYY
                        expiry = datetime.strptime(str(exp_raw)[:10],
                                                   "%m/%d/%Y").date()
                    except ValueError:
                        expiry = None
            lat, lon = _lic_point(rec.get(f["geo"])) if f["geo"] else (None, None)
            rows.append((
                key, src["state"], lic_id, nm, name_norm(nm),
                (rec.get(f["street"]) or "").strip() or None,
                addr_norm(rec.get(f["street"])),
                (rec.get(f["city"]) or "").strip().upper() or None,
                (rec.get(f["zip"]) or "").strip()[:5] or None,
                (rec.get(f["type"]) or "").strip() or None,
                expiry, lat, lon, src["url"],
            ))
            kept += 1
        print(f"[licence]   {src['state']}: {len(raw)} rows -> {kept} repair "
              f"licences kept", flush=True)

    with get_conn() as pg:
        pg.execute("TRUNCATE core.mechanic_licences")
        with pg.cursor() as cur:
            cur.executemany("""
                INSERT INTO core.mechanic_licences
                  (licence_key, state, licence_id, name, name_norm, address,
                   addr_norm, city, zip, licence_type, expiry, lat, lon, geom,
                   source_url, observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        -- NJ publishes no coordinates, so geom must be
                        -- nullable here; the casts let the server infer the
                        -- parameter types when every value in a batch is NULL.
                        CASE WHEN %s::float8 IS NULL THEN NULL
                             ELSE ST_SetSRID(
                                    ST_MakePoint(%s::float8, %s::float8), 4326)
                        END,
                        %s, now())
                ON CONFLICT (licence_key) DO NOTHING
            """, [r[:13] + (r[12], r[12], r[11], r[13]) for r in rows])
    print(f"[licence] mirrored {len(rows)} licences", flush=True)
    return len(rows)


def licence_join() -> int:
    """Match shops to licences and stamp licence_* on core.mechanic_shops.

    Three rules, strongest first, first hit wins. Each records WHICH rule
    fired in `licence_rule`, because a name+city match is weaker evidence than
    a name+zip match and the HTML must be able to say so.
    """
    ensure_schema()
    covered = sorted({s["state"] for s in LICENCE_SOURCES})
    print(f"[licence] matching shops in {', '.join(covered)} …", flush=True)
    with get_conn() as pg:
        pg.execute("""
            UPDATE core.mechanic_shops SET
              licence_verified=NULL, licence_id=NULL, licence_state=NULL,
              licence_expiry=NULL, licence_expired=NULL, licence_rule=NULL
            WHERE state = ANY(%s)
        """, (covered,))
        # A shop in an uncovered state is not "unlicensed" — it is unknown, so
        # licence_verified stays NULL there and only covered states get FALSE.
        pg.execute("""
            UPDATE core.mechanic_shops SET licence_verified=FALSE
            WHERE state = ANY(%s)
        """, (covered,))
        pg.execute("""
            CREATE TEMP TABLE shop_key ON COMMIT DROP AS
            SELECT shop_id, state, left(zip,5) AS zip5, upper(city) AS city_u,
                   geom, name, address FROM core.mechanic_shops
            WHERE state = ANY(%s)
        """, (covered,))
        with pg.cursor() as cur:
            cur.execute("SELECT shop_id, name, address FROM shop_key")
            # Both sides go through the SAME normalisers — that is the whole
            # point of doing this in Python rather than as two SQL expressions
            # that could drift apart.
            keys = [(name_norm(n), addr_norm(a), sid)
                    for sid, n, a in cur.fetchall()]
            cur.execute("ALTER TABLE shop_key ADD COLUMN name_norm TEXT, "
                        "ADD COLUMN addr_norm TEXT")
            cur.executemany("UPDATE shop_key SET name_norm=%s, addr_norm=%s "
                            "WHERE shop_id=%s", keys)
        pg.execute("CREATE INDEX ON shop_key (state, name_norm)")
        pg.execute("CREATE INDEX ON shop_key (state, addr_norm)")

        rules = {
            # name + zip: two independent identifiers agreeing.
            "name_zip": """
                JOIN core.mechanic_licences l
                  ON l.state = s.state AND l.name_norm = s.name_norm
                 AND l.zip IS NOT NULL AND l.zip = s.zip5
            """,
            # name + city: same business name in the same town.
            "name_city": """
                JOIN core.mechanic_licences l
                  ON l.state = s.state AND l.name_norm = s.name_norm
                 AND l.city IS NOT NULL AND l.city = s.city_u
            """,
            # geographic: a licensed facility standing within 150 m whose name
            # shares its leading 6 characters. Only NY publishes coordinates.
            "geo_name": """
                JOIN core.mechanic_licences l
                  ON l.state = s.state AND l.geom IS NOT NULL
                 AND ST_DWithin(l.geom::geography, s.geom::geography, 150)
                 AND left(l.name_norm, 6) = left(s.name_norm, 6)
            """,
            # street address + zip: catches the very common case where the
            # registry holds the LEGAL name and Overture the TRADING name.
            # Weaker than the name rules — two businesses can share a building
            # — so it runs last and is labelled as itself.
            "addr_zip": """
                JOIN core.mechanic_licences l
                  ON l.state = s.state AND l.addr_norm IS NOT NULL
                 AND l.addr_norm = s.addr_norm
                 AND l.zip IS NOT NULL AND l.zip = s.zip5
            """,
        }
        total = 0
        for rule, join in rules.items():
            key_col = "addr_norm" if rule == "addr_zip" else "name_norm"
            n = pg.execute(f"""
                UPDATE core.mechanic_shops m SET
                  licence_verified = TRUE,
                  licence_id       = x.licence_id,
                  licence_state    = x.state,
                  licence_expiry   = x.expiry,
                  licence_expired  = (x.expiry IS NOT NULL AND x.expiry < current_date),
                  licence_rule     = %s
                FROM (
                  SELECT DISTINCT ON (s.shop_id)
                         s.shop_id, l.licence_id, l.state, l.expiry
                  FROM shop_key s {join}
                  WHERE s.{key_col} IS NOT NULL
                  ORDER BY s.shop_id, l.expiry DESC NULLS LAST
                ) x
                WHERE x.shop_id = m.shop_id AND m.licence_rule IS NULL
            """, (rule,)).rowcount
            print(f"[licence]   {rule}: {n:,} matched", flush=True)
            total += n
    print(f"[licence] {total:,} shops licence-verified "
          f"(of those in {', '.join(covered)})", flush=True)
    return total


# ------------------------------------------------------------- chain hours
# Opening hours are the field a driver stranded at 2 a.m. actually needs, and
# Overture does not carry them. AllThePlaces (CC0) publishes per-brand scrapes
# from the chains' own store locators.
#
# MEASURED 2026-07-27, and it corrects the deep dive's assumption that ATP is
# "the" hours source: of the truck-relevant US spiders, only pilot_flying_j
# (724/724) and fleetpride_us (374/378) actually carry opening_hours. Love's,
# TA/Petro and Penske publish locations WITHOUT hours. The other three are
# still ingested for the `chain_brand` badge — knowing a shop is a national
# chain is itself the reliability signal (DEEP_DIVE §6).
ATP_RUN_URL = "https://data.alltheplaces.xyz/runs/latest.json"
ATP_SPIDERS = ("loves_us", "travelcenters_of_america_us", "pilot_flying_j",
               "fleetpride_us", "penske")
ATP_MATCH_M = 500          # chain point -> our shop, metres
_24H_RE = re.compile(r"^(24/7|Mo-Su 00:00-24:00|24 hours)$", re.I)


def _atp_fetch(spider: str) -> list[dict]:
    import urllib.request
    url = f"https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson"
    req = urllib.request.Request(
        url, headers={"User-Agent": "truck-intel/1.0 (public-data enrichment)"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read()).get("features", [])


def chains_hours() -> int:
    """Stamp opening_hours / open_24h / chain_brand from AllThePlaces (CC0)."""
    ensure_schema()
    pts: list[tuple] = []
    for spider in ATP_SPIDERS:
        try:
            feats = _atp_fetch(spider)
        except Exception as exc:                      # a dead spider is not fatal
            print(f"[chains] {spider}: FETCH FAILED ({exc}) — skipped", flush=True)
            continue
        n_oh = 0
        for ft in feats:
            p = ft.get("properties") or {}
            if (p.get("addr:country") or "US") != "US":
                continue
            g = ft.get("geometry") or {}
            if g.get("type") != "Point":
                continue
            lon, lat = g["coordinates"][:2]
            oh = (p.get("opening_hours") or "").strip() or None
            n_oh += bool(oh)
            pts.append((
                f"{spider}/{p.get('ref') or len(pts)}", spider,
                p.get("brand") or p.get("name"), name_norm(p.get("name")),
                oh, bool(oh and _24H_RE.match(oh)), lat, lon,
            ))
        print(f"[chains] {spider}: {len(feats)} features, "
              f"{n_oh} with opening_hours", flush=True)
    if not pts:
        print("[chains] no chain points fetched — nothing to stamp", flush=True)
        return 0

    with get_conn() as pg:
        pg.execute("""
            CREATE TEMP TABLE atp (
              atp_id TEXT, spider TEXT, brand TEXT, name_norm TEXT,
              opening_hours TEXT, open_24h BOOLEAN,
              lat DOUBLE PRECISION, lon DOUBLE PRECISION,
              geom geometry(Point,4326)
            ) ON COMMIT DROP
        """)
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO atp (atp_id,spider,brand,name_norm,opening_hours,"
                "open_24h,lat,lon,geom) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                "ST_SetSRID(ST_MakePoint(%s,%s),4326))",
                [p + (p[7], p[6]) for p in pts])
        pg.execute("CREATE INDEX ON atp USING GIST (geom)")
        # Clear only what THIS stage owns. A blanket reset would delete the
        # OSM-sourced hours that --osm-match fills for independents, so a
        # standalone --chains re-run would silently strip them.
        pg.execute("""
            UPDATE core.mechanic_shops SET
              opening_hours = CASE WHEN hours_source = 'alltheplaces'
                                   THEN NULL ELSE opening_hours END,
              open_24h      = CASE WHEN hours_source = 'alltheplaces'
                                   THEN NULL ELSE open_24h END,
              hours_source  = CASE WHEN hours_source = 'alltheplaces'
                                   THEN NULL ELSE hours_source END,
              chain_brand   = NULL
        """)
        # Nearest chain point within 500 m. No name test: at that radius on a
        # truck-stop parcel the only candidate IS the chain, and our Overture
        # row is often named 'Speedco' where ATP says "Love's Travel Stop".
        n = pg.execute("""
            UPDATE core.mechanic_shops m SET
              opening_hours = x.opening_hours,
              open_24h      = x.open_24h,
              hours_source  = CASE WHEN x.opening_hours IS NOT NULL
                                   THEN 'alltheplaces' END,
              chain_brand   = x.brand
            FROM (
              SELECT DISTINCT ON (s.shop_id) s.shop_id, a.brand,
                     a.opening_hours, a.open_24h
              FROM core.mechanic_shops s
              JOIN atp a ON ST_DWithin(a.geom::geography, s.geom::geography, %s)
              WHERE s.geom IS NOT NULL
              ORDER BY s.shop_id, a.opening_hours IS NULL,
                       a.geom::geography <-> s.geom::geography
            ) x
            WHERE x.shop_id = m.shop_id
        """, (ATP_MATCH_M,)).rowcount
        got_hours = pg.execute(
            "SELECT count(*) FROM core.mechanic_shops "
            "WHERE opening_hours IS NOT NULL").fetchone()[0]
    print(f"[chains] {n:,} shops badged as chain sites; "
          f"{got_hours:,} gained opening_hours", flush=True)
    return n


# --------------------------------------------------------- OSM corroboration
# ODbL data never enters core.* (brief D3). What crosses the boundary here is
# a MATCH FLAG — the fact that an independently-mapped truck-repair POI stands
# at the same place — not any OSM field. That flag is what makes the
# independence count in verify() mean something outside NY/NJ.
OSM_MATCH_M = 250


def osm_match() -> int:
    """Flag shops that an independent OSM truck-repair POI corroborates."""
    ensure_schema()
    with get_conn() as pg:
        exists = pg.execute(
            "SELECT to_regclass('osm.truck_repair') IS NOT NULL").fetchone()[0]
        if not exists:
            print("[osm] osm.truck_repair absent — run "
                  "scripts/osm_extract.py --job pois first", flush=True)
            return 0
        # Same rule as --chains: clear only this stage's own output, so a
        # re-run re-derives OSM hours instead of leaving a stale value behind
        # a match that may no longer exist.
        pg.execute("""
            UPDATE core.mechanic_shops SET
              osm_match_id=NULL, osm_match_m=NULL,
              opening_hours = CASE WHEN hours_source='osm' THEN NULL ELSE opening_hours END,
              open_24h      = CASE WHEN hours_source='osm' THEN NULL ELSE open_24h END,
              hours_source  = CASE WHEN hours_source='osm' THEN NULL ELSE hours_source END
        """)
        n = pg.execute("""
            UPDATE core.mechanic_shops m SET
              osm_match_id = x.osm_id,
              osm_match_m  = round(x.d)::int
            FROM (
              SELECT DISTINCT ON (s.shop_id) s.shop_id, o.osm_id,
                     ST_Distance(o.geom::geography, s.geom::geography) AS d
              FROM core.mechanic_shops s
              JOIN osm.truck_repair o
                ON ST_DWithin(o.geom::geography, s.geom::geography, %s)
              WHERE s.geom IS NOT NULL
              ORDER BY s.shop_id, ST_Distance(o.geom::geography, s.geom::geography)
            ) x
            WHERE x.shop_id = m.shop_id
        """, (OSM_MATCH_M,)).rowcount
        # OSM also carries opening_hours for independents, which no permissive
        # source does. Fill only where the chain feed left a gap.
        h = pg.execute("""
            UPDATE core.mechanic_shops m SET
              opening_hours = o.props->>'opening_hours',
              open_24h      = (o.props->>'opening_hours') IN ('24/7','Mo-Su 00:00-24:00'),
              hours_source  = 'osm'
            FROM osm.truck_repair o
            WHERE o.osm_id = m.osm_match_id
              AND m.opening_hours IS NULL
              AND o.props->>'opening_hours' IS NOT NULL
        """).rowcount
    print(f"[osm] {n:,} shops corroborated by an OSM truck-repair POI; "
          f"{h:,} gained opening_hours from OSM", flush=True)
    return n


# --------------------------------------------------------- coverage denominator
# "3 mechanics near this route" means something different in Montana than in
# Ohio, and a raw count cannot tell you whether South Dakota is genuinely empty
# or merely unlisted. Census County Business Patterns is the free, public-domain
# denominator that separates the two (DEEP_DIVE §5).
#
# The Census DATA API now refuses keyless requests (HTTP 302 -> "Missing Key",
# measured 2026-07-27), so this reads the BULK state file instead — same
# agency, same numbers, no key, no account.
CBP_YEAR = 2022
CBP_URL = (f"https://www2.census.gov/programs-surveys/cbp/datasets/"
           f"{CBP_YEAR}/cbp{str(CBP_YEAR)[2:]}st.zip")
CBP_DIR = Path("data/raw/cbp")
# 811111 General Automotive Repair — the NAICS class that carries "truck
# repair, general". 811310 (commercial/industrial machinery repair) is tracked
# alongside it as context, not as the denominator: it is mostly non-vehicle.
CBP_NAICS = ("811111", "811310")
_FIPS_STATE = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}


def _cbp_estabs() -> dict[str, dict[str, int]]:
    """{state: {naics: establishments}} from the CBP bulk state file.

    lfo (legal form of organisation) must be '-' — the file repeats every
    (state, naics) once per corporate form, and summing them triple-counts.
    """
    import csv
    import urllib.request
    import zipfile
    CBP_DIR.mkdir(parents=True, exist_ok=True)
    zpath = CBP_DIR / Path(CBP_URL).name
    tpath = CBP_DIR / f"cbp{str(CBP_YEAR)[2:]}st.txt"
    if not tpath.exists():
        if not zpath.exists():
            print(f"[cbp] downloading {CBP_URL} …", flush=True)
            req = urllib.request.Request(
                CBP_URL, headers={"User-Agent": "truck-intel/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(zpath, "wb") as f:
                f.write(r.read())
        with zipfile.ZipFile(zpath) as z:
            z.extractall(CBP_DIR)
    out: dict[str, dict[str, int]] = {}
    with open(tpath, newline="") as f:
        for row in csv.DictReader(f):
            if row["lfo"].strip() != "-":
                continue
            naics = row["naics"].strip()
            if naics not in CBP_NAICS:
                continue
            st = _FIPS_STATE.get(row["fipstate"])
            if st:
                out.setdefault(st, {})[naics] = int(row["est"])
    return out


def coverage() -> int:
    """Per-state capture rate + a verdict on thin-data vs real scarcity."""
    ensure_schema()
    cbp = _cbp_estabs()
    print(f"[cbp] {CBP_YEAR} CBP: {len(cbp)} states, "
          f"{sum(v.get('811111', 0) for v in cbp.values()):,} establishments "
          f"in NAICS 811111", flush=True)
    with get_conn() as pg:
        shops = dict(pg.execute(
            "SELECT state, count(*) FROM core.mechanic_shops "
            "WHERE state IS NOT NULL GROUP BY 1").fetchall())
        on_route = dict(pg.execute(
            "SELECT state, count(*) FROM core.mechanic_shops "
            "WHERE state IS NOT NULL AND on_route_5km GROUP BY 1").fetchall())
        miles = dict(pg.execute(
            "SELECT state, sum(ST_Length(geom::geography))/1609.344 "
            "FROM core.truck_routes WHERE state IS NOT NULL GROUP BY 1"
        ).fetchall())

        rows = []
        for st in sorted(set(cbp) | set(shops)):
            n = shops.get(st, 0)
            est = cbp.get(st, {}).get("811111")
            mi = miles.get(st)
            capture = (n / est) if est else None
            mps = (mi / n) if (mi and n) else None
            # National capture rate is the yardstick; a state at less than half
            # of it is under-listed rather than genuinely empty. Computed after
            # the loop, so the verdict pass runs second.
            rows.append([st, est, cbp.get(st, {}).get("811310"), n,
                         on_route.get(st, 0), mi, mps, capture, None])

        nat_shops = sum(r[3] for r in rows)
        nat_est = sum(r[1] or 0 for r in rows)
        nat_capture = nat_shops / nat_est if nat_est else None
        for r in rows:
            cap = r[7]
            if cap is None or nat_capture is None:
                r[8] = "unknown"
            elif cap < 0.5 * nat_capture:
                r[8] = "thin_data"
            elif r[6] is not None and r[6] >= 40:
                r[8] = "real_scarcity"
            else:
                r[8] = "ok"

        pg.execute("TRUNCATE core.mechanic_coverage")
        with pg.cursor() as cur:
            cur.executemany("""
                INSERT INTO core.mechanic_coverage
                  (state, cbp_year, cbp_estab_811111, cbp_estab_811310, shops,
                   shops_on_route, route_miles, miles_per_shop, capture_rate,
                   verdict)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, [(r[0], CBP_YEAR, r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                   r[8]) for r in rows])

    print(f"[cbp] national capture rate: {nat_shops:,} truck shops / "
          f"{nat_est:,} CBP repair establishments = "
          f"{(nat_capture or 0)*100:.1f}%", flush=True)
    thin = [r[0] for r in rows if r[8] == "thin_data"]
    scarce = [r[0] for r in rows if r[8] == "real_scarcity"]
    print(f"[cbp] thin_data: {', '.join(thin) or '—'}", flush=True)
    print(f"[cbp] real_scarcity: {', '.join(scarce) or '—'}", flush=True)
    return len(rows)


# ------------------------------------------------------------- fill tracking
# Boss's ask, 2026-07-27: "mechanic detail — if you find unknown detail".
# A refresh that fills a phone number and a refresh that fills nothing look the
# same from outside, so the pipeline has to SAY which it was. Every refresh
# snapshots how many shops have each field and diffs against the previous
# snapshot; the deltas are what a daily job is actually for.
FILL_METRICS = {
    "phone":          "phone IS NOT NULL",
    "website":        "website IS NOT NULL",
    "email":          "email IS NOT NULL",
    "opening_hours":  "opening_hours IS NOT NULL",
    "chain_brand":    "chain_brand IS NOT NULL",
    "licence_verified": "licence_verified",
    "osm_corroborated": "osm_match_id IS NOT NULL",
    "multi_source":   "n_independent >= 2",
    "verified":       "verification_status = 'verified'",
    "on_route_5km":   "on_route_5km",
}


def fill_report(*, record: bool = True) -> dict[str, tuple[int, int]]:
    """Snapshot per-field fill counts; print the change since last time.

    `record=False` reports without writing a snapshot — for a read-only check
    that must not disturb the baseline the next real run compares against.
    """
    ensure_schema()
    with get_conn() as pg:
        total = pg.execute("SELECT count(*) FROM core.mechanic_shops").fetchone()[0]
        sel = ", ".join(f"count(*) FILTER (WHERE {expr})"
                        for expr in FILL_METRICS.values())
        counts = pg.execute(
            f"SELECT {sel} FROM core.mechanic_shops").fetchone()
        now = dict(zip(FILL_METRICS, counts))

        prev_at = pg.execute(
            "SELECT max(snapshot_at) FROM core.mechanic_fill_history").fetchone()[0]
        prev = {}
        if prev_at:
            prev = dict(pg.execute(
                "SELECT metric, filled FROM core.mechanic_fill_history "
                "WHERE snapshot_at = %s", (prev_at,)).fetchall())

        if record:
            with pg.cursor() as cur:
                cur.executemany(
                    "INSERT INTO core.mechanic_fill_history "
                    "(metric, filled, total) VALUES (%s,%s,%s)",
                    [(m, n, total) for m, n in now.items()])

    print(f"[fill] {total:,} shops — field coverage"
          + (f" (vs {prev_at:%Y-%m-%d %H:%M} UTC)" if prev_at else " (first snapshot)"),
          flush=True)
    gained = 0
    for metric, n in now.items():
        was = prev.get(metric)
        delta = "" if was is None else f"  {n - was:+d}"
        if was is not None and n > was:
            gained += n - was
        print(f"[fill]   {metric:18} {n:6,} / {total:,}"
              f"  {100 * n / total:5.1f}%{delta}", flush=True)
    if prev_at:
        print(f"[fill] newly filled this run: {gained}"
              + ("" if gained else "  (nothing new upstream — not a failure)"),
              flush=True)
    return {m: (n, total) for m, n in now.items()}


# ---------------------------------------------------------------------- CSV
# Columns in the order a human reads them: who and where first, how to reach
# them second, then the evidence. `_ARRAY_COLS` are flattened to '; '-joined
# text — a CSV cell cannot hold a list, and Postgres's own '{a,b}' braces would
# make a spreadsheet show literal punctuation.
CSV_COLUMNS = [
    "shop_id", "name", "category", "brand", "chain_brand",
    "address", "city", "state", "zip", "lat", "lon",
    "phone", "website", "email", "socials",
    "opening_hours", "open_24h", "hours_source",
    "route_ref", "route_name", "route_dist_m", "on_route_5km",
    "verification_status", "confidence", "n_independent", "source_orgs",
    "licence_verified", "licence_id", "licence_expiry", "licence_expired",
    "licence_rule", "osm_match_id", "osm_match_m",
    "flags", "gmaps_url", "observed_at",
]
_ARRAY_COLS = {"socials", "source_orgs", "flags"}


def render_csv() -> Path:
    """Every shop, every enrichment field, one row each.

    UTF-8 **with BOM**: shop names carry accents and Excel on Windows reads a
    plain UTF-8 CSV as mojibake. The BOM is invisible to Excel, LibreOffice,
    pandas and DuckDB alike, so it costs nothing and fixes the one reader most
    likely to open this.

    An empty cell means UNKNOWN, never "no" — the same rule the rest of this
    layer follows. Booleans are written true/false only where the source
    actually said so; where it did not, the cell is blank rather than false.
    """
    ensure_schema()
    with get_conn() as pg:
        rows = pg.execute(f"""
            SELECT {', '.join(CSV_COLUMNS)}
            FROM core.mechanic_shops
            ORDER BY state NULLS LAST, city, name
        """).fetchall()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "truck_mechanics.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        w.writerow(CSV_COLUMNS)
        for row in rows:
            cells = []
            for col, val in zip(CSV_COLUMNS, row):
                if val is None:
                    cells.append("")                 # blank = unknown
                elif col in _ARRAY_COLS:
                    cells.append("; ".join(str(v) for v in val))
                elif isinstance(val, bool):
                    cells.append("true" if val else "false")
                elif col == "observed_at":
                    cells.append(val.date().isoformat())
                else:
                    cells.append(str(val))
            w.writerow(cells)

    filled = {c: 0 for c in CSV_COLUMNS}
    for row in rows:
        for col, val in zip(CSV_COLUMNS, row):
            if val not in (None, "", []):
                filled[col] += 1
    thin = [f"{c} {100 * n / max(len(rows), 1):.0f}%"
            for c, n in filled.items() if n < len(rows) * 0.5]
    print(f"[csv] wrote {out} ({len(rows):,} shops × {len(CSV_COLUMNS)} columns, "
          f"{out.stat().st_size / 1048576:.1f} MB)", flush=True)
    if thin:
        # Say which columns are mostly empty, here, at write time — a reader
        # who opens the file and sees blanks should already know which are
        # sparsely published rather than assume the export dropped them.
        print(f"[csv] sparsely populated (blank = unknown): {', '.join(thin)}",
              flush=True)
    return out


def render_html() -> Path:
    with get_conn() as pg:
        with pg.cursor() as cur:
            cur.execute("""
                SELECT name, category, brand, address, city, state, zip,
                       phone, website, email, socials, lat, lon, gmaps_url,
                       route_ref, route_name, route_dist_m, on_route_5km,
                       verification_status, confidence, src_confidence,
                       n_sources, source_names, source_orgs, n_independent,
                       licence_verified, licence_id, licence_expiry,
                       licence_expired, licence_rule,
                       opening_hours, open_24h, hours_source, chain_brand,
                       osm_match_id, flags, observed_at
                FROM core.mechanic_shops
                ORDER BY (verification_status='verified') DESC,
                         confidence DESC NULLS LAST, state, name
            """)
            cols = [d[0] for d in cur.description]
            data = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("""
                SELECT state, shops, shops_on_route, cbp_estab_811111,
                       capture_rate, miles_per_shop, verdict, cbp_year
                FROM core.mechanic_coverage ORDER BY state
            """)
            cov = [dict(zip([d[0] for d in cur.description], r))
                   for r in cur.fetchall()]

    counts = {"verified": 0, "probable": 0, "unverified": 0}
    states, cats = {}, {}
    for r in data:
        counts[r["verification_status"]] = counts.get(r["verification_status"], 0) + 1
        states[r["state"]] = states.get(r["state"], 0) + 1
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    n_lic = sum(1 for r in data if r["licence_verified"])
    n_hours = sum(1 for r in data if r["opening_hours"])
    n_osm = sum(1 for r in data if r["osm_match_id"])
    n_multi = sum(1 for r in data if (r["n_independent"] or 0) >= 2)
    vintage = data[0]["observed_at"].date().isoformat() if data and data[0]["observed_at"] else "unknown"
    cbp_year = cov[0]["cbp_year"] if cov else "—"

    def js(v):
        return json.dumps(v, default=str)

    rows_json = js([{
        "name": r["name"] or "—",
        "cat": r["category"] or "—",
        "brand": r["brand"] or "",
        "addr": ", ".join(x for x in (r["address"], r["city"], r["state"], r["zip"]) if x) or "—",
        "state": r["state"] or "—",
        "phone": r["phone"] or "",
        "website": r["website"] or "",
        "email": r["email"] or "",
        "socials": r["socials"] or [],
        "gmaps": r["gmaps_url"] or "",
        "route": (f'{r["route_ref"] or r["route_name"] or "—"}'),
        "dist": r["route_dist_m"],
        "on5": r["on_route_5km"],
        "status": r["verification_status"],
        "conf": r["confidence"],
        "srcconf": round(r["src_confidence"], 2) if r["src_confidence"] is not None else None,
        "nsrc": r["n_sources"],
        "srcs": r["source_names"] or [],
        "orgs": r["source_orgs"] or [],
        "nind": r["n_independent"],
        "lic": r["licence_verified"],
        "licid": r["licence_id"] or "",
        "licexp": r["licence_expiry"].isoformat() if r["licence_expiry"] else "",
        "licdead": r["licence_expired"],
        "licrule": r["licence_rule"] or "",
        "hours": r["opening_hours"] or "",
        "h24": r["open_24h"],
        "hsrc": r["hours_source"] or "",
        "chain": r["chain_brand"] or "",
        "osm": bool(r["osm_match_id"]),
        "flags": r["flags"] or [],
    } for r in data])
    cov_json = js([{
        "state": c["state"], "shops": c["shops"], "on5": c["shops_on_route"],
        "cbp": c["cbp_estab_811111"],
        "cap": round(c["capture_rate"] * 100, 1) if c["capture_rate"] else None,
        "mps": round(c["miles_per_shop"]) if c["miles_per_shop"] else None,
        "verdict": c["verdict"],
    } for c in cov])

    tpl = _HTML_TEMPLATE
    out = OUT_DIR / "truck_mechanics.html"
    out.write_text(
        tpl.replace("__ROWS__", rows_json)
           .replace("__COVERAGE__", cov_json)
           .replace("__TOTAL__", str(len(data)))
           .replace("__VERIFIED__", str(counts.get("verified", 0)))
           .replace("__PROBABLE__", str(counts.get("probable", 0)))
           .replace("__UNVERIFIED__", str(counts.get("unverified", 0)))
           .replace("__NSTATES__", str(len([s for s in states if s])))
           .replace("__LICENSED__", str(n_lic))
           .replace("__HOURS__", str(n_hours))
           .replace("__OSMC__", str(n_osm))
           .replace("__MULTI__", str(n_multi))
           .replace("__CBPYEAR__", str(cbp_year))
           .replace("__VINTAGE__", vintage)
           .replace("__GENERATED__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        encoding="utf-8")
    # companion JSON for reuse
    (OUT_DIR / "truck_mechanics.json").write_text(rows_json, encoding="utf-8")
    print(f"[html] wrote {out} ({len(data)} shops)", flush=True)
    return out


_HTML_TEMPLATE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>US Truck Mechanics — verified & enriched</title>
<style>
:root{--bg:#0b0f14;--card:#141b24;--line:#233042;--fg:#e7edf3;--mut:#8ba0b6;
--ok:#3fb950;--warn:#d29922;--bad:#6e7681;--acc:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{margin:0 0 4px;font-size:20px}.sub{color:var(--mut);font-size:13px}
.stats{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
.stat b{font-size:18px}.stat span{color:var(--mut);font-size:12px;display:block}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
input,select{background:var(--card);border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:8px 10px;font-size:13px}input{min-width:240px}
.wrap{padding:16px 24px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--bg);cursor:pointer;white-space:nowrap}
tr:hover td{background:#0f1620}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.verified{background:rgba(63,185,80,.15);color:var(--ok)}
.probable{background:rgba(210,153,34,.15);color:var(--warn)}
.unverified{background:rgba(110,118,129,.18);color:var(--bad)}
.conf{font-variant-numeric:tabular-nums}
.bar{height:5px;border-radius:3px;background:var(--line);margin-top:3px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--acc)}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.mut{color:var(--mut)}.flag{color:var(--warn);font-size:11px}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:8px;padding:10px 14px;margin-top:12px;color:var(--mut);font-size:12px}
/* flex:0 0 auto — without it the filter labels stretch to share the row's
   free space and read as broken input fields. nowrap keeps two-token badges
   ("licence ✓") on one line. */
.pill{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:20px;
padding:1px 7px;white-space:nowrap;display:inline-block}
.controls .pill{flex:0 0 auto;display:inline-flex;align-items:center;gap:6px;padding:6px 10px}
.controls .pill input{min-width:0;margin:0}
footer{padding:18px 24px;color:var(--mut);font-size:12px;border-top:1px solid var(--line)}
</style></head><body>
<header>
<h1>US Truck Mechanics — verified &amp; enriched</h1>
<div class="sub">Truck-specific repair / trailer / roadside shops · source: Overture Maps Places (CDLA-Permissive-2.0), vintage __VINTAGE__ · nearest route from NTAD National Network · generated __GENERATED__</div>
<div class="stats">
<div class="stat"><b>__TOTAL__</b><span>shops</span></div>
<div class="stat"><b style="color:var(--ok)">__VERIFIED__</b><span>verified</span></div>
<div class="stat"><b style="color:var(--warn)">__PROBABLE__</b><span>probable</span></div>
<div class="stat"><b style="color:var(--bad)">__UNVERIFIED__</b><span>unverified</span></div>
<div class="stat"><b>__MULTI__</b><span>≥2 independent orgs</span></div>
<div class="stat"><b>__LICENSED__</b><span>state-licence matched</span></div>
<div class="stat"><b>__OSMC__</b><span>OSM-corroborated</span></div>
<div class="stat"><b>__HOURS__</b><span>with opening hours</span></div>
<div class="stat"><b>__NSTATES__</b><span>states</span></div>
</div>
<div class="controls">
<input id="q" placeholder="Search name / city / route / phone…">
<select id="fstatus"><option value="">All statuses</option><option value="verified">Verified only</option><option value="probable">Probable+</option></select>
<select id="fstate"></select>
<select id="fcat"></select>
<label class="pill"><input type="checkbox" id="f5km"> within 5 km of a route</label>
<label class="pill"><input type="checkbox" id="fhours"> has opening hours</label>
<label class="pill"><input type="checkbox" id="flic"> state-licensed</label>
<button id="cov" class="pill" style="cursor:pointer;background:var(--card);color:var(--fg)">state coverage ▾</button>
</div>
<div class="note"><b>What “verified” means here.</b> Free structural checks only: the phone is NANP-valid <i>and</i> its area code matches the shop's state, the coordinate is sane and not a shared-point cluster, and — the part that changed on 2026-07-27 — the record is attested by <b>≥2 independent organisations</b>. Earlier revisions counted Overture's own feeds (<code>Overture</code> and <code>Overture-signals</code>) as two corroborations; they are one organisation's two pipelines, so every row scored full marks and the signal separated nothing. Independent votes now come from <b>Meta</b>, <b>Microsoft</b>, the <b>NY/NJ state licence registries</b>, and <b>OpenStreetMap</b>. It is still <b>not</b> a physical confirmation that the shop is open today.</div>
<div class="note"><b>Deliberately blank.</b> Ratings, review counts, review text, photos, WhatsApp numbers and live “open now” status are <b>not shown</b> — no free, legal US source exists for any of them (RESEARCH_BRIEF.md §4.3). Opening hours are shown only where a permissive source publishes them, so most independents read “—”, which means <i>unknown</i>, never <i>closed</i>. A licence non-match in NY/NJ likewise means <i>not found in that registry</i>, not <i>unlicensed</i>; outside NY/NJ no registry was consulted at all.</div>
<div id="covpanel" class="note" style="display:none;border-left-color:var(--acc)">
<b>State coverage vs Census County Business Patterns __CBPYEAR__</b> — a shop count alone cannot say whether a state is empty or merely unlisted. <code>capture</code> is our shops ÷ CBP establishments in NAICS 811111 (general automotive repair, the class that carries general truck repair). A state well below the national rate is <b>under-listed</b>; a state at a normal rate with very high route-miles-per-shop is <b>genuinely thin on the ground</b>, and there the product should show distance-to-next-shop rather than a count.
<div id="covtable" style="margin-top:10px"></div></div>
</header>
<div class="wrap">
<table id="t"><thead><tr>
<th data-k="name">Shop</th><th data-k="cat">Type</th><th data-k="addr">Address</th>
<th data-k="phone">Phone</th><th data-k="hours">Hours</th><th data-k="links">Links</th>
<th data-k="route">Nearest route</th><th data-k="dist">Dist</th>
<th data-k="nind">Sources</th>
<th data-k="status">Status</th><th data-k="conf">Confidence</th>
</tr></thead><tbody id="tb"></tbody></table>
<div id="empty" class="mut" style="padding:20px;display:none">No shops match.</div>
</div>
<footer>Shops © Overture Maps Foundation (CDLA-Permissive-2.0). Route overlay: BTS/FHWA NTAD National Network (US public domain). Chain sites &amp; opening hours: All The Places (CC0). Licence checks: NY DMV and NJ MVC open data. Coverage denominator: US Census County Business Patterns __CBPYEAR__ (public domain). Independent corroboration flag derived from OpenStreetMap (© OpenStreetMap contributors, ODbL) — OSM data is held in a separate schema and only the match flag is carried here. This is an advisory list from public data — not for enforcement or navigation. NULL renders as “unknown”, never as “no”.</footer>
<script>
const ROWS=__ROWS__;
const COV=__COVERAGE__;
const tb=document.getElementById('tb'),empty=document.getElementById('empty');
const q=document.getElementById('q'),fs=document.getElementById('fstatus'),
 fst=document.getElementById('fstate'),fc=document.getElementById('fcat'),f5=document.getElementById('f5km'),
 fh=document.getElementById('fhours'),fl=document.getElementById('flic');
const states=[...new Set(ROWS.map(r=>r.state).filter(x=>x&&x!=='—'))].sort();
fst.innerHTML='<option value="">All states</option>'+states.map(s=>`<option>${s}</option>`).join('');
const cats=[...new Set(ROWS.map(r=>r.cat).filter(Boolean))].sort();
fc.innerHTML='<option value="">All types</option>'+cats.map(c=>`<option>${c}</option>`).join('');
let sortK='status',sortDir=1;
function esc(s){return (s==null?'':''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function links(r){let o=[];if(r.website)o.push(`<a href="${esc(r.website)}" target="_blank" rel="noopener">web</a>`);
 if(r.gmaps)o.push(`<a href="${esc(r.gmaps)}" target="_blank" rel="noopener">map</a>`);
 if(r.email)o.push(`<a href="mailto:${esc(r.email)}">email</a>`);
 (r.socials||[]).forEach(s=>o.push(`<a href="${esc(s)}" target="_blank" rel="noopener">social</a>`));
 return o.join(' · ')||'<span class=mut>—</span>'}
function render(){
 const t=q.value.toLowerCase(),vs=fs.value,vst=fst.value,vc=fc.value,only5=f5.checked,
  onlyH=fh.checked,onlyL=fl.checked;
 let rows=ROWS.filter(r=>{
  if(vst&&r.state!==vst)return 0;
  if(vc&&r.cat!==vc)return 0;
  if(vs==='verified'&&r.status!=='verified')return 0;
  if(vs==='probable'&&r.status==='unverified')return 0;
  if(only5&&!r.on5)return 0;
  if(onlyH&&!r.hours)return 0;
  if(onlyL&&!r.lic)return 0;
  if(t){const h=(r.name+' '+r.addr+' '+r.route+' '+r.phone+' '+r.chain).toLowerCase();if(!h.includes(t))return 0}
  return 1});
 rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];
  if(sortK==='status'){const o={verified:0,probable:1,unverified:2};x=o[x];y=o[y]}
  if(x==null)x=sortDir>0?1e9:-1e9;if(y==null)y=sortDir>0?1e9:-1e9;
  return (x>y?1:x<y?-1:0)*sortDir});
 empty.style.display=rows.length?'none':'block';
 tb.innerHTML=rows.slice(0,3000).map(r=>{
  const flags=(r.flags||[]).length?`<div class="flag">⚑ ${r.flags.map(esc).join(', ')}</div>`:'';
  const conf=r.conf==null?'—':`<span class="conf">${r.conf}</span><div class="bar"><i style="width:${r.conf}%"></i></div>`;
  const srcs=(r.orgs||[]).length?`<div class="mut" style="font-size:11px">${esc((r.orgs||[]).join(' · '))}</div>`:'';
  // Hours are unknown far more often than they are known, so the cell says so
  // in words rather than leaving a blank that reads as "closed".
  const hrs=r.hours
    ?`${r.h24?'<span class=pill style="color:var(--ok)">24/7</span> ':''}${esc(r.hours)}`
     +`<div class="mut" style="font-size:11px">${esc(r.hsrc)}</div>`
    :'<span class=mut>unknown</span>';
  const lic=r.lic
    ?`<span class="pill" style="color:${r.licdead?'var(--warn)':'var(--ok)'}" title="${esc(r.licrule)} match${r.licexp?', expires '+esc(r.licexp):''}">licence ${r.licdead?'expired':'✓'}</span>`
    :'';
  const osm=r.osm?'<span class="pill" title="an independently-mapped OSM truck-repair POI stands at this location">OSM ✓</span>':'';
  return `<tr>
   <td><b>${esc(r.name)}</b>${r.chain?` <span class=pill style="color:var(--acc)">${esc(r.chain)}</span>`:(r.brand?` <span class=mut>· ${esc(r.brand)}</span>`:'')}${flags}</td>
   <td>${esc(r.cat)}</td>
   <td>${esc(r.addr)}</td>
   <td>${r.phone?esc(r.phone):'<span class=mut>—</span>'}</td>
   <td>${hrs}</td>
   <td>${links(r)}</td>
   <td>${esc(r.route)}${r.on5?' <span class=pill style="color:var(--ok)">≤5km</span>':''}</td>
   <td>${r.dist==null?'<span class=mut>—</span>':(r.dist>=1000?(r.dist/1000).toFixed(1)+' km':r.dist+' m')}</td>
   <td>${r.nind==null?'<span class=mut>—</span>':`<b>${r.nind}</b> org${r.nind===1?'':'s'}`} ${lic} ${osm}${srcs}</td>
   <td><span class="badge ${r.status}">${r.status}</span></td>
   <td>${conf}</td></tr>`}).join('');
 if(rows.length>3000)tb.innerHTML+=`<tr><td colspan=11 class=mut>Showing first 3000 of ${rows.length} — narrow the filter to see more.</td></tr>`;
}
function renderCov(){
 const nat=COV.reduce((a,c)=>a+(c.shops||0),0),natE=COV.reduce((a,c)=>a+(c.cbp||0),0);
 const natCap=natE?100*nat/natE:0;
 const cls={thin_data:'var(--warn)',real_scarcity:'var(--acc)',ok:'var(--mut)',unknown:'var(--bad)'};
 document.getElementById('covtable').innerHTML=
  `<div style="margin-bottom:6px">National capture rate: <b>${natCap.toFixed(1)}%</b> (${nat.toLocaleString()} shops / ${natE.toLocaleString()} CBP establishments)</div>`+
  '<table style="font-size:12px"><thead><tr><th>State</th><th>Shops</th><th>≤5km of route</th><th>CBP 811111</th><th>Capture</th><th>Route miles / shop</th><th>Reading</th></tr></thead><tbody>'+
  COV.slice().sort((a,b)=>(a.cap??999)-(b.cap??999)).map(c=>`<tr>
   <td>${esc(c.state)}</td><td>${c.shops}</td><td>${c.on5}</td>
   <td>${c.cbp==null?'—':c.cbp.toLocaleString()}</td>
   <td>${c.cap==null?'—':c.cap+'%'}</td>
   <td>${c.mps==null?'—':c.mps}</td>
   <td style="color:${cls[c.verdict]||'var(--mut)'}">${esc(c.verdict.replace('_',' '))}</td></tr>`).join('')+
  '</tbody></table>';
}
document.getElementById('cov').addEventListener('click',()=>{
 const p=document.getElementById('covpanel');
 const show=p.style.display==='none';p.style.display=show?'block':'none';
 if(show&&!p.dataset.done){renderCov();p.dataset.done='1'}
});
[q,fs,fst,fc].forEach(e=>e.addEventListener('input',render));
[f5,fh,fl].forEach(e=>e.addEventListener('change',render));
document.querySelectorAll('#t th[data-k]').forEach(th=>th.addEventListener('click',()=>{
 const k=th.dataset.k;if(k==='links')return;if(sortK===k)sortDir*=-1;else{sortK=k;sortDir=1}render()}));
render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--enrich", action="store_true",
                    help="re-assign nearest truck route without re-pulling "
                         "(a --pull TRUNCATEs, so enrichment needs its own door)")
    ap.add_argument("--licence", action="store_true",
                    help="mirror the NY/NJ licence registries and join them "
                         "(the only government-independent verification vote)")
    ap.add_argument("--chains", action="store_true",
                    help="stamp chain brand + opening hours from AllThePlaces")
    ap.add_argument("--osm-match", action="store_true",
                    help="flag shops an OSM truck-repair POI corroborates "
                         "(needs osm.truck_repair — see osm_extract.py)")
    ap.add_argument("--cbp", action="store_true",
                    help="per-state coverage vs Census CBP denominator")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--fill-report", action="store_true",
                    help="report per-field coverage and what changed since "
                         "the last snapshot")
    ap.add_argument("--refresh", action="store_true",
                    help="DAILY door: every detail-filling stage EXCEPT the "
                         "Overture --pull. Costs minutes, not hours, so the "
                         "cheap sources (licence registries, chain feeds, OSM "
                         "edits) are picked up daily instead of monthly.")
    ap.add_argument("--csv", action="store_true",
                    help="write truck_mechanics.csv — every shop, every "
                         "enrichment column, one row each")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--release", default=None)
    ap.add_argument("--local-dir", default=None,
                    help="scan a local mirror of the release parquet files "
                         "instead of reading them over S3")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock = _acquire_lock()
    except _Busy as exc:
        print(f"[skip] {exc} — nothing to do", flush=True)
        return 0
    do_all = not (a.pull or a.enrich or a.licence or a.chains or a.osm_match
                  or a.cbp or a.verify or a.html or a.csv or a.fill_report
                  or a.refresh)
    # --refresh is do_all minus the 3-hour Overture scan. Overture publishes
    # monthly, so pulling it daily would burn a national parquet scan to
    # rewrite identical rows AND reset observed_at, making stale data look
    # fresh. Everything else here changes daily and is cheap.
    cheap = a.refresh or do_all
    # One audited ops.source_runs row per invocation, whichever subcommand mix
    # was asked for — mirrors chain_sites.py / osm_extract.py. A skip from
    # _Busy above never reaches here on purpose (see _acquire_lock's
    # docstring: a skipped run is correct behaviour, not a failure to audit).
    run_id = _start_run()
    try:
        try:
            if a.pull or do_all:
                pull(a.release, a.local_dir)
            if a.pull or a.enrich or do_all:
                enrich_routes()
            # Order is load-bearing: the independence count in verify() reads
            # the licence and OSM flags, so both must be stamped before it runs.
            if a.licence or cheap:
                fetch_licences()
                licence_join()
            if a.chains or cheap:
                chains_hours()
            if a.osm_match or cheap:
                osm_match()
            if a.verify or cheap:
                verify()
            if a.cbp or cheap:
                coverage()
            if a.fill_report or cheap:
                fill_report()
            if a.csv or cheap:
                render_csv()
            if a.html or cheap:
                render_html()
        finally:
            lock.close()      # releases the advisory lock
    except BaseException as exc:
        # The audit must not lie by omission: a run that blew up gets a
        # 'failed' row, never silence (2026-08-18 data-critic F-01 — this
        # pipeline had NO run row at all before this fix, so a silent 0-row
        # publish or a bare exception was invisible to freshness_check.py).
        _finish_run(run_id, "failed", message=f"{type(exc).__name__}: {exc}")
        print(f"[mechanic-list] FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 1
    with get_conn() as pg:
        total = pg.execute(
            "SELECT count(*) FROM core.mechanic_shops").fetchone()[0]
    flags = [k.replace("_", "-") for k, v in vars(a).items()
             if isinstance(v, bool) and v]
    _finish_run(run_id, "success",
                message=f"flags={','.join(flags) or 'default(all)'}; "
                        f"core.mechanic_shops={total}",
                rows_published=total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
