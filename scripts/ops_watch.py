#!/usr/bin/env python
"""Ops watchdog — alerts on the failures the freshness SLO cannot see.

WHY A SECOND WATCHER
--------------------
scripts/freshness_check.py asks one question: "is the DATA too old?" That is
the right question, and it is not the only one. A source can fail every single
run and still sit inside its SLO for days, because the last good publish is
still recent enough. During that window nothing is wrong by the SLO's
definition and nothing is right in reality.

Measured on 2026-07-27: osm_pois had failed 5 times in 7 days. Its data was
4 days old against a 10-day budget, so freshness reported PASS the whole time.
The failures were found by running a smoke test by hand.

WHAT THIS CHECKS (each is a distinct failure the others miss)
  1. repeated failures   — N+ failed runs in the window, whatever the SLO says
  2. all-time-failing    — a source that has NEVER succeeded
  3. stuck jobs          — 'running' far longer than any real run takes
  4. disarmed alerting   — a source still running while ops.sources says
                           disabled, i.e. freshness_check skips it entirely
  5. queue backlog       — jobs queued and not progressing
  6. self-check failure  — pipeline_smoke / verify_claims / route_rebuild
                           --check failed, escalated on the FIRST failure
                           since they only run once a day (see SELFCHECK_SOURCES)

DEDUPLICATION: state lives in data/ops_watch_state.json. A condition already
alerted within --cooldown hours is counted but not re-sent. Without this, an
hourly watchdog on a persistently broken source becomes a notification the
reader learns to swipe away, which is worse than no alert at all.

Exit 0 when clean OR when findings were only suppressed by cooldown; exit 1
when something new was found (so a supervisor can react), and exit 2 when
delivery itself failed.

Usage:
  uv run python scripts/ops_watch.py              # check, alert, update state
  uv run python scripts/ops_watch.py --dry-run    # check and print only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402
from truckintel.notify import deliver, report  # noqa: E402

STATE_FILE = REPO / "data" / "ops_watch_state.json"

# Test fixtures seed and disable sources on purpose (tests/test_quality.py and
# friends). Alerting on those would put permanent noise in front of the one
# finding that matters.
#
# The backslash is load-bearing: in SQL LIKE, '_' is a single-character
# WILDCARD, so the obvious pattern '_%' matches every source id in the table
# and the watchdog excludes everything — silently reporting "clean" forever.
# Caught by testing it against a week that really did contain 19 failures.
TEST_PREFIX_LIKE = r"\_%"

# scripts/pipeline_smoke.py, scripts/verify_claims.py and
# scripts/route_rebuild.py --check are the nightly self-checks
# (truckintel-nightly-checks.service, 03:50 daily) — the jobs whose entire
# purpose is to catch drift before Boss does. They run ONCE a day, not once
# every few minutes like a real source, so check_repeated_failures' 2-in-24h
# threshold and check_never_succeeded's 3-runs floor both mean the very
# checker built to close the "a nightly self-check can fail and nobody is
# told" gap could itself sit broken for a day (or three) before anyone heard.
# See check_selfcheck_failures() below.
SELFCHECK_SOURCES = ("pipeline_smoke", "verify_claims", "route_rebuild")


def _rows(sql: str, params=()) -> list[tuple]:
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def check_repeated_failures(window_h: int, threshold: int) -> list[dict]:
    """Sources failing repeatedly, regardless of whether the SLO is happy."""
    out = []
    for source_id, fails, last_msg in _rows("""
        SELECT source_id, count(*),
               (array_agg(message ORDER BY started_at DESC))[1]
        FROM ops.source_runs
        WHERE status = 'failed' AND started_at > now() - (%s || ' hours')::interval
          AND source_id NOT LIKE %s
        GROUP BY source_id HAVING count(*) >= %s
    """, (window_h, TEST_PREFIX_LIKE, threshold)):
        out.append({
            "key": f"failing/{source_id}",
            "severity": "high",
            "text": (f"{source_id}: {fails} failed run(s) in {window_h}h\n"
                     f"    last error: {(last_msg or '(no message)')[:160]}"),
        })
    return out


def check_selfcheck_failures(window_h: int) -> list[dict]:
    """Any single failure of a once-a-day nightly self-check, escalated now.

    Unlike check_repeated_failures, this does not wait for a second failure —
    a source that retries every 5 minutes can shrug off one bad tick, but a
    job that only runs once a day cannot: waiting for a repeat means waiting
    a full extra day before hearing about it. window_h is wider than 24 on
    purpose, to survive the timer's own RandomizedDelaySec jitter without a
    finding ever falling just outside the window.
    """
    out = []
    for source_id, run_id, msg, started in _rows("""
        SELECT DISTINCT ON (source_id) source_id, run_id, message, started_at
        FROM ops.source_runs
        WHERE source_id = ANY(%s) AND status = 'failed'
          AND started_at > now() - (%s || ' hours')::interval
        ORDER BY source_id, started_at DESC
    """, (list(SELFCHECK_SOURCES), window_h)):
        out.append({
            "key": f"selfcheck/{source_id}",
            "severity": "high",
            "text": (f"{source_id}: nightly self-check FAILED (run {run_id}, "
                     f"{started:%Y-%m-%d %H:%M} UTC)\n"
                     f"    {(msg or '(no message)')[:200]}"),
        })
    return out


def check_never_succeeded() -> list[dict]:
    """A source with runs but no success has never worked at all."""
    return [{
        "key": f"never-ok/{source_id}",
        "severity": "high",
        "text": f"{source_id}: {n} run(s) recorded, NEVER succeeded",
    } for source_id, n in _rows("""
        SELECT source_id, count(*) FROM ops.source_runs
        WHERE source_id NOT LIKE %s
        GROUP BY source_id
        HAVING count(*) FILTER (WHERE status = 'success') = 0
           AND count(*) >= 3
    """, (TEST_PREFIX_LIKE,))]


def check_stuck_runs(max_h: int) -> list[dict]:
    """A run still 'running' after max_h is a crashed worker, not slow work.

    The longest legitimate job measured here is the US OSM POI pass at ~3 h,
    so the default ceiling sits well above it.
    """
    return [{
        "key": f"stuck/{run_id}",
        "severity": "high",
        "text": (f"{source_id}: run {run_id} has been 'running' for "
                 f"{age.days*24 + age.seconds//3600}h — worker likely died"),
    } for run_id, source_id, age in _rows("""
        SELECT run_id, source_id, now() - started_at FROM ops.source_runs
        WHERE status = 'running' AND started_at < now() - (%s || ' hours')::interval
          AND source_id NOT LIKE %s
    """, (max_h, TEST_PREFIX_LIKE))]


def check_alerting_disarmed() -> list[dict]:
    """A source still running while ops.sources says disabled.

    freshness_check only looks at enabled sources, so this is a job operating
    with its smoke detector removed. Happened for real: a new source seeded
    with the wrong `kind` was switched off by tick's registry sweep within a
    minute of its first run.
    """
    return [{
        "key": f"disarmed/{source_id}",
        "severity": "high",
        "text": (f"{source_id}: ran within 7d but ops.sources.enabled = false "
                 f"(kind={kind}) — freshness_check skips it, so it has NO "
                 f"staleness alerting"),
    } for source_id, kind in _rows("""
        SELECT s.source_id, s.kind
        FROM ops.sources s JOIN ops.source_runs r USING (source_id)
        WHERE NOT s.enabled AND s.source_id NOT LIKE %s
        GROUP BY s.source_id, s.kind
        HAVING max(r.started_at) > now() - interval '7 days'
    """, (TEST_PREFIX_LIKE,))]


def check_queue_backlog(max_age_h: int) -> list[dict]:
    """Jobs queued and not picked up — the worker is down or wedged."""
    return [{
        "key": "backlog",
        "severity": "medium",
        "text": (f"{n} job(s) queued for over {max_age_h}h "
                 f"(oldest: {oldest}) — is truckintel-worker running?"),
    } for n, oldest in _rows("""
        SELECT count(*), min(enqueued_at) FROM ops.job_queue
        WHERE status = 'queued' AND enqueued_at < now() - (%s || ' hours')::interval
        HAVING count(*) > 0
    """, (max_age_h,))]


def check_stuck_deferrals(max_h: int) -> list[dict]:
    """A job the resource gate has been refusing for a whole day.

    A single deferral is correct and must stay silent — the machine was busy,
    the job waits, the next tick retries. But a job that can NEVER start is a
    real outage wearing a deferral's clothes: a disk that never frees up, a
    load average permanently above the ceiling, a laptop left on battery.

    This is the counterpart to the gate in truckintel/resources.py. Without it,
    'defer, never fail' would mean 'never run, never say so'.
    """
    return [{
        "key": f"deferred/{source_id}",
        "severity": "high",
        "text": (f"{source_id}: queued and repeatedly DEFERRED for "
                 f"{age.days * 24 + age.seconds // 3600}h — the resource gate "
                 f"has never let it start.\n    last reason: {(msg or '')[:200]}"),
    } for source_id, age, msg in _rows("""
        SELECT source_id, now() - enqueued_at, message
        FROM ops.job_queue
        WHERE status = 'queued'
          AND message LIKE 'deferred%%'
          AND enqueued_at < now() - (%s || ' hours')::interval
          AND source_id NOT LIKE %s
    """, (max_h, TEST_PREFIX_LIKE))]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--fail-threshold", type=int, default=2,
                    help="failed runs in the window before it is a finding")
    ap.add_argument("--selfcheck-hours", type=int, default=30,
                    help="a nightly self-check (pipeline_smoke, verify_claims, "
                         "route_rebuild) failing within this window is escalated "
                         "on its FIRST failure, not its second — see "
                         "check_selfcheck_failures()")
    ap.add_argument("--stuck-hours", type=int, default=6,
                    help="a run still 'running' this long is a dead worker "
                         "(longest real job here is ~3 h)")
    ap.add_argument("--backlog-hours", type=int, default=2)
    ap.add_argument("--deferred-hours", type=int, default=24,
                    help="a job the resource gate has refused for this long "
                         "is an outage, not a busy moment")
    ap.add_argument("--cooldown-hours", type=int, default=12,
                    help="do not re-send a finding already alerted this recently")
    ap.add_argument("--dry-run", action="store_true",
                    help="check and print; send nothing, record nothing")
    args = ap.parse_args()
    load_dotenv()

    findings = (check_repeated_failures(args.window_hours, args.fail_threshold)
                + check_selfcheck_failures(args.selfcheck_hours)
                + check_never_succeeded()
                + check_stuck_runs(args.stuck_hours)
                + check_alerting_disarmed()
                + check_queue_backlog(args.backlog_hours)
                + check_stuck_deferrals(args.deferred_hours))

    if not findings:
        print("[ops-watch] clean — no findings", flush=True)
        return 0

    now = datetime.now(timezone.utc)
    state = load_state()
    fresh, suppressed = [], []
    for f in findings:
        last = state.get(f["key"])
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=args.cooldown_hours):
                    suppressed.append(f)
                    continue
            except ValueError:
                pass
        fresh.append(f)

    for f in findings:
        mark = "NEW " if f in fresh else "held"
        print(f"[ops-watch] {mark} [{f['severity']}] {f['text']}", flush=True)

    if suppressed:
        print(f"[ops-watch] {len(suppressed)} finding(s) held by the "
              f"{args.cooldown_hours}h cooldown — still true, already alerted",
              flush=True)
    if not fresh:
        return 0
    if args.dry_run:
        print("[ops-watch] --dry-run: nothing sent, state unchanged", flush=True)
        return 1

    body = "\n\n".join(f["text"] for f in fresh)
    if suppressed:
        body += (f"\n\n(+{len(suppressed)} older finding(s) still unresolved — "
                 f"suppressed by cooldown)")
    ok = report(deliver(body, title=f"⚠️ truck-intel: {len(fresh)} new ops finding(s)",
                        full_text_at="journalctl --user -u truckintel-ops-watch",
                        priority="high"))

    for f in fresh:
        state[f["key"]] = now.isoformat()
    # Forget keys that no longer fire, so a recurrence alerts again instead of
    # sitting silently under a stale cooldown.
    live = {f["key"] for f in findings}
    save_state({k: v for k, v in state.items() if k in live})

    return 1 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
