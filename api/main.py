"""truck-intel API — the MVP /v1 surface (plan §6/§11): bridges, parking,
weather-alerts, fuel prices, coverage, health. Run: make api
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api import common, routes_bridges, routes_fuel, routes_live, routes_meta, routes_parking

app = FastAPI(
    title="truck-intel",
    version="0.1.0",
    description="US truck intelligence — free, legal, honestly-labeled data. MVP spine.",
)
common.install_error_handlers(app)

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
    """Liveness: SELECT 1 + age of the newest ops.source_runs row (the engine's
    observable tick). Honest: DB down -> 503, never a fake ok."""
    try:
        with common.connect_ro() as conn:
            db_ok = conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
            last_run = conn.execute(
                "SELECT max(started_at) AS t FROM ops.source_runs"
            ).fetchone()["t"]
    except Exception as exc:  # connection refused, auth failure, ...
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": f"down ({type(exc).__name__})"},
        )
    if not db_ok:
        return JSONResponse(
            status_code=503, content={"status": "degraded", "database": "bad response"}
        )
    age = (datetime.now(timezone.utc) - last_run).total_seconds() if last_run else None
    return {
        "status": "ok",
        "database": "up",
        "last_run_at": last_run,  # newest ops.source_runs row; null = engine never ran
        "last_run_age_seconds": round(age, 1) if age is not None else None,
    }
