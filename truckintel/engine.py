"""The ingestion engine: tick (scheduler pass), worker loop, and run_source —
the one generic fetch->validate->publish path. Only parsers are per-source code.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import signal
import socket
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import subprocess

import psycopg
from psycopg.types.json import Jsonb

from truckintel import jobs, quality, resources
from truckintel.config import load_dotenv, raw_dir
from truckintel.db import get_conn
from truckintel.loaders import event_lifecycle_upsert, fuel_upsert, snapshot_swap
from truckintel.politeness import PoliteRefusal, PoliteResult, polite_get
from truckintel.registry import SNAPSHOT_TARGETS, load_registry, sync_sources
from truckintel.validate import gate1_schema, gate2_coords

# source_id -> parser module (pipeline.md §16: one parser per format).
# MVP fallback map — Phase-2 sources set `parser:` in their registry YAML
# instead (validated importable at sync time); this map never grows again.
_PARSER_MODULE = {
    "nbi_annual": "nbi",
    "ntad_parking": "ntad_parking",
    "nws_alerts": "nws",
    "eia_diesel": "eia",
}

# Raw-zone file extension per fetch kind (interface: what the parser receives).
_RAW_EXT = {"bulk_http": "zip", "arcgis": "geojson", "live_json": "json", "api_keyed": "json"}

# Gate-1 required fields per source — MVP fallback map only. Phase-2 sources
# declare `gates.required_fields` in their registry YAML instead (pipeline.md
# §4.1 required_columns; validated a list of strings at sync time); the
# registry value wins, this map never grows again.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "nbi_annual": ("nbi_id", "lat", "lon"),
    "ntad_parking": ("site_id", "kind", "lat", "lon"),
    "nws_alerts": ("event_id", "kind", "observed_at"),
    "eia_diesel": ("region", "product", "week_of", "price_usd_gal"),
}

# snapshot_swap targets (plan §5.1 naming map). MVP fallback map — Phase-2
# sources set `target:` in their registry YAML (allow-list-validated at sync).
_SNAPSHOT_TARGET = {"nbi_annual": "core.bridges", "ntad_parking": "core.parking_sites"}

# Gate 3 (quality ladder §2): within-source natural-key dedup, keyed off the
# publish shape — event_lifecycle feeds dedup on 'event_id'; snapshot targets
# dedup on their table PK; upsert sources (eia_diesel: composite key
# region/product/week_of) need no gate 3, their loader is idempotent on it.
_DEDUP_KEY_BY_TARGET = {
    "core.bridges": "nbi_id",
    "core.tunnels": "tunnel_id",
    "core.parking_sites": "site_id",
    "osm.ways": "way_id",
    "osm.fuel_stations": "osm_id",
    "osm.rest_areas": "osm_id",
    "osm.weigh_points": "osm_id",
}


def _dedup_key_field(src: dict) -> str | None:
    """Natural-key field gate 3 dedups on, or None (no gate 3 for upserts)."""
    if src["load_pattern"] == "event_lifecycle":
        return "event_id"
    if src["load_pattern"] == "snapshot_swap":
        return _DEDUP_KEY_BY_TARGET.get(_resolve_snapshot_target(src))
    return None

# ---------------------------------------------------------------------------
# Feed-health circuit breaker (pipeline.md §10.3) — the engine records run
# outcomes into ops.feed_health; jobs.enqueue_due reads the state.
#
# How it composes with the exponential backoff in jobs.enqueue_due (exactly
# one paces any attempt — see _ENQUEUE_SQL): while the circuit is CLOSED,
# BACKOFF spaces individual retries after failed/gated runs (5 min doubling,
# cap 6 h). Once the circuit is OPEN/HALF_OPEN (BREAKER_THRESHOLD consecutive
# 'failed' runs), the breaker's cooldown alone paces recovery — one half-open
# probe per cooldown_minutes — and gives ops a queryable per-feed liveness
# state. 'gated' and skips count as healthy CONTACT for the breaker (the feed
# answered; the data was bad) — backoff still paces them.
# ---------------------------------------------------------------------------
BREAKER_THRESHOLD = 5        # consecutive 'failed' runs that open the circuit
BREAKER_COOLDOWN_MIN = 60    # default cooldown; per-source override lives in
                             # ops.feed_health.cooldown_minutes

_HEALTHY_STATUSES = frozenset(
    {"success", "skipped_unchanged", "skipped_no_key", "gated"}
)

_FEED_FAILURE_SQL = """
INSERT INTO ops.feed_health AS fh
    (source_id, consecutive_failures, state, opened_at, cooldown_minutes,
     last_failure_at, updated_at)
VALUES
    (%(sid)s, 1,
     CASE WHEN 1 >= %(threshold)s THEN 'open' ELSE 'closed' END,
     CASE WHEN 1 >= %(threshold)s THEN now() END,
     %(cooldown)s, now(), now())
