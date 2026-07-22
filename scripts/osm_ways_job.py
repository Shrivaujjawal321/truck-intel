"""OSM highways extraction -> osm.ways (MASTER_PLAN ruling §3.1-5).

Streams the drivable public-highway subset of a Geofabrik PBF into the
ODbL-isolated osm schema (ruling §3.1-4a: unconflated OSM lives ONLY in osm.*,
joined at query time; every OSM-derived response carries the ODbL attribution).
osm.ways is the conflation substrate for NBI->OSM matching — NOT a routing
graph; the routable graph never enters Postgres (§3.1-5).

RAM budget (binding): node locations are NEVER held in RAM. The pass uses
pyosmium's disk-based sparse_file_array node-location index, written under the
PBF's own directory (data/pbf/), and spools parsed way rows to a gzip ndjson
file on disk. The Postgres load then streams the spool through
loaders.snapshot_swap, whose COPY consumes an iterator row-by-row — no
materialized row list at any point, so peak RSS is flat regardless of PBF size.

Measured (Delaware, 21 MB PBF, 2026-07-22): 137,963 highway ways scanned,
109,777 published, pass 16.8 s, end-to-end ~25 s, peak RSS 156 MB.
US projection (12 GB PBF, same code path): the sparse_file_array index grows
~16 bytes per node (~1.4 B US nodes -> ~22 GB on data/pbf/'s volume, 275 GB
free); RSS stays flat (osmium buffers + Python only), well inside the ~7 GB
budget — the OS page cache absorbs index I/O, so the pass is disk-bound, not
RAM-bound. Expect tens of millions of drivable ways and roughly 4-8 h
wall-clock for pass + COPY + index build: run it in the background
(`--keep-workdir` preserves the node index + spool for cheap re-runs).

Two phases per run (one audited ops.source_runs row under source 'osm_ways'):
  A. osmium pass: FileProcessor(NODE|WAY) + disk location index + KeyFilter on
     'highway' -> allow-listed classes only -> spool file. Long but resumable
     by re-running; progress printed every `progress_every` kept ways.
  B. snapshot_swap into the target: short transaction, atomic replace; a
     failed load never touches the live table.

observed_at honesty: the PBF's osmosis_replication_timestamp header (the
Geofabrik replication point — the data's real-world vintage), NEVER the load
date. A PBF without the header yields observed_at NULL (unknown, not faked).
Tri-state booleans (bridge/tunnel): tag absent -> NULL (unknown), 'no' ->
FALSE, anything else -> TRUE — NULL never means false. Unparseable dimension
tags -> NULL with the raw value kept in props (full tag map) — never guessed.

Engine contract: engine._DERIVED_RUNNERS['osm_ways'] runs
`scripts/osm_extract.py --job ways`; osm_extract.py (POI track) lazily imports
this module and calls run_ways_job(pbf_path, target='osm.ways'). Until
osm_extract.py exists, invoke directly:

  uv run python scripts/osm_ways_job.py --pbf data/pbf/delaware-latest.osm.pbf
  uv run python scripts/osm_ways_job.py            # full US PBF (long run)

Exit codes: 0 = success, 1 = failed (failure also recorded on the run row).
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import resource
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import osmium

from truckintel.config import load_dotenv
from truckintel.db import get_conn
from truckintel.loaders import snapshot_swap

SOURCE_ID = "osm_ways"
SLO_HOURS = 400
DEFAULT_PBF = Path("data/pbf/us-latest.osm.pbf")
DEFAULT_TARGET = "osm.ways"

# Ruling §3.1-5: osm.ways is the conflation substrate for NBI/state-restriction
# matching, not a routing graph — so ONLY drivable public highway classes are
# mirrored (motorway..service + their _link variants). footway / cycleway /
# path / track / bridleway / steps / proposed / construction etc. are excluded:
# trucks are never conflated onto them, and they would triple the row count.
HIGHWAY_CLASSES = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "service",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
})

# ---------------------------------------------------------------------------
# OSM dimension/weight tag parsing (rigorous; unparseable -> None, NEVER guessed)
# ---------------------------------------------------------------------------

_M_TO_IN = 39.3700787402
_LB_PER_TONNE = 2204.62262185
_LB_PER_KG = 2.20462262185
_LB_PER_SHORT_TON = 2000.0
_LB_PER_LONG_TON = 2240.0

_NUM = r"(\d[\d.,]*)"
# feet-inches: 13'6"  14'  13 ft  13ft6in  6' 6"
_FT_IN_RE = re.compile(
    rf"^{_NUM}\s*(?:'|′|ft\.?|feet|foot)\s*"
    rf"(?:{_NUM}\s*(?:\"|''|″|in\.?|inches|inch)?)?$",
    re.IGNORECASE,
)
_IN_RE = re.compile(rf"^{_NUM}\s*(?:\"|''|″|in\.?|inches|inch)$", re.IGNORECASE)
_M_RE = re.compile(rf"^{_NUM}\s*(?:m|meter|meters|metre|metres)$", re.IGNORECASE)
_CM_RE = re.compile(rf"^{_NUM}\s*(?:cm|centimeters?|centimetres?)$", re.IGNORECASE)
_BARE_RE = re.compile(rf"^{_NUM}$")
_WEIGHT_UNIT_RE = re.compile(rf"^{_NUM}\s*([a-z]+\.?)$", re.IGNORECASE)

_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_DECIMAL_COMMA_RE = re.compile(r"^\d+,\d+$")

_WEIGHT_LB_PER_UNIT = {
    "t": _LB_PER_TONNE, "tonne": _LB_PER_TONNE, "tonnes": _LB_PER_TONNE,
    "mt": _LB_PER_TONNE,
    "kg": _LB_PER_KG, "kgs": _LB_PER_KG,
    "lb": 1.0, "lbs": 1.0, "pound": 1.0, "pounds": 1.0,
    "st": _LB_PER_SHORT_TON,   # OSM short ton
    "lt": _LB_PER_LONG_TON,    # OSM long ton
}


def _to_float(num: str) -> float | None:
    """'26,000' -> 26000.0; '4,1' -> 4.1; '4.1' -> 4.1; junk/negative -> None.
    Zero is allowed here — the ft-in composite needs it (12'0" has zero
    inches); callers reject all-zero totals."""
    num = num.strip()
    if _THOUSANDS_RE.match(num):
        num = num.replace(",", "")
    elif _DECIMAL_COMMA_RE.match(num):
        num = num.replace(",", ".")
    elif "," in num:
        return None
    try:
        value = float(num)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_length_in(raw: str | None) -> float | None:
    """OSM length tag (maxheight/maxwidth/maxlength) -> inches, or None.

    Formats: bare number = meters (OSM default), '4.1 m', '420 cm',
    feet-inches (13'6\", 14', 13 ft, 13ft6in), plain inches (78\").
    'default'/'none'/'unsigned'/multi-values/junk -> None (raw stays in props).
    """
    if not raw:
        return None
    raw = raw.strip()
    m = _FT_IN_RE.match(raw)
    if m:
        feet = _to_float(m.group(1))
        inches = _to_float(m.group(2)) if m.group(2) else 0.0
        if feet is None or inches is None:
            return None
        total = feet * 12.0 + inches
        return _finite_1dp(total) if total > 0 else None
    m = _IN_RE.match(raw)
    if m:
        val = _to_float(m.group(1))
        return _finite_1dp(val) if val else None
    m = _CM_RE.match(raw)
    if m:
        val = _to_float(m.group(1))
        return _finite_1dp(val / 100.0 * _M_TO_IN) if val else None
    m = _M_RE.match(raw) or _BARE_RE.match(raw)
    if m:
        val = _to_float(m.group(1))
        return _finite_1dp(val * _M_TO_IN) if val else None
    return None


def parse_weight_lb(raw: str | None) -> float | None:
    """OSM weight tag (maxweight) -> pounds, or None.

    Formats: bare number = metric tonnes (OSM default), '15 t', '3500 kg',
    '26000 lbs', '10 st' (short tons), '3 lt' (long tons). Junk -> None.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = _BARE_RE.match(raw)
    if m:
        val = _to_float(m.group(1))
        return _finite_0dp(val * _LB_PER_TONNE) if val else None
    m = _WEIGHT_UNIT_RE.match(raw)
    if m:
        factor = _WEIGHT_LB_PER_UNIT.get(m.group(2).rstrip(".").lower())
        if factor is None:
            return None
        val = _to_float(m.group(1))
        return _finite_0dp(val * factor) if val else None
    return None


def _finite_1dp(value: float) -> float | None:
    """Round to 1 dp; None if it would overflow NUMERIC(6,1) (never truncated).
    (maxheight_in/maxwidth_in are NUMERIC(6,1); no real-world dimension nears
    the 99999.9-inch cap — beyond it is tag junk, honestly NULL.)"""
    value = round(value, 1)
    return value if value < 10**5 else None


def _finite_0dp(value: float) -> float | None:
    """Round to whole; None if it would overflow NUMERIC(9,0)."""
    value = float(round(value))
    return value if value < 10**9 else None


def _tristate(raw: str | None) -> bool | None:
    """Tri-state tag flag: absent -> NULL (unknown, never false), 'no' ->
    False, any other value ('yes', 'viaduct', 'building_passage', ...) -> True."""
    if raw is None:
        return None
    return raw.strip().lower() != "no"


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def way_row(way_id: int, tags: dict[str, str],
            coords: list[tuple[float, float]], *,
            partial_geom: bool = False) -> dict:
    """One osm.ways row dict (loaders.py row-dict conventions).

    Unparseable dimension tags land as NULL with the raw value kept in props
    (props carries the FULL tag map) — never guessed.
    """
    wkt = "LINESTRING(" + ", ".join(
        f"{lon:.7f} {lat:.7f}" for lon, lat in coords) + ")"
    flags = ["partial_geom"] if partial_geom else []
    return {
        "way_id": way_id,
        "highway": tags["highway"],
        "name": tags.get("name"),
        "ref": tags.get("ref"),
        "oneway": tags.get("oneway"),
        "maxheight_in": parse_length_in(tags.get("maxheight")),
        "maxweight_lb": parse_weight_lb(tags.get("maxweight")),
        "maxlength_in": parse_length_in(tags.get("maxlength")),
        "maxwidth_in": parse_length_in(tags.get("maxwidth")),
        "hgv": tags.get("hgv"),
        "bridge": _tristate(tags.get("bridge")),
        "tunnel": _tristate(tags.get("tunnel")),
        "geom_wkt": wkt,
        "flags": flags,
        "props": tags,
    }


def replication_timestamp(pbf_path: Path) -> datetime | None:
    """The PBF's Geofabrik replication timestamp — the honest observed_at
    basis (data vintage, never the load date). Missing header -> None."""
    reader = osmium.io.Reader(str(pbf_path), osmium.osm.osm_entity_bits.NOTHING)
    try:
        raw = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Phase A — osmium pass: PBF -> gzip ndjson spool (disk node index, flat RAM)
# ---------------------------------------------------------------------------

def _spool_ways(pbf_path: Path, workdir: Path, *,
                progress_every: int = 500_000,
                index_path: Path | None = None) -> dict:
    """Single streaming pass: highway-tagged ways -> allow-listed classes with
    >= 2 located nodes -> one json line each in <workdir>/ways.ndjson.gz.

    Node locations live in a sparse_file_array index file under workdir (on
    the data/pbf/ volume) — NEVER in RAM (binding hardware budget). Ways whose
    nodes are partially missing from the extract keep their valid coords and
    are flagged 'partial_geom'; < 2 valid coords -> skipped (counted)."""
    workdir.mkdir(parents=True, exist_ok=True)
    if index_path is None:
        index_path = workdir / "node-locations.idx"
    spool_path = workdir / "ways.ndjson.gz"
    scanned = kept = skipped_geom = skipped_class = 0
    t0 = time.monotonic()

    fp = (
        osmium.FileProcessor(str(pbf_path), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations(f"sparse_file_array,{index_path}")
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter("highway").enable_for(osmium.osm.WAY))
    )
    with gzip.open(spool_path, "wt", encoding="utf-8", compresslevel=4) as spool:
        for way in fp:
            scanned += 1
            tags = {t.k: t.v for t in way.tags}
            if tags.get("highway") not in HIGHWAY_CLASSES:
                skipped_class += 1
                continue
            coords: list[tuple[float, float]] = []
            n_nodes = 0
            for node in way.nodes:
                n_nodes += 1
                loc = node.location
                if loc.valid():
                    coords.append((loc.lon, loc.lat))
            if len(coords) < 2:
                skipped_geom += 1
                continue
            row = way_row(way.id, tags, coords,
                          partial_geom=len(coords) < n_nodes)
            spool.write(json.dumps(row, ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
            kept += 1
            if progress_every and kept % progress_every == 0:
                print(f"osm_ways pass: scanned={scanned:,} kept={kept:,} "
                      f"({time.monotonic() - t0:,.0f}s)", flush=True)
    return {
        "spool": spool_path,
        "scanned": scanned,
        "kept": kept,
        "skipped_geom": skipped_geom,
        "skipped_class": skipped_class,
        "pass_seconds": round(time.monotonic() - t0, 1),
    }


def _rows_from_spool(spool_path: Path,
                     observed_at: datetime | None) -> Iterator[dict]:
    """Stream spooled rows back as loader dicts — one line in memory at a time."""
    with gzip.open(spool_path, "rt", encoding="utf-8") as spool:
        for line in spool:
            row = json.loads(line)
            row["observed_at"] = observed_at
            yield row


# ---------------------------------------------------------------------------
# Phase B — audited snapshot_swap load
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Additive extension of the phase-2 osm.ways DDL (sql/schema_phase2.sql) with
# the §3.1-5 columns this track owns. Idempotent ADD COLUMN IF NOT EXISTS —
# applied to the live table before every swap (snapshot_swap clones the live
# DDL via LIKE ... INCLUDING ALL, so the live table must carry them first).
# INTEGRATOR SEAM: fold these into sql/schema_phase2.sql's CREATE TABLE when
# that file is next touched; until then this is the single source of truth.
_WAYS_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("oneway", "TEXT"),              # raw oneway tag ('yes','no','-1',...)
    ("maxlength_in", "NUMERIC(7,1)"),
    ("maxwidth_in", "NUMERIC(6,1)"),
    ("bridge", "BOOLEAN"),           # tri-state: NULL=untagged=unknown
    ("tunnel", "BOOLEAN"),           # tri-state: NULL=untagged=unknown
)


def ensure_ways_columns(conn, target: str = DEFAULT_TARGET) -> None:
    schema, _, table = target.partition(".")
    if not (_IDENT_RE.match(schema) and _IDENT_RE.match(table)):
        raise ValueError(f"target must be schema-qualified, got {target!r}")
    for column, ddl_type in _WAYS_EXTRA_COLUMNS:
        conn.execute(
            f'ALTER TABLE "{schema}"."{table}" '
            f'ADD COLUMN IF NOT EXISTS "{column}" {ddl_type}'
        )


# Defensive re-seed of the derived source row (canonically seeded by
# sql/schema_wave2.sql — same values, ON CONFLICT DO NOTHING).
_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s,
     'Derived: osmium-filtered highways from Geofabrik US PBF -> osm.ways (§3.1-5)',
     'truck-intel osm track', 'derived', 'derived', NULL, %(slo)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""


def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID, "slo": SLO_HOURS})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id",
            (SOURCE_ID,),
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows_published: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, message, rows_published, run_id),
        )


