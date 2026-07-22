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
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from truckintel import jobs
from truckintel.config import load_dotenv, raw_dir
from truckintel.db import get_conn
from truckintel.loaders import event_lifecycle_upsert, fuel_upsert, snapshot_swap
from truckintel.politeness import PoliteRefusal, PoliteResult, polite_get
from truckintel.registry import load_registry, sync_sources
from truckintel.validate import gate1_schema, gate2_coords

# source_id -> parser module (pipeline.md §16: one parser per format).
_PARSER_MODULE = {
    "nbi_annual": "nbi",
    "ntad_parking": "ntad_parking",
    "nws_alerts": "nws",
    "eia_diesel": "eia",
}

# Raw-zone file extension per fetch kind (interface: what the parser receives).
_RAW_EXT = {"bulk_http": "zip", "arcgis": "geojson", "live_json": "json", "api_keyed": "json"}

# Gate-1 required fields per source (the MVP registry carries no
# required_columns; these mirror each parser's documented output contract).
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "nbi_annual": ("nbi_id", "lat", "lon"),
    "ntad_parking": ("site_id", "kind", "lat", "lon"),
    "nws_alerts": ("event_id", "kind", "observed_at"),
    "eia_diesel": ("region", "product", "week_of", "price_usd_gal"),
}

# snapshot_swap targets (plan §5.1 naming map).
_SNAPSHOT_TARGET = {"nbi_annual": "core.bridges", "ntad_parking": "core.parking_sites"}

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
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), message = %s, "
            "rows_in = %s, rows_published = %s, rows_rejected = %s, raw_sha256 = %s, "
            "http_status = %s WHERE run_id = %s",
            (status, message, rows_in, rows_published, rows_rejected, raw_sha256,
             http_status, run_id),
        )


def _load_source(source_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source_id, url, kind, load_pattern, gates, auth "
            "FROM ops.sources WHERE source_id = %s",
            (source_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown source {source_id!r} — run `make sync` first")
    return dict(zip(("source_id", "url", "kind", "load_pattern", "gates", "auth"), row))


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
    parser = importlib.import_module(f"truckintel.parsers.{_PARSER_MODULE[source_id]}")
    # parse streams straight into gate 1 — never a second full materialization
    ok1, rejects1 = gate1_schema(parser.parse(content), _REQUIRED_FIELDS.get(source_id, ()))
    rows_in = len(ok1) + len(rejects1)
    ok_rows, rejects2 = gate2_coords(ok1)
    rejects = rejects1 + rejects2
    _write_rejects(source_id, run_id, rejects)

    # step 6 — registry gates: abort publish, old table stays live.
    # Unconditional safety gate first: a poll whose rows are ALL rejected is
    # upstream schema drift (pipeline.md §10.2), never a publishable state —
    # without this, an event_lifecycle publish of [] would soft-close every
    # active event and report success.
    gates = src.get("gates") or {}
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
                conn, _SNAPSHOT_TARGET[source_id], ok_rows,
                source_id=source_id, run_id=run_id,
            )
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


def worker_loop() -> None:
    """Drain ops.job_queue forever: claim_job -> run_source -> finish_job.
    One job at a time in MVP; sleeps briefly when the queue is empty."""
    print("worker: draining ops.job_queue (Ctrl-C to stop)")
    while True:
        # Claim in its own SHORT transaction — a multi-minute ingest must not
        # hold the job-row lock open (the minutely tick's INSERT would wait on
        # it). A worker that dies mid-run leaves the job 'running'; the
        # freshness timer's stale-job reaper re-queues it (pipeline.md §6).
        with get_conn() as conn:
            job = jobs.claim_job(conn)
        if job is None:
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
