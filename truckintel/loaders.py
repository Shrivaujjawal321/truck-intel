"""The three MVP load patterns (plan §5.1). Generic — parsers feed them dicts.

Every published row must carry (source_id, run_id, ingested_at, observed_at) —
the loaders stamp lineage; parsers supply observed_at (the fact's real-world
date, never the download date).
"""
from __future__ import annotations

from typing import Iterable, Iterator

import psycopg


def snapshot_swap(
    conn: psycopg.Connection,
    target: str,
    rows: Iterable[dict],
    *,
    source_id: str,
    run_id: int,
) -> int:
    """Atomic full-table replace for reference datasets (bridges, parking).

    Build `<target>_new` (same DDL, indexes included), COPY the rows in, then in
    one transaction RENAME-swap and drop the old table. A failed load never
    touches the live table — that is the point.
    `target` is schema-qualified, e.g. 'core.bridges'. Returns rows published.
    """
    raise NotImplementedError


def event_lifecycle_upsert(
    conn: psycopg.Connection,
    rows: Iterable[dict],
    *,
    source_id: str,
    run_id: int,
) -> int:
    """Live-feed lifecycle for core.live_events (NWS alerts in MVP).

    Upsert on (source_id, event_id): new events get first_seen=last_seen=now,
    seen-again events refresh last_seen (and geom/props). Events present in the
    table but absent from this poll get soft_closed_at=now() — rows are never
    deleted here. Returns rows upserted.
    """
    raise NotImplementedError


def fuel_upsert(
    conn: psycopg.Connection,
    rows: Iterable[dict],
    *,
    source_id: str,
    run_id: int,
) -> int:
    """Native time series for core.fuel_prices.

    INSERT ... ON CONFLICT (region, product, week_of) DO UPDATE — re-fetching a
    week is idempotent; history accumulates one row per region/product/week.
    Returns rows upserted.
    """
    raise NotImplementedError