def _peak_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def run_ways_job(pbf_path: str | Path = DEFAULT_PBF,
                 target: str = DEFAULT_TARGET, *,
                 keep_workdir: bool = False,
                 progress_every: int = 500_000,
                 index_path: Path | None = None) -> dict:
    """Extract + load one PBF into `target` under ONE audited source run.

    Contract for scripts/osm_extract.py (--job ways): call exactly this.
    Returns a summary dict; raises on failure (the run row records it first).
    """
    load_dotenv()
    pbf = Path(pbf_path)
    if not pbf.is_file():
        raise FileNotFoundError(f"PBF not found: {pbf}")
    run_id = _start_run()
    # workdir sits next to the PBF (data/pbf/ volume) per the hardware budget:
    # the node index + spool are disk-based, never RAM. Suffixed with the
    # run_id so concurrent runs on the same PBF (parallel test sessions,
    # overlapping dispatches) can never rmtree each other's files.
    workdir = pbf.parent / f".osmways-work-{pbf.stem}-run{run_id}"
    try:
        observed_at = replication_timestamp(pbf)
        stats = _spool_ways(pbf, workdir, progress_every=progress_every,
                            index_path=index_path)
        with get_conn() as conn:  # one short transaction: DDL guard + atomic swap
            ensure_ways_columns(conn, target)
            published = snapshot_swap(
                conn, target, _rows_from_spool(stats["spool"], observed_at),
                source_id=SOURCE_ID, run_id=run_id,
            )
    except BaseException as exc:
        _finish_run(run_id, "failed",
                    message=(str(exc) or type(exc).__name__)[:1000])
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        raise
    summary = {
        "run_id": run_id,
        "target": target,
        "published": published,
        "scanned": stats["scanned"],
        "skipped_class": stats["skipped_class"],
        "skipped_geom": stats["skipped_geom"],
        "observed_at": observed_at,
        "pass_seconds": stats["pass_seconds"],
        "peak_rss_mb": _peak_rss_mb(),
    }
    _finish_run(
        run_id, "success",
        message=(
            f"pbf={pbf.name} observed_at="
            f"{observed_at.isoformat() if observed_at else 'unknown'} "
            f"scanned={stats['scanned']} kept={published} "
            f"skipped_class={stats['skipped_class']} "
            f"skipped_geom={stats['skipped_geom']} "
            f"pass_s={stats['pass_seconds']} peak_rss_mb={summary['peak_rss_mb']}"
        ),
        rows_published=published,
    )
    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"osm_ways run {run_id}: published {published:,} ways -> {target} "
          f"(scanned {stats['scanned']:,}, pass {stats['pass_seconds']}s, "
          f"peak RSS {summary['peak_rss_mb']} MB)")
    return summary


