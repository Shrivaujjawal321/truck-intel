"""GET /v1/meta/coverage — the honesty surface (stub)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/v1/meta/coverage")
def coverage():
    """Per-dataset truth: row counts, data vintage (observed_at range), last
    successful run, freshness-SLO state, known gaps. Built from ops.sources +
    ops.source_runs — honesty as an API, from day one."""
    raise HTTPException(status_code=501, detail="not implemented: MVP scaffold stub")
