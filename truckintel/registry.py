"""Source registry: one YAML per source in registry/, synced to ops.sources.

Git is the source of truth — adding/pausing a source is a commit, and
`git log registry/<id>.yaml` is the config audit trail.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

_KINDS = {"bulk_http", "arcgis", "live_json", "api_keyed"}
_LOAD_PATTERNS = {"snapshot_swap", "event_lifecycle", "upsert"}

_UPSERT_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, url, kind, load_pattern, schedule_minutes,
     slo_hours, license, attribution_text, gates, auth, enabled, synced_at)
VALUES
    (%(id)s, %(name)s, %(owner)s, %(url)s, %(kind)s, %(load_pattern)s,
     %(schedule_minutes)s, %(slo_hours)s, %(license)s, %(attribution)s,
     %(gates)s, %(auth)s, TRUE, now())
ON CONFLICT (source_id) DO UPDATE SET
    name             = EXCLUDED.name,
    owner            = EXCLUDED.owner,
    url              = EXCLUDED.url,
    kind             = EXCLUDED.kind,
    load_pattern     = EXCLUDED.load_pattern,
    schedule_minutes = EXCLUDED.schedule_minutes,
    slo_hours        = EXCLUDED.slo_hours,
    license          = EXCLUDED.license,
    attribution_text = EXCLUDED.attribution_text,
    gates            = EXCLUDED.gates,
    auth             = EXCLUDED.auth,
    enabled          = TRUE,
    synced_at        = now()
"""


def _positive_int(value: object, field: str, path: Path) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{path}: {field} must be a positive int, got {value!r}")
    return value


def load_registry(registry_dir: str | Path = "registry") -> list[dict]:
    """Load and validate every registry/*.yaml.

    Returns one dict per source with exactly these keys:
        id, name, owner, url, kind, load_pattern, schedule_minutes,
        license, attribution, slo_hours, gates (dict), auth (dict | None)

    Validation to implement:
    - kind in {bulk_http, arcgis, live_json, api_keyed}
    - load_pattern in {snapshot_swap, event_lifecycle, upsert}
    - schedule_minutes and slo_hours positive ints
    - if auth.env is set, fail LOUDLY at sync time when the env var is
      referenced but the variable name is empty (a forgotten key is caught at
      deploy, not at 3 a.m.; an *unset value* for EIA_API_KEY is allowed —
      that is the graceful skipped_no_key path, plan rule).
    """
    paths = sorted(Path(registry_dir).glob("*.yaml"))
    if not paths:
        raise ValueError(f"no *.yaml files found in {registry_dir!r}")

    sources: list[dict] = []
    seen_ids: set[str] = set()
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: malformed YAML: {exc}") from exc
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: expected a YAML mapping, got {type(doc).__name__}")

        for field in ("id", "name", "url", "kind", "load_pattern"):
            if not isinstance(doc.get(field), str) or not doc[field].strip():
                raise ValueError(f"{path}: missing/empty required field {field!r}")
        if doc["id"] in seen_ids:
            raise ValueError(f"{path}: duplicate source id {doc['id']!r}")
        seen_ids.add(doc["id"])
        if doc["kind"] not in _KINDS:
            raise ValueError(f"{path}: kind {doc['kind']!r} not in {sorted(_KINDS)}")
        if doc["load_pattern"] not in _LOAD_PATTERNS:
            raise ValueError(
                f"{path}: load_pattern {doc['load_pattern']!r} not in {sorted(_LOAD_PATTERNS)}"
            )

        gates = doc.get("gates") or {}
        if not isinstance(gates, dict):
            raise ValueError(f"{path}: gates must be a mapping")

        auth = doc.get("auth")
        if auth is not None:
            # Fail loudly on a malformed auth block; an env var whose *value*
            # is unset is fine (skipped_no_key path handles it at run time).
            if not isinstance(auth, dict):
                raise ValueError(f"{path}: auth must be a mapping or null")
            env_name = auth.get("env")
            if not isinstance(env_name, str) or not env_name.strip():
                raise ValueError(f"{path}: auth.env must name an environment variable")

        sources.append(
            {
                "id": doc["id"],
                "name": doc["name"],
                "owner": doc.get("owner"),
                "url": doc["url"],
                "kind": doc["kind"],
                "load_pattern": doc["load_pattern"],
                "schedule_minutes": _positive_int(
                    doc.get("schedule_minutes"), "schedule_minutes", path
                ),
                "license": doc.get("license"),
                "attribution": doc.get("attribution"),
                "slo_hours": _positive_int(doc.get("slo_hours"), "slo_hours", path),
                "gates": gates,
                "auth": auth,
            }
        )
    return sources


def sync_sources(conn: psycopg.Connection, sources: list[dict]) -> int:
    """Upsert registry entries into ops.sources (key: source_id).

    Sources present in the table but missing from the registry are marked
    enabled=false, never deleted (audit history must keep resolving).
    Returns the number of rows written.
    """
    for src in sources:
        params = dict(src)
        params["gates"] = Jsonb(src["gates"])
        params["auth"] = Jsonb(src["auth"]) if src["auth"] is not None else None
        conn.execute(_UPSERT_SQL, params)
    conn.execute(
        "UPDATE ops.sources SET enabled = FALSE, synced_at = now() "
        "WHERE enabled AND source_id != ALL(%s)",
        ([src["id"] for src in sources],),
    )
    return len(sources)


if __name__ == "__main__":
    # `make sync` entry point: load registry, sync into ops.sources.
    from truckintel.db import get_conn

    with get_conn() as _conn:
        _n = sync_sources(_conn, load_registry())
        print(f"synced {_n} sources")