ON CONFLICT (source_id) DO UPDATE SET
    consecutive_failures = fh.consecutive_failures + 1,
    state = CASE WHEN fh.consecutive_failures + 1 >= %(threshold)s
                 THEN 'open' ELSE fh.state END,
    opened_at = CASE WHEN fh.consecutive_failures + 1 >= %(threshold)s
                     THEN now() ELSE fh.opened_at END,
    last_failure_at = now(),
    updated_at      = now()
"""

_FEED_SUCCESS_SQL = """
INSERT INTO ops.feed_health AS fh
    (source_id, consecutive_failures, state, cooldown_minutes,
     last_success_at, updated_at)
VALUES (%(sid)s, 0, 'closed', %(cooldown)s, now(), now())
ON CONFLICT (source_id) DO UPDATE SET
    consecutive_failures = 0,
    state           = 'closed',
    opened_at       = NULL,
    last_success_at = now(),
    updated_at      = now()
"""


def record_feed_health(conn: psycopg.Connection, source_id: str, *, ok: bool) -> None:
    """Fold one finished run into ops.feed_health.

    ok=False (a 'failed' run) increments consecutive_failures; reaching
    BREAKER_THRESHOLD opens the circuit (opened_at = now(), also re-arming an
    open/half_open circuit after a failed probe). ok=True resets the counter
    and closes the circuit — a half-open probe success recovers the feed.
    """
    params = {"sid": source_id, "threshold": BREAKER_THRESHOLD,
              "cooldown": BREAKER_COOLDOWN_MIN}
    conn.execute(_FEED_SUCCESS_SQL if ok else _FEED_FAILURE_SQL, params)


# ---------------------------------------------------------------------------
# Post-swap rescore hook (ruling §3.1-10): a snapshot swap replaces the table
# object, silently dropping nightly-computed quality columns — so every
# successful swap enqueues one rescore job. The quality track implements the
# job runner; the engine only enqueues (and its worker never claims derived
# jobs — see jobs.claim_job).
# ---------------------------------------------------------------------------
RESCORE_SOURCE_ID = "quality_rescore"  # seeded in sql/schema_phase2.sql
# Seeded by scripts/route_rebuild.py itself on first run (it is not in any
# registry YAML — it is derived work, triggered by a publish, never scheduled).
ROUTE_REBUILD_SOURCE_ID = "route_rebuild"

_RESCORE_ENQUEUE_SQL = (
    "INSERT INTO ops.job_queue (source_id) VALUES (%s) "
    "ON CONFLICT (source_id) WHERE status IN ('queued', 'running') DO NOTHING"
)


def enqueue_rescore(conn: psycopg.Connection,
                    source_id: str = RESCORE_SOURCE_ID) -> bool:
    """Enqueue the synthetic rescore job in the caller's transaction.

    Savepoint-guarded: a missing ops.sources seed row (schema_phase2 not
    applied yet) must never roll back the publish it rides on — the hook
    degrades to a no-op and returns False. An already-QUEUED rescore job is a
    silent no-op too (partial unique index) — safe, it runs after this commit.
    A RUNNING rescore job also no-ops the insert (same index), which WOULD
    lose this swap's rescore — the rescore runners close that window by
    re-checking jobs.snapshot_swapped_since(claim time) after every job and
    re-enqueueing (ruling §3.1-10). Returns True if a job was queued.
    """
    conn.execute("SAVEPOINT rescore_hook")
    try:
        n = conn.execute(_RESCORE_ENQUEUE_SQL, (source_id,)).rowcount
    except psycopg.Error:
        conn.execute("ROLLBACK TO SAVEPOINT rescore_hook")
        return False
    conn.execute("RELEASE SAVEPOINT rescore_hook")
    return bool(n)

# EIA Open Data API v2 query for weekly on-highway diesel (route in registry).
_EIA_PARAMS = {
    "frequency": "weekly",
    "data[0]": "value",
    "facets[product][]": "EPD2D",
    "length": "5000",
}
_EIA_MAX_PAGES = 20

_WORKER_IDLE_SLEEP_S = 5.0

# Secret-bearing query params (EIA api_key etc.). requests exceptions embed the
# full URL, so anything persisted (run message, job message) is redacted first —
# pipeline.md §15: keys never logged, key params redacted.
_SECRET_PARAM_RE = re.compile(r"(?i)\b(api_key|apikey|key|token|access_token)=[^&\s'\"]+")

# 4-digit vintage year in a bulk URL (NBI: .../2025allstatesallrecsdel.zip).
_URL_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _redact(text: str) -> str:
    """Mask secret query-param values before a string is persisted anywhere."""
    return _SECRET_PARAM_RE.sub(r"\1=REDACTED", text)


def _candidate_urls(url: str) -> list[str]:
    """Stateless annual-vintage probe (plan §8.1 / §11 'vintage probing').

    For a year-versioned bulk URL, candidates are every plausible NEWER vintage
    (url-year+1 .. calendar-year+1), newest first, then the configured URL as
    the fallback. The engine tries each in order; a probe miss (>=400) falls
    through. No stored state: the registry URL stays the source of truth and a
    newer file is simply found again on every run (one cheap 404 per probe).
    """
    m = _URL_YEAR_RE.search(url)
    if not m:
        return [url]
    url_year, this_year = int(m.group(0)), date.today().year
    newer = range(max(url_year, this_year) + 1, url_year, -1)
    return [url[: m.start()] + str(y) + url[m.end():] for y in newer] + [url]


def _check_http(res: PoliteResult) -> None:
    if res.status_code >= 400:
        raise RuntimeError(f"HTTP {res.status_code}")


def _start_run(source_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) VALUES (%s, 'running') "
            "RETURNING run_id",
            (source_id,),
        ).fetchone()
        return row[0]


def _finish_run(
    run_id: int,
    status: str,
    *,
    message: str | None = None,
    rows_in: int | None = None,
    rows_published: int | None = None,
    rows_rejected: int | None = None,
    raw_sha256: str | None = None,
    http_status: int | None = None,
) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), message = %s, "
            "rows_in = %s, rows_published = %s, rows_rejected = %s, raw_sha256 = %s, "
            "http_status = %s WHERE run_id = %s RETURNING source_id",
            (status, message, rows_in, rows_published, rows_rejected, raw_sha256,
             http_status, run_id),
        ).fetchone()
        # Circuit breaker bookkeeping rides the same transaction as the run
        # row: 'failed' counts against the feed, every other terminal status
        # is a healthy contact/decision that resets the counter.
        # SAVEPOINT-guarded like enqueue_rescore: a missing ops.feed_health
        # (sql/schema_phase2.sql not applied yet) must never roll back the run
        # row itself — without the guard, EVERY terminal run would raise
        # UndefinedTable, lose its status write, and leave phantom 'running'
        # rows forever. The breaker degrades to a no-op instead.
        if row is not None and (status == "failed" or status in _HEALTHY_STATUSES):
            conn.execute("SAVEPOINT feed_health_hook")
            try:
                record_feed_health(conn, row[0], ok=status != "failed")
            except psycopg.Error:
                conn.execute("ROLLBACK TO SAVEPOINT feed_health_hook")
            else:
                conn.execute("RELEASE SAVEPOINT feed_health_hook")


def _load_source(source_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source_id, url, kind, load_pattern, gates, auth, parser, target "
            "FROM ops.sources WHERE source_id = %s",
            (source_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown source {source_id!r} — run `make sync` first")
    return dict(zip(
        ("source_id", "url", "kind", "load_pattern", "gates", "auth", "parser", "target"),
        row,
    ))


def _resolve_parser_module(src: dict) -> str:
    """Registry `parser:` wins; the hardcoded MVP map is the fallback."""
    name = src.get("parser") or _PARSER_MODULE.get(src["source_id"])
    if name is None:
        raise ValueError(
            f"no parser configured for {src['source_id']!r} — set `parser:` in "
            "its registry YAML"
        )
    return name


def _resolve_snapshot_target(src: dict) -> str:
    """Registry `target:` wins; the hardcoded MVP map is the fallback. The
    §5.1 allow-list is re-checked here — the value came through the DB, and an
    unvalidated identifier must never reach a SQL string (defense in depth on
    top of the sync-time check)."""
    target = src.get("target") or _SNAPSHOT_TARGET.get(src["source_id"])
    if target is None:
        raise ValueError(
            f"no snapshot target configured for {src['source_id']!r} — set "
            "`target:` in its registry YAML"
        )
    if target not in SNAPSHOT_TARGETS:
        raise ValueError(
            f"snapshot target {target!r} for {src['source_id']!r} is not in "
            f"the §5.1 allow-list: {sorted(SNAPSHOT_TARGETS)}"
        )
    return target


def _last_success(source_id: str) -> tuple[str | None, int | None]:
    """(raw_sha256, rows_published) of the last successful run, else (None, None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT raw_sha256, rows_published FROM ops.source_runs "
            "WHERE source_id = %s AND status = 'success' "
            "ORDER BY started_at DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _prev_meta(source_id: str, raw_sha256: str | None) -> dict:
    """Sidecar meta (etag / last_modified) of the last success's raw file."""
    if not raw_sha256:
        return {}
    for path in sorted(
        raw_dir().glob(f"{source_id}/*/{raw_sha256[:16]}.meta.json"), reverse=True
    ):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _write_raw(source_id: str, content: bytes, ext: str, meta: dict) -> tuple[str, Path]:
    """Content-addressed raw zone: data/raw/<source_id>/<date>/<sha[:16]>.<ext>."""
    sha = hashlib.sha256(content).hexdigest()
    day_dir = raw_dir() / source_id / date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{sha[:16]}.{ext}"
    if not path.exists():  # same hash = same bytes; immutable by construction
        path.write_bytes(content)
    meta_path = day_dir / f"{sha[:16]}.meta.json"
    if not meta_path.exists():  # sidecar is immutable too — first fetch's
        meta_path.write_text(   # lineage (url/fetched_at/run_id) is never lost
            json.dumps({**meta, "sha256": sha}, indent=2, default=str)
        )
    return sha, path


def _arcgis_json(content: bytes) -> dict:
    """ArcGIS servers report auth/layer/query failures as HTTP 200 + an
    {"error": {...}} envelope — surface those as real errors, never as an
    empty layer (a misattributed 'min_rows gate: 0' hides the true cause)."""
    doc = json.loads(content)
    if isinstance(doc, dict) and "error" in doc:
        err = doc.get("error") or {}
        raise RuntimeError(
            f"ArcGIS error {err.get('code')}: {err.get('message') or 'unknown'}"
        )
    return doc


def _fetch_arcgis(service_url: str) -> tuple[bytes, int]:
    """Page {url}/0/query into ONE merged GeoJSON FeatureCollection.

    maxRecordCount comes from the layer metadata — never hardcoded (plan §10).
    """
    meta_res = polite_get(service_url.rstrip("/") + "/0", params={"f": "json"})
    _check_http(meta_res)
    layer_meta = _arcgis_json(meta_res.content)
    page_size = int(layer_meta.get("maxRecordCount") or 1000)
    oid_field = layer_meta.get("objectIdField") or "OBJECTID"

    features: list = []
    offset = 0
    status = meta_res.status_code
    while True:
        res = polite_get(
            service_url.rstrip("/") + "/0/query",
            params={
                "where": "1=1",
                "outFields": "*",
                "f": "geojson",
                "orderByFields": oid_field,
                "resultOffset": str(offset),
                "resultRecordCount": str(page_size),
            },
        )
        _check_http(res)
        status = res.status_code
        page = _arcgis_json(res.content).get("features") or []
        features.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    merged = {"type": "FeatureCollection", "features": features}
    return json.dumps(merged, sort_keys=True).encode(), status


def _fetch_eia(url: str, api_key: str) -> tuple[bytes, int]:
    """EIA v2 JSON; pages via offset/length when total > one page, merged into
    one response document so the parser sees a single bytes blob."""
    params = dict(_EIA_PARAMS)
    params["api_key"] = api_key
    res = polite_get(url, params={**params, "offset": "0"})
    _check_http(res)
    doc = json.loads(res.content)
    response = doc.get("response") or {}
    data = list(response.get("data") or [])
    total = int(response.get("total") or len(data))

    pages = 1
    while len(data) < total and pages < _EIA_MAX_PAGES:
        more = polite_get(url, params={**params, "offset": str(len(data))})
        _check_http(more)
        chunk = (json.loads(more.content).get("response") or {}).get("data") or []
        if not chunk:
            break
        data.extend(chunk)
        pages += 1
    if "response" in doc:
        doc["response"]["data"] = data
    # EIA v2 echoes the request (including api_key) in the response body; the
    # raw zone is immutable and backed up, so the secret is redacted BEFORE
    # hashing/writing (pipeline.md §15: keys env-only, never persisted).
    echo_params = (doc.get("request") or {}).get("params")
    if isinstance(echo_params, dict) and "api_key" in echo_params:
        echo_params["api_key"] = "REDACTED"
    return json.dumps(doc, sort_keys=True).encode(), res.status_code


def _write_rejects(source_id: str, run_id: int, rejects: list[dict]) -> None:
    if not rejects:
        return
    dumps = lambda obj: json.dumps(obj, default=str)  # noqa: E731 — non-JSON-native values
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO quality.rejects (source_id, run_id, reason, raw_record) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (source_id, run_id, r["reason"], Jsonb(r["raw_record"], dumps=dumps))
                    for r in rejects
                ],
            )


