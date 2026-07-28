"""OSM POI extraction tests (scripts/osm_extract.py --job pois).

Three layers:
- pure tag-parsing tests (classify / tri-states / state code): no files, no DB
- synthetic-PBF tests: tiny in-test PBFs built with osmium.SimpleWriter
  (fuel node with diesel yes/no/absent, rest area, weighbridge, fuel WAY
  whose centroid must resolve through the DISK node cache)
- full-run tests against data/pbf/delaware-latest.osm.pbf into a SCRATCH
  schema (LIKE osm.* clones) — live core/osm tables are never written.

Runner tests follow the test_quality.py pattern: run rows land under a
synthetic source id and every ops row created is deleted afterwards.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import osmium
import pytest
from osmium.osm import mutable

from tests.conftest import needs_db
from truckintel.db import get_conn

REPO_ROOT = Path(__file__).resolve().parents[1]
DELAWARE_PBF = REPO_ROOT / "data" / "pbf" / "delaware-latest.osm.pbf"

SCHEMA = "scratch_osm_extract_test"
SRC = "test_osm_pois_src"

STAMP = "2026-07-21T20:21:50Z"
STAMP_DT = datetime(2026, 7, 21, 20, 21, 50, tzinfo=timezone.utc)


def _load_extract():
    """scripts/ is not a package — load the runner by path."""
    spec = importlib.util.spec_from_file_location(
        "osm_extract", REPO_ROOT / "scripts" / "osm_extract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ox = _load_extract()


# ------------------------------------------------------------- pure: parsing

def test_classify_matches_the_three_kinds():
    assert ox.classify({"amenity": "fuel"}) == "fuel"
    assert ox.classify({"highway": "rest_area"}) == "rest"
    assert ox.classify({"highway": "services"}) == "rest"
    assert ox.classify({"amenity": "weighbridge"}) == "weigh"
    assert ox.classify({"man_made": "weighbridge"}) == "weigh"
    assert ox.classify({"highway": "weigh_station"}) == "weigh"
    assert ox.classify({"amenity": "cafe"}) is None
    assert ox.classify({"highway": "primary"}) is None
    # a fuel station inside a service area is still a fuel station
    assert ox.classify({"amenity": "fuel", "highway": "services"}) == "fuel"


def test_has_diesel_tristate_never_defaults_false():
    assert ox.has_diesel({"fuel:diesel": "yes"}) is True
    assert ox.has_diesel({"fuel:diesel": "no"}) is False
    assert ox.has_diesel({}) is None                    # absent = unknown
    assert ox.has_diesel({"fuel:diesel": "maybe"}) is None
    assert ox.has_diesel({"fuel:HGV_diesel": "yes"}) is True  # truck lanes sell diesel


def test_hgv_access_tristate():
    assert ox.hgv_access({"hgv": "yes"}) is True
    assert ox.hgv_access({"hgv": "designated"}) is True
    assert ox.hgv_access({"hgv": "no"}) is False
    assert ox.hgv_access({}) is None
    assert ox.hgv_access({"fuel:HGV_diesel": "yes"}) is True


def test_has_def_tristate_sparse_means_unknown():
    assert ox.has_def({"fuel:adblue": "yes"}) is True
    assert ox.has_def({"fuel:adblue": "no"}) is False
    assert ox.has_def({}) is None


def test_state_code_valid_usps_only():
    assert ox.state_code({"addr:state": "DE"}) == "DE"
    assert ox.state_code({"addr:state": "de"}) == "DE"
    assert ox.state_code({"addr:state": "Delaware"}) is None  # raw stays in props
    assert ox.state_code({}) is None


# ------------------------------------------------------- synthetic-PBF layer

def _write_pbf(path: Path, *, header_stamp: str | None = STAMP) -> Path:
    """Tiny deterministic PBF: 3 fuel nodes (diesel yes/no/absent), rest area,
    weighbridge, one fuel WAY over untagged nodes (centroid test), a plain
    truck-repair shop, a car-repair shop that declares a truck capability, and
    a fuel station that is ALSO a repair shop (the overlap case)."""
    header = osmium.io.Header()
    if header_stamp:
        header.set("osmosis_replication_timestamp", header_stamp)
    w = osmium.SimpleWriter(str(path), header=header)
    w.add_node(mutable.Node(id=1, location=(-75.50, 39.70), tags={
        "amenity": "fuel", "name": "Truck Alpha", "brand": "Pilot",
        "fuel:diesel": "yes", "hgv": "yes", "fuel:adblue": "yes",
        "opening_hours": "24/7", "addr:state": "DE"}))
    w.add_node(mutable.Node(id=2, location=(-75.51, 39.71), tags={
        "amenity": "fuel", "name": "No Diesel Here", "fuel:diesel": "no"}))
    w.add_node(mutable.Node(id=3, location=(-75.52, 39.72), tags={
        "amenity": "fuel", "name": "Untagged Fuel"}))
    w.add_node(mutable.Node(id=4, location=(-75.53, 39.73), tags={
        "highway": "rest_area", "name": "I-95 Rest"}))
    w.add_node(mutable.Node(id=5, location=(-75.54, 39.74), tags={
        "amenity": "weighbridge"}))
    w.add_node(mutable.Node(id=6, location=(-75.55, 39.75), tags={
        "amenity": "cafe", "name": "Not A POI We Want"}))
    w.add_node(mutable.Node(id=7, location=(-75.56, 39.76), tags={
        "shop": "truck_repair", "name": "Big Rig Repair",
        "opening_hours": "Mo-Fr 08:00-18:00", "addr:state": "DE"}))
    w.add_node(mutable.Node(id=8, location=(-75.57, 39.77), tags={
        "shop": "car_repair", "name": "Capability Tagged",
        "service:vehicle:trailer_repair": "yes"}))
    # amenity=fuel AND shop=truck_repair: must appear in BOTH layers, because
    # classify() is exclusive but repair is additive.
    w.add_node(mutable.Node(id=9, location=(-75.58, 39.78), tags={
        "amenity": "fuel", "shop": "truck_repair", "name": "Speedco-ish",
        "fuel:diesel": "yes"}))
    # untagged geometry nodes for the fuel way (locations resolve via the
    # disk node cache, not RAM)
    w.add_node(mutable.Node(id=10, location=(-75.60, 39.80)))
    w.add_node(mutable.Node(id=11, location=(-75.62, 39.82)))
    w.add_way(mutable.Way(id=100, nodes=[10, 11], tags={
        "amenity": "fuel", "name": "Way Fuel", "fuel:diesel": "yes"}))
    w.close()
    return path


@pytest.fixture()
def mini_pbf(tmp_path):
    return _write_pbf(tmp_path / "mini.osm.pbf")


def test_collect_pois_synthetic(mini_pbf, tmp_path):
    rows, stats = ox.collect_pois(mini_pbf, node_cache=tmp_path / "cache.bin")
    by_id = {r["osm_id"]: r for kind in rows.values() for r in kind}

    assert len(rows["fuel"]) == 5 and len(rows["rest"]) == 1 and len(rows["weigh"]) == 1
    assert len(rows["repair"]) == 3
    assert "node/6" not in by_id  # the cafe never matches

    # node/9 is both a fuel station and a repair shop — the repair layer must
    # not steal it out of the fuel layer.
    assert "node/9" in {r["osm_id"] for r in rows["fuel"]}
    assert "node/9" in {r["osm_id"] for r in rows["repair"]}
    rep = {r["osm_id"]: r for r in rows["repair"]}
    assert rep["node/7"]["truck_repair"] is True
    assert rep["node/7"]["props"]["opening_hours"] == "Mo-Fr 08:00-18:00"
    assert rep["node/8"]["trailer_repair"] is True
    assert rep["node/8"]["truck_repair"] is None   # tri-state: unstated stays unknown

    alpha = by_id["node/1"]
    assert alpha["has_diesel"] is True
    assert alpha["hgv_access"] is True
    assert alpha["has_def"] is True
    assert alpha["brand"] == "Pilot"
    assert alpha["state"] == "DE"
    assert alpha["props"]["opening_hours"] == "24/7"  # no column — props carries it
    assert by_id["node/2"]["has_diesel"] is False
    # tri-state honesty: absent tag stays None, never False
    assert by_id["node/3"]["has_diesel"] is None
    assert by_id["node/3"]["has_def"] is None

    # way centroid resolved through the disk node cache
    way = by_id["way/100"]
    assert way["lat"] == pytest.approx(39.81, abs=1e-6)
    assert way["lon"] == pytest.approx(-75.61, abs=1e-6)
    assert stats["ways"] == 1 and stats["ways_skipped_no_location"] == 0

    # observed_at = the PBF's replication timestamp, never the load date
    assert stats["observed_at"] == STAMP_DT
    assert stats["observed_at_basis"] == "pbf_replication_timestamp"
    assert all(r["observed_at"] == STAMP_DT for r in by_id.values())


def test_observed_at_falls_back_to_mtime_documented(tmp_path):
    pbf = _write_pbf(tmp_path / "nostamp.osm.pbf", header_stamp=None)
    observed, basis = ox.pbf_observed_at(pbf)
    assert observed == datetime.fromtimestamp(pbf.stat().st_mtime, tz=timezone.utc)
    assert "mtime" in basis  # the basis is documented, honestly


# ------------------------------------------------------------- DB-backed run

@pytest.fixture()
def scratch():
    """Scratch schema with LIKE-clones of the four osm.* targets, plus ops
    cleanup — live osm/core tables are never touched.

    Every kind in POIS_TARGETS must appear here. A kind missing from this map
    falls through to its LIVE table, so an omission does not fail the test —
    it writes to production.
    """
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        for table in ("fuel_stations", "rest_areas", "weigh_points",
                      "truck_repair"):
            conn.execute(
                f"CREATE TABLE {SCHEMA}.{table} "
                f"(LIKE osm.{table} INCLUDING ALL)"
            )
    yield {
        "fuel": f"{SCHEMA}.fuel_stations",
        "rest": f"{SCHEMA}.rest_areas",
        "weigh": f"{SCHEMA}.weigh_points",
        "repair": f"{SCHEMA}.truck_repair",
    }
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute("DELETE FROM ops.source_runs WHERE source_id = %s", (SRC,))
        conn.execute("DELETE FROM ops.sources WHERE source_id = %s", (SRC,))


@needs_db
def test_run_pois_synthetic_end_to_end(mini_pbf, tmp_path, scratch):
    published = ox.run_pois(mini_pbf, targets=scratch, source_id=SRC,
                            node_cache=tmp_path / "cache.bin")
    # 'repair' is deliberately NOT in the default set: osm.truck_repair is
    # owned by the DAILY Overpass job, and republishing it here from a weekly
    # PBF snapshot would push fresher rows backwards every Sunday.
    assert published == {"fuel": 5, "rest": 1, "weigh": 1}

    with get_conn() as conn:
        fuel = conn.execute(
            f"SELECT osm_id, has_diesel, has_def, state, observed_at, "
            f"       props ->> 'opening_hours' AS oh, "
            f"       ST_X(geom) AS lon, ST_Y(geom) AS lat "
            f"FROM {scratch['fuel']} ORDER BY osm_id"
        ).fetchall()
        by_id = {r[0]: r for r in fuel}
        assert set(by_id) == {"node/1", "node/2", "node/3", "node/9", "way/100"}
        assert by_id["node/1"][1] is True and by_id["node/1"][3] == "DE"
        assert by_id["node/1"][5] == "24/7"
        assert by_id["node/3"][1] is None          # tri-state survives the swap
        assert by_id["node/3"][2] is None
        assert by_id["way/100"][6] == pytest.approx(-75.61)
        assert all(r[4] == STAMP_DT for r in fuel)  # vintage, not load date

        status, message, rows_published = conn.execute(
            "SELECT status, message, rows_published FROM ops.source_runs "
            "WHERE source_id = %s ORDER BY run_id DESC LIMIT 1", (SRC,)
        ).fetchone()
    assert status == "success"
    assert rows_published == 7   # 5 fuel + 1 rest + 1 weigh (repair is not default)
    assert "pbf_replication_timestamp" in message


@needs_db
def test_run_pois_missing_pbf_fails_honestly(tmp_path, scratch):
    with pytest.raises(FileNotFoundError):
        ox.run_pois(tmp_path / "nope.osm.pbf", targets=scratch, source_id=SRC)
    # no run row was even started for a nonexistent input
    with get_conn() as conn:
        n = conn.execute(
            "SELECT count(*) FROM ops.source_runs WHERE source_id = %s", (SRC,)
        ).fetchone()[0]
    assert n == 0


@needs_db
@pytest.mark.skipif(not DELAWARE_PBF.exists(),
                    reason="data/pbf/delaware-latest.osm.pbf not downloaded")
def test_run_pois_delaware_full_extract(tmp_path, scratch):
    """Real-extract regression: the Delaware PBF (2026-07-21 vintage) carries
    25 amenity=fuel NODES (plus ~277 way-centroids). Bounds are loose enough
    to survive normal OSM edit drift on a re-downloaded extract."""
    published = ox.run_pois(DELAWARE_PBF, targets=scratch, source_id=SRC,
                            node_cache=tmp_path / "de.nodecache")
    assert published["fuel"] >= 100  # 302 at vintage 2026-07-21
    with get_conn() as conn:
        nodes, ways = conn.execute(
            f"SELECT count(*) FILTER (WHERE osm_id LIKE 'node/%%'), "
            f"       count(*) FILTER (WHERE osm_id LIKE 'way/%%') "
            f"FROM {scratch['fuel']}"
        ).fetchone()
        diesel_known = conn.execute(
            f"SELECT count(*) FROM {scratch['fuel']} WHERE has_diesel IS NOT NULL"
        ).fetchone()[0]
        rest, weigh = conn.execute(
            f"SELECT (SELECT count(*) FROM {scratch['rest']}), "
            f"       (SELECT count(*) FROM {scratch['weigh']})"
        ).fetchone()
        vintages = conn.execute(
            f"SELECT DISTINCT observed_at FROM {scratch['fuel']}"
        ).fetchall()
    assert 10 <= nodes <= 60          # ~25 fuel nodes land (task-checked: 25)
    assert ways >= 100                # way-centroids resolved via disk cache
    assert diesel_known >= 5          # a few stations tag fuel:diesel (10 at vintage)
    assert rest >= 2 and weigh >= 1   # 4 rest areas / 6 weigh points at vintage
    # one honest vintage across the whole extract = the PBF replication stamp
    assert len(vintages) == 1 and vintages[0][0] is not None
    assert not (tmp_path / "de.nodecache").exists()  # cache cleaned up


@needs_db
def test_repair_still_publishes_when_asked_for_explicitly(mini_pbf, tmp_path, scratch):
    """The PBF path for truck repair is the FALLBACK, not the schedule.

    It must keep working — if Overpass is ever unavailable, `--only repair` is
    how the layer gets rebuilt — so removing it from the default set must not
    quietly remove the capability.
    """
    published = ox.run_pois(mini_pbf, targets=scratch, source_id=SRC,
                            node_cache=tmp_path / "cache2.bin",
                            only=("repair",))
    assert published == {"repair": 3}
    with get_conn() as conn:
        n = conn.execute(f"SELECT count(*) FROM {scratch['repair']}").fetchone()[0]
    assert n == 3
