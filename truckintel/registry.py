"""Source registry: one YAML per source in registry/, synced to ops.sources.

Git is the source of truth — adding/pausing a source is a commit, and
`git log registry/<id>.yaml` is the config audit trail.
"""
from __future__ import annotations

from pathlib import Path

import psycopg


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
    raise NotImplementedError


def sync_sources(conn: psycopg.Connection, sources: list[dict]) -> int:
    """Upsert registry entries into ops.sources (key: source_id).

    Sources present in the table but missing from the registry are marked
    enabled=false, never deleted (audit history must keep resolving).
    Returns the number of rows written.
    """
    raise NotImplementedError


if __name__ == "__main__":
    # `make sync` entry point: load registry, sync into ops.sources.
    from truckintel.db import get_conn

    with get_conn() as _conn:
        _n = sync_sources(_conn, load_registry())
        print(f"synced {_n} sources")
