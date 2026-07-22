"""GET /v1/parking — truck parking sites (stub)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/v1/parking")
def list_parking(bbox: str):
    """Parking sites in a bbox with capacity + vintage badge. HONEST: NTAD
    truck_spaces are ~2019 survey-era estimates and observed_at says so;
    NULL capacity renders 'unknown'."""
    raise HTTPException(status_code=501, detail="not implemented: MVP scaffold stub")
