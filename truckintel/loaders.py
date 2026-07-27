"""The three MVP load patterns (plan §5.1). Generic — parsers feed them dicts.

Every published row must carry (source_id, run_id, ingested_at, observed_at) —
the loaders stamp lineage; parsers supply observed_at (the fact's real-world
date, never the download date).

Row-dict -> column conventions (binding for parsers):
- keys matching target columns map 1:1
- 'lat'/'lon' pair, or 'geom_wkt' (WKT, EPSG:4326, may be None), becomes geom
- 'props' dict becomes the props JSONB blob
"""
from __future__ import annotations

import re
from itertools import chain
from typing import Iterable

import psycopg
from psycopg.types.json import Jsonb

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class EmptyPublishRefused(RuntimeError):
    """A full-table swap would have replaced live data with (near-)nothing.

    Distinct from a generic RuntimeError so callers and tests can assert on
    the specific refusal rather than string-matching a message.
    """


def _split_target(target: str) -> tuple[str, str]:
    schema, dot, table = target.partition(".")
    if not dot or not _IDENT_RE.match(schema) or not _IDENT_RE.match(table):
        raise ValueError(f"target must be a schema-qualified identifier, got {target!r}")
    return schema, table


def _table_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()
    if not rows:
        raise ValueError(f"no such table: {schema}.{table}")
    return [r[0] for r in rows]


def _geom_ewkt(row: dict) -> str | None:
    """EWKT for the row's geometry; None = honest NULL (never fabricated)."""
    if "geom_wkt" in row:
        return f"SRID=4326;{row['geom_wkt']}" if row["geom_wkt"] else None
    return f"SRID=4326;POINT({float(row['lon'])} {float(row['lat'])})"


_INDEXDEF_RE = re.compile(r"^(CREATE(?: UNIQUE)? INDEX )(\S+)( ON .+)$", re.DOTALL)


def _index_names_by_def(conn: psycopg.Connection, schema: str, table: str) -> dict[str, str]:
    """{indexdef-with-name-stripped: indexname} for one table. Used to map the
    auto-generated names on a swapped-in table back to the live table's
    original names (same definitions, both queried while the table holds the
    live name, so only the index-name token differs)."""
    out: dict[str, str] = {}
    for name, indexdef in conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = %s",
        (schema, table),
    ).fetchall():
        m = _INDEXDEF_RE.match(indexdef)
        out[(m.group(1) + m.group(3)) if m else indexdef] = name
    return out


def snapshot_swap(
    conn: psycopg.Connection,
    target: str,
    rows: Iterable[dict],
    *,
    source_id: str,
    run_id: int,
    min_rows: int = 1,
) -> int:
    """Atomic full-table replace for reference datasets (bridges, parking).

    Build `<target>_new` (same DDL, indexes included), COPY the rows in, then in
    one transaction RENAME-swap and drop the old table. A failed load never
    touches the live table — that is the point.
    `target` is schema-qualified, e.g. 'core.bridges'. Returns rows published.

    `min_rows` (default 1) is a floor on what may replace a live table. Without
    it an exhausted iterator swapped an EMPTY table over live data and returned
    0, which every caller then recorded as a successful run — a truncated
    upstream file, an aborted spool, or a parser that started yielding nothing
    would silently delete the dataset and report success. Engine-driven sources
    are gated on the registry's own `min_rows` before they ever reach here
    (engine.py); this is the backstop for DIRECT callers (osm_ways_job,
    businesses conflate) that bypass that layer entirely. Raising rolls back
    the caller's transaction, so the live table is untouched.

    Pass `min_rows=0` only where publishing nothing is genuinely meaningful —
    and say why at the call site.
    """
    schema, table = _split_target(target)
    new, old = f"{table}_new", f"{table}_old"

    # Everything below runs in the caller's transaction: any failure rolls the
    # whole build back and the live table is never touched.
    orig_index_names = _index_names_by_def(conn, schema, table)
    conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{new}"')
    conn.execute(f'CREATE TABLE "{schema}"."{new}" (LIKE "{schema}"."{table}" INCLUDING ALL)')
    columns = _table_columns(conn, schema, new)

    rows_iter = iter(rows)
    first = next(rows_iter, None)
    published = 0
    if first is not None:
        has_geom = "geom" in columns and (
            "geom_wkt" in first or ("lat" in first and "lon" in first)
        )
        insert_cols = [c for c in columns if c in first and c not in ("lat", "lon")]
        if has_geom and "geom" not in insert_cols:
            insert_cols.append("geom")
        for lineage_col in ("source_id", "run_id"):
            if lineage_col in columns and lineage_col not in insert_cols:
                insert_cols.append(lineage_col)

        col_sql = ", ".join(f'"{c}"' for c in insert_cols)
        with conn.cursor() as cur:
            with cur.copy(f'COPY "{schema}"."{new}" ({col_sql}) FROM STDIN') as copy:
                for row in chain([first], rows_iter):
                    values = []
                    for col in insert_cols:
                        if col == "geom" and has_geom:
                            values.append(_geom_ewkt(row))
                        elif col == "source_id":
                            values.append(row.get("source_id", source_id))
                        elif col == "run_id":
                            values.append(row.get("run_id", run_id))
                        elif col == "props":
                            values.append(Jsonb(row.get("props") or {}))
                        else:
                            values.append(row.get(col))
                    copy.write_row(values)
                    published += 1

    # Guard BEFORE the rename: past this point the live table is gone.
    if published < min_rows:
        raise EmptyPublishRefused(
            f"refusing to replace {target}: the load produced {published:,} "
            f"row(s), below the min_rows floor of {min_rows:,}. The live table "
            f"is untouched. If publishing this few rows is correct here, pass "
            f"min_rows explicitly at the call site."
        )

    conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{old}"')
    conn.execute(f'ALTER TABLE "{schema}"."{table}" RENAME TO "{old}"')
    conn.execute(f'ALTER TABLE "{schema}"."{new}" RENAME TO "{table}"')
    conn.execute(f'DROP TABLE "{schema}"."{old}"')
    # LIKE … INCLUDING ALL auto-names the copied indexes/constraints
    # (<table>_new_pkey style); rename them back so schema.sql's documented
    # names survive every swap and its idempotent re-apply never duplicates an
    # index. ALTER INDEX RENAME also renames a backing constraint (verified).
    for key, auto_name in _index_names_by_def(conn, schema, table).items():
        orig = orig_index_names.get(key)
        if orig and orig != auto_name:
            conn.execute(f'ALTER INDEX "{schema}"."{auto_name}" RENAME TO "{orig}"')
    return published