def run_source(source_id: str) -> None:
    """Run one source end-to-end. Every outcome writes EXACTLY one
    ops.source_runs row — success, skip, gate-abort, or failure. Never fake success.

    Steps to implement:
    1. Load source config from ops.sources; insert source_runs row (status='running').
    2. auth.env set but env var empty -> finish run status='skipped_no_key'
       with a clear message and return (EIA rule; no crash).
    3. polite_get() the url (conditional headers from the last successful run);
       not_modified -> status='skipped_unchanged'.
    4. Write raw bytes to data/raw/<source_id>/<date>/<sha256[:16]>.<ext>;
       store raw_sha256 + http_status on the run row.
    5. parsers.<source>.parse(raw) -> rows; gate1_schema + gate2_coords;
       rejects to quality.rejects with reasons.
    6. Registry gates (min_rows, max_row_delta_pct vs last success): failure ->
       status='gated', publish ABORTED, old table stays live.
    7. Load via the source's load_pattern (snapshot_swap | event_lifecycle |
       upsert); finish run status='success' with rows_in/published/rejected.
    """
    load_dotenv()
    src = _load_source(source_id)
    run_id = _start_run(source_id)
    try:
        _execute(src, run_id)
    except BaseException as exc:
        # BaseException so Ctrl-C / SIGTERM (SystemExit via the worker's signal
        # handler) still closes the run row instead of leaving a phantom
        # 'running' forever; message is redacted (URLs can embed api keys).
        _finish_run(run_id, "failed", message=_redact(str(exc) or type(exc).__name__)[:1000])
        raise


