"""Businesses pipeline tests (scripts/businesses_pipeline.py).

Layers:
- pure: category-map coverage, normalization, pg_trgm-mirror trigram,
  conflation scorer (table-driven merge/distinct/gray), DEF inference
  marker, geohash + business_id stability
- DB parity (needs_db): the Python normalizer/trigram/geohash mirrors are
  pinned to their Postgres twins (norm_name_sql / pg_trgm similarity /
  ST_GeoHash) so the two implementations cannot drift
- DB end-to-end (needs_db): canned staging rows -> run_conflate into SCRATCH
  clones (live staging/core tables are never touched); collision-resolution
  branches unit-tested against a scratch build table
- live pulls: SKIPPED by default — network-marked via
  TRUCKINTEL_NETWORK_TESTS=1 (the verify phase runs them for real)
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from psycopg.types.json import Jsonb

from tests.conftest import needs_db
from truckintel.db import get_conn

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "scratch_businesses_test"
CONFLATE_SRC = "test_biz_conflate_src"

needs_network = pytest.mark.skipif(
    os.environ.get("TRUCKINTEL_NETWORK_TESTS") != "1",
    reason="live-pull test (set TRUCKINTEL_NETWORK_TESTS=1 to run)",
)


def _load_pipeline():
    """scripts/ is not a package — load the runner by path."""
    spec = importlib.util.spec_from_file_location(
        "businesses_pipeline", REPO_ROOT / "scripts" / "businesses_pipeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bp = _load_pipeline()


# --------------------------------------------------------- category map (pure)

def test_category_map_every_slug_reachable_or_documented():
    """Coverage contract: every taxonomy slug (minus the 'unclassified'
    default) is either produced by a mapping entry or explicitly documented
    as unreachable_from_sources — no slug silently forgotten."""
    doc = yaml.safe_load(
        (REPO_ROOT / "data/config/category_map.yaml").read_text())
    mapped = set(doc["overture"].values()) | set(doc["fsq"].values())
    unreachable = set(doc["unreachable_from_sources"])
    taxonomy = set(bp.CATEGORY_SLUGS) - {"unclassified"}
    assert mapped <= taxonomy
    assert "unclassified" not in mapped  # default, never a mapping target
    assert mapped & unreachable == set()
    assert mapped | unreachable == taxonomy, (
        "unaccounted slugs: " + str(taxonomy - mapped - unreachable))
    # General-vehicle slugs are HONESTLY sourced (mapped from generic Overture/
    # FSQ repair + parts categories), so they must be REACHABLE — i.e. present
    # in `mapped` and absent from `unreachable_from_sources`. They are distinct
    # from the truck-specific truck_repair / truck_parts, which stay mapped too.
    for reachable_slug in ("auto_repair", "auto_parts"):
        assert reachable_slug in taxonomy
        assert reachable_slug in mapped
        assert reachable_slug not in unreachable
    assert {"truck_repair", "truck_parts"} <= mapped  # truck-specific stays


def test_category_map_loader_validates(tmp_path):
    good = bp.load_category_map()  # the checked-in config passes validation
    assert good["overture"]["truck_stop"] == "truck_stop"
    assert good["fsq"]["Travel and Transportation > Truck Stop"] == "truck_stop"
    bad = tmp_path / "bad.yaml"
    bad.write_text("overture: {gas_station: made_up_slug}\nfsq: {x: truck_stop}\n")
    with pytest.raises(ValueError, match="illegal slug"):
        bp.load_category_map(bad)


def test_fsq_slug_priority_truck_specific_wins():
    cmap = bp.load_category_map()["fsq"]
    labels = ["Dining and Drinking > Restaurant",
              "Travel and Transportation > Truck Stop"]
    slug, label = bp._fsq_slug(labels, cmap)
    assert slug == "truck_stop"
    assert label == "Travel and Transportation > Truck Stop"


# -------------------------------------------------------- normalization (pure)

def test_squeeze_and_norm_name():
    assert bp.squeeze("Love's Travel Stop #291") == "lovestravelstop291"
    assert bp.squeeze(None) == ""
    assert bp.norm_name("Love's Travel Stop #291") == "love s travel stop"
    assert bp.norm_name("PILOT TVL CTR") == "pilot travel center"  # abbrevs
    assert bp.norm_name(None) == ""


def test_trigram_matches_pg_trgm_documented_example():
    # The pg_trgm docs example: similarity('word', 'two words') = 4/11.
    assert abs(bp.trigram_similarity("word", "two words") - 4 / 11) < 1e-9
    assert bp.trigram_similarity("same", "same") == 1.0
    assert bp.trigram_similarity("", "anything") == 0.0
    # set semantics: word ORDER does not change the trigram set
    assert bp.trigram_similarity("casey fuel stop", "fuel stop casey") == 1.0


# --------------------------------------------------------------- scorer (pure)

def test_score_from_thresholds_table_driven():
    cases = [
        # (name_sim, dist_m, bonus, band)
        (1.00, 10.0, 1.0, "merge"),      # 0.983
        (1.00, 30.0, 1.0, "merge"),      # 0.950
        (0.90, 40.0, 1.0, "merge"),      # 0.873
        (1.00, 90.0, 0.0, "gray"),       # 0.700
        (0.80, 60.0, 0.0, "gray"),       # 0.630
        (0.95, 150.0, 0.0, "gray"),      # 0.570
        (0.40, 100.0, 0.0, "distinct"),  # 0.323
        (0.00, 140.0, 0.0, "distinct"),  # 0.017
        (0.55, 130.0, 0.0, "distinct"),  # 0.363
    ]
    for name_sim, dist, bonus, band in cases:
        score = bp.score_from(name_sim, dist, bonus)
        if band == "merge":
            assert score >= bp.MERGE_THRESHOLD, (name_sim, dist, bonus, score)
        elif band == "distinct":
            assert score <= bp.DISTINCT_THRESHOLD, (name_sim, dist, bonus, score)
        else:
            assert bp.DISTINCT_THRESHOLD < score < bp.MERGE_THRESHOLD, (
                name_sim, dist, bonus, score)


def test_score_pair_end_to_end_dicts():
    loves_o = {"name": "Love's Travel Stop #291", "lat": 39.7000,
               "lon": -104.9000, "brand": "Love's", "phone": "302-555-0100",
               "address": None}
    loves_f = {"name": "Love's Travel Stop #291", "lat": 39.70027,
               "lon": -104.9000, "brand": None, "phone": "+1 (302) 555-0100",
               "address": None}
    assert bp.score_pair(loves_o, loves_f) >= bp.MERGE_THRESHOLD
    unrelated = {"name": "Waffle House", "lat": 39.7001, "lon": -104.9000,
                 "brand": None, "phone": None, "address": None}
    assert bp.score_pair(loves_o, unrelated) <= bp.DISTINCT_THRESHOLD


def test_pair_bonus_brand_phone_address():
    assert bp.pair_bonus("Love's", "LOVES", None, None, None, None) == 1.0
    assert bp.pair_bonus(None, None, "302-555-0100", "+1 302 555 0100",
                         None, None) == 1.0
    assert bp.pair_bonus(None, None, None, None,
                         "100 Main St.", "100 MAIN ST") == 1.0
    assert bp.pair_bonus("Pilot", "Love's", "555", "555", "A St", "B St") == 0.0
    # empty values never count as a match
    assert bp.pair_bonus(None, None, None, None, None, None) == 0.0


# ------------------------------------------------------------------ DEF (pure)

def test_def_inferred_marker_rules():
    cfg = bp.load_def_brands()
    # brand match inside the category gate -> 'inferred'
    assert bp.def_inferred("x", "Love's", "truck_stop", cfg) == "inferred"
    assert bp.def_inferred("x", "Pilot", "fuel_station", cfg) == "inferred"
    assert bp.def_inferred("x", "TA", "truck_stop", cfg) == "inferred"
    # name-phrase match without a brand field
    assert bp.def_inferred(
        "Love's Travel Stop #291", None, "truck_stop", cfg) == "inferred"
    # category gate: a Love's-branded restaurant is NOT a DEF pump
    assert bp.def_inferred("x", "Love's", "restaurant", cfg) is None
    # exact-brand equality only: 'TA' must not fire on 'TAvern'
    assert bp.def_inferred("Tavern on the Green", "Tavern",
                           "fuel_station", cfg) is None
    # unknown brand -> None (renders unknown, never 'no')
    assert bp.def_inferred("Random Fuel", None, "fuel_station", cfg) is None


def test_def_config_carries_evidence():
    cfg = bp.load_def_brands()
    assert all(e["evidence_url"].startswith("http") for e in cfg["brands"])
    assert set(cfg["categories_gate"]) == {"truck_stop", "fuel_station"}


# --------------------------------------------------- geohash / id (pure)

def test_geohash_known_vector():
    assert bp.geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert bp.geohash_encode(57.64911, 10.40744, 7) == "u4pruyd"


def test_business_id_stability_and_derivation():
    bid = bp.business_id("Love's Travel Stop #291", 39.7392, -104.9903)
    assert bid.startswith("biz_") and len(bid) == 20
    # independently recompute per the foundation rule
    key = ("lovestravelstop291|"
           + bp.geohash_encode(39.7392, -104.9903, 7)).encode()
    assert bid == "biz_" + hashlib.sha256(key).hexdigest()[:16]
    # replayable: same inputs, same id
    assert bid == bp.business_id("Love's Travel Stop #291", 39.7392, -104.9903)
    # different cell -> different id
    assert bid != bp.business_id("Love's Travel Stop #291", 39.9, -104.9903)


def test_state_code_usps_only():
    assert bp.state_code("de") == "DE"
    assert bp.state_code("Delaware") is None
    assert bp.state_code(None) is None
    assert bp.state_code("PR") == "PR"


# ------------------------------------------------------- DB parity (needs_db)

@needs_db
def test_norm_name_sql_matches_python():
    samples = ["Love's Travel Stop #291", "PILOT TVL CTR  #42",
               "Joe's Truck & Trailer Svc", "  Plain Name  ", "A#1 Towing"]
    expr = bp.norm_name_sql("name")
    with get_conn() as conn:
        for s in samples:
            got = conn.execute(
                f"SELECT {expr} FROM (VALUES (%s)) v(name)", (s,)
            ).fetchone()[0]
            assert got == bp.norm_name(s), s


@needs_db
def test_trigram_similarity_matches_pg_trgm():
    pairs = [("love s travel stop", "loves travel stop"),
             ("bob s truck repair", "bob s truck service"),
             ("word", "two words"),
             ("casey fuel stop", "fuel stop casey")]
    with get_conn() as conn:
        for a, b in pairs:
            got = conn.execute(
                "SELECT similarity(%s, %s)", (a, b)).fetchone()[0]
            assert abs(float(got) - bp.trigram_similarity(a, b)) < 1e-6, (a, b)


@needs_db
def test_geohash_matches_st_geohash():
    points = [(39.7392, -104.9903), (57.64911, 10.40744),
              (21.3069, -157.8583), (0.001, 0.001)]
    with get_conn() as conn:
        for lat, lon in points:
            got = conn.execute(
                "SELECT ST_GeoHash(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 7)",
                (lon, lat),
            ).fetchone()[0]
            assert got == bp.geohash_encode(lat, lon, 7), (lat, lon)


# ------------------------------------------------- conflate E2E (needs_db)

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _mk_scratch(conn):
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute(f"CREATE TABLE {SCHEMA}.overture_places "
                 "(LIKE staging.overture_places INCLUDING ALL)")
    conn.execute(f"CREATE TABLE {SCHEMA}.fsq_places "
                 "(LIKE staging.fsq_places INCLUDING ALL)")
    conn.execute(f"CREATE TABLE {SCHEMA}.businesses "
                 "(LIKE core.businesses INCLUDING ALL)")


def _cleanup(conn, *, had_rescore_job: bool):
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute("DELETE FROM ops.source_runs WHERE source_id LIKE 'test_biz_%'")
    conn.execute("DELETE FROM ops.sources WHERE source_id LIKE 'test_biz_%'")
    if not had_rescore_job:
        # drop only the rescore job OUR swap enqueued, never a real one
        conn.execute(
            "DELETE FROM ops.job_queue WHERE source_id = 'quality_rescore' "
            "AND status = 'queued'")


def _ins_overture(conn, rec, name, cat, lat, lon, *, brand=None, phone=None,
                  conf=0.9):
    conn.execute(
        f"INSERT INTO {SCHEMA}.overture_places (source_record_id, name, brand,"
        " category_source, category, lat, lon, phone, src_confidence,"
        " observed_at, run_id, props) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        " 0, '{}')",
        (rec, name, brand, cat, cat, lat, lon, phone, conf, _NOW),
    )


def _ins_fsq(conn, rec, name, cat, lat, lon, *, phone=None):
    conn.execute(
        f"INSERT INTO {SCHEMA}.fsq_places (source_record_id, name,"
        " category_source, category, lat, lon, phone, observed_at, run_id,"
        " props) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, 0, '{}')",
        (rec, name, cat, cat, lat, lon, phone, _NOW),
    )


def _same_cell_pair(base_lat: float, lon: float) -> tuple[float, float]:
    """Two latitudes ~5 m apart guaranteed inside ONE geohash-7 cell."""
    lat = base_lat
    for _ in range(200):
        if (bp.geohash_encode(lat, lon, 7)
                == bp.geohash_encode(lat + 0.000045, lon, 7)):
            return lat, lat + 0.000045
        lat += 0.00005
    raise AssertionError("no same-cell pair found")


@needs_db
def test_conflate_end_to_end_scratch():
    with get_conn() as conn:
        had_job = conn.execute(
            "SELECT 1 FROM ops.job_queue WHERE source_id = 'quality_rescore' "
            "AND status IN ('queued', 'running')").fetchone() is not None
        _mk_scratch(conn)
        # clear merge: identical names, ~30 m, same phone digits
        _ins_overture(conn, "o1", "Love's Travel Stop #291", "truck_stop",
                      39.70000, -104.90000, brand="Love's",
                      phone="302-555-0100", conf=0.9)
        _ins_fsq(conn, "f1", "Love's Travel Stop #291", "truck_stop",
                 39.70027, -104.90000, phone="+1 (302) 555-0100")
        # distinct: near in space, unrelated names (blocked by trigram gate)
        _ins_overture(conn, "o2", "Bob's Truck Repair", "truck_repair",
                      39.72000, -104.90000, conf=0.8)
        _ins_fsq(conn, "f2", "Waffle House", "restaurant",
                 39.72020, -104.90000)
        # gray: identical trigram SET (word order), ~90 m, no bonus -> 0.70
        _ins_overture(conn, "o3", "Casey Fuel Stop", "fuel_station",
                      39.74000, -104.90000, conf=0.9)
        _ins_fsq(conn, "f3", "Fuel Stop Casey", "fuel_station",
                 39.74081, -104.90000)
        # same-source cell collision: one deterministic survivor
        la, lb = _same_cell_pair(39.76000, -104.90000)
        _ins_overture(conn, "o4", "Dup Depot", "truck_stop", la, -104.90000,
                      conf=0.9)
        _ins_overture(conn, "o5", "Dup Depot", "truck_stop", lb, -104.90000,
                      conf=0.5)
    try:
        published = bp.run_conflate(
            target=f"{SCHEMA}.businesses",
            staging_overture=f"{SCHEMA}.overture_places",
            staging_fsq=f"{SCHEMA}.fsq_places",
            build_table=f"{SCHEMA}.conflate_build",
            source_id=CONFLATE_SRC,
        )
        # o1+f1 merged; o2, f2, o3, f3 distinct; o4/o5 -> 1 survivor
        assert published == 6
        with get_conn() as conn:
            rows = {
                r[1]: r for r in conn.execute(
                    f"SELECT business_id, name, category, brand, present_in,"
                    f" def, confidence, conf_agree, flags, props"
                    f" FROM {SCHEMA}.businesses").fetchall()
            }
            assert len(rows) == 6
            loves = rows["Love's Travel Stop #291"]
            assert sorted(loves[4]) == ["fsq", "overture"]  # present_in
            assert loves[5] == "inferred"                   # §6 DEF marker
            assert loves[7] == 100                          # A=1.0 corroborated
            assert set(loves[9]) >= {"overture", "fsq"}     # both blobs kept
            assert loves[0].startswith("biz_") and len(loves[0]) == 20
            # gray pair: both kept distinct, both flagged
            for name in ("Casey Fuel Stop", "Fuel Stop Casey"):
                assert rows[name][4] in (["overture"], ["fsq"])
                assert "dedup_gray_zone" in rows[name][8]
                assert rows[name][7] == 50                  # single-source A
            # unrelated names never paired
            assert rows["Waffle House"][4] == ["fsq"]
            assert rows["Bob's Truck Repair"][4] == ["overture"]
            assert rows["Bob's Truck Repair"][8] == []      # no gray flag
            # collision survivor flagged, drop counted in the run message
            assert "cell_name_collision" in rows["Dup Depot"][8]
            msg = conn.execute(
                "SELECT message FROM ops.source_runs WHERE source_id = %s "
                "ORDER BY run_id DESC LIMIT 1", (CONFLATE_SRC,)
            ).fetchone()[0]
            assert "cell_collision_drops=1" in msg
            assert "merged=1" in msg
            assert "def_inferred=1" in msg
    finally:
        with get_conn() as conn:
            _cleanup(conn, had_rescore_job=had_job)


@needs_db
def test_resolve_collisions_cross_source_cell_merge():
    """The cross-source branch of the business_id-cell resolution: identical
    squeezed name + same cell from different single sources -> merged with
    flag 'cell_name_merge' (module-documented rule)."""
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        build = f"{SCHEMA}.build"
        conn.execute(f"CREATE TABLE {build} ({bp._BUILD_COLS})")
        ins = (f"INSERT INTO {build} (src, name, category, lat, lon, phone,"
               " observed_at, present_in, props) VALUES"
               " (%s, %s, 'truck_stop', %s, %s, %s, %s, %s, %s)")
        conn.execute(ins, ("overture", "Solo Stop", 39.5, -104.5, "111",
                           _NOW, ["overture"],
                           Jsonb({"overture": {"src_confidence": 0.9}})))
        conn.execute(ins, ("fsq", "Solo Stop", 39.5, -104.5, None,
                           _NOW, ["fsq"], Jsonb({"fsq": {}})))
        # 3-row same-source group -> deterministic survivor
        for i in range(3):
            conn.execute(ins, ("overture", "Triple T", 39.6, -104.6, None,
                               _NOW, ["overture"], Jsonb({"overture": {}})))
        merges, drops = bp._resolve_collisions(conn, build)
        assert (merges, drops) == (1, 2)
        solo = conn.execute(
            f"SELECT src, present_in, cell_flag, phone, props FROM {build} "
            "WHERE name = 'Solo Stop'").fetchall()
        assert len(solo) == 1
        assert solo[0][0] == "merged"
        assert sorted(solo[0][1]) == ["fsq", "overture"]
        assert solo[0][2] == "cell_name_merge"
        assert solo[0][3] == "111"                 # canonical keeps its phone
        assert set(solo[0][4]) == {"overture", "fsq"}
        triple = conn.execute(
            f"SELECT cell_flag FROM {build} WHERE name = 'Triple T'"
        ).fetchall()
        assert len(triple) == 1 and triple[0][0] == "cell_name_collision"
        conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")


# ------------------------------------------------ live pulls (network-marked)

@needs_network
def test_discover_overture_release_live():
    rel = bp.discover_overture_release()
    assert len(rel) >= 10 and rel[:4].isdigit() and rel[4] == "-"


@needs_network
def test_discover_fsq_release_live():
    rel = bp.discover_fsq_release()
    assert len(rel) == 10 and rel[:4].isdigit()


@needs_network
def test_discover_fsq_mirror_release_live():
    rel = bp.discover_fsq_mirror_release()
    assert len(rel) == 10 and rel >= "2024-11-19"


@needs_network
@needs_db
def test_pull_overture_live_small_bbox():
    """Real S3 read, Delaware-ish bbox, capped rows, scratch staging clone."""
    src_id = "test_biz_overture_pull"
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.overture_places "
                     "(LIKE staging.overture_places INCLUDING ALL)")
    try:
        n = bp.pull_overture(
            bbox=(-75.9, 38.4, -74.9, 39.9), max_rows=200,
            count_unmapped=False,
            staging_table=f"{SCHEMA}.overture_places", source_id=src_id)
        assert n > 0
        with get_conn() as conn:
            cats = {r[0] for r in conn.execute(
                f"SELECT DISTINCT category FROM {SCHEMA}.overture_places"
            ).fetchall()}
            assert cats <= set(bp.CATEGORY_SLUGS)
            status, msg = conn.execute(
                "SELECT status, message FROM ops.source_runs "
                "WHERE source_id = %s ORDER BY run_id DESC LIMIT 1",
                (src_id,)).fetchone()
            assert status == "success"
            assert "release=" in msg
    finally:
        with get_conn() as conn:
            _cleanup(conn, had_rescore_job=True)


@needs_network
@needs_db
def test_pull_fsq_mirror_live_small_bbox():
    """Anonymous source.coop mirror read (frozen release, honestly labeled)."""
    src_id = "test_biz_fsq_pull"
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.fsq_places "
                     "(LIKE staging.fsq_places INCLUDING ALL)")
    try:
        n = bp.pull_fsq(
            bbox=(-75.9, 38.4, -74.9, 39.9), max_rows=200, mirror=True,
            count_probe=False,
            staging_table=f"{SCHEMA}.fsq_places", source_id=src_id)
        assert n > 0
        with get_conn() as conn:
            row = conn.execute(
                f"SELECT category, observed_at FROM {SCHEMA}.fsq_places "
                "LIMIT 1").fetchone()
            assert row[0] in bp.CATEGORY_SLUGS
            assert row[1] is not None  # vintage, never the load date
    finally:
        with get_conn() as conn:
            _cleanup(conn, had_rescore_job=True)
