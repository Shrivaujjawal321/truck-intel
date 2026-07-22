"""truck-intel API. MVP: /v1/health is real; the 5 data endpoints are stubs
(HTTP 501) until the connectors land. Run: make api
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api import routes_bridges, routes_fuel, routes_live, routes_meta, routes_parking
from truckintel import db

app = FastAPI(
    title="truck-intel",
    version="0.1.0",
    description="US truck intelligence — free, legal, honestly-labeled data. MVP spine.",
)

for _router in (
    routes_bridges.router,
    routes_parking.router,
    routes_live.router,
    routes_fuel.router,
    routes_meta.router,
):
    app.include_router(_router)


@app.get("/v1/health")
def health():
    """Liveness: SELECT 1 against PostGIS. Honest: DB down -> 503, never a fake ok."""
    try:
        row = db.fetch_one("SELECT 1")
        db_ok = row is not None and row[0] == 1
    except Exception as exc:  # connection refused, auth failure, ...
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": f"down ({type(exc).__name__})"},
        )
    if not db_ok:
        return JSONResponse(
            status_code=503, content={"status": "degraded", "database": "bad response"}
        )
    return {"status": "ok", "database": "up"}