def _execute(src: dict, run_id: int) -> None:
    source_id = src["source_id"]

    # step 2 — keyed source with no key: graceful skip, never a crash
    auth = src.get("auth") or {}
    api_key = None
    if auth.get("env"):
        api_key = os.environ.get(auth["env"]) or None
        if api_key is None:
            _finish_run(
                run_id,
                "skipped_no_key",
                message=f"{auth['env']} is not set — add it to .env to enable {source_id}",
            )
            return

    prev_sha, prev_rows = _last_success(source_id)
    prev_meta = _prev_meta(source_id, prev_sha)

    # step 3 — fetch per registry kind
    kind = src["kind"]
    fetched_url = src["url"]
    etag = last_modified = None
    if kind == "bulk_http":
        res = None
        for candidate in _candidate_urls(src["url"]):
            # conditional headers only apply to the URL they were earned on
            same = candidate == prev_meta.get("url", src["url"])
            attempt = polite_get(
                candidate,
                etag=prev_meta.get("etag") if same else None,
                last_modified=prev_meta.get("last_modified") if same else None,
                timeout_s=300.0,
            )
            if candidate != src["url"] and attempt.status_code >= 400:
                continue  # probed newer vintage not published yet — fall back
            res, fetched_url = attempt, candidate
            break
        if res.not_modified:
            _finish_run(run_id, "skipped_unchanged", message="304 Not Modified",
                        raw_sha256=prev_sha, http_status=304)
            return
        _check_http(res)
        content, http_status, etag, last_modified = (
            res.content, res.status_code, res.etag, res.last_modified,
        )
    elif kind == "live_json":
        res = polite_get(src["url"])
        _check_http(res)
        content, http_status = res.content, res.status_code
    elif kind == "arcgis":
        content, http_status = _fetch_arcgis(src["url"])
    elif kind == "api_keyed":
        content, http_status = _fetch_eia(src["url"], api_key)
    else:
        raise ValueError(f"unknown source kind {kind!r}")

    sha = hashlib.sha256(content).hexdigest()
    if prev_sha is not None and sha == prev_sha:
        _finish_run(run_id, "skipped_unchanged", message="payload hash unchanged",
                    raw_sha256=sha, http_status=http_status)
        return

    # step 4 — immutable raw zone + sidecar meta (replay + conditional headers)
    sha, _ = _write_raw(
        source_id, content, _RAW_EXT[kind],
        {
            "url": fetched_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "etag": etag,
            "last_modified": last_modified,
            "http_status": http_status,
        },
    )

    # step 5 — parse + gates 1-2; rejects are replayable diagnostics and are
    # written even when a later registry gate aborts the publish
    parser = importlib.import_module(f"truckintel.parsers.{_resolve_parser_module(src)}")
    # Gate-1 required fields: registry `gates.required_fields` wins (Phase-2
    # sources — pipeline.md §4.1); the hardcoded MVP map is the fallback.
    gates = src.get("gates") or {}
    required = tuple(gates.get("required_fields") or _REQUIRED_FIELDS.get(source_id, ()))
    # parse streams straight into gate 1 — never a second full materialization
    ok1, rejects1 = gate1_schema(parser.parse(content), required)
    rows_in = len(ok1) + len(rejects1)
    ok_rows, rejects2 = gate2_coords(ok1)
    # gate 3 — within-source natural-key dedup (quality ladder; reason
    # 'duplicate_natural_key'). Keyless rows pass through: missing keys are
    # gate 1's rejection, and upsert sources skip gate 3 entirely.
    dedup_field = _dedup_key_field(src)
    if dedup_field is not None:
        ok_rows, rejects3 = quality.dedup(ok_rows, dedup_field)
    else:
        rejects3 = []
    rejects = rejects1 + rejects2 + rejects3
    _write_rejects(source_id, run_id, rejects)

    # step 6 — registry gates: abort publish, old table stays live.
    # Unconditional safety gate first: a poll whose rows are ALL rejected is
    # upstream schema drift (pipeline.md §10.2), never a publishable state —
    # without this, an event_lifecycle publish of [] would soft-close every
    # active event and report success.
    if rows_in > 0 and not ok_rows:
        _finish_run(
            run_id, "gated",
            message=f"all {rows_in} rows rejected — upstream schema drift suspected, "
                    "publish aborted",
            rows_in=rows_in, rows_published=0, rows_rejected=len(rejects),
            raw_sha256=sha, http_status=http_status,
        )
        return
    min_rows = gates.get("min_rows")
    if min_rows is not None and len(ok_rows) < int(min_rows):
        _finish_run(
            run_id, "gated",
            message=f"min_rows gate: {len(ok_rows)} < {min_rows} — publish aborted",
            rows_in=rows_in, rows_published=0, rows_rejected=len(rejects),
            raw_sha256=sha, http_status=http_status,
        )
        return
    # geometry-valid >= threshold (MASTER_PLAN §11 validation checks): catches
    # systematic coordinate drift that min_rows alone would let through.
    geom_gate = gates.get("geometry_valid_pct")
    if geom_gate is not None and rows_in > 0:
        geom_bad = sum(
            1 for r in rejects if r["reason"].startswith(
                ("missing_required:lat", "missing_required:lon",
                 "unparseable:lat", "unparseable:lon",
                 "coords_", "latlon_swapped"))
        )
        valid_pct = (rows_in - geom_bad) / rows_in * 100.0
        if valid_pct < float(geom_gate):
            _finish_run(
                run_id, "gated",
                message=f"geometry_valid_pct gate: {valid_pct:.1f}% < {geom_gate}% "
                        "— publish aborted",
                rows_in=rows_in, rows_published=0, rows_rejected=len(rejects),
                raw_sha256=sha, http_status=http_status,
            )
            return
    max_delta = gates.get("max_row_delta_pct")
    if max_delta is not None and prev_rows:
        delta_pct = abs(len(ok_rows) - prev_rows) / prev_rows * 100.0
        if delta_pct > float(max_delta):
            _finish_run(
                run_id, "gated",
                message=(
                    f"max_row_delta_pct gate: {delta_pct:.1f}% > {max_delta}% "
                    f"(last success {prev_rows} rows, now {len(ok_rows)}) — publish aborted"
                ),
                rows_in=rows_in, rows_published=0, rows_rejected=len(rejects),
                raw_sha256=sha, http_status=http_status,
            )
            return

    # step 7 — publish via the source's load pattern, one transaction
    load_pattern = src["load_pattern"]
    with get_conn() as conn:
        if load_pattern == "snapshot_swap":
            published = snapshot_swap(
                conn, _resolve_snapshot_target(src), ok_rows,
                source_id=source_id, run_id=run_id,
                # step-6 already gated on len(ok_rows); passing the source's
                # own floor down makes the loader's backstop agree with the
                # registry instead of falling back to a generic 1, and catches
                # any divergence between rows counted and rows actually COPYed.
                min_rows=int(min_rows) if min_rows is not None else 1,
            )
            # Ruling §3.1-10: every successful swap re-enqueues the quality
            # rescore job (same transaction as the publish — they land or
            # roll back together).
            enqueue_rescore(conn)
            # A swap of the ROUTE SPINE additionally invalidates everything
            # derived from it: the routable graph, its connectivity labels,
            # the snap index, per-edge limits and the map's low-zoom geometry.
            # None of those were rebuilt before 2026-07-27, so a new NTAD
            # vintage left the router answering from the PREVIOUS network with
            # no error, no failed run and no stale-data alert — every source
            # was fresh; only the derivatives were wrong.
            # Enqueued, not run inline: the rebuild is ~50 minutes and must not
            # hold the publish transaction open.
            if _resolve_snapshot_target(src) == "core.truck_routes":
                enqueue_rescore(conn, ROUTE_REBUILD_SOURCE_ID)
        elif load_pattern == "event_lifecycle":
            published = event_lifecycle_upsert(
                conn, ok_rows, source_id=source_id, run_id=run_id
            )
        elif load_pattern == "upsert":
            published = fuel_upsert(conn, ok_rows, source_id=source_id, run_id=run_id)
        else:
            raise ValueError(f"unknown load_pattern {load_pattern!r}")

    _finish_run(
        run_id, "success",
        rows_in=rows_in, rows_published=published, rows_rejected=len(rejects),
        raw_sha256=sha, http_status=http_status,
    )


