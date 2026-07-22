"""GET /v1/meta/coverage — the honesty surface, from ops.sources + ops.source_runs."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from api import common

router = APIRouter()

# Which core table each MVP source publishes into (closed map — never built
# from user input, so the f-string table name below is injection-safe).
_TABLES = {
    "nbi_annual": "core.bridges",
    "ntad_parking": "core.parking_sites",
    "nws_alerts": "core.live_events",
    "eia_diesel": "core.fuel_prices",
}
_VINTAGE_NOTES = {
    "nbi_annual": "annual snapshot; observed_at = NBI inventory vintage",
    "ntad_parking": "~2019 Jason's Law survey era — re-downloaded, not re-observed",
    "nws_alerts": "live feed; observed_at = alert issue time",
    "eia_diesel": "weekly regional averages; observed_at = survey week",
}


@router.get("/v1/meta/coverage")
def coverage() -> dict:
    """Per-source truth: last run, last success, freshness-SLO state, row
    counts, observed_at range (vintage), license + attribution."""
    sources = common.q_all(
        """
        SELECT source_id, name, owner, kind, load_pattern, schedule_minutes,
               slo_hours, license, attribution_text, enabled, verify_status
        FROM ops.sources ORDER BY source_id
        """
    )
    last_runs = {
        r["source_id"]: r
        for r in common.q_all(
            """
            SELECT DISTINCT ON (source_id)
                   source_id, run_id, status, started_at, finished_at,
                   rows_in, rows_published, rows_rejected, message
            FROM ops.source_runs ORDER BY source_id, started_at DESC
            """
        )
    }
    # skipped_unchanged counts as fresh: a 304 proves the published data is
    # still the source's current data. skipped_no_key does NOT (nothing flowed).
    last_ok = {
        r["source_id"]: r["t"]
        for r in common.q_all(
            """
            SELECT source_id, max(started_at) AS t FROM ops.source_runs
            WHERE status IN ('success', 'skipped_unchanged') GROUP BY source_id
            """
        )
    }
    now = datetime.now(timezone.utc)

    out = []
    for s in sources:
        sid = s["source_id"]
        entry: dict = {
            "source_id": sid,
            "name": s["name"],
            "owner": common.unknown(s["owner"]),
            "kind": s["kind"],
            "load_pattern": s["load_pattern"],
            "enabled": s["enabled"],
            "verify_status": s["verify_status"],
            "license": common.unknown(s["license"]),
            "attribution": common.unknown(s["attribution_text"]),
            "slo_hours": s["slo_hours"],
            "vintage": _VINTAGE_NOTES.get(sid, "see observed_at_range"),
            "last_run": last_runs.get(sid),  # full audit row; null = never ran
        }

        ok_at = last_ok.get(sid)
        if ok_at is None:
            entry["slo_status"] = "never_ran"
            entry["last_success_at"] = None
            entry["last_success_age_hours"] = None
        else:
            age_h = (now - ok_at).total_seconds() / 3600
            entry["slo_status"] = "ok" if age_h <= s["slo_hours"] else "stale"
            entry["last_success_at"] = ok_at
            entry["last_success_age_hours"] = round(age_h, 2)

        table = _TABLES.get(sid)
        if table is None:
            entry["row_count"] = "unknown"
            entry["observed_at_range"] = "unknown"
        else:
            stats = common.q_all(
                f"SELECT count(*) AS n, min(observed_at) AS omin, max(observed_at) AS omax "
                f"FROM {table} WHERE source_id = %s",
                [sid],
            )[0]
            entry["row_count"] = stats["n"]
            entry["observed_at_range"] = (
                {"min": common.unknown(stats["omin"]), "max": common.unknown(stats["omax"])}
                if stats["n"]
                else None
            )
            if table == "core.live_events":
                entry["rows_active"] = common.q_all(
                    "SELECT count(*) AS n FROM core.live_events "
                    "WHERE source_id = %s AND soft_closed_at IS NULL",
                    [sid],
                )[0]["n"]
        out.append(entry)

    return {
        "generated_at": now,
        "note": (
            "Honesty surface: freshness, vintage, and row counts read straight "
            "from ops.sources + ops.source_runs. Empty sources = registry not "
            "yet synced."
        ),
        "sources": out,
    }
