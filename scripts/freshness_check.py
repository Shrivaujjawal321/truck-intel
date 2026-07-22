"""Freshness SLO check (MASTER_PLAN §11): one timer that catches every failure class.

For each source in registry/*.yaml, compare the age of its last SUCCESSFUL run
(ops.source_runs) against the registry slo_hours. Prints one line per source;
violations go to stderr. Exit codes: 0 all fresh, 1 violations, 2 cannot check
(DB unreachable — also a page-worthy failure, never silently OK).

slo_hours is read from the registry YAMLs directly (git is the source of truth),
so the check works even before ops.sources is synced.

--telegram is accepted but is a NO-OP in MVP: the alert hook lands post-MVP
(print + exit code is the MVP contract). TODO markers below show the seam.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from truckintel.db import execute, fetch_all

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
# Both prove the feed is alive: re-verified-unchanged data is fresh data.
FRESH_STATUSES = ["success", "skipped_unchanged"]
# Stuck-run/job reaper horizon (pipeline.md §6): anything 'running' this long
# is a dead worker (OOM/SIGKILL), not a slow ingest.
STALE_RUNNING_HOURS = 2


def reap_stale() -> None:
    """Close phantom 'running' rows a dead worker left behind: the run row is
    marked failed (audit must not lie forever) and the job re-queued so the
    source retries without human intervention."""
    n_runs = execute(
        "UPDATE ops.source_runs SET status = 'failed', finished_at = now(), "
        "message = 'reaped: stale running row (worker died mid-run)' "
        "WHERE status = 'running' AND started_at < now() - make_interval(hours => %s)",
        (STALE_RUNNING_HOURS,))
    n_jobs = execute(
        "UPDATE ops.job_queue SET status = 'queued', started_at = NULL, "
        "message = 'reaped: stale running job' "
        "WHERE status = 'running' AND started_at < now() - make_interval(hours => %s)",
        (STALE_RUNNING_HOURS,))
    if n_runs or n_jobs:
        print(f"reaped {n_runs} stale run(s), re-queued {n_jobs} stale job(s)")


def load_slos(registry_dir: Path = REGISTRY_DIR) -> dict[str, int]:
    """source_id -> slo_hours, straight from registry/*.yaml."""
    slos: dict[str, int] = {}
    for path in sorted(registry_dir.glob("*.yaml")):
        y = yaml.safe_load(path.read_text())
        slos[y["id"]] = int(y["slo_hours"])
    return slos


def last_runs() -> tuple[dict[str, datetime], dict[str, tuple[str, str]]]:
    """Per source: time of last fresh run, and (status, message) of last run of any kind."""
    fresh = dict(fetch_all(
        "SELECT source_id, max(coalesce(finished_at, started_at)) FROM ops.source_runs"
        " WHERE status = ANY(%s) GROUP BY source_id", (FRESH_STATUSES,)))
    latest = {sid: (status, msg or "") for sid, status, msg in fetch_all(
        "SELECT DISTINCT ON (source_id) source_id, status, left(message, 160)"
        " FROM ops.source_runs ORDER BY source_id, started_at DESC")}
    return fresh, latest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram", action="store_true",
                        help="no-op in MVP; reserved for the alert hook")
    args = parser.parse_args()

    if args.telegram:
        # TODO(post-MVP): send violation summary via Telegram bot API.
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("[--telegram] no TELEGRAM_BOT_TOKEN configured — printing only (TODO: alert hook).")
        else:
            print("[--telegram] token present but sending is post-MVP — printing only (TODO: alert hook).")

    slos = load_slos()
    if not slos:
        print("no sources in registry/ — nothing to check", file=sys.stderr)
        return 2
    try:
        reap_stale()
        fresh, latest = last_runs()
    except Exception as exc:
        print(f"CANNOT CHECK FRESHNESS: database unreachable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    violations = 0
    for source_id, slo_hours in sorted(slos.items()):
        ok_at = fresh.get(source_id)
        age_h = (now - ok_at).total_seconds() / 3600 if ok_at else None
        status, msg = latest.get(source_id, (None, ""))
        context = f"; last run: {status}" + (f" — {msg}" if msg else "") if status else "; never ran"
        if age_h is None:
            violations += 1
            print(f"FRESHNESS VIOLATION {source_id}: no successful run ever"
                  f" (SLO {slo_hours}h){context}", file=sys.stderr)
        elif age_h > slo_hours:
            violations += 1
            print(f"FRESHNESS VIOLATION {source_id}: last success {age_h:.1f}h ago"
                  f" exceeds SLO {slo_hours}h{context}", file=sys.stderr)
        else:
            print(f"ok {source_id}: last success {age_h:.1f}h ago (SLO {slo_hours}h)")

    if violations:
        print(f"{violations} freshness violation(s)", file=sys.stderr)
        return 1
    print("all sources fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
