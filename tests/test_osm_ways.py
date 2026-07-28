"""OSM ways track tests (ruling §3.1-5): unit parsers (table-driven), row
mapping honesty (tri-state flags, unparseable -> NULL + raw in props),
synthetic mini-PBF end-to-end into a scratch schema, and a full Delaware
extract run (skipped when data/pbf/delaware-latest.osm.pbf is absent).

Scratch-schema discipline: osm.ways itself is NEVER written; targets are
LIKE-cloned tables in throwaway schemas. ops.source_runs rows created here are
deleted by run_id on cleanup (only ours — real audit history is untouched).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import needs_db
from truckintel.db import get_conn

REPO_ROOT = Path(__file__).resolve().parent.parent
DELAWARE_PBF = REPO_ROOT / "data" / "pbf" / "delaware-latest.osm.pbf"


def _load_module():
    """scripts/ is not a package — load the job module by path."""
    spec = importlib.util.spec_from_file_location(
        "osm_ways_job", REPO_ROOT / "scripts" / "osm_ways_job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


owj = _load_module()


# ---------------------------------------------------------------- unit parsers

# (raw tag value, expected inches) — None = unparseable, honest NULL.
LENGTH_CASES = [
    ("13'6\"", 162.0),          # feet'inches"
    ("12'0\"", 144.0),          # zero inches component is valid (Delaware-real)
    ("14'", 168.0),             # bare feet
    ("6' 6\"", 78.0),           # spaced
    ("13ft6in", 162.0),         # squashed unit words
    ("13 ft", 156.0),
    ("78\"", 78.0),             # plain inches
    ("4.1", 161.4),             # bare number = meters (OSM default)
    ("4.1 m", 161.4),
    ("4.1m", 161.4),
    ("4,1", 161.4),             # decimal comma
    ("420 cm", 165.4),
    ("2 meters", 78.7),
    ("default", None),          # signage words, never numbers
    ("none", None),
    ("unsigned", None),
    ("below_default", None),
    ("physical", None),
    ("tall", None),
    ("", None),
    (None, None),
    ("-4", None),               # negative = junk
    ("0", None),                # zero total = junk
    ("0'0\"", None),
    ("3.5.1", None),            # malformed number
    ("4;5", None),              # multi-value
    ("~4", None),
]

# (raw tag value, expected pounds)
WEIGHT_CASES = [
    ("15 t", 33069.0),          # metric tonnes
    ("15t", 33069.0),
    ("15", 33069.0),            # bare number = tonnes (OSM default)
    ("7.5", 16535.0),
    ("26000 lbs", 26000.0),
    ("26000lbs", 26000.0),
    ("26,000 lbs", 26000.0),    # thousands comma
    ("26000 lb", 26000.0),
    ("3500 kg", 7716.0),
    ("10 st", 20000.0),         # OSM short tons
    ("3 lt", 6720.0),           # OSM long tons
    ("none", None),
    ("unsigned", None),
    ("", None),
    (None, None),
    ("15 bananas", None),       # unknown unit -> NULL, never guessed
    ("23000 lbs; 10.4 t", None),  # multi-value -> NULL, raw stays in props
    ("-15 t", None),
    ("0", None),
]


@pytest.mark.parametrize("raw,expected", LENGTH_CASES)
def test_parse_length_in(raw, expected):
    assert owj.parse_length_in(raw) == expected


@pytest.mark.parametrize("raw,expected", WEIGHT_CASES)
def test_parse_weight_lb(raw, expected):
    assert owj.parse_weight_lb(raw) == expected


def test_length_overflow_is_null_never_truncated():
    assert owj.parse_length_in("99999999") is None       # > NUMERIC(6,1)
    assert owj.parse_weight_lb("999999999 t") is None    # > NUMERIC(9,0)


# ---------------------------------------------------------------- row mapping

COORDS = [(-75.5, 39.7), (-75.51, 39.71)]


def test_way_row_parses_and_keeps_full_tags():
    tags = {"highway": "primary", "name": "Test Rd", "ref": "US 13",
            "oneway": "yes", "maxheight": "13'6\"", "maxweight": "15 t",
            "hgv": "designated", "surface": "asphalt"}
    row = owj.way_row(100, tags, COORDS)
    assert row["way_id"] == 100 and row["highway"] == "primary"
    assert row["oneway"] == "yes" and row["hgv"] == "designated"
    assert row["maxheight_in"] == 162.0
    assert row["maxweight_lb"] == 33069.0
    assert row["props"] == tags            # FULL tag map, raw values kept
    assert row["geom_wkt"].startswith("LINESTRING(-75.5")
    assert row["flags"] == []


def test_way_row_unparseable_dimension_is_null_with_raw_in_props():
    row = owj.way_row(1, {"highway": "service", "maxheight": "tall"}, COORDS)
    assert row["maxheight_in"] is None
    assert row["props"]["maxheight"] == "tall"   # never guessed, never lost


def test_way_row_tristate_bridge_tunnel():
    absent = owj.way_row(1, {"highway": "primary"}, COORDS)
    assert absent["bridge"] is None and absent["tunnel"] is None  # unknown
    yes = owj.way_row(2, {"highway": "primary", "bridge": "viaduct",
                          "tunnel": "yes"}, COORDS)
    assert yes["bridge"] is True and yes["tunnel"] is True
    no = owj.way_row(3, {"highway": "primary", "bridge": "no",
                         "tunnel": "no"}, COORDS)
    assert no["bridge"] is False and no["tunnel"] is False


def test_way_row_partial_geom_flag():
    assert owj.way_row(1, {"highway": "primary"}, COORDS,
                       partial_geom=True)["flags"] == ["partial_geom"]


def test_highway_allowlist_is_drivable_public_only():
    # §3.1-5: conflation substrate — drivable public classes + _link variants.
    assert "motorway" in owj.HIGHWAY_CLASSES
    assert "service" in owj.HIGHWAY_CLASSES
    assert "motorway_link" in owj.HIGHWAY_CLASSES
    for excluded in ("footway", "cycleway", "path", "track", "steps",
                     "bridleway", "construction", "proposed"):
        assert excluded not in owj.HIGHWAY_CLASSES


# ------------------------------------------------------- synthetic PBF -> DB

MINI_SCHEMA = "scratch_osmways_mini"


def _write_mini_pbf(path: Path) -> None:
    import osmium
    writer = osmium.SimpleWriter(str(path))
    try:
        for nid, lon, lat in [(1, -75.50, 39.70), (2, -75.51, 39.71),
                              (3, -75.52, 39.72)]:
            writer.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat)))
        # kept: allow-listed class, parsed dimensions
        writer.add_way(osmium.osm.mutable.Way(
            id=100, nodes=[1, 2, 3],
            tags={"highway": "primary", "name": "Main St", "ref": "US 13",
                  "oneway": "yes", "maxheight": "13'6\"", "maxweight": "15 t",
                  "maxwidth": "junk", "hgv": "designated", "bridge": "yes"}))
        # excluded: non-drivable class
        writer.add_way(osmium.osm.mutable.Way(
            id=101, nodes=[1, 2], tags={"highway": "footway"}))
        # kept with partial geometry: node 999 missing from the extract
        writer.add_way(osmium.osm.mutable.Way(
            id=102, nodes=[1, 2, 999], tags={"highway": "service"}))
        # skipped: < 2 locatable nodes
        writer.add_way(osmium.osm.mutable.Way(
            id=103, nodes=[3, 998], tags={"highway": "residential"}))
        # excluded: no highway tag at all (KeyFilter)
        writer.add_way(osmium.osm.mutable.Way(
            id=104, nodes=[1, 2], tags={"waterway": "river"}))
    finally:
        writer.close()


@pytest.fixture
def mini_scratch():
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {MINI_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {MINI_SCHEMA}")
        conn.execute(
            f"CREATE TABLE {MINI_SCHEMA}.ways (LIKE osm.ways INCLUDING ALL)")
    run_ids: list[int] = []
    yield run_ids
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {MINI_SCHEMA} CASCADE")
        if run_ids:  # only OUR audit rows — real history untouched
            conn.execute("DELETE FROM ops.source_runs WHERE run_id = ANY(%s)",
                         (run_ids,))


@needs_db
def test_mini_pbf_end_to_end(mini_scratch, tmp_path):
    pbf = tmp_path / "mini.osm.pbf"
    _write_mini_pbf(pbf)
    summary = owj.run_ways_job(pbf, f"{MINI_SCHEMA}.ways", progress_every=0)
    mini_scratch.append(summary["run_id"])

    assert summary["published"] == 2          # ways 100 + 102
    assert summary["skipped_class"] == 1      # footway
    assert summary["skipped_geom"] == 1       # way 103
    assert summary["observed_at"] is None     # synthetic PBF: no repl header

    with get_conn() as conn:
        rows = {r[0]: r for r in conn.execute(
            f"SELECT way_id, highway, name, ref, oneway, maxheight_in, "
            f"       maxweight_lb, maxwidth_in, hgv, bridge, tunnel, flags, "
            f"       props, source_id, run_id, observed_at, "
            f"       ST_SRID(geom), ST_NPoints(geom) "
            f"FROM {MINI_SCHEMA}.ways").fetchall()}
    assert set(rows) == {100, 102}

    full = rows[100]
    assert full[1:5] == ("primary", "Main St", "US 13", "yes")
    assert float(full[5]) == 162.0            # 13'6" -> inches
    assert float(full[6]) == 33069.0          # 15 t -> lbs
    assert full[7] is None                    # 'junk' maxwidth -> honest NULL
    assert full[12]["maxwidth"] == "junk"     # ... raw preserved in props
    assert full[8] == "designated" and full[9] is True and full[10] is None
    assert full[11] == [] and full[13] == "osm_ways"
    assert full[14] == summary["run_id"] and full[15] is None
    assert full[16] == 4326 and full[17] == 3

    partial = rows[102]
    assert partial[11] == ["partial_geom"]    # missing node flagged, not hidden
    assert partial[17] == 2

    with get_conn() as conn:
        run = conn.execute(
            "SELECT source_id, status, rows_published FROM ops.source_runs "
            "WHERE run_id = %s", (summary["run_id"],)).fetchone()
    assert run == ("osm_ways", "success", 2)


@needs_db
def test_mini_pbf_rerun_fully_replaces_snapshot(mini_scratch, tmp_path):
    pbf = tmp_path / "mini.osm.pbf"
    _write_mini_pbf(pbf)
    s1 = owj.run_ways_job(pbf, f"{MINI_SCHEMA}.ways", progress_every=0)
    s2 = owj.run_ways_job(pbf, f"{MINI_SCHEMA}.ways", progress_every=0)
    mini_scratch.extend([s1["run_id"], s2["run_id"]])
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT run_id FROM {MINI_SCHEMA}.ways").fetchall()
    assert rows == [(s2["run_id"],)]          # atomic replace, single lineage


@needs_db
def test_run_ways_adapter_matches_osm_extract_contract(mini_scratch, tmp_path):
    """scripts/osm_extract.py --job ways calls run_ways(pbf, *, node_cache,
    keep_cache) — the adapter must accept that exact shape and honor
    keep_cache for an external node-cache path."""
    pbf = tmp_path / "mini.osm.pbf"
    _write_mini_pbf(pbf)
    cache = tmp_path / "custom.nodecache"
    summary = owj.run_ways(pbf, node_cache=cache, keep_cache=False,
                           target=f"{MINI_SCHEMA}.ways")
    mini_scratch.append(summary["run_id"])
    assert summary["published"] == 2
    assert not cache.exists()                 # keep_cache=False -> removed

    cache2 = tmp_path / "kept.nodecache"
    summary2 = owj.run_ways(pbf, node_cache=cache2, keep_cache=True,
                            target=f"{MINI_SCHEMA}.ways")
    mini_scratch.append(summary2["run_id"])
    assert cache2.exists()                    # keep_cache=True -> preserved


def test_missing_pbf_raises_before_any_run_row():
    # The input check precedes _start_run — no phantom audit rows for typos.
    with pytest.raises(FileNotFoundError):
        owj.run_ways_job("data/pbf/does-not-exist.osm.pbf",
                         f"{MINI_SCHEMA}.ways")


# ------------------------------------------------------------- Delaware run

DE_SCHEMA = "scratch_osmways_de"


@pytest.fixture
def delaware_scratch():
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {DE_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {DE_SCHEMA}")
        conn.execute(
            f"CREATE TABLE {DE_SCHEMA}.ways (LIKE osm.ways INCLUDING ALL)")
    run_ids: list[int] = []
    yield run_ids
    with get_conn() as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {DE_SCHEMA} CASCADE")
        if run_ids:
            conn.execute("DELETE FROM ops.source_runs WHERE run_id = ANY(%s)",
                         (run_ids,))
    # Remove the spool this test created, next to the Delaware PBF.
    #
    # osm_ways_job._cleanup_workdir KEEPS any workdir whose phase A completed,
    # whatever keep_workdir says — a deliberate rule, and the right one: it was
    # added after a clean 3.4-hour US pass was destroyed by a failure path on
    # 2026-07-23, and a completed pass is not cheap to redo.
    #
    # But the Delaware pass finishes in seconds, so its spool is worth nothing
    # and the rule just accumulates 58 MB per test run into data/pbf/. That is
    # how 25 workdirs and 26 GB piled up before 2026-07-28. Fixed HERE rather
    # than by weakening the production rule: the test owns what the test made.
    import shutil
    for spool in DELAWARE_PBF.parent.glob(
            f".osmways-work-{DELAWARE_PBF.stem}-run*"):
        shutil.rmtree(spool, ignore_errors=True)


@needs_db
@pytest.mark.skipif(not DELAWARE_PBF.is_file(),
                    reason="delaware-latest.osm.pbf not downloaded")
def test_delaware_full_run(delaware_scratch):
    summary = owj.run_ways_job(DELAWARE_PBF, f"{DE_SCHEMA}.ways",
                               progress_every=0)
    delaware_scratch.append(summary["run_id"])

    # Plausibility band for Delaware's drivable network (measured 109,777 on
    # the 2026-07-21 extract) — wide enough to survive normal OSM growth.
    assert 90_000 <= summary["published"] <= 150_000
    assert summary["peak_rss_mb"] < 1024      # disk index: RAM stays flat

    with get_conn() as conn:
        classes = {r[0] for r in conn.execute(
            f"SELECT DISTINCT highway FROM {DE_SCHEMA}.ways").fetchall()}
        assert classes <= owj.HIGHWAY_CLASSES

        # observed_at = the PBF's replication timestamp on every row.
        obs = conn.execute(
            f"SELECT DISTINCT observed_at FROM {DE_SCHEMA}.ways").fetchall()
        assert len(obs) == 1
        assert obs[0][0] == owj.replication_timestamp(DELAWARE_PBF)

        # Spot-check: stored maxheight_in must equal re-parsing the raw tag
        # kept in props (self-consistent, robust to extract updates).
        checked = conn.execute(
            f"SELECT maxheight_in, props->>'maxheight' FROM {DE_SCHEMA}.ways "
            f"WHERE maxheight_in IS NOT NULL LIMIT 25").fetchall()
        assert len(checked) >= 10             # DE tags plenty of bridges
        for stored, raw in checked:
            assert float(stored) == owj.parse_length_in(raw)


# ------------------------------------- phase-B replay + disk headroom guard
#
# Regression cover for the 2026-07-23 US incident: a clean 3.4 h osmium pass
# was lost when COPY hit DiskFull mid-load and the failure path rmtree'd the
# workdir. The guard refuses the swap up front; --from-spool replays phase B
# from a kept workdir; and a resumed run never deletes a spool it was handed.

def test_resolve_spool_accepts_workdir_or_file(tmp_path):
    workdir = tmp_path / ".osmways-work-x-run1"
    workdir.mkdir()
    spool = workdir / owj.SPOOL_NAME
    spool.write_bytes(b"")
    assert owj.resolve_spool(workdir) == spool     # a workdir
    assert owj.resolve_spool(spool) == spool       # or the spool itself


def test_resolve_spool_missing_raises_loudly(tmp_path):
    # A mistyped resume path must fail, never silently redo the 3-4 h pass.
    with pytest.raises(FileNotFoundError, match="no phase-A spool"):
        owj.resolve_spool(tmp_path / "nope")


def test_count_spool_lines(tmp_path):
    import gzip
    import json as _json
    spool = tmp_path / owj.SPOOL_NAME
    with gzip.open(spool, "wt", encoding="utf-8") as fh:
        for way_id in range(7):
            fh.write(_json.dumps({"way_id": way_id}) + "\n")
    assert owj._count_spool_lines(spool) == 7


def test_headroom_guard_raises_when_volume_too_small(tmp_path, monkeypatch):
    spool = tmp_path / owj.SPOOL_NAME
    spool.write_bytes(b"x" * 1_000_000)
    monkeypatch.setenv(owj._DB_VOLUME_ENV, str(tmp_path))
    monkeypatch.setattr(owj, "_free_bytes", lambda _p: 5_000_000)
    with pytest.raises(owj.InsufficientDiskSpace) as exc:
        owj.check_load_headroom(spool, factor=12.0)
    # The message must carry the actionable replay command, not just a number.
    assert "--from-spool" in str(exc.value)
    assert str(tmp_path) in str(exc.value)


def test_headroom_guard_passes_with_room(tmp_path, monkeypatch):
    spool = tmp_path / owj.SPOOL_NAME
    spool.write_bytes(b"x" * 1_000_000)
    monkeypatch.setenv(owj._DB_VOLUME_ENV, str(tmp_path))
    monkeypatch.setattr(owj, "_free_bytes", lambda _p: 100_000_000)
    diag = owj.check_load_headroom(spool, factor=12.0)
    assert diag["checked"] is True
    assert diag["needed"] == 12_000_000


def test_headroom_guard_skip_is_explicit_not_silent(tmp_path, monkeypatch,
                                                    capsys):
    spool = tmp_path / owj.SPOOL_NAME
    spool.write_bytes(b"x" * 1000)
    monkeypatch.setenv(owj._DB_VOLUME_ENV, "")     # opt out
    diag = owj.check_load_headroom(spool)
    assert diag["checked"] is False
    assert "SKIPPED" in capsys.readouterr().out    # never silent


def test_headroom_factor_env_override(tmp_path, monkeypatch):
    spool = tmp_path / owj.SPOOL_NAME
    spool.write_bytes(b"x" * 1_000_000)
    monkeypatch.setenv(owj._DB_VOLUME_ENV, str(tmp_path))
    monkeypatch.setenv("TRUCKINTEL_LOAD_SIZE_FACTOR", "2")
    monkeypatch.setattr(owj, "_free_bytes", lambda _p: 3_000_000)
    assert owj.check_load_headroom(spool)["needed"] == 2_000_000  # 12x would fail


def test_cleanup_never_deletes_a_handed_in_spool(tmp_path):
    workdir = tmp_path / ".osmways-work-x-run1"
    workdir.mkdir()
    (workdir / owj.SPOOL_NAME).write_bytes(b"kept")
    # resumed=True + keep_workdir=False: the destructive default must not apply
    owj._cleanup_workdir(workdir, keep_workdir=False, resumed=True)
    assert workdir.exists()
    # a run that created its own workdir still cleans up
    owj._cleanup_workdir(workdir, keep_workdir=False, resumed=False)
    assert not workdir.exists()


@needs_db
def test_from_spool_replays_phase_b_without_rescanning(mini_scratch, tmp_path):
    pbf = tmp_path / "mini.osm.pbf"
    _write_mini_pbf(pbf)
    first = owj.run_ways_job(pbf, f"{MINI_SCHEMA}.ways", progress_every=0,
                             keep_workdir=True)
    mini_scratch.append(first["run_id"])
    workdir = pbf.parent / f".osmways-work-{pbf.stem}-run{first['run_id']}"
    assert (workdir / owj.SPOOL_NAME).is_file()   # kept for the replay

    resumed = owj.run_ways_job(pbf, f"{MINI_SCHEMA}.ways", progress_every=0,
                               from_spool=workdir)
    mini_scratch.append(resumed["run_id"])

    assert resumed["published"] == first["published"]
    assert resumed["resumed_from"] == str(workdir / owj.SPOOL_NAME)
    # Phase A counters are unknowable from a spool -> None, never faked as 0.
    assert resumed["scanned"] is None
    assert resumed["skipped_class"] is None
    assert resumed["pass_seconds"] is None
    # The handed-in spool survives even though keep_workdir defaulted False.
    assert (workdir / owj.SPOOL_NAME).is_file()

    with get_conn() as conn:
        lineage = conn.execute(
            f"SELECT DISTINCT run_id FROM {MINI_SCHEMA}.ways").fetchall()
        msg = conn.execute(
            "SELECT message FROM ops.source_runs WHERE run_id = %s",
            (resumed["run_id"],)).fetchone()[0]
    assert lineage == [(resumed["run_id"],)]      # replay owns the snapshot
    assert "resumed=" in msg                      # audit row says so