def event_lifecycle_upsert(
    conn: psycopg.Connection,
    rows: Iterable[dict],
    *,
    source_id: str,
    run_id: int,
    target: str = "core.live_events",
) -> int:
    """Live-feed lifecycle for core.live_events (NWS alerts in MVP).

    Upsert on (source_id, event_id): new events get first_seen=last_seen=now,
    seen-again events refresh last_seen (and geom/props). Events present in the
    table but absent from this poll get soft_closed_at=now() — rows are never
    deleted here. Returns rows upserted.
    """
    schema, table = _split_target(target)
    qualified = f'"{schema}"."{table}"'
    seen_ids: list[str] = []
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {qualified}
                    (event_id, source_id, kind, geom, first_seen, last_seen,
                     run_id, observed_at, props)
                VALUES (%s, %s, %s, ST_GeomFromEWKT(%s), now(), now(), %s, %s, %s)
                ON CONFLICT (source_id, event_id) DO UPDATE SET
                    last_seen      = now(),
                    soft_closed_at = NULL,
                    geom           = EXCLUDED.geom,
                    run_id         = EXCLUDED.run_id,
                    observed_at    = EXCLUDED.observed_at,
                    props          = EXCLUDED.props
                """,
                (
                    row["event_id"],
                    source_id,
                    row["kind"],
                    _geom_ewkt(row)
                    if ("geom_wkt" in row or ("lat" in row and "lon" in row))
                    else None,
                    run_id,
                    row.get("observed_at"),
                    Jsonb(row.get("props") or {}),
                ),
            )
            seen_ids.append(row["event_id"])
        # Soft-close what vanished from this poll — never delete.
        cur.execute(
            f"UPDATE {qualified} SET soft_closed_at = now() "
            "WHERE source_id = %s AND soft_closed_at IS NULL "
            "  AND NOT (event_id = ANY(%s))",
            (source_id, seen_ids),
        )
    return len(seen_ids)


def fuel_upsert(
    conn: psycopg.Connection,
    rows: Iterable[dict],
    *,
    source_id: str,
    run_id: int,
    target: str = "core.fuel_prices",
) -> int:
    """Native time series for core.fuel_prices.

    INSERT ... ON CONFLICT (region, product, week_of) DO UPDATE — re-fetching a
    week is idempotent; history accumulates one row per region/product/week.
    Returns rows upserted.
    """
    schema, table = _split_target(target)
    qualified = f'"{schema}"."{table}"'
    count = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {qualified}
                    (region, product, week_of, price_usd_gal,
                     source_id, run_id, observed_at, props)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (region, product, week_of) DO UPDATE SET
                    price_usd_gal = EXCLUDED.price_usd_gal,
                    source_id     = EXCLUDED.source_id,
                    run_id        = EXCLUDED.run_id,
                    ingested_at   = now(),
                    observed_at   = EXCLUDED.observed_at,
                    props         = EXCLUDED.props
                """,
                (
                    row["region"],
                    row["product"],
                    row["week_of"],
                    row["price_usd_gal"],
                    source_id,
                    run_id,
                    row.get("observed_at"),
                    Jsonb(row.get("props") or {}),
                ),
            )
            count += 1
    return count
