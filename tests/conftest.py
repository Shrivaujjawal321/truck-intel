"""Shared test plumbing: two skip markers, for two different preconditions.

Engine tests never hit the network. DB-backed tests use the live dev PostGIS
(truckintel-pg) but clean up after themselves and never drop core tables.

`needs_db` — is there a database at all?
`needs_data` — is the ROUTE NETWORK loaded into it?

The second one exists because CI builds a clean PostGIS container every run,
and a whole class of test here asserts against real loaded data: that a GPS
ping matches a truck route, that the map draws the same count the table holds,
that generalized geometry still covers the network. Those are integration
tests over 454,830 route segments, not unit tests, and on an empty schema they
fail for a reason that says nothing about the code.

Skipping is the honest outcome, not a dodge — the same call the repo already
makes when PostGIS is unreachable. What would be dishonest is loosening the
assertions until they pass on no data, because then they would keep passing
after the network went missing.
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


def _routes_loaded() -> bool:
    """True when core.truck_routes holds rows. The route spine is the shared
    precondition of every data-backed suite: the service layers are filtered
    against it and the tracking layer matches onto it."""
    try:
        with get_conn() as conn:
            return bool(conn.execute(
                "SELECT EXISTS (SELECT 1 FROM core.truck_routes LIMIT 1)"
            ).fetchone()[0])
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostGIS unreachable")

needs_data = pytest.mark.skipif(
    not _routes_loaded(),
    reason="core.truck_routes is empty — needs a populated database "
           "(make ingest SOURCE=ntad_national_network)")