def run_ways(pbf: str | Path, *, node_cache: Path | None = None,
             keep_cache: bool = False, target: str = DEFAULT_TARGET) -> dict:
    """Adapter for scripts/osm_extract.py --job ways (the signature the POI
    track's main() calls). Thin mapping onto run_ways_job: node_cache
    overrides the disk node-index path (default: workdir-managed under
    data/pbf/), keep_cache keeps the workdir (index + spool) for re-runs."""
    node_cache = Path(node_cache) if node_cache is not None else None
    try:
        return run_ways_job(pbf, target, keep_workdir=keep_cache,
                            index_path=node_cache)
    finally:
        # An externally-supplied cache path lives outside the workdir, so the
        # workdir cleanup can't remove it — honor keep_cache=False ourselves.
        if node_cache is not None and not keep_cache:
            node_cache.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OSM highways -> osm.ways (§3.1-5, ODbL-isolated mirror)")
    parser.add_argument("--pbf", default=str(DEFAULT_PBF),
                        help=f"input PBF (default {DEFAULT_PBF})")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="schema-qualified target table (default osm.ways)")
    parser.add_argument("--keep-workdir", action="store_true",
                        help="keep the node index + spool for a fast re-run")
    args = parser.parse_args()
    try:
        run_ways_job(args.pbf, args.target, keep_workdir=args.keep_workdir)
    except BaseException as exc:
        print(f"osm_ways run failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
