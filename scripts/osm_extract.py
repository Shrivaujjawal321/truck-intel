"""OSM extraction — Geofabrik PBF -> osm.* mirrors (ODbL-isolated, §3.1-4a).

--job pois (this module): ONE streaming pyosmium pass over the PBF extracts
  amenity=fuel                                  -> osm.fuel_stations
  highway=rest_area / highway=services          -> osm.rest_areas
  amenity=weighbridge / man_made=weighbridge
    / highway=weigh_station                     -> osm.weigh_points
as nodes + way-centroids, then publishes each table via the existing
snapshot_swap loader (targets are in registry.SNAPSHOT_TARGETS). Relations are
NOT processed (877 US amenity=fuel relations skipped, honestly — a later pass
may add osmium's area assembler; entities=NODE|WAY makes the skip structural).

--job ways: owned by the ways track in a SEPARATE module,
scripts/osm_ways_job.py, imported lazily by main() (same directory, so the
plain `import osm_ways_job` resolves both under the engine's dispatch
[sys.executable, scripts/osm_extract.py, --job, ways] and under
`uv run python scripts/osm_extract.py`). Contract for that module:
    run_ways(pbf: Path, *, node_cache: Path | None, keep_cache: bool) -> None
raising on failure (main() converts to exit code 1). Until the module exists
the job fails honestly with a clear message.

MEMORY BUDGET (binding): node locations are NEVER held in RAM. Way centroids
resolve through osmium's DISK-based sparse_file_array node-location index
(default: <pbf>.nodecache, i.e. on data/pbf/ — ~16 bytes/node, ~25 GB for the
US extract, deleted after the run unless --keep-cache). The C++ TagFilter
drops non-matching objects before Python, so the Python callback only sees
the ~10^5 matched POIs; the matched rows themselves (~150k dicts) are the
only RAM the pass accumulates.
NOTE: .with_locations() is chained BEFORE .with_filter() — osmium fills the
location index from every node while the filter only gates what reaches
Python (verified by the synthetic-PBF way-centroid test).

HONESTY:
- observed_at = the PBF's Geofabrik replication timestamp read from the file
  header (osmosis_replication_timestamp), NEVER the load date. Fallback when
  the header lacks it (e.g. hand-built test files): the PBF file's mtime —
  the closest honest bound on data vintage we have; which basis was used is
  recorded in the ops.source_runs message.
- tri-state booleans: NULL = tag absent = unknown, never False.
- state comes from the addr:state tag only when it is a valid 2-letter USPS
  code (raw value stays in props); no reverse geocoding here.
- opening_hours has no dedicated column in osm.fuel_stations — it travels in
  props (the full tag dict), like every other tag.

Audit: every invocation writes EXACTLY one ops.source_runs row under
'osm_pois' (seeded by sql/schema_wave2.sql; re-seeded here idempotently,
mirroring scripts/quality_nightly.py). Exit codes: 0 = success, 1 = failure
(also recorded on the run row — the audit never lies).

Usage:
  uv run python scripts/osm_extract.py --job pois                 # US PBF
  uv run python scripts/osm_extract.py --job pois --pbf data/pbf/delaware-latest.osm.pbf
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

import osmium

from truckintel.config import load_dotenv
from truckintel.db import get_conn
from truckintel.loaders import snapshot_swap

POIS_SOURCE_ID = "osm_pois"
POIS_SLO_HOURS = 400
DEFAULT_PBF = Path("data/pbf/us-latest.osm.pbf")

# kind -> live target (registry.SNAPSHOT_TARGETS members). Tests override
# with scratch-schema clones — never live osm.* tables.
POIS_TARGETS: dict[str, str] = {
    "fuel": "osm.fuel_stations",
    "rest": "osm.rest_areas",
    "weigh": "osm.weigh_points",
}

# Exact key=value pairs the C++ TagFilter passes through to Python.
_MATCH_TAGS = (
    ("amenity", "fuel"),
    ("highway", "rest_area"),
    ("highway", "services"),
    ("amenity", "weighbridge"),
    ("man_made", "weighbridge"),
    ("highway", "weigh_station"),
)

_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY".split()
)

_PROGRESS_EVERY = 50_000  # matched objects between progress lines

# Idempotent seed, mirroring quality_nightly.ensure_nightly_source — the row
# normally exists (sql/schema_wave2.sql) but a fresh DB must not crash here.
_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s,
     'Derived: OSM POI mirrors (fuel/rest/weigh) from Geofabrik US PBF -> osm.*',
     'truck-intel wave-2 OSM track', 'derived', 'derived', NULL, %(slo)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""


# ---------------------------------------------------------------- tag parsing

def classify(tags: dict) -> str | None:
    """'fuel' | 'rest' | 'weigh' | None. amenity=fuel wins over a stray
    highway=services on the same object (a fuel station inside a service
    area is still a fuel station)."""
    if tags.get("amenity") == "fuel":
        return "fuel"
    if (
        tags.get("amenity") == "weighbridge"
        or tags.get("man_made") == "weighbridge"
        or tags.get("highway") == "weigh_station"
    ):
        return "weigh"
    if tags.get("highway") in ("rest_area", "services"):
        return "rest"
    return None


def _tristate(value: str | None, *, true: tuple[str, ...], false: tuple[str, ...]) -> bool | None:
    """Tri-state by decree: unrecognized/absent -> None (unknown), never False."""
    if value in true:
        return True
    if value in false:
        return False
    return None


def has_diesel(tags: dict) -> bool | None:
    """fuel:diesel=yes -> True, =no -> False, else NULL — with one honest
    deterministic widening: fuel:HGV_diesel=yes (truck diesel lanes) also
    means diesel is sold."""
    if tags.get("fuel:HGV_diesel") == "yes":
        return True
    return _tristate(tags.get("fuel:diesel"), true=("yes",), false=("no",))


def hgv_access(tags: dict) -> bool | None:
    """hgv=yes/designated -> True, hgv=no -> False, else NULL.
    fuel:HGV_diesel=yes (dedicated truck lanes) also implies access."""
    if tags.get("fuel:HGV_diesel") == "yes":
        return True
    return _tristate(tags.get("hgv"), true=("yes", "designated"), false=("no",))


def has_def(tags: dict) -> bool | None:
    """fuel:adblue=yes -> True, =no -> False, else NULL. Sparse in the US
    (research/fuel.md) — usually NULL, and NULL renders 'unknown', never 'no'."""
    return _tristate(tags.get("fuel:adblue"), true=("yes",), false=("no",))


def state_code(tags: dict) -> str | None:
    """addr:state only when it is a valid 2-letter USPS code; anything else
    (spelled-out names, junk) -> NULL, raw value still visible in props."""
    raw = (tags.get("addr:state") or "").strip().upper()
    return raw if raw in _US_STATES else None


# ---------------------------------------------------------------- PBF helpers

def pbf_observed_at(pbf: Path) -> tuple[datetime, str]:
    """(observed_at, basis). Basis 1: the header's Geofabrik replication
    timestamp — when the data was true in OSM. Fallback: file mtime,
    explicitly labeled, never the load date."""
    stamp = osmium.io.Reader(str(pbf)).header().get("osmosis_replication_timestamp")
    if stamp:
        return (
            datetime.fromisoformat(stamp.replace("Z", "+00:00")),
            "pbf_replication_timestamp",
        )
    return (
        datetime.fromtimestamp(pbf.stat().st_mtime, tz=timezone.utc),
        "pbf_file_mtime (header had no replication timestamp)",
    )


def _way_centroid(way) -> tuple[float, float] | None:
    """(lat, lon) mean of the way's resolvable node locations (closed ways
    drop the duplicated closing node). Approximate representative point —
    fine for POI pins, documented. None when no location resolved."""
    nodes = list(way.nodes)
    if len(nodes) > 1 and nodes[0].ref == nodes[-1].ref:
        nodes = nodes[:-1]
    lats = [n.location.lat for n in nodes if n.location.valid()]
    lons = [n.location.lon for n in nodes if n.location.valid()]
    if not lats:
        return None
    return fmean(lats), fmean(lons)


def _row(kind: str, osm_id: str, tags: dict, lat: float, lon: float,
         observed_at: datetime) -> dict:
    row = {
        "osm_id": osm_id,
        "name": tags.get("name"),
        "state": state_code(tags),
        "lat": lat,
        "lon": lon,
        "observed_at": observed_at,
        "props": tags,  # full tag dict (opening_hours etc. live here)
    }
    if kind == "fuel":
        row.update(
            brand=tags.get("brand"),
            has_diesel=has_diesel(tags),
            hgv_access=hgv_access(tags),
            has_def=has_def(tags),
        )
    return row


def collect_pois(pbf: Path, *, node_cache: Path) -> tuple[dict[str, list[dict]], dict]:
    """One streaming pass: {'fuel': [...], 'rest': [...], 'weigh': [...]} row
    dicts (loaders.py conventions) + honest stats. Node locations go through
    the disk-based sparse_file_array at node_cache — never RAM."""
    observed_at, basis = pbf_observed_at(pbf)
    rows: dict[str, list[dict]] = {"fuel": [], "rest": [], "weigh": []}
    stats = {
        "observed_at": observed_at,
        "observed_at_basis": basis,
        "nodes": 0,
        "ways": 0,
        "ways_skipped_no_location": 0,
    }
    fp = (
        osmium.FileProcessor(str(pbf), entities=osmium.osm.NODE | osmium.osm.WAY)
        .with_locations(f"sparse_file_array,{node_cache}")   # disk index FIRST,
        .with_filter(osmium.filter.TagFilter(*_MATCH_TAGS))  # then the C++ gate
    )
    matched = 0
    for obj in fp:
        tags = dict(obj.tags)
        kind = classify(tags)
        if kind is None:  # TagFilter is exact, but classify stays the authority
            continue
        if obj.is_node():
            if not obj.location.valid():
                continue
            rows[kind].append(_row(kind, f"node/{obj.id}", tags,
                                   obj.location.lat, obj.location.lon, observed_at))
            stats["nodes"] += 1
        else:
            centroid = _way_centroid(obj)
            if centroid is None:
                stats["ways_skipped_no_location"] += 1
                continue
            rows[kind].append(_row(kind, f"way/{obj.id}", tags,
                                   centroid[0], centroid[1], observed_at))
            stats["ways"] += 1
        matched += 1
        if matched % _PROGRESS_EVERY == 0:
            print(f"  ... {matched} POIs matched "
                  f"(fuel={len(rows['fuel'])} rest={len(rows['rest'])} "
                  f"weigh={len(rows['weigh'])})", flush=True)
    return rows, stats


# ---------------------------------------------------------------- audit + run

def _start_run(source_id: str) -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": source_id, "slo": POIS_SLO_HOURS})
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
            (status, message, rows_published, run_id),
        )


def run_pois(pbf: Path = DEFAULT_PBF, *, targets: dict[str, str] | None = None,
             source_id: str = POIS_SOURCE_ID, node_cache: Path | None = None,
             keep_cache: bool = False) -> dict[str, int]:
    """Full --job pois run: collect, then swap all three tables in ONE
    transaction (a failure rolls everything back — live tables untouched),
    under ONE audited ops.source_runs row. Returns rows published per kind.

    targets/source_id overrides exist for the tests (scratch schemas —
    production callers pass nothing)."""
    pbf = Path(pbf)
    if not pbf.exists():
        raise FileNotFoundError(f"PBF not found: {pbf}")
    tgt = {**POIS_TARGETS, **(targets or {})}
    cache = Path(node_cache) if node_cache else pbf.with_name(pbf.name + ".nodecache")
    run_id = _start_run(source_id)
    print(f"{source_id} run {run_id}: pass over {pbf} "
          f"(node cache: {cache})", flush=True)
    try:
        try:
            rows, stats = collect_pois(pbf, node_cache=cache)
        finally:
            if not keep_cache and cache.exists():
                cache.unlink()
        with get_conn() as conn:  # one transaction: all three swaps or none
            published = {
                kind: snapshot_swap(conn, tgt[kind], rows[kind],
                                    source_id=source_id, run_id=run_id)
                for kind in ("fuel", "rest", "weigh")
            }
    except BaseException as exc:
        _finish_run(run_id, "failed",
                    message=(str(exc) or type(exc).__name__)[:1000])
        raise
    message = (
        f"pbf={pbf.name}; observed_at={stats['observed_at']:%Y-%m-%dT%H:%M:%SZ} "
        f"({stats['observed_at_basis']}); "
        + ", ".join(f"{tgt[k]}={n}" for k, n in published.items())
        + f"; nodes={stats['nodes']} way_centroids={stats['ways']}"
        f" ways_skipped_no_location={stats['ways_skipped_no_location']}"
    )
    _finish_run(run_id, "success", message=message,
                rows_published=sum(published.values()))
    print(f"{source_id} run {run_id}: {message}", flush=True)
    return published


# ---------------------------------------------------------------- entrypoint

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OSM PBF -> osm.* mirrors (pois: this module; "
                    "ways: scripts/osm_ways_job.py)")
    parser.add_argument("--job", required=True, choices=("pois", "ways"))
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF,
                        help=f"input PBF (default: {DEFAULT_PBF})")
    parser.add_argument("--node-cache", type=Path, default=None,
                        help="disk node-location index path "
                             "(default: <pbf>.nodecache)")
    parser.add_argument("--keep-cache", action="store_true",
                        help="keep the node cache file after the run")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        if args.job == "ways":
            try:
                import osm_ways_job  # scripts/osm_ways_job.py (ways track)
            except ImportError:
                print("scripts/osm_ways_job.py not present yet — the ways "
                      "track owns '--job ways'", file=sys.stderr)
                return 1
            osm_ways_job.run_ways(args.pbf, node_cache=args.node_cache,
                                  keep_cache=args.keep_cache)
        else:
            run_pois(args.pbf, node_cache=args.node_cache,
                     keep_cache=args.keep_cache)
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"osm_extract --job {args.job} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
