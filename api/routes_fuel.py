"""GET /v1/fuel/prices — EIA weekly regional averages (stub)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/v1/fuel/prices")
def list_fuel_prices(region: str | None = None):
    """Weekly regional diesel averages from core.fuel_prices. Response is
    explicitly labeled "kind": "regional_weekly_estimate" — never presented
    as station-level pump prices (no free legal source exists; honest gap)."""
    raise HTTPException(status_code=501, detail="not implemented: MVP scaffold stub")
