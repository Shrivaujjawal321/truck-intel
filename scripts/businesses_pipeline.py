"""Businesses pipeline — Overture + FSQ pulls and the core.businesses conflation.

Three subcommands sharing one DuckDB profile (memory_limit 4 GB, temp on disk —
the hardware budget is binding: PG shares the box, OOM is not acceptable,
long runs are):

  --pull-overture   Overture Maps places theme (GeoParquet on public S3,
                    CDLA-Permissive-2.0) -> staging.overture_places.
                    The release id is DISCOVERED live from the documented S3
                    listing (research/businesses.md §1.2) — never a stale
                    hardcode; --release overrides. Verified live 2026-07-22:
                    the bucket retains exactly one release, 2026-06-17.0.
  --pull-fsq        Foursquare OS Places (Apache-2.0) -> staging.fsq_places.
                    Current releases live on Hugging Face (gated FREE account;
                    dataset card verified live 2026-07-22: latest release
                    dt=2026-07-09). Needs HF_TOKEN (or FSQ_HF_TOKEN); without
                    one the run records status='skipped_no_key' — the
                    eia_diesel pattern, never a crash. --fsq-mirror instead
                    reads the ANONYMOUS source.coop mirror (fused/fsq-os-places
                    — frozen at dt=2025-02-06; observed_at stays honest, the
                    run message names the staleness).
  --conflate        deterministic conflation per design/quality-ai.md §3.2 ->
                    full rebuild of core.businesses + atomic snapshot swap
                    (ruling §3.1-6: derived multi-source table, never
                    snapshot_swap per source), then engine.enqueue_rescore
                    (ruling §3.1-10 post-swap hook).

ODbL LAW (ruling §3.1-4c, structural here): this pipeline touches ONLY
Overture + FSQ. No OSM attribute value is ever read, copied, or conflated in —
core.businesses stays permissively licensed (the present_in CHECK constraint
makes 'osm' physically impossible). OSM POIs remain in the osm schema as
query-time corroboration only.

CONFLATION (quality-ai.md §3.2, deterministic core — no AI in this script):
  block   pairs = cross-source POIs within 150 m (geography ST_DWithin, GiST
          on temp tables) with pg_trgm similarity(name_norm) > 0.3
  score   sim = 0.60*name_trgm + 0.25*(1 - min(dist_m,150)/150) + 0.15*bonus
          (bonus = 1 when brand, phone last-10-digits, or squeezed street
          address matches). The formula lives ONCE, in score_from(); the SQL
          side only supplies similarity() + distance; a DB-parity test pins
          the Python trigram mirror to pg_trgm.
  merge   sim >= 0.85 -> greedy 1:1 auto-merge (best score first, both sides
          used at most once). Attributes by source precedence: the CANONICAL
          source (Overture when its per-record confidence >= 0.5 — FSQ
          publishes no per-record confidence, assumed 0.5, research
          businesses.md §1.2/§1.3) wins first, the other fills NULLs;
          present_in lists both; both per-source blobs kept under
          props.overture / props.fsq (merge stays reversible, §3.2).
  distinct sim <= 0.55 -> both kept.
  gray    0.55 < sim < 0.85 -> BOTH KEPT DISTINCT + flags ['dedup_gray_zone'].
          AI adjudication is Phase 4 — until then the safe default is
          distinct, honestly flagged (quality-ai.md §10.1).

BUSINESS_ID (foundation rule, sql/schema_wave2.sql DDL comment):
  'biz_' || sha256_hex(squeeze(name) || '|' || geohash7(canonical point))[:16]
  Known edge: same squeezed name inside one ~153 m geohash-7 cell collides.
  Resolution (the conflate track's, documented): a cross-source single pair
  colliding this way IS the same-name-within-a-cell case the blocking radius
  was chosen for — it is merged (flag 'cell_name_merge'; name similarity is
  1.0 by construction, the cell diagonal ~217 m only just exceeds the 150 m
  block). Any remaining collision (same-source duplicates) keeps ONE
  deterministic survivor (most sources, newest observed_at, lowest id) with
  flag 'cell_name_collision'; dropped rows are COUNTED in the run message —
  logged loss, never silent.

DEF (§6 ruling — the ONLY permitted inference in the platform): deterministic
brand match against data/config/def_brands.yaml (config in git, each entry
with evidence_url) -> def='inferred'; anything else stays NULL (= unknown).
The businesses_def_inferred_only CHECK makes other values impossible.

CONFIDENCE (computed here at build time — quality_nightly's TABLE_SCORING
does not cover core.businesses yet, noted for the quality track):
  T = 0.65 (open_aggregate, both sources — quality-ai.md §8)
  F = 0.5^(age/730 d) from observed_at (newest contributing source vintage —
      Overture release date / FSQ per-row date_refreshed, NEVER the load date)
  C = weighted fill of brand/address/city/zip/phone/website
  A = 1.0 when present_in has both sources (§7.3 POI-existence corroboration),
      0.5 single-source
  via quality.compute_confidence — components stored on the row.

Audit: every invocation writes EXACTLY one ops.source_runs row under its
source id ('overture_places' / 'fsq_places' / 'businesses_conflate' — the
latter seeded by sql/schema_wave2.sql, the pulls seeded here idempotently,
kind='derived' so sync_sources never disables them and the tick never
enqueues them). Exit codes: 0 = success/skip, 1 = failure (also recorded on
the run row — the audit never lies).

Usage:
  uv run python scripts/businesses_pipeline.py --pull-overture
  uv run python scripts/businesses_pipeline.py --pull-overture --bbox -75.9,38.4,-74.9,39.9
  uv run python scripts/businesses_pipeline.py --pull-fsq            # needs HF_TOKEN
  uv run python scripts/businesses_pipeline.py --pull-fsq --fsq-mirror
  uv run python scripts/businesses_pipeline.py --conflate            # engine argv
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml
from psycopg.types.json import Jsonb

from truckintel import quality
from truckintel.config import load_dotenv, user_agent
from truckintel.db import get_conn
from truckintel.engine import enqueue_rescore
from truckintel.loaders import (
    _geom_ewkt,
    _index_names_by_def,
    _split_target,
    _table_columns,
)

# ------------------------------------------------------------------ constants

OVERTURE_SOURCE_ID = "overture_places"
FSQ_SOURCE_ID = "fsq_places"
CONFLATE_SOURCE_ID = "businesses_conflate"
# A freshness budget below the firing cadence can never be satisfied, so it
# does not alert on staleness — it alerts on the calendar. These three run off
# truckintel-businesses.timer, OnCalendar=*-*-01, i.e. once a month (~31d).
# At the old 400h (16.7d) every one of them went "stale" around the 17th and
# stayed there until the 1st, roughly half of every month, which is how a
# freshness alert stops being read. 45 days is a month plus enough slack to
# absorb a cycle that ran late off Persistent=true catch-up, while still
# flagging a genuinely missed month (that lands near 60d).
SLO_HOURS = 1080

CONFIG_DIR = Path("data/config")
CATEGORY_MAP_PATH = CONFIG_DIR / "category_map.yaml"
DEF_BRANDS_PATH = CONFIG_DIR / "def_brands.yaml"
DUCKDB_TMP = Path("data/duckdb_tmp")
DUCKDB_MEMORY_LIMIT = "4GB"  # binding hardware budget — PG shares the box

# The CHECK-enforced taxonomy (sql/schema_wave2.sql), in PRIORITY order:
# truck-specific first — a multi-label FSQ place gets the most truck-relevant
# slug deterministically.
CATEGORY_SLUGS: tuple[str, ...] = (
    "truck_stop", "fuel_station", "def_retail", "truck_repair", "mobile_repair",
    "trailer_repair",
    # general-vehicle slugs (honest, NOT truck-specific): auto_repair =
    # general vehicle repair (engine/brake/transmission/electrical/exhaust);
    # auto_parts = general vehicle parts retail. A disabled tractor can use
    # these; they stay DISTINCT from truck_repair / truck_parts and are never
    # relabeled truck-specific. Priority order keeps truck-specific slugs
    # AHEAD of these so a truck-tagged record wins the merge (see _fsq_slug).
    "auto_repair",
    "tire_service", "towing", "truck_wash", "truck_parts", "auto_parts",
    "truck_dealer", "cat_scale", "weigh_station", "truck_parking", "rest_area",
    "restaurant", "fast_food", "cafe", "grocery", "motel", "hotel", "medical",
    "pharmacy", "laundry", "atm_bank", "unclassified",
)

# Overture public bucket (research/businesses.md §1.2; anonymous reads).
OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_REGION = "us-west-2"
OVERTURE_LIST_URL = (
    f"https://{OVERTURE_BUCKET}.s3.{OVERTURE_REGION}.amazonaws.com/"
    "?list-type=2&prefix=release/&delimiter=/"
)

# FSQ OS Places: HF is the current canonical distribution (gated free
# account); the retired public S3 bucket now holds only LICENSE/NOTICE
# (verified live 2026-07-22). source.coop mirror = anonymous but frozen.
FSQ_HF_DATASET = "foursquare/fsq-os-places"
FSQ_HF_TREE_URL = f"https://huggingface.co/api/datasets/{FSQ_HF_DATASET}/tree/main/release"
FSQ_MIRROR_BASE = "https://data.source.coop/fused/fsq-os-places"

# US bounding boxes (quality-ai.md §4 C2 pre-filter): CONUS, AK, HI, PR-VI.
# Honest limit: Aleutian islands west of the antimeridian are excluded.
US_BBOXES: tuple[tuple[float, float, float, float], ...] = (
    (-125.5, 24.3, -66.5, 49.5),
    (-170.0, 51.0, -129.0, 71.6),
    (-160.6, 18.5, -154.5, 22.5),
    (-68.1, 17.4, -64.4, 18.6),
)

_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY PR VI".split()
)

# Conflation thresholds (quality-ai.md §3.2 — verbatim).
BLOCK_RADIUS_M = 150.0
BLOCK_NAME_SIM = 0.3
MERGE_THRESHOLD = 0.85
DISTINCT_THRESHOLD = 0.55
W_NAME, W_DIST, W_BONUS = 0.60, 0.25, 0.15

# FSQ publishes no per-record confidence; the neutral midpoint keeps the
# canonical-source rule deterministic (Overture wins at >= 0.5, i.e. ties).
FSQ_ASSUMED_CONFIDENCE = 0.5

# POI freshness half-life (quality-ai.md §6 table).
POI_HALF_LIFE_DAYS = 730.0

# Completeness manifest for core.businesses (quality-ai.md §5 pattern —
# weights encode trucker value; structure is the commitment).
BUSINESS_COMPLETENESS: dict[str, int] = {
    "brand": 2, "address": 2, "city": 1, "zip": 1, "phone": 1, "website": 1,
}

_HTTP_TIMEOUT = 60
_COPY_BATCH = 10_000
_PROGRESS_EVERY = 100_000

# The ~50-entry normalization vocabulary of quality-ai.md §3.2 step 2, kept
# to the entries with observed value; static, in git, NOT AI. The SQL
# normalizer is GENERATED from this table so Python and Postgres can never
# drift apart.
_ABBREV: dict[str, str] = {
    "tvl": "travel", "trvl": "travel", "ctr": "center", "ctrs": "centers",
    "cntr": "center", "svc": "service", "svcs": "services", "stn": "station",
    "hwy": "highway", "pkwy": "parkway", "intl": "international",
    "amer": "american", "assoc": "associated",
}

_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, url, kind, load_pattern, schedule_minutes,
     slo_hours, license, attribution_text, enabled, verify_status)
VALUES
    (%(sid)s, %(name)s, 'truck-intel wave-2 businesses track', %(url)s,
     'derived', 'derived', NULL, %(slo)s, %(license)s, %(attr)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""

_PULL_SEEDS: dict[str, dict] = {
    OVERTURE_SOURCE_ID: {
        "name": "Overture Maps places theme (GeoParquet S3) -> staging.overture_places",
        "url": f"s3://{OVERTURE_BUCKET}/release/",
        "license": "CDLA-Permissive-2.0",
        "attr": "© Overture Maps Foundation",
    },
    FSQ_SOURCE_ID: {
        "name": "Foursquare OS Places (HF parquet) -> staging.fsq_places",
        "url": f"hf://datasets/{FSQ_HF_DATASET}",
        "license": "Apache-2.0",
        "attr": "Foursquare OS Places (Apache-2.0)",
    },
}


# ------------------------------------------------------------- config loading

def load_category_map(path: Path = CATEGORY_MAP_PATH) -> dict:
    """{'overture': {...}, 'fsq': {...}, 'unreachable_from_sources': [...]}
    Validated: every mapped value is a legal taxonomy slug and 'unclassified'
    is never a mapping target (it is the default, not a category)."""
    doc = yaml.safe_load(Path(path).read_text())
    for section in ("overture", "fsq"):
        mapping = doc.get(section) or {}
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError(f"{path}: section {section!r} must be a non-empty mapping")
        for raw, slug in mapping.items():
            if slug not in CATEGORY_SLUGS or slug == "unclassified":
                raise ValueError(
                    f"{path}: {section}.{raw!r} maps to illegal slug {slug!r}"
                )
    return doc


def load_def_brands(path: Path = DEF_BRANDS_PATH) -> dict:
    doc = yaml.safe_load(Path(path).read_text())
    if not doc.get("brands") or not doc.get("categories_gate"):
        raise ValueError(f"{path}: needs 'brands' and 'categories_gate'")
    for entry in doc["brands"]:
        if not entry.get("evidence_url"):
            raise ValueError(
                f"{path}: DEF entry {entry.get('brand')!r} lacks evidence_url "
                "(§6: the inference must carry evidence, in git)"
            )
    return doc


# --------------------------------------------------------------- text helpers

def squeeze(text: str | None) -> str:
    """lowercase, keep [a-z0-9] only — the business_id / brand-match key."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def norm_name(name: str | None) -> str:
    """Matching-name normalization (quality-ai.md §3.2 step 2): lowercase,
    strip '#NNN' store numbers, non-alnum -> space, expand the static
    abbreviation table word-by-word, collapse whitespace."""
    s = (name or "").lower()
    s = re.sub(r"#\s*\d+", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [_ABBREV.get(w, w) for w in s.split()]
    return " ".join(words)


def norm_name_sql(column: str) -> str:
    """The SQL twin of norm_name(), GENERATED from the same _ABBREV table so
    the two implementations cannot drift (a needs_db test pins parity)."""
    expr = f"lower({column})"
    expr = f"regexp_replace({expr}, '#\\s*\\d+', ' ', 'g')"
    expr = f"regexp_replace({expr}, '[^a-z0-9]+', ' ', 'g')"
    for abbr, full in _ABBREV.items():
        expr = f"regexp_replace({expr}, '\\m{abbr}\\M', '{full}', 'g')"
    return f"btrim(regexp_replace({expr}, '\\s+', ' ', 'g'))"


def trigram_similarity(a: str, b: str) -> float:
    """pg_trgm-compatible similarity for ALREADY-normalized names: per word,
    pad '  word ' and take 3-grams; set semantics; |A∩B| / |A∪B|.
    Mirrors similarity() (a needs_db test pins the parity); exists so the
    pure scorer is self-contained for table-driven tests."""
    def grams(s: str) -> set[str]:
        out: set[str] = set()
        for word in re.split(r"[^a-z0-9]+", s.lower()):
            if not word:
                continue
            padded = f"  {word} "
            out.update(padded[i:i + 3] for i in range(len(padded) - 2))
        return out

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_GEOHASH32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    """Standard geohash (matches PostGIS ST_GeoHash — needs_db test pins it)."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, bit, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_GEOHASH32[ch])
            bit, ch = 0, 0
    return "".join(out)


def business_id(name: str, lat: float, lon: float) -> str:
    """Foundation derivation rule (DDL comment, deterministic + replayable):
    'biz_' + first 16 hex of sha256(squeeze(name) + '|' + geohash7)."""
    key = f"{squeeze(name)}|{geohash_encode(lat, lon, 7)}"
    return "biz_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def state_code(raw: str | None) -> str | None:
    """USPS 2-letter codes only; anything else -> NULL (raw stays in props)."""
    code = (raw or "").strip().upper()
    return code if code in _US_STATES else None


# ------------------------------------------------------------------- scoring

def _phone_digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def pair_bonus(brand_a: str | None, brand_b: str | None,
               phone_a: str | None, phone_b: str | None,
               addr_a: str | None, addr_b: str | None) -> float:
    """1.0 when brand, phone (last 10 digits), or squeezed street address
    matches — else 0.0 (quality-ai.md §3.2 step 3)."""
    ba, bb = squeeze(brand_a), squeeze(brand_b)
    if ba and ba == bb:
        return 1.0
    pa, pb = _phone_digits(phone_a), _phone_digits(phone_b)
    if len(pa) >= 10 and pa[-10:] == pb[-10:]:
        return 1.0
    aa, ab = squeeze(addr_a), squeeze(addr_b)
    if aa and aa == ab:
        return 1.0
    return 0.0


def score_from(name_sim: float, dist_m: float, bonus: float) -> float:
    """THE formula (quality-ai.md §3.2 step 3) — the only place it exists."""
    return (W_NAME * name_sim
            + W_DIST * (1.0 - min(dist_m, BLOCK_RADIUS_M) / BLOCK_RADIUS_M)
            + W_BONUS * bonus)


def score_pair(a: dict, b: dict) -> float:
    """Pure scorer over two row dicts (name, lat, lon, brand, phone, address)
    — the table-driven-test surface; production scoring feeds score_from with
    pg_trgm similarity + geography distance from SQL."""
    return score_from(
        trigram_similarity(norm_name(a.get("name")), norm_name(b.get("name"))),
        haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]),
        pair_bonus(a.get("brand"), b.get("brand"), a.get("phone"),
                   b.get("phone"), a.get("address"), b.get("address")),
    )


def def_inferred(name: str | None, brand: str | None, category: str,
                 cfg: dict) -> str | None:
    """§6 DEF ruling: 'inferred' on a deterministic brand match inside the
    category gate; otherwise None (= unknown). Never any other value."""
    if category not in cfg["categories_gate"]:
        return None
    b, n = squeeze(brand), squeeze(name)
    for entry in cfg["brands"]:
        if b and b in entry.get("brand_squeezed", ()):
            return "inferred"
        for phrase in entry.get("name_squeezed_substrings", ()):
            if phrase and phrase in n:
                return "inferred"
    return None


# ------------------------------------------------------------ run bookkeeping

def _start_run(source_id: str) -> int:
    with get_conn() as conn:
        # Idempotent seed (ops.source_runs has an FK to ops.sources).
        # businesses_conflate is seeded by sql/schema_wave2.sql (no-op here);
        # the pulls carry their registry-style metadata; test source ids get
        # a generic derived row.
        seed = _PULL_SEEDS.get(source_id, {
            "name": f"Derived: businesses pipeline ({source_id})",
            "url": None, "license": None, "attr": None,
        })
        conn.execute(_SEED_SQL, {"sid": source_id, "slo": SLO_HOURS, **seed})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id",
            (source_id,),
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows_published: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, (message or "")[:1000] or None, rows_published, run_id),
        )


# --------------------------------------------------------------- DuckDB setup

def _duck():
    """One shared DuckDB profile: 4 GB cap, spill to disk (binding budget)."""
    import duckdb

    DUCKDB_TMP.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory='{DUCKDB_TMP}'")
    con.execute("SET threads=4")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # The mirror pull reads 81 parquet files over plain HTTP in one query, so
    # it is exposed to every transient blip for the whole ~20 minutes it runs.
    # On 2026-08-04 it staged 100,000 rows and then died on "Could not resolve
    # hostname", losing the batch — DuckDB aborts the pending result and the
    # COPY rolls back, so partial progress is worth nothing.
    #
    # connection_caching is off by default, which means the client can
    # re-establish (and re-resolve) per request across those 81 files; turning
    # it on removes most of the lookups that can fail. The stock retry budget
    # is 3 attempts starting at 100 ms with backoff 4 — about 2 seconds of
    # cover, shorter than a typical resolver hiccup. 5 attempts from 1 s gives
    # roughly 85 s instead.
    #
    # Caveat worth keeping: http_retries is documented as retrying I/O errors,
    # and it is not established that a resolver failure is classified as one.
    # This widens the window and cuts the number of chances to fail; if the
    # pull dies this way again, the fix is a retry around the whole pull (safe
    # to do — the staging load TRUNCATEs before COPY, so a rerun is clean).
    con.execute("SET httpfs_connection_caching=true")
    con.execute("SET http_retries=5")
    con.execute("SET http_retry_wait_ms=1000")
    return con


def _bbox_sql(lon_col: str, lat_col: str,
              bbox: tuple[float, float, float, float] | None) -> str:
    boxes = [bbox] if bbox else list(US_BBOXES)
    parts = [
        f"({lon_col} BETWEEN {w} AND {e} AND {lat_col} BETWEEN {s} AND {n})"
        for (w, s, e, n) in boxes
    ]
    return "(" + " OR ".join(parts) + ")"


# ----------------------------------------------------------- release discovery

def discover_overture_release() -> str:
    """Latest release id from the documented public S3 listing — live, never
    a stale hardcode. (Verified 2026-07-22: exactly one retained release,
    2026-06-17.0 — the bucket drops older releases.)"""
    resp = requests.get(OVERTURE_LIST_URL, timeout=_HTTP_TIMEOUT,
                        headers={"User-Agent": user_agent()})
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    releases = []
    for el in root.iter():
        if el.tag.endswith("Prefix") and (el.text or "").startswith("release/"):
            rid = el.text.removeprefix("release/").strip("/")
            if rid:
                releases.append(rid)
    if not releases:
        raise RuntimeError("Overture S3 listing returned no release/ prefixes")
    return max(releases)  # ISO-dated ids sort lexicographically


def discover_fsq_release() -> str:
    """Latest dt=YYYY-MM-DD release dir on the HF dataset (the tree API is
    anonymous even though the parquet itself is gated). Verified 2026-07-22:
    latest = 2026-07-09, matching the dataset card's data_files."""
    resp = requests.get(FSQ_HF_TREE_URL, timeout=_HTTP_TIMEOUT,
                        headers={"User-Agent": user_agent()})
    resp.raise_for_status()
    dts = [
        entry["path"].split("dt=", 1)[1]
        for entry in resp.json()
        if isinstance(entry, dict) and "dt=" in entry.get("path", "")
    ]
    if not dts:
        raise RuntimeError("HF tree listing returned no release/dt= dirs")
    return max(dts)


def discover_fsq_mirror_release() -> str:
    """Latest release folder on the anonymous source.coop mirror (frozen —
    2025-02-06 as of 2026-07-22; the caller labels the staleness)."""
    resp = requests.get(f"{FSQ_MIRROR_BASE}/", timeout=_HTTP_TIMEOUT,
                        headers={"User-Agent": user_agent()})
    resp.raise_for_status()
    dts = set(re.findall(r"<Key>fsq-os-places/(\d{4}-\d{2}-\d{2})/places/", resp.text))
    if not dts:
        raise RuntimeError("source.coop mirror listing returned no release dirs")
    return max(dts)


def fsq_mirror_parquet_urls(rel: str) -> list[str]:
    """Explicit parquet URL list for a source.coop mirror release.

    The mirror is served over plain HTTPS (an S3-gateway), and DuckDB cannot
    glob (`*`) generic HTTP paths ("Globs for generic HTTP files are not
    supported"). So we list the bucket — the same S3 ListBucket XML
    discover_fsq_mirror_release() reads — and hand read_parquet an explicit
    list of file URLs instead of a glob.

    No-silent-truncation rule: an S3 listing caps at 1000 keys and sets
    <IsTruncated>true</IsTruncated> when there are more. A truncated listing
    would silently drop parquet files -> silently missing places. We refuse
    that: if truncated we raise (verified 2026-07-23: the whole bucket is 335
    keys, IsTruncated=false, 81 parquet files for 2025-02-06 — one page)."""
    resp = requests.get(f"{FSQ_MIRROR_BASE}/", timeout=_HTTP_TIMEOUT,
                        headers={"User-Agent": user_agent()})
    resp.raise_for_status()
    if "<IsTruncated>true</IsTruncated>" in resp.text:
        raise RuntimeError(
            "source.coop mirror listing is truncated (>1000 keys) — refusing "
            "to read a partial file list (would silently drop places); add "
            "continuation-token paging before trusting this path"
        )
    keys = re.findall(
        rf"<Key>(fsq-os-places/{re.escape(rel)}/places/[^<]*\.parquet)</Key>",
        resp.text,
    )
    if not keys:
        raise RuntimeError(
            f"source.coop mirror listing has no parquet for release {rel!r}")
    root = FSQ_MIRROR_BASE.rsplit("/", 1)[0]  # https://data.source.coop/fused
    return [f"{root}/{k}" for k in sorted(keys)]


# -------------------------------------------------------------- pull: overture

_OVERTURE_STAGING_COLS = (
    "source_record_id", "name", "brand", "category_source", "category",
    "lat", "lon", "address", "city", "state", "zip", "phone", "website",
    "src_confidence", "observed_at", "run_id", "props",
)


def pull_overture(*, release: str | None = None,
                  bbox: tuple[float, float, float, float] | None = None,
                  max_rows: int | None = None, count_unmapped: bool = True,
                  staging_table: str = "staging.overture_places",
                  source_id: str = OVERTURE_SOURCE_ID) -> int:
    """Overture places -> staging (truncated per run, §5.1). Pushdown: US
    bbox + mapped categories only; unmapped are excluded AND counted in the
    run message. observed_at = the RELEASE vintage, never the load date."""
    cmap: dict[str, str] = load_category_map()["overture"]
    schema, table = _split_target(staging_table)
    run_id = _start_run(source_id)
    try:
        rel = release or discover_overture_release()
        observed_at = datetime.strptime(rel[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
        con = _duck()
        con.execute(f"SET s3_region='{OVERTURE_REGION}'")
        # DuckDB's S3 glob needs the extension to list a public prefix; a bare
        # `/*` returns "No files found" on this bucket (verified 2026-07-22).
        src = (f"read_parquet('s3://{OVERTURE_BUCKET}/release/{rel}"
               "/theme=places/type=place/*.parquet')")
        in_list = ", ".join(f"'{s}'" for s in sorted(cmap))
        where = (
            f"categories.primary IN ({in_list}) "
            f"AND {_bbox_sql('bbox.xmin', 'bbox.ymin', bbox)} "
            "AND (addresses[1].country = 'US' OR addresses[1].country IS NULL)"
        )
        limit = f"LIMIT {int(max_rows)}" if max_rows else ""
        print(f"{source_id} run {run_id}: release {rel} "
              f"({'custom bbox' if bbox else 'US bboxes'})", flush=True)
        cur = con.execute(f"""
            SELECT id, names.primary AS name, brand.names.primary AS brand,
                   categories.primary AS category_source,
                   bbox.ymin AS lat, bbox.xmin AS lon,
                   addresses[1].freeform AS address,
                   addresses[1].locality AS city,
                   addresses[1].region  AS state_raw,
                   addresses[1].postcode AS zip,
                   phones[1] AS phone, websites[1] AS website,
                   confidence
            FROM {src} WHERE {where} {limit}
        """)
        loaded = 0
        with get_conn() as pg:
            pg.execute(f'TRUNCATE "{schema}"."{table}"')
            col_sql = ", ".join(f'"{c}"' for c in _OVERTURE_STAGING_COLS)
            with pg.cursor() as pcur, pcur.copy(
                f'COPY "{schema}"."{table}" ({col_sql}) FROM STDIN'
            ) as copy:
                while True:
                    batch = cur.fetchmany(_COPY_BATCH)
                    if not batch:
                        break
                    for (gid, name, brand, cat_src, lat, lon, address, city,
                         st_raw, zip_, phone, website, conf) in batch:
                        copy.write_row((
                            gid, name, brand, cat_src, cmap[cat_src],
                            lat, lon, address, city, state_code(st_raw), zip_,
                            phone, website, conf, observed_at, run_id,
                            Jsonb({"release": rel, "state_raw": st_raw}),
                        ))
                        loaded += 1
                    if loaded % _PROGRESS_EVERY < _COPY_BATCH:
                        print(f"  ... {loaded} rows staged", flush=True)
        unmapped = None
        if count_unmapped:
            unmapped = con.execute(f"""
                SELECT count(*) FROM {src}
                WHERE categories.primary NOT IN ({in_list})
                  AND {_bbox_sql('bbox.xmin', 'bbox.ymin', bbox)}
                  AND (addresses[1].country = 'US'
                       OR addresses[1].country IS NULL)
            """).fetchone()[0]
    except BaseException as exc:
        _finish_run(run_id, "failed", message=(str(exc) or type(exc).__name__))
        raise
    message = (
        f"release={rel}; observed_at={observed_at:%Y-%m-%d} (release vintage); "
        f"{staging_table}={loaded}; "
        f"unmapped_categories_excluded="
        f"{unmapped if unmapped is not None else 'not_counted'}; "
        f"bbox={'custom' if bbox else 'US'}"
        + (f"; max_rows={max_rows}" if max_rows else "")
    )
    _finish_run(run_id, "success", message=message, rows_published=loaded)
    print(f"{source_id} run {run_id}: {message}", flush=True)
    return loaded


# ------------------------------------------------------------------ pull: fsq

_FSQ_STAGING_COLS = (
    "source_record_id", "name", "brand", "category_source", "category",
    "lat", "lon", "address", "city", "state", "zip", "phone", "website",
    "date_refreshed", "date_closed", "observed_at", "run_id", "props",
)


def _fsq_slug(labels: list[str], cmap: dict[str, str]) -> tuple[str, str]:
    """(slug, winning label) — the most truck-relevant mapped label wins,
    deterministically by CATEGORY_SLUGS priority order."""
    best = min(
        ((CATEGORY_SLUGS.index(cmap[lbl]), cmap[lbl], lbl)
         for lbl in labels if lbl in cmap),
        default=None,
    )
    if best is None:  # cannot happen: SQL filter requires a mapped label
        raise ValueError(f"no mapped label in {labels!r}")
    return best[1], best[2]


def _parse_date(raw) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def pull_fsq(*, release: str | None = None,
             bbox: tuple[float, float, float, float] | None = None,
             max_rows: int | None = None, mirror: bool = False,
             count_probe: bool = True,
             staging_table: str = "staging.fsq_places",
             source_id: str = FSQ_SOURCE_ID) -> int:
    """FSQ OS Places -> staging. HF (gated free token) by default; --fsq-mirror
    reads the anonymous-but-frozen source.coop mirror. Closed places
    (date_closed NOT NULL) are excluded by pushdown and counted. observed_at =
    per-row date_refreshed (FSQ's own freshness signal), falling back to the
    release date — never the load date."""
    cmap: dict[str, str] = load_category_map()["fsq"]
    schema, table = _split_target(staging_table)
    run_id = _start_run(source_id)
    try:
        token = os.environ.get("HF_TOKEN") or os.environ.get("FSQ_HF_TOKEN")
        if not mirror and not token:
            message = (
                "HF_TOKEN/FSQ_HF_TOKEN unset — FSQ OS Places is a gated FREE "
                "download (huggingface.co/datasets/foursquare/fsq-os-places); "
                "set a token or run --fsq-mirror (anonymous source.coop "
                "mirror, frozen at 2025-02-06)"
            )
            _finish_run(run_id, "skipped_no_key", message=message)
            print(f"{source_id} run {run_id}: {message}", flush=True)
            return 0

        con = _duck()
        if mirror:
            rel = release or discover_fsq_mirror_release()
            # DuckDB can't glob generic HTTP paths — hand it an explicit,
            # listing-derived file list (see fsq_mirror_parquet_urls).
            urls = fsq_mirror_parquet_urls(rel)
            url_list = ", ".join("'" + u.replace("'", "''") + "'" for u in urls)
            src = f"read_parquet([{url_list}])"
            basis = (f"source.coop mirror (FROZEN — {len(urls)} parquet files, "
                     "newest mirrored release)")
        else:
            rel = release or discover_fsq_release()
            escaped = token.replace("'", "''")
            con.execute(
                f"CREATE SECRET hf_secret (TYPE huggingface, TOKEN '{escaped}')"
            )
            src = (f"read_parquet('hf://datasets/{FSQ_HF_DATASET}"
                   f"/release/dt={rel}/places/parquet/*.parquet')")
            basis = "HF release"
        release_date = datetime.strptime(rel[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
        labels_list = ", ".join(
            "'" + lbl.replace("'", "''") + "'" for lbl in sorted(cmap)
        )
        base_where = (
            "country = 'US' AND latitude IS NOT NULL AND longitude IS NOT NULL "
            f"AND {_bbox_sql('longitude', 'latitude', bbox)}"
        )
        match_expr = (
            f"list_filter(fsq_category_labels, l -> l IN ({labels_list}))"
        )
        limit = f"LIMIT {int(max_rows)}" if max_rows else ""
        print(f"{source_id} run {run_id}: release {rel} ({basis})", flush=True)
        cur = con.execute(f"""
            SELECT fsq_place_id, name, latitude, longitude, address, locality,
                   region, postcode, tel, website,
                   date_refreshed, date_closed, {match_expr} AS matched
            FROM {src}
            WHERE {base_where} AND len({match_expr}) > 0
              AND (date_closed IS NULL OR date_closed = '') {limit}
        """)
        loaded = 0
        with get_conn() as pg:
            pg.execute(f'TRUNCATE "{schema}"."{table}"')
            col_sql = ", ".join(f'"{c}"' for c in _FSQ_STAGING_COLS)
            with pg.cursor() as pcur, pcur.copy(
                f'COPY "{schema}"."{table}" ({col_sql}) FROM STDIN'
            ) as copy:
                while True:
                    batch = cur.fetchmany(_COPY_BATCH)
                    if not batch:
                        break
                    for (fid, name, lat, lon, address, city, region, zip_,
                         tel, website, refreshed, closed, matched) in batch:
                        slug, label = _fsq_slug(list(matched), cmap)
                        refreshed_d = _parse_date(refreshed)
                        observed = (
                            datetime(refreshed_d.year, refreshed_d.month,
                                     refreshed_d.day, tzinfo=timezone.utc)
                            if refreshed_d else release_date
                        )
                        copy.write_row((
                            fid, name, None, label, slug, lat, lon, address,
                            city, state_code(region), zip_, tel, website,
                            refreshed_d, _parse_date(closed), observed, run_id,
                            Jsonb({"release": rel, "mirror": mirror,
                                   "matched_labels": list(matched),
                                   "region_raw": region}),
                        ))
                        loaded += 1
                    if loaded % _PROGRESS_EVERY < _COPY_BATCH:
                        print(f"  ... {loaded} rows staged", flush=True)
        counts_msg = "probe_skipped"
        if count_probe:
            unmapped, closed_matched = con.execute(f"""
                SELECT count(*) FILTER (WHERE len({match_expr}) = 0),
                       count(*) FILTER (WHERE len({match_expr}) > 0
                                        AND date_closed IS NOT NULL
                                        AND date_closed <> '')
                FROM {src} WHERE {base_where}
            """).fetchone()
            counts_msg = (f"unmapped_categories_excluded={unmapped}; "
                          f"closed_excluded={closed_matched}")
    except BaseException as exc:
        _finish_run(run_id, "failed", message=(str(exc) or type(exc).__name__))
        raise
    message = (
        f"release={rel} ({basis}); observed_at=per-row date_refreshed "
        f"(fallback {release_date:%Y-%m-%d}); {staging_table}={loaded}; "
        f"{counts_msg}; bbox={'custom' if bbox else 'US'}"
        + (f"; max_rows={max_rows}" if max_rows else "")
    )
    _finish_run(run_id, "success", message=message, rows_published=loaded)
    print(f"{source_id} run {run_id}: {message}", flush=True)
    return loaded


# ------------------------------------------------------------------- conflate

def _swap_businesses(conn, target: str, rows, *, source_id: str,
                     run_id: int) -> int:
    """snapshot_swap-style atomic rebuild for core.businesses.

    Same pattern as loaders.snapshot_swap (build <table>_new LIKE INCLUDING
    ALL, COPY, RENAME-swap, restore index names) with ONE difference: the
    shared loader consumes row 'lat'/'lon' into geom ONLY, but
    core.businesses stores lat + lon as real NOT NULL columns alongside geom
    — so here they are written as columns AND composed into geom. Kept local
    to this script (loaders.py is not businesses-track-owned); integrator may
    unify later. Runs in the caller's transaction: any failure rolls the
    whole build back, the live table is never touched."""
    from itertools import chain

    from psycopg.types.json import Jsonb as _Jsonb

    schema, table = _split_target(target)
    new, old = f"{table}_new", f"{table}_old"
    orig_index_names = _index_names_by_def(conn, schema, table)
    conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{new}"')
    conn.execute(
        f'CREATE TABLE "{schema}"."{new}" '
        f'(LIKE "{schema}"."{table}" INCLUDING ALL)')
    columns = _table_columns(conn, schema, new)

    rows_iter = iter(rows)
    first = next(rows_iter, None)
    published = 0
    if first is not None:
        insert_cols = [c for c in columns if c in first]
        if "geom" in columns and "geom" not in insert_cols:
            insert_cols.append("geom")
        for lineage_col in ("source_id", "run_id"):
            if lineage_col in columns and lineage_col not in insert_cols:
                insert_cols.append(lineage_col)
        col_sql = ", ".join(f'"{c}"' for c in insert_cols)
        with conn.cursor() as cur:
            with cur.copy(
                f'COPY "{schema}"."{new}" ({col_sql}) FROM STDIN'
            ) as copy:
                for row in chain([first], rows_iter):
                    values = []
                    for col in insert_cols:
                        if col == "geom":
                            values.append(_geom_ewkt(row))
                        elif col == "source_id":
                            values.append(row.get("source_id", source_id))
                        elif col == "run_id":
                            values.append(row.get("run_id", run_id))
                        elif col == "props":
                            values.append(_Jsonb(row.get("props") or {}))
                        else:
                            values.append(row.get(col))
                    copy.write_row(values)
                    published += 1

    conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{old}"')
    conn.execute(f'ALTER TABLE "{schema}"."{table}" RENAME TO "{old}"')
    conn.execute(f'ALTER TABLE "{schema}"."{new}" RENAME TO "{table}"')
    conn.execute(f'DROP TABLE "{schema}"."{old}"')
    for key, auto_name in _index_names_by_def(conn, schema, table).items():
        orig = orig_index_names.get(key)
        if orig and orig != auto_name:
            conn.execute(
                f'ALTER INDEX "{schema}"."{auto_name}" RENAME TO "{orig}"')
    return published



_BUILD_COLS = """
    id BIGSERIAL PRIMARY KEY,
    src TEXT NOT NULL,            -- 'merged' | 'overture' | 'fsq'
    name TEXT NOT NULL, category TEXT NOT NULL, brand TEXT,
    lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL,
    address TEXT, city TEXT, state CHAR(2), zip TEXT, phone TEXT, website TEXT,
    observed_at TIMESTAMPTZ, present_in TEXT[] NOT NULL,
    gray BOOLEAN NOT NULL DEFAULT FALSE,
    cell_flag TEXT,               -- NULL | 'cell_name_merge' | 'cell_name_collision'
    props JSONB NOT NULL
"""

_STAGE_TEMP_SQL = """
CREATE TEMP TABLE {tmp} AS
SELECT row_number() OVER (ORDER BY source_record_id, name) AS rid,
       source_record_id, name, {norm} AS name_norm, brand, category,
       lat, lon, address, city, state, zip, phone, website,
       {conf} AS src_confidence, observed_at,
       jsonb_build_object(
           'source_record_id', source_record_id, 'name', name, 'brand', brand,
           'category_source', category_source, 'category', category,
           'lat', lat, 'lon', lon, 'address', address, 'city', city,
           'state', state, 'zip', zip, 'phone', phone, 'website', website,
           'observed_at', observed_at, 'src_confidence', {conf}{extra})
           || props AS blob,
       ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography AS g
FROM {staging}
WHERE category IS NOT NULL AND name IS NOT NULL
  AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180
  AND NOT (lat = 0 AND lon = 0) {closed_guard}
"""

_PAIRS_SQL = """
SELECT o.rid, f.rid, ST_Distance(o.g, f.g) AS dist_m,
       similarity(o.name_norm, f.name_norm) AS name_sim,
       o.brand, f.brand, o.phone, f.phone, o.address, f.address
FROM _bo o
JOIN _bf f ON ST_DWithin(o.g, f.g, %(radius)s)
          AND similarity(o.name_norm, f.name_norm) > %(min_sim)s
"""

# Canonical-source precedence (research freshness/confidence evidence): the
# contributing source with the highest source confidence wins first; the
# other fills NULLs. Overture carries per-record confidence; FSQ is assumed
# the 0.5 midpoint, so Overture is canonical at confidence >= 0.5 (ties
# included) — deterministic, same rule the business_id canonical point uses.
_MERGED_INSERT_SQL = """
INSERT INTO {build}
    (src, name, category, brand, lat, lon, address, city, state, zip,
     phone, website, observed_at, present_in, gray, props)
SELECT 'merged',
       CASE WHEN canon_o THEN o.name ELSE f.name END,
       CASE WHEN canon_o THEN o.category ELSE f.category END,
       CASE WHEN canon_o THEN COALESCE(o.brand, f.brand)
            ELSE COALESCE(f.brand, o.brand) END,
       CASE WHEN canon_o THEN o.lat ELSE f.lat END,
       CASE WHEN canon_o THEN o.lon ELSE f.lon END,
       CASE WHEN canon_o THEN COALESCE(o.address, f.address)
            ELSE COALESCE(f.address, o.address) END,
       CASE WHEN canon_o THEN COALESCE(o.city, f.city)
            ELSE COALESCE(f.city, o.city) END,
       CASE WHEN canon_o THEN COALESCE(o.state, f.state)
            ELSE COALESCE(f.state, o.state) END,
       CASE WHEN canon_o THEN COALESCE(o.zip, f.zip)
            ELSE COALESCE(f.zip, o.zip) END,
       CASE WHEN canon_o THEN COALESCE(o.phone, f.phone)
            ELSE COALESCE(f.phone, o.phone) END,
       CASE WHEN canon_o THEN COALESCE(o.website, f.website)
            ELSE COALESCE(f.website, o.website) END,
       GREATEST(o.observed_at, f.observed_at),
       ARRAY['overture', 'fsq'],
       (o.rid = ANY(%(gray_o)s::bigint[]) OR f.rid = ANY(%(gray_f)s::bigint[])),
       jsonb_build_object('overture', o.blob, 'fsq', f.blob)
FROM _merges m
JOIN _bo o ON o.rid = m.o_rid
JOIN _bf f ON f.rid = m.f_rid
CROSS JOIN LATERAL (
    SELECT COALESCE(o.src_confidence, 0) >= %(fsq_conf)s AS canon_o
) c
"""

_SINGLE_INSERT_SQL = """
INSERT INTO {build}
    (src, name, category, brand, lat, lon, address, city, state, zip,
     phone, website, observed_at, present_in, gray, props)
SELECT %(label)s, t.name, t.category, t.brand, t.lat, t.lon, t.address,
       t.city, t.state, t.zip, t.phone, t.website, t.observed_at,
       ARRAY[%(label)s::text], t.rid = ANY(%(gray)s::bigint[]),
       jsonb_build_object(%(label)s, t.blob)
FROM {tmp} t
WHERE NOT EXISTS (SELECT 1 FROM _merges m WHERE m.{col} = t.rid)
ORDER BY t.rid
"""

_COLLISION_SQL = """
SELECT regexp_replace(lower(name), '[^a-z0-9]', '', 'g')
           || '|' || ST_GeoHash(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 7)
           AS key,
       array_agg(id ORDER BY cardinality(present_in) DESC,
                 observed_at DESC NULLS LAST, id) AS ids,
       array_agg(DISTINCT src) AS srcs
FROM {build}
GROUP BY 1 HAVING count(*) > 1
"""


def _resolve_collisions(conn, build: str) -> tuple[int, int]:
    """business_id-cell collision resolution (module docstring): cross-source
    single pairs merge ('cell_name_merge'); anything else keeps ONE
    deterministic survivor ('cell_name_collision'), drops counted."""
    cell_merges = cell_drops = 0
    groups = conn.execute(_COLLISION_SQL.format(build=build)).fetchall()
    for _key, ids, srcs in groups:
        rows = {
            r[0]: r for r in conn.execute(
                f"SELECT id, src, present_in, props, observed_at FROM {build} "
                "WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        }
        if (len(ids) == 2 and sorted(srcs) == ["fsq", "overture"]
                and all(rows[i][1] != "merged" for i in ids)):
            o_id = next(i for i in ids if rows[i][1] == "overture")
            f_id = next(i for i in ids if rows[i][1] == "fsq")
            o_conf = (rows[o_id][3].get("overture") or {}).get("src_confidence")
            canon, other = ((o_id, f_id)
                            if (o_conf or 0) >= FSQ_ASSUMED_CONFIDENCE
                            else (f_id, o_id))
            conn.execute(
                f"""
                UPDATE {build} k SET
                    src = 'merged',
                    brand   = COALESCE(k.brand, x.brand),
                    address = COALESCE(k.address, x.address),
                    city    = COALESCE(k.city, x.city),
                    state   = COALESCE(k.state, x.state),
                    zip     = COALESCE(k.zip, x.zip),
                    phone   = COALESCE(k.phone, x.phone),
                    website = COALESCE(k.website, x.website),
                    observed_at = GREATEST(k.observed_at, x.observed_at),
                    present_in = ARRAY['overture', 'fsq'],
                    gray = k.gray OR x.gray,
                    cell_flag = 'cell_name_merge',
                    props = k.props || x.props
                FROM {build} x WHERE k.id = %s AND x.id = %s
                """,
                (canon, other),
            )
            conn.execute(f"DELETE FROM {build} WHERE id = %s", (other,))
            cell_merges += 1
        else:
            keep, drops = ids[0], ids[1:]
            conn.execute(
                f"UPDATE {build} SET cell_flag = 'cell_name_collision' "
                "WHERE id = %s", (keep,))
            conn.execute(f"DELETE FROM {build} WHERE id = ANY(%s)", (drops,))
            cell_drops += len(drops)
    return cell_merges, cell_drops


def run_conflate(*, target: str = "core.businesses",
                 staging_overture: str = "staging.overture_places",
                 staging_fsq: str = "staging.fsq_places",
                 build_table: str = "staging.businesses_conflate_build",
                 source_id: str = CONFLATE_SOURCE_ID) -> int:
    """Full deterministic conflation -> atomic rebuild of core.businesses.
    Overrides exist for the tests (scratch clones — production callers pass
    nothing). Returns rows published."""
    def_cfg = load_def_brands()
    for t in (target, staging_overture, staging_fsq, build_table):
        _split_target(t)  # identifier safety before any interpolation
    run_id = _start_run(source_id)
    stats: dict[str, int] = {}
    try:
        # ---- phase 1: temp normalize + block + score + greedy match --------
        with get_conn() as conn:
            for tmp, staging, conf_expr, extra, closed in (
                ("_bo", staging_overture, "src_confidence", "", ""),
                ("_bf", staging_fsq, f"{FSQ_ASSUMED_CONFIDENCE}::numeric",
                 ", 'date_refreshed', date_refreshed, 'date_closed', date_closed",
                 "AND date_closed IS NULL"),
            ):
                conn.execute(_STAGE_TEMP_SQL.format(
                    tmp=tmp, staging=staging, norm=norm_name_sql("name"),
                    conf=conf_expr, extra=extra, closed_guard=closed,
                ))
                conn.execute(f"CREATE INDEX ON {tmp} USING GIST (g)")
                stats[tmp] = conn.execute(
                    f"SELECT count(*) FROM {tmp}").fetchone()[0]
            src_counts = {
                s: conn.execute(f"SELECT count(*) FROM {s}").fetchone()[0]
                for s in (staging_overture, staging_fsq)
            }
            if stats["_bo"] + stats["_bf"] == 0:
                raise RuntimeError(
                    "both staging tables are empty (or fully filtered) — run "
                    "--pull-overture / --pull-fsq first"
                )
            stats["dropped_overture"] = src_counts[staging_overture] - stats["_bo"]
            stats["dropped_fsq"] = src_counts[staging_fsq] - stats["_bf"]

            pairs: list[tuple[float, int, int]] = []
            gray_o: set[int] = set()
            gray_f: set[int] = set()
            with conn.cursor(name="conflate_pairs") as pcur:
                pcur.itersize = _COPY_BATCH
                pcur.execute(_PAIRS_SQL,
                             {"radius": BLOCK_RADIUS_M,
                              "min_sim": BLOCK_NAME_SIM})
                for (o_rid, f_rid, dist_m, name_sim, b_o, b_f, p_o, p_f,
                     a_o, a_f) in pcur:
                    score = score_from(
                        float(name_sim), float(dist_m),
                        pair_bonus(b_o, b_f, p_o, p_f, a_o, a_f))
                    if score >= MERGE_THRESHOLD:
                        pairs.append((score, o_rid, f_rid))
                    elif score > DISTINCT_THRESHOLD:
                        gray_o.add(o_rid)
                        gray_f.add(f_rid)
            pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
            used_o: set[int] = set()
            used_f: set[int] = set()
            merges: list[tuple[int, int]] = []
            for score, o_rid, f_rid in pairs:
                if o_rid in used_o or f_rid in used_f:
                    # ambiguous second-best >=0.85 match: honest gray, distinct
                    gray_o.add(o_rid)
                    gray_f.add(f_rid)
                    continue
                used_o.add(o_rid)
                used_f.add(f_rid)
                merges.append((o_rid, f_rid))
            # NOTE: a merged record can ALSO carry the gray flag — it means
            # "this entity had another ambiguous near-match", which stays
            # honest information after the merge.
            stats["merged"] = len(merges)
            stats["gray_zone"] = len(gray_o) + len(gray_f)

            conn.execute("CREATE TEMP TABLE _merges (o_rid BIGINT, f_rid BIGINT)")
            with conn.cursor() as cur, cur.copy(
                "COPY _merges (o_rid, f_rid) FROM STDIN"
            ) as copy:
                for o_rid, f_rid in merges:
                    copy.write_row((o_rid, f_rid))

            # ---- phase 2: build-table rebuild (regular table: a second
            # connection streams it during the swap; DROP+CREATE per run) ----
            bschema, btable = _split_target(build_table)
            build = f'"{bschema}"."{btable}"'
            conn.execute(f"DROP TABLE IF EXISTS {build}")
            conn.execute(f"CREATE TABLE {build} ({_BUILD_COLS})")
            gray_params = {"gray_o": sorted(gray_o), "gray_f": sorted(gray_f),
                           "fsq_conf": FSQ_ASSUMED_CONFIDENCE}
            conn.execute(_MERGED_INSERT_SQL.format(build=build), gray_params)
            conn.execute(
                _SINGLE_INSERT_SQL.format(build=build, tmp="_bo", col="o_rid"),
                {"label": "overture", "gray": sorted(gray_o)})
            conn.execute(
                _SINGLE_INSERT_SQL.format(build=build, tmp="_bf", col="f_rid"),
                {"label": "fsq", "gray": sorted(gray_f)})
            stats["cell_merges"], stats["cell_drops"] = _resolve_collisions(
                conn, build)
        # phase-1/2 transaction committed: build table is now visible.

        # ---- phase 3: stream build rows -> snapshot swap -------------------
        def_count = 0
        published = 0

        def rows():
            nonlocal def_count, published
            read = get_conn()
            try:
                with read.cursor(name="conflate_publish") as cur:
                    cur.itersize = _COPY_BATCH
                    cur.execute(
                        f"SELECT name, category, brand, lat, lon, address, "
                        f"city, state, zip, phone, website, observed_at, "
                        f"present_in, gray, cell_flag, props FROM {build} "
                        "ORDER BY id")
                    for (name, category, brand, lat, lon, address, city, st,
                         zip_, phone, website, observed_at, present_in, gray,
                         cell_flag, props) in cur:
                        d = def_inferred(name, brand, category, def_cfg)
                        if d:
                            def_count += 1
                        row = {
                            "business_id": business_id(name, lat, lon),
                            "name": name, "category": category, "brand": brand,
                            "lat": lat, "lon": lon, "address": address,
                            "city": city, "state": st, "zip": zip_,
                            "phone": phone, "website": website, "def": d,
                            "present_in": present_in,
                            "observed_at": observed_at,
                            "props": props,
                        }
                        score = quality.compute_confidence(
                            quality.AUTHORITY_BASE_TRUST["open_aggregate"],
                            quality.freshness(observed_at, POI_HALF_LIFE_DAYS),
                            quality.completeness(row, BUSINESS_COMPLETENESS),
                            quality.agreement(
                                0, corroborated=len(present_in) > 1),
                        )
                        flags = ([]
                                 + (["dedup_gray_zone"] if gray else [])
                                 + ([cell_flag] if cell_flag else []))
                        row.update(
                            confidence=score.confidence,
                            conf_trust=score.conf_trust,
                            conf_fresh=score.conf_fresh,
                            conf_complete=score.conf_complete,
                            conf_agree=score.conf_agree,
                            flags=flags,
                        )
                        published += 1
                        yield row
            finally:
                read.close()

        with get_conn() as pub:  # one transaction: swap + rescore enqueue
            _swap_businesses(pub, target, rows(), source_id=source_id,
                             run_id=run_id)
            # Ruling §3.1-10: every successful swap enqueues one rescore.
            # (quality_rescore's TABLE_SCORING does not include businesses
            # yet — this row's confidence is computed above at build time;
            # the enqueue keeps the swap contract uniform.)
            enqueue_rescore(pub)
        with get_conn() as conn:
            bschema, btable = _split_target(build_table)
            conn.execute(f'DROP TABLE IF EXISTS "{bschema}"."{btable}"')
    except BaseException as exc:
        _finish_run(run_id, "failed", message=(str(exc) or type(exc).__name__))
        raise
    message = (
        f"{target}={published}; overture_in={stats['_bo']} "
        f"(dropped_invalid={stats['dropped_overture']}); "
        f"fsq_in={stats['_bf']} "
        f"(dropped_invalid_or_closed={stats['dropped_fsq']}); "
        f"merged={stats['merged']}; gray_flagged={stats['gray_zone']}; "
        f"cell_name_merges={stats['cell_merges']}; "
        f"cell_collision_drops={stats['cell_drops']}; "
        f"def_inferred={def_count}"
    )
    _finish_run(run_id, "success", message=message, rows_published=published)
    print(f"{source_id} run {run_id}: {message}", flush=True)
    return published


# ----------------------------------------------------------------- entrypoint

def _parse_bbox_arg(raw: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be minLon,minLat,maxLon,maxLat")
    return tuple(parts)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Overture/FSQ pulls + core.businesses conflation")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pull-overture", action="store_true")
    mode.add_argument("--pull-fsq", action="store_true")
    mode.add_argument("--conflate", action="store_true")
    parser.add_argument("--release", default=None,
                        help="pin a release id (default: discover live)")
    parser.add_argument("--bbox", type=_parse_bbox_arg, default=None,
                        help="dev pulls: minLon,minLat,maxLon,maxLat "
                             "(default: the four US boxes)")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="dev pulls: cap staged rows")
    parser.add_argument("--fsq-mirror", action="store_true",
                        help="use the anonymous source.coop mirror "
                             "(frozen releases) instead of gated HF")
    parser.add_argument("--no-count", action="store_true",
                        help="skip the unmapped/closed COUNT probe")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        if args.pull_overture:
            pull_overture(release=args.release, bbox=args.bbox,
                          max_rows=args.max_rows,
                          count_unmapped=not args.no_count)
        elif args.pull_fsq:
            pull_fsq(release=args.release, bbox=args.bbox,
                     max_rows=args.max_rows, mirror=args.fsq_mirror,
                     count_probe=not args.no_count)
        else:
            run_conflate()
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"businesses_pipeline failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