def tick() -> None:
    """One scheduler pass (systemd timer target): sync registry into
    ops.sources, then jobs.enqueue_due(). Cheap and idempotent."""
    sources = load_registry()
    with get_conn() as conn:
        sync_sources(conn, sources)
        enqueued = jobs.enqueue_due(conn)
    print(f"tick: synced {len(sources)} sources, enqueued {enqueued} jobs")


# ---------------------------------------------------------------------------
# Derived-job dispatch (ruling §3.1-6). The allow-list below is the ONLY way a
# derived job reaches a subprocess: exact argv per source_id, scripts always
# inside <repo>/scripts/. The runner scripts own their audited ops.source_runs
# rows (synthetic/derived ids — the quality_nightly pattern). A source_id
# missing here, or an entry whose script does not exist yet (wave-2 tracks
# create osm_extract.py / businesses_pipeline.py), finishes the job 'failed'
# with an honest message — never a crash, never a fake 'done'.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

_DERIVED_RUNNERS: dict[str, list[str]] = {
    RESCORE_SOURCE_ID: ["scripts/quality_nightly.py", "--rescore", "all"],
    # --if-stale, not a bare rebuild: the hook fires on every truck_routes
    # swap, and a swap that republished an unchanged network would otherwise
    # cost 50 minutes to rebuild an identical graph.
    ROUTE_REBUILD_SOURCE_ID: ["scripts/route_rebuild.py", "--if-stale"],
    "osm_pois": ["scripts/osm_extract.py", "--job", "pois"],
    # (see _RESOURCE_GATED below — every runner here is gated by default)
    "osm_ways": ["scripts/osm_extract.py", "--job", "ways"],
    # businesses rebuild chain (ruling §3.1-6): the two pulls refill their own
    # staging tables, then the conflate rebuilds core.businesses from staging.
    # Ordering is enforced by the weekly-businesses timer (pull -> pull ->
    # conflate), not the queue; these entries exist so an enqueued pull/conflate
    # job actually runs (consistency with the seeded derived sources) rather
    # than failing "no runner". fsq_places uses the keyless source.coop mirror
    # by default (no HF_TOKEN wait-item) — a token, if set, is used by the CLI.
    "overture_places": ["scripts/businesses_pipeline.py", "--pull-overture"],
    "fsq_places": ["scripts/businesses_pipeline.py", "--pull-fsq", "--fsq-mirror"],
    "businesses_conflate": ["scripts/businesses_pipeline.py", "--conflate"],
}


