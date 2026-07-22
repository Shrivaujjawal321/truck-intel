"""Database access: get_conn() + tiny helpers. Plain SQL by design — no ORM."""
from __future__ import annotations

from typing import Any, Sequence

import psycopg

from truckintel.config import database_url


def get_conn(autocommit: bool = False) -> psycopg.Connection:
    """Open a new connection from DATABASE_URL.

    Caller owns the transaction; prefer `with get_conn() as conn:` which commits
    on clean exit and rolls back on exception (psycopg3 semantics).
    """
    return psycopg.connect(database_url(), autocommit=autocommit)


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> tuple | None:
    """One-shot query returning the first row (or None)."""
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """One-shot query returning all rows."""
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Run one statement in its own transaction; return affected row count
    (-1 when the driver cannot tell)."""
    with get_conn() as conn:
        return conn.execute(sql, params).rowcount
