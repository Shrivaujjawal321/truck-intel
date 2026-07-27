"""Source registry: one YAML per source in registry/, synced to ops.sources.

Git is the source of truth — adding/pausing a source is a commit, and
`git log registry/<id>.yaml` is the config audit trail.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

_KINDS = {"bulk_http", "arcgis", "live_json", "api_keyed"}
_LOAD_PATTERNS = {"snapshot_swap", "event_lifecycle", "upsert"}

# §5.1 naming map: the ONLY tables a YAML `target:` may name. The engine
# re-checks against this same set before interpolating the identifier into
# SQL — an unvalidated identifier never reaches a query string.
SNAPSHOT_TARGETS = frozenset({
    "core.bridges",
    "core.tunnels",
    "core.parking_sites",
    "core.truck_routes",
    "osm.ways",
    "osm.fuel_stations",
    "osm.rest_areas",
    "osm.weigh_points",
    "osm.truck_repair",
})

# `parser:` must be a bare module name inside truckintel/parsers/ — no dots,
# no path separators (dotted names would escape the parsers package).
_PARSER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_UPSERT_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, url, kind, load_pattern, schedule_minutes,
     slo_hours, license, attribution_text, gates, auth, parser, target,
     enabled, synced_at)
VALUES
    (%(id)s, %(name)s, %(owner)s, %(url)s, %(kind)s, %(load_pattern)s,
     %(schedule_minutes)s, %(slo_hours)s, %(license)s, %(attribution)s,
     %(gates)s, %(auth)s, %(parser)s, %(target)s, TRUE, now())
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
    parser           = EXCLUDED.parser,
    target           = EXCLUDED.target,
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
        license, attribution, slo_hours, gates (dict), auth (dict | None),
        parser (str | None), target (str | None)

    Phase-2 OPTIONAL keys (both default to the engine's hardcoded MVP maps):
    - parser: bare module name in truckintel/parsers/ — validated importable
      at sync time (a typo'd parser is caught at deploy, not at 3 a.m.)
    - target: schema-qualified snapshot_swap table — must be in the §5.1
      allow-list SNAPSHOT_TARGETS and only valid with load_pattern
      snapshot_swap (it would be silently ignored otherwise, so it errors)

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
        # Phase-2 gate-1 channel (pipeline.md §4.1 required_columns): a list of
        # row fields the engine's gate 1 requires present + non-null. Optional —
        # sources absent from the engine's MVP fallback map run gate 1 empty
        # otherwise, so new sources SHOULD declare it.
        required_fields = gates.get("required_fields")
        if required_fields is not None and (
            not isinstance(required_fields, list)
            or not required_fields
            or not all(isinstance(f, str) and f.strip() for f in required_fields)
        ):
            raise ValueError(
                f"{path}: gates.required_fields must be a non-empty list of "
                f"field names, got {required_fields!r}"
            )

        auth = doc.get("auth")
        if auth is not None:
            # Fail loudly on a malformed auth block; an env var whose *value*
            # is unset is fine (skipped_no_key path handles it at run time).
            if not isinstance(auth, dict):
                raise ValueError(f"{path}: auth must be a mapping or null")
            env_name = auth.get("env")
            if not isinstance(env_name, str) or not env_name.strip():
                raise ValueError(f"{path}: auth.env must name an environment variable")

        parser = doc.get("parser")
        if parser is not None:
            if not isinstance(parser, str) or not _PARSER_NAME_RE.match(parser):
                raise ValueError(
                    f"{path}: parser must be a bare module name in "
                    f"truckintel/parsers/ (lowercase identifier), got {parser!r}"
                )
            try:
                importlib.import_module(f"truckintel.parsers.{parser}")
            except Exception as exc:
                raise ValueError(
                    f"{path}: parser module truckintel.parsers.{parser} is not "
                    f"importable: {exc}"
                ) from exc

        target = doc.get("target")
        if target is not None:
            if target not in SNAPSHOT_TARGETS:
                raise ValueError(
                    f"{path}: target {target!r} is not in the §5.1 snapshot "
                    f"allow-list: {sorted(SNAPSHOT_TARGETS)}"
                )
            if doc["load_pattern"] != "snapshot_swap":
                raise ValueError(
                    f"{path}: target is only valid with load_pattern "
                    f"snapshot_swap, not {doc['load_pattern']!r}"
                )

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
                "parser": parser,
                "target": target,
            }
        )
    return sources


def sync_sources(conn: psycopg.Connection, sources: list[dict]) -> int:
    """Upsert registry entries into ops.sources (key: source_id).

    Sources present in the table but missing from the registry are marked
    enabled=false, never deleted (audit history must keep resolving).
    kind='derived' rows are registry-less by design (synthetic sources seeded
    in schema_phase2.sql, e.g. quality_rescore) — the sweep never disables them.
    Returns the number of rows written.
    """
    for src in sources:
        params = dict(src)
        params["gates"] = Jsonb(src["gates"])
        params["auth"] = Jsonb(src["auth"]) if src["auth"] is not None else None
        conn.execute(_UPSERT_SQL, params)
    conn.execute(
        "UPDATE ops.sources SET enabled = FALSE, synced_at = now() "
        "WHERE enabled AND kind != 'derived' AND source_id != ALL(%s)",
        ([src["id"] for src in sources],),
    )
    return len(sources)


if __name__ == "__main__":
    # `make sync` entry point: load registry, sync into ops.sources.
    from truckintel.db import get_conn

    with get_conn() as _conn:
        _n = sync_sources(_conn, load_registry())
        print(f"synced {_n} sources")
