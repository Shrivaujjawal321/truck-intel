"""GET /v1/live/weather-alerts — active NWS alert polygons (stub)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/v1/live/weather-alerts")
def list_weather_alerts(bbox: str):
    """Active (not soft-closed) NWS alerts intersecting the bbox, from
    core.live_events. Alerts without polygon geometry are zone-only and are
    reported as such, not silently dropped."""
    raise HTTPException(status_code=501, detail="not implemented: MVP scaffold stub")