# Which derived runners the resource gate applies to. Default TRUE (gate it):
# every entry in _DERIVED_RUNNERS today is measured in tens of minutes to hours.
# Set False for a runner that is genuinely cheap — gating a job that finishes in
# seconds adds a failure mode and saves nothing.
_RESOURCE_GATED: dict[str, bool] = {
    RESCORE_SOURCE_ID: False,   # rescore is an in-database UPDATE, seconds
}


def _runner_env() -> dict[str, str]:
    """Subprocess environment for derived runners: os.environ WITH .env folded
    in (wave-1 gap: a worker that never ran run_source had not loaded .env, so
    runners missed DATABASE_URL/keys). load_dotenv mutates os.environ once and
    real env vars still win."""
    load_dotenv()
    return dict(os.environ)


def _run_derived_job(job: dict) -> None:
    """Execute one claimed DERIVED job via the _DERIVED_RUNNERS allow-list.

    Dispatch rules (all failures are honest 'failed' jobs, never crashes):
    - source_id not in the allow-list -> failed ("no derived-job runner").
    - argv script resolving outside <repo>/scripts/ -> failed (defense in
      depth: the dict is code, but a tampered/monkeypatched entry must never
      execute an arbitrary path).
    - script not created yet -> failed ("runner script missing").
    Runners execute with cwd=repo root and .env-loaded env (_runner_env); they
    write their own audited ops.source_runs rows.
    """
    source_id = job["source_id"]
    argv = _DERIVED_RUNNERS.get(source_id)
    if argv is None:
        with get_conn() as conn:
            jobs.finish_job(
                conn, job["job_id"], "failed",
                f"no derived-job runner for {source_id!r} "
                f"(engine allow-list: {sorted(_DERIVED_RUNNERS)})",
            )
        return
    script = (_REPO_ROOT / argv[0]).resolve()
    if _SCRIPTS_DIR.resolve() not in script.parents:
        with get_conn() as conn:
            jobs.finish_job(
                conn, job["job_id"], "failed",
                f"refusing derived runner for {source_id!r}: {argv[0]!r} "
                "resolves outside <repo>/scripts/",
            )
        return
    if not script.is_file():
        with get_conn() as conn:
            jobs.finish_job(
                conn, job["job_id"], "failed",
                f"runner script missing for {source_id!r}: {argv[0]} "
                "(not created yet — honest failed run, no work done)",
            )
        return
    # Resource gate. Every derived runner here is a HEAVY job — the OSM passes
    # run for hours and route_rebuild for ~50 minutes — and `Nice` only lowers
    # priority once started, it never declines to start. On 2026-07-27 an
    # idle-classed OSM pass still took the machine from 11 GB free to 1.6 GB
    # and had to be killed by hand.
    #
    # 'deferred' is NOT a failure: the job stays QUEUED and the next tick
    # retries it. Marking it failed would burn the backoff and eventually trip
    # the circuit breaker over a laptop that was merely busy.
    # '_'-prefixed ids are test fixtures. They must NEVER be gated: their
    # runners are synthetic and finish instantly, and gating them makes the
    # suite non-deterministic — it passed on an idle laptop and failed under
    # pytest's own load (load_per_cpu 3.6 against a 1.5 ceiling), which would
    # have surfaced as a flaky CI job rather than as this bug.
    if not source_id.startswith("_") and _RESOURCE_GATED.get(source_id, True):
        may_start, why = resources.check(work_path=_REPO_ROOT)
        if not may_start:
            print(f"[derived] {source_id}: {why}", flush=True)
            with get_conn() as conn:
                jobs.defer_job(conn, job["job_id"], why)
            return
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=_REPO_ROOT, env=_runner_env(),
        capture_output=True, text=True,
    )
    with get_conn() as conn:
        if proc.returncode == 0:
            jobs.finish_job(conn, job["job_id"], "done")
        else:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            jobs.finish_job(
                conn, job["job_id"], "failed",
                _redact(f"{source_id} runner exited {proc.returncode}: {detail}")[:500],
            )
        # Ruling §3.1-10 guard (quality_rescore only): a swap that committed
        # while this rescore was RUNNING had its enqueue swallowed by the
        # one-active-job index (and the in-flight rescore may have scored the
        # pre-swap table, or died on the dropped one) — re-queue so the fresh
        # table is rescored now, not at the 03:30 nightly.
        if (source_id == RESCORE_SOURCE_ID
                and job.get("started_at") is not None
                and jobs.snapshot_swapped_since(conn, job["started_at"])):
            enqueue_rescore(conn)


