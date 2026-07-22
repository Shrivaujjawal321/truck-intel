"""GET /v1/bridges — the signature low-bridge query (stub)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/v1/bridges")
def list_bridges(bbox: str, max_clearance_lt_in: float | None = None):
    """Bridges in a bbox, optionally only those with min vertical clearance
    below the given inches. GeoJSON out; every feature carries source, vintage
    (observed_at) and confidence (NULL = unknown in MVP). bbox='minLon,minLat,
    maxLon,maxLat', capped at 4x4 degrees.
    """
    raise HTTPException(status_code=501, detail="not implemented: MVP scaffold stub")
