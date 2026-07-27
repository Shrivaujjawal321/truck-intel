"""OSM extraction — Geofabrik PBF -> osm.* mirrors (ODbL-isolated, §3.1-4a).

--job pois (this module): ONE streaming pyosmium pass over the PBF extracts
  amenity=fuel                                  -> osm.fuel_stations
  highway=rest_area / highway=services          -> osm.rest_areas
  amenity=weighbridge / man_made=weighbridge
    / highway=weigh_station                     -> osm.weigh_points
  shop=truck_repair / service:vehicle:truck_repair=yes
    / service:vehicle:trailer_repair=yes        -> osm.truck_repair
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
    "repair": "osm.truck_repair",
}

# Exact key=value pairs the C++ TagFilter passes through to Python.
_MATCH_TAGS = (
    ("amenity", "fuel"),
    ("highway", "rest_area"),
    ("highway", "services"),
    ("amenity", "weighbridge"),
    ("man_made", "weighbridge"),
    ("highway", "weigh_station"),
    # truck repair: the direct shop tag, plus the capability tags that sit on
    # shops whose primary tag is shop=car_repair. Overture exposes neither.
    ("shop", "truck_repair"),
    ("service:vehicle:truck_repair", "yes"),
    ("service:vehicle:trailer_repair", "yes"),
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


def is_truck_repair(tags: dict) -> bool:
    """Does this object offer truck or trailer repair?

    Deliberately NOT a branch of classify(). A truck stop can be BOTH a fuel
    station and a repair shop, and classify() returns exactly one kind — so
    folding repair into it would silently move those sites out of
    osm.fuel_stations and shrink a working layer. Repair is an OVERLAPPING
    layer instead: an object can appear in both, which is what the map needs.
    """
    return (
        tags.get("shop") == "truck_repair"
        or tags.get("service:vehicle:truck_repair") == "yes"
        or tags.get("service:vehicle:trailer_repair") == "yes"
    )


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
    elif kind == "repair":
        row.update(
            brand=tags.get("brand"),
            # Tri-state as everywhere else: a shop=truck_repair that says
            # nothing about trailers is NULL there, not False.
            truck_repair=(
                True if (tags.get("shop") == "truck_repair"
                         or tags.get("service:vehicle:truck_repair") == "yes")
                else _tristate(tags.get("service:vehicle:truck_repair"),
                               true=("yes",), false=("no",))
            ),
            trailer_repair=_tristate(tags.get("service:vehicle:trailer_repair"),
                                     true=("yes",), false=("no",)),
            hgv_access=hgv_access(tags),
        )
    return row


def collect_pois(pbf: Path, *, node_cache: Path) -> tuple[dict[str, list[dict]], dict]:
    """One streaming pass: {'fuel': [...], 'rest': [...], 'weigh': [...]} row
    dicts (loaders.py conventions) + honest stats. Node locations go through
    the disk-based sparse_file_array at node_cache — never RAM."""
    observed_at, basis = pbf_observed_at(pbf)
    rows: dict[str, list[dict]] = {"fuel": [], "rest": [], "weigh": [],
                                   "repair": []}
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
        # classify() stays the authority for the three mutually-exclusive
        # kinds; repair is additive, so an object can land in two lists.
        kinds = [k for k in (classify(tags),) if k is not None]
        if is_truck_repair(tags):
            kinds.append("repair")
        if not kinds:  # TagFilter is exact, but the classifiers decide
            continue
        if obj.is_node():
            if not obj.location.valid():
                continue
            lat, lon, osm_id = obj.location.lat, obj.location.lon, f"node/{obj.id}"
            stats["nodes"] += 1
        else:
            centroid = _way_centroid(obj)
            if centroid is None:
                stats["ways_skipped_no_location"] += 1
                continue
            lat, lon, osm_id = centroid[0], centroid[1], f"way/{obj.id}"
            stats["ways"] += 1
        for kind in kinds:
            rows[kind].append(_row(kind, osm_id, tags, lat, lon, observed_at))
        matched += 1
        if matched % _PROGRESS_EVERY == 0:
            print(f"  ... {matched} POIs matched "
                  f"(fuel={len(rows['fuel'])} rest={len(rows['rest'])} "
                  f"weigh={len(rows['weigh'])} repair={len(rows['repair'])})",
                  flush=True)
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


def _is_empty(conn, target: str) -> bool:
    """True when `target` is absent or holds no rows — i.e. a swap that
    publishes nothing would destroy nothing."""
    if conn.execute("SELECT to_regclass(%s) IS NULL", (target,)).fetchone()[0]:
        return True
    return not conn.execute(
        f"SELECT EXISTS (SELECT 1 FROM {target} LIMIT 1)").fetchone()[0]


def _reassign_fuel_routes(target: str) -> int:
    """Re-derive nearest-truck-route columns after a POI-table swap.

    Returns rows assigned. ~132 s for the national 108k-station table, against a
    POI pass that already runs for many minutes, so it is not worth deferring to
    a separate schedule that could drift out of step with the data.
    """
    from truckintel.route_assign import add_route_columns, assign_nearest_route
    with get_conn() as conn:
        add_route_columns(conn, target)
        n = assign_nearest_route(conn, target, "osm_id")
    print(f"route reassignment: {n} rows in {target}", flush=True)
    return n


def run_pois(pbf: Path = DEFAULT_PBF, *, targets: dict[str, str] | None = None,
             source_id: str = POIS_SOURCE_ID, node_cache: Path | None = None,
             keep_cache: bool = False,
             only: tuple[str, ...] | None = None) -> dict[str, int]:
    """Full --job pois run: collect, then swap the tables in ONE transaction
    (a failure rolls everything back — live tables untouched), under ONE
    audited ops.source_runs row. Returns rows published per kind.

    `only` restricts which tables are PUBLISHED; the pass itself always reads
    every kind, so this never costs a second walk of the PBF. Two reasons it
    exists: refreshing the repair layer should not force a re-swap of three
    unrelated tables, and a swap can be blocked by an object outside this
    repo's control — a hand-made view left on osm.fuel_stations makes
    DROP ... _old fail, which would otherwise take the whole run down with it.

    targets/source_id overrides exist for the tests (scratch schemas —
    production callers pass nothing)."""
    pbf = Path(pbf)
    if not pbf.exists():
        raise FileNotFoundError(f"PBF not found: {pbf}")
    tgt = {**POIS_TARGETS, **(targets or {})}
    kinds = tuple(only) if only else ("fuel", "rest", "weigh", "repair")
    unknown = set(kinds) - set(tgt)
    if unknown:
        raise ValueError(f"unknown --only kind(s): {sorted(unknown)}")
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
        with get_conn() as conn:  # one transaction: every selected swap or none
            published, skipped = {}, []
            for kind in kinds:
                # snapshot_swap's min_rows floor exists so a truncated upstream
                # can never silently delete a live dataset. But a REGIONAL
                # extract legitimately holds zero of a POI class — Delaware has
                # no truck-repair POI at all — and refusing there would take
                # the whole run down over a layer that is genuinely empty.
                # The distinction that matters is whether anything would be
                # LOST: skip only when the live target is empty too, so a
                # populated table is still never replaced by nothing.
                if not rows[kind] and _is_empty(conn, tgt[kind]):
                    skipped.append(kind)
                    continue
                published[kind] = snapshot_swap(
                    conn, tgt[kind], rows[kind],
                    source_id=source_id, run_id=run_id)
        # The swap replaces the table with a LIKE … INCLUDING ALL clone, so the
        # route-assignment columns exist but come back NULL — and the fuel map
        # layer draws only `on_route_5km`, so skipping this would empty that
        # layer while every run still reported success. Re-derive in the same
        # invocation: the assignment is a *derivative* of the swap, not a
        # separate schedule that might be disabled. Only for the kinds actually
        # published — a --only run must not touch a table it did not swap.
        reassigned = sum(_reassign_fuel_routes(tgt[k])
                         for k in ("fuel", "repair") if k in published)
    except BaseException as exc:
        _finish_run(run_id, "failed",
                    message=(str(exc) or type(exc).__name__)[:1000])
        raise
    message = (
        f"pbf={pbf.name}; observed_at={stats['observed_at']:%Y-%m-%dT%H:%M:%SZ} "
        f"({stats['observed_at_basis']}); "
        + ", ".join(f"{tgt[k]}={n}" for k, n in published.items())
        # Never let a skip pass as a publish: the audit row says so out loud.
        + (f"; skipped_empty={','.join(tgt[k] for k in skipped)}" if skipped else "")
        + f"; nodes={stats['nodes']} way_centroids={stats['ways']}"
        f" ways_skipped_no_location={stats['ways_skipped_no_location']}"
        f"; route_reassigned={reassigned}"
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
    parser.add_argument("--only", default=None,
                        help="--job pois only: comma-separated kinds to "
                             "PUBLISH (fuel,rest,weigh,repair). The pass still "
                             "reads all of them; this restricts which tables "
                             "are swapped. Default: all four.")
    parser.add_argument("--from-spool", metavar="WORKDIR_OR_SPOOL", default=None,
                        help="--job ways only: skip the osmium pass and replay "
                             "phase B from a kept workdir. Without this the "
                             "recovery path was unreachable from the engine's "
                             "derived runner, which is the entry point that "
                             "actually runs unattended.")
    parser.add_argument("--min-rows", type=int, default=1,
                        help="--job ways only: refuse the swap below this many "
                             "rows (default 1 — never replace live with empty)")
    parser.add_argument("--accept-unverified-spool", action="store_true",
                        help="--job ways only: load a spool with no completion "
                             "manifest (an interrupted pass holds PART of the "
                             "ways)")
    args = parser.parse_args(argv)
    if args.job != "ways" and (args.from_spool or args.accept_unverified_spool):
        parser.error("--from-spool / --accept-unverified-spool apply to "
                     "--job ways only")
    if args.job != "pois" and args.only:
        parser.error("--only applies to --job pois only")

    load_dotenv()
    try:
        if args.job == "ways":
            try:
                import osm_ways_job  # scripts/osm_ways_job.py (ways track)
            except ImportError:
                print("scripts/osm_ways_job.py not present yet — the ways "
                      "track owns '--job ways'", file=sys.stderr)
                return 1
            osm_ways_job.run_ways(
                args.pbf, node_cache=args.node_cache,
                keep_cache=args.keep_cache, from_spool=args.from_spool,
                min_rows=args.min_rows,
                accept_unverified_spool=args.accept_unverified_spool)
        else:
            run_pois(args.pbf, node_cache=args.node_cache,
                     keep_cache=args.keep_cache,
                     only=tuple(k.strip() for k in args.only.split(","))
                     if args.only else None)
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"osm_extract --job {args.job} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