_NET_PROBE_HOST = "one.one.one.one"
_NET_PROBE_TTL_S = 30.0
_net_probe: tuple[float, bool] | None = None


def _network_up() -> bool:
    """Can this machine resolve anything at all?

    The same probe scripts/wait_ready.sh uses, for the same reason: getaddrinfo
    goes through NSS, which is the path requests actually takes, so it tests
    what the job will do rather than something adjacent.

    Cached for a few seconds — the queue can hand out many jobs a minute and
    the answer does not change that fast.
    """
    global _net_probe
    now = time.monotonic()
    if _net_probe is not None and now - _net_probe[0] < _NET_PROBE_TTL_S:
        return _net_probe[1]
    try:
        socket.getaddrinfo(_NET_PROBE_HOST, 443)
        up = True
    except OSError:
        up = False
    _net_probe = (now, up)
    return up


def worker_loop() -> None:
    """Drain ops.job_queue forever: claim_job -> run_source -> finish_job.
    One job at a time in MVP; sleeps briefly when the queue is empty.
    When no non-derived job is queued, derived jobs are claimed with
    jobs.claim_job(derived=True) and dispatched via the _DERIVED_RUNNERS
    allow-list — real ingests always take priority over derived work."""
    print("worker: draining ops.job_queue (Ctrl-C to stop)")
    while True:
        # Claim in its own SHORT transaction — a multi-minute ingest must not
        # hold the job-row lock open (the minutely tick's INSERT would wait on
        # it). A worker that dies mid-run leaves the job 'running'; the
        # freshness timer's stale-job reaper re-queues it (pipeline.md §6).
        with get_conn() as conn:
            job = jobs.claim_job(conn)
        if job is None:
            with get_conn() as conn:
                derived_job = jobs.claim_job(conn, derived=True)
            if derived_job is not None:
                _run_derived_job(derived_job)
                continue
            time.sleep(_WORKER_IDLE_SLEEP_S)
            continue
        # An offline machine is "not now", not "this feed is broken". Overnight
        # on 2026-08-18 the laptop lost DNS and the worker kept claiming jobs
        # and attempting fetches against nothing: 97 failed runs across eight
        # sources, every one of them "Max retries exceeded", none of them about
        # the sources. That noise buries a real failure and, left long enough,
        # would trip the circuit breaker on eight healthy feeds.
        #
        # ExecStartPre=wait_ready.sh gates the worker at START; it cannot help
        # when the network dies hours later. defer_job is the existing answer
        # and its own docstring argues this exact case: a deferral keeps the
        # job queued, spends no backoff, counts toward no breaker, and alerts
        # nobody.
        if not _network_up():
            with get_conn() as conn:
                jobs.defer_job(conn, job["job_id"],
                               "no DNS on this machine — deferring rather than "
                               "recording a failure the source did not cause")
            time.sleep(_WORKER_IDLE_SLEEP_S)
            continue
        try:
            run_source(job["source_id"])
        except BaseException as exc:  # run row already records the failure
            with get_conn() as conn:
                jobs.finish_job(conn, job["job_id"], "failed",
                                _redact(str(exc) or type(exc).__name__)[:500])
            if not isinstance(exc, Exception):
                raise  # SIGTERM/Ctrl-C: bookkeeping done, now actually stop
        else:
            with get_conn() as conn:
                jobs.finish_job(conn, job["job_id"], "done")


def main(argv: list[str]) -> int:
    """CLI: python -m truckintel.engine {tick | ingest <source_id> | worker}"""
    if len(argv) >= 1 and argv[0] == "tick":
        tick()
        return 0
    if len(argv) >= 2 and argv[0] == "ingest":
        run_source(argv[1])
        return 0
    if len(argv) >= 1 and argv[0] == "worker":
        # systemctl stop sends SIGTERM; convert to SystemExit so an in-flight
        # run closes its ops.source_runs row instead of dying as a phantom.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        worker_loop()
        return 0
    print(__doc__)
    print("usage: python -m truckintel.engine {tick | ingest <source_id> | worker}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
