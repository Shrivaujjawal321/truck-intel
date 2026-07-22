"""Shared plumbing for the /v1 surface.

Three binding rules live here so every route inherits them:
- one error envelope everywhere: {"error": {"code", "message"}} with stable codes
  (invalid_bbox, bbox_too_large, invalid_param, upstream_unavailable, not_found)
- read-only DB access: the API session cannot write, enforced by Postgres itself
- tri-state honesty: NULL renders as "unknown", never as 0, empty, or "no"
"""
from __future__ import annotations

import json
from typing import Any, Sequence

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from starlette.exceptions import HTTPException as StarletteHTTPException

from truckintel.config import database_url

MAX_BBOX_DEG = 4.0


class ApiError(Exception):
    """Raise anywhere in a route; the handler renders the one envelope."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _envelope(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def install_error_handlers(app: FastAPI) -> None:
    """Every failure path funnels into the one envelope."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's 422 shape replaced: bbox problems get their own stable code.
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(part) for part in first.get("loc", ())]
        param = loc[-1] if loc else "request"
        code = "invalid_bbox" if param == "bbox" else "invalid_param"
        return _envelope(code, f"{param}: {first.get('msg', 'invalid value')}", 400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _envelope(codes.get(exc.status_code, "error"), str(exc.detail), exc.status_code)


def connect_ro() -> psycopg.Connection:
    """New read-only session. `default_transaction_read_only=on` means even a
    buggy route physically cannot write — defense in depth, not convention."""
    return psycopg.connect(
        database_url(),
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    )


def q_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Run one read-only query; any DB trouble -> 503 upstream_unavailable."""
    try:
        with connect_ro() as conn:
            return conn.execute(sql, params).fetchall()
    except psycopg.Error as exc:
        raise ApiError(
            "upstream_unavailable",
            f"database unavailable ({type(exc).__name__})",
            status=503,
        ) from exc


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """'minLon,minLat,maxLon,maxLat' -> 4 floats. Capped at 4x4 degrees."""
    parts = raw.split(",")
    if len(parts) != 4:
        raise ApiError("invalid_bbox", "bbox must be 'minLon,minLat,maxLon,maxLat'")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError:
        raise ApiError("invalid_bbox", "bbox values must be numbers") from None
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ApiError("invalid_bbox", "longitude must be within [-180, 180]")
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ApiError("invalid_bbox", "latitude must be within [-90, 90]")
    if min_lon > max_lon or min_lat > max_lat:
        raise ApiError("invalid_bbox", "bbox min corner must be south-west of max corner")
    if (max_lon - min_lon) > MAX_BBOX_DEG or (max_lat - min_lat) > MAX_BBOX_DEG:
        raise ApiError(
            "bbox_too_large",
            f"bbox is {max_lon - min_lon:.2f}x{max_lat - min_lat:.2f} degrees; "
            f"the cap is {MAX_BBOX_DEG:g}x{MAX_BBOX_DEG:g}",
        )
    return (min_lon, min_lat, max_lon, max_lat)


def unknown(value: Any) -> Any:
    """NULL renders as 'unknown'. 0 and '' are real data and pass through."""
    return "unknown" if value is None else value


def feature(fid: Any, geojson_geom: str | None, properties: dict) -> dict:
    """One GeoJSON Feature. geojson_geom is ST_AsGeoJSON output (or NULL)."""
    return {
        "type": "Feature",
        "id": fid,
        "geometry": json.loads(geojson_geom) if geojson_geom else None,
        "properties": properties,
    }


def feature_collection(features: list[dict], **extra: Any) -> dict:
    return {"type": "FeatureCollection", "count": len(features), **extra, "features": features}
