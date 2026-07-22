"""Weekly digest tests — pure render (no DB) + a live integration smoke.

Run: uv run pytest tests/test_weekly_digest.py
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from tests.conftest import needs_db

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "weekly_digest", REPO_ROOT / "scripts" / "weekly_digest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wd = _load()

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def _fresh(sid, age_h, slo, state, status="success"):
    return {"source_id": sid, "slo": slo, "kind": "live_json", "age_h": age_h,
            "state": state, "status": status, "message": ""}


def _act(sid, runs, ok, failed, rows, running=0):
    return {"source_id": sid, "runs": runs, "ok": ok, "skipped": 0,
            "failed": failed, "running": running, "rows_pub": rows,
            "last_at": _NOW}


def test_render_attention_lists_stale_and_failing():
    body = wd.render(
        activity=[_act("wzdx_az", 3, 1, 2, 100)],
        freshness=[_fresh("nws_alerts", 5.0, 1, "STALE"),
                   _fresh("wzdx_az", 2.0, 24, "fresh")],
        holdings=[{"label": "bridges", "table": "core.bridges",
                   "count": 629710, "vintage": "2025-01-01"}],
        breakers=[{"source_id": "wzdx_mn", "state": "open", "fails": 6,
                   "opened_at": _NOW, "last_failure_at": _NOW}],
        days=7, generated="2026-07-23 12:00 UTC",
    )
    assert "Needs a human" in body
    assert "STALE: nws_alerts" in body
    assert "BREAKER OPEN: wzdx_mn" in body
    assert "FAILING: wzdx_az" in body          # failed runs, not already stale
    # sections present
    assert "## Ingestion activity" in body
    assert "## Freshness vs SLO" in body
    assert "## Data holdings" in body
    assert "629,710" in body                    # thousands-formatted


def test_render_all_clear_when_fresh():
    body = wd.render(
        activity=[_act("wzdx_az", 3, 3, 0, 100)],
        freshness=[_fresh("wzdx_az", 2.0, 24, "fresh")],
        holdings=[{"label": "bridges", "table": "core.bridges",
                   "count": 629710, "vintage": "2025-01-01"}],
        breakers=[],
        days=7, generated="2026-07-23 12:00 UTC",
    )
    assert "✓ Needs a human" in body
    assert "nothing" in body
    # no breaker section when none are open
    assert "Circuit breakers" not in body


def test_deliver_without_config_is_honest_not_a_crash(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    results = wd.deliver("hello")
    assert len(results) == 1
    assert results[0]["channel"] == "telegram"
    assert results[0]["status"] == "skipped"     # unconfigured != error


def test_telegram_body_marks_truncation_not_silent():
    big = "x" * 6000
    out = wd._telegram_body(big)
    assert len(out) <= wd._TELEGRAM_LIMIT
    assert "truncated" in out and "status_weekly.md" in out


def test_source_surfaced_once_breaker_and_failing():
    # a source that is both breaker-open AND has failed runs appears ONCE
    body = wd.render(
        activity=[_act("wzdx_mn", 5, 0, 5, 0)],
        freshness=[_fresh("wzdx_mn", 3.0, 24, "warn")],   # not stale
        holdings=[],
        breakers=[{"source_id": "wzdx_mn", "state": "open", "fails": 6,
                   "opened_at": _NOW, "last_failure_at": _NOW}],
        days=7, generated="2026-07-23 12:00 UTC",
    )
    assert body.count("wzdx_mn") >= 1
    # exactly one attention line for wzdx_mn (BREAKER OPEN), not also FAILING
    attention = body.split("## Ingestion")[0]
    assert "BREAKER OPEN: wzdx_mn" in attention
    assert "FAILING: wzdx_mn" not in attention


@needs_db
def test_build_digest_against_live_db():
    from truckintel.db import get_conn
    with get_conn() as conn:
        body = wd.build_digest(conn, days=7)
    assert body.startswith("# truck-intel weekly digest")
    assert "## Freshness vs SLO" in body
    assert "## Data holdings" in body
