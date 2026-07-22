"""Shared test plumbing: DB-availability skip marker.

Engine tests never hit the network. DB-backed tests use the live dev PostGIS
(truckintel-pg) but clean up after themselves and never drop core tables.
"""
from __future__ import annotations

import pytest

from truckintel.db import get_conn


def _db_available() -> bool:
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostGIS unreachable")
