#!/usr/bin/env python
"""Prove the scheduled pipeline works — without waiting a day for the timers.

WHY THIS EXISTS
---------------
`make test` proves the CODE is correct. It does not prove the PIPELINE is
correct, and those fail differently:

  - a timer file with a typo'd unit name is valid systemd and never fires
  - a service whose ExecStart references a renamed flag fails once a day, at
    05:00, into the journal, where nobody is looking
  - a source can keep succeeding while its data quietly goes stale
  - a schedule can be twice as slow as its own SLO and nothing says so

Every check here is about the pipeline as a running system. Each prints PASS,
FAIL or WARN with the measurement that decided it. Exit 1 if any FAIL.

WARN vs FAIL: a WARN is a fact Boss should see that is not necessarily wrong —
a timer that is installed but not enabled, or a source that has never run
because it was added today. A FAIL is a contradiction: a unit that references a
script that does not exist, or an SLO the schedule cannot possibly meet.

Usage:
  uv run python scripts/pipeline_smoke.py            # checks only, no writes
  uv run python scripts/pipeline_smoke.py --run-cheap  # also EXECUTES the
      cheap daily jobs end to end (Overpass + mechanic refresh), which is the
      only way to prove they still work after a refactor.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402

DEPLOY = REPO / "deploy"

# unit stem -> (what it must run, how often it should fire at most)
EXPECTED_UNITS = {
    "truckintel-tick": timedelta(minutes=5),
    "truckintel-freshness": timedelta(minutes=30),
    "truckintel-quality": timedelta(days=2),
    "truckintel-aaa-prices": timedelta(days=2),
    "truckintel-osm-truck-repair": timedelta(days=2),
    "truckintel-mechanics-daily": timedelta(days=2),
    "truckintel-mechanics": timedelta(days=40),
    "truckintel-pois": timedelta(days=40),   # monthly since 2026-07-28
    "truckintel-businesses": timedelta(days=40),
    "truckintel-weekly-digest": timedelta(days=9),
    "truckintel-track-prune": timedelta(days=2),
    "truckintel-ops-watch": timedelta(hours=3),
    "truckintel-nightly-checks": timedelta(days=2),
    "truckintel-fuel-verify": timedelta(days=9),
    # Both daily (10:45 and 05:20), so 2d for the same reason the other daily
    # jobs get it: the laptop is off at the nominal time and Persistent=true
    # catch-up can land a run a day late without anything being wrong.
    # Added 2026-08-18 — this dict had never listed either of them, so
    # check_unit_files silently checked 14 of 16 units. See
    # check_units_are_all_known below, which is why that can no longer happen.
    "truckintel-git-push": timedelta(days=2),
    "truckintel-liveness": timedelta(days=2),
    # Every 2h while armed; 3h of slack for the RandomizedDelay-free gap timer.
    "truckintel-status-push": timedelta(hours=3),
    "truckintel-raw-prune": timedelta(days=2),
}

# The data Boss named: "daily fuel prize update ho, truck routes, mechanic
# detail". Each maps to the source_id that must keep producing runs, and how
# old its newest SUCCESS may be before this is a problem.
DATA_SLO = {
    "aaa_daily": ("fuel prices (AAA, per state, daily)", timedelta(hours=48)),
    "eia_diesel": ("fuel prices (EIA, regional, weekly)", timedelta(days=9)),
    "ntad_national_network": ("truck routes (NTAD National Network)", timedelta(days=400)),
    "osm_truck_repair_overpass": ("mechanic corroboration (OSM via Overpass)", timedelta(days=3)),
    # 45d, not 10d: truckintel-pois.timer went monthly on 2026-07-28 and
    # EXPECTED_UNITS was updated to 40d while this budget was left behind, so
    # the job reported STALE for most of every month while running exactly as
    # designed. check_cadence_beats_budget() below now makes that class of
    # drift a FAIL instead of leaving it to be noticed by eye.
    "osm_pois": ("fuel stations (OSM PBF)", timedelta(days=45)),
    # Same monthly shape as osm_pois and the same 400h budget, so it carried
    # the same unmeetable contradiction — it simply had no DATA_SLO entry to
    # report it. OnCalendar=*-*-01.
    "businesses_conflate": ("business POIs (Overture + FSQ conflation)",
                            timedelta(days=45)),
    # The mechanic deliverable itself. Added 2026-08-18: scripts/mechanic_list.py
    # wrote no run rows at all until then, so the headline liveness feature was
    # the one derived product with no staleness alerting of any kind.
    "mechanic_list": ("truck mechanic list (Overture + licences + OSM)",
                      timedelta(days=2)),
    # The self-checks, watched the same way they watch everything else. If the
    # nightly self-check silently stops RUNNING, its own FAIL path never fires
    # — only a staleness budget catches that. Both run from
    # truckintel-nightly-checks, daily.
    #
    # route_rebuild is deliberately absent: `--check` writes no success row when
    # the graph is current (a success there would forge staleness()'s own "last
    # real rebuild" signal), so its newest success is the last actual rebuild
    # and a freshness budget on it would report stale forever.
    "pipeline_smoke": ("nightly self-check (units, freshness, alerting)",
                       timedelta(days=2)),
    "verify_claims": ("published-figure verification", timedelta(days=2)),
    # 3d against a daily unit: its own ops.sources SLO is 72 h.
    "chain_sites": ("truck-chain store locators (All The Places)",
                    timedelta(days=3)),
    # Its ops.sources SLO is 36 h against a daily unit; 2d here matches the
    # EXPECTED_UNITS cadence for a daily job that may slip a day when the
    # laptop is off at 03:30.
    "quality_nightly": ("quality ladder (gates 4-5 + confidence rescore)",
                        timedelta(days=2)),
}

# unit stem -> the source_id whose freshness that unit is responsible for.
# Only units that feed a DATA_SLO entry need to appear; the rest are covered
# by their own checks.
# unit -> the source ids it is responsible for. Values are tuples because
# truckintel-nightly-checks drives three sources from one timer; a 1:1 dict
# could not say so, and the two it could not name showed up as "cadence
# unknown" warnings instead (2026-08-18).
#
# NOTE the cadence deliberately lives here and not in ops.sources.schedule_minutes:
# truckintel/jobs.py:55 enqueues work for every source WHERE schedule_minutes
# IS NOT NULL, so filling that column in to silence a warning would hand these
# scripts to the queue worker as if they were fetchable feeds.
UNIT_FEEDS = {
    "truckintel-aaa-prices": ("aaa_daily",),
    "truckintel-osm-truck-repair": ("osm_truck_repair_overpass",),
    "truckintel-pois": ("osm_pois",),
    "truckintel-businesses": ("businesses_conflate",),
    "truckintel-mechanics-daily": ("mechanic_list",),
    "truckintel-nightly-checks": ("pipeline_smoke", "verify_claims"),
    # chain_sites runs from the liveness unit's second ExecStart, not a timer
    # of its own — deploy/truckintel-liveness.service:26.
    "truckintel-liveness": ("chain_sites",),
    "truckintel-quality": ("quality_nightly",),
}
# Flat view for the membership tests below.
_FED_SOURCES = {sid for sids in UNIT_FEEDS.values() for sid in sids}

_OK, _WARN, _FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []

# Audited under its own source id — the deploy/truckintel-nightly-checks.service
# comment claimed "ops_watch is what escalates" while this script only ever
# printed to a journal nobody reads. Same pattern as osm_extract.py /
# chain_sites.py: seed ops.sources once, one ops.source_runs row per run, so
# ops_watch's existing queries (check_repeated_failures, check_never_succeeded)
# and the new check_selfcheck_failures() see this the same way they see every
# real data source.
SOURCE_ID = "pipeline_smoke"
SLO_HOURS = 48   # matches EXPECTED_UNITS["truckintel-nightly-checks"] above

_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s,
     'Derived: nightly pipeline smoke test (unit files, flags, install, '
     'data freshness)',
     'truck-intel ops track', 'derived', 'derived', NULL, %(slo)s,
     TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""


def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID, "slo": SLO_HOURS})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id", (SOURCE_ID,)
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s WHERE run_id = %s",
            (status, (message or "")[:1000] or None, run_id))


def report(status: str, check: str, detail: str) -> None:
    _results.append((status, check, detail))
    print(f"[{status:4}] {check}: {detail}", flush=True)


# ------------------------------------------------------------- unit file checks

def check_units_are_all_known() -> None:
    """Every timer in deploy/ must be listed in EXPECTED_UNITS.

    EXPECTED_UNITS is hand-maintained because each entry carries a cadence the
    glob cannot know. That is fine; silently checking a subset is not. On
    2026-08-18 a review found truckintel-liveness.timer missing from three
    separate hand-written lists, and this dict turned out to be a fourth: it
    listed 14 of the 16 timers, so the two it had never heard of — git-push
    and liveness — were exempt from every check in this file.

    A unit that should not be held to these checks still has to say so out
    loud, by being listed here, rather than by being forgotten.
    """
    on_disk = {p.stem for p in DEPLOY.glob("truckintel-*.timer")}
    unknown = sorted(on_disk - set(EXPECTED_UNITS))
    if unknown:
        report(_FAIL, "unit inventory",
               f"deploy/ has {len(unknown)} timer(s) absent from "
               f"EXPECTED_UNITS, so nothing checks them: {unknown}")
    else:
        report(_OK, "unit inventory",
               f"all {len(on_disk)} timers in deploy/ are listed in EXPECTED_UNITS")


def check_unit_files() -> None:
    """Every expected unit exists, and its ExecStart points at a real script.

    This is the check that catches a renamed script or a dropped flag — the
    failure mode that otherwise only shows up once a day in the journal.
    """
    for stem in EXPECTED_UNITS:
        svc, timer = DEPLOY / f"{stem}.service", DEPLOY / f"{stem}.timer"
        if not svc.exists():
            report(_FAIL, f"unit {stem}", "no .service file in deploy/")
            continue
        if not timer.exists():
            report(_FAIL, f"unit {stem}", "no .timer file in deploy/")
            continue
        text = svc.read_text()
        execs = re.findall(r"^ExecStart=(.+)$", text, re.M)
        if not execs:
            report(_FAIL, f"unit {stem}", "service has no ExecStart")
            continue
        missing = []
        for line in execs:
            for token in line.split():
                # only validate paths inside this repo; %h/uv/etc are runtime
                if token.startswith("scripts/") or token.startswith("sql/"):
                    if not (REPO / token).exists():
                        missing.append(token)
        if missing:
            report(_FAIL, f"unit {stem}", f"ExecStart references missing {missing}")
        else:
            report(_OK, f"unit {stem}", "service + timer present, ExecStart resolves")


def check_flags_exist() -> None:
    """Every CLI flag a unit passes is actually accepted by the script.

    A unit that calls `--refresh` after someone renames it to `--daily` is
    still a valid unit file. It just fails every morning.
    """
    for stem in EXPECTED_UNITS:
        svc = DEPLOY / f"{stem}.service"
        if not svc.exists():
            continue
        for line in re.findall(r"^ExecStart=(.+)$", svc.read_text(), re.M):
            tokens = line.split()
            script = next((t for t in tokens if t.startswith("scripts/")), None)
            flags = [t for t in tokens if t.startswith("--")]
            if not script or not flags:
                continue
            path = REPO / script
            if not path.exists():
                continue
            try:
                help_text = subprocess.run(
                    [sys.executable, str(path), "--help"],
                    capture_output=True, text=True, timeout=90, cwd=REPO).stdout
            except Exception as exc:                       # noqa: BLE001
                report(_WARN, f"flags {stem}", f"could not read --help ({exc})")
                continue
            unknown = [f for f in flags if f.split("=")[0] not in help_text]
            if unknown:
                report(_FAIL, f"flags {stem}",
                       f"{script} does not accept {unknown}")
            else:
                report(_OK, f"flags {stem}", f"{script} accepts {flags}")


def check_installed() -> None:
    """Are the units actually installed and enabled on THIS machine?

    deploy/ holding a correct timer proves nothing if it was never installed —
    which is exactly the state a new unit is in until `make install-timers`.
    """
    if not shutil.which("systemctl"):
        report(_WARN, "systemd", "systemctl not available — cannot check timers")
        return
    out = subprocess.run(["systemctl", "--user", "list-unit-files", "--no-legend"],
                         capture_output=True, text=True).stdout
    enabled = {line.split()[0] for line in out.splitlines()
               if line.strip() and " enabled" in line}
    known = {line.split()[0] for line in out.splitlines() if line.strip()}
    for stem in EXPECTED_UNITS:
        unit = f"{stem}.timer"
        if unit in enabled:
            report(_OK, f"installed {stem}", "timer enabled")
        elif unit in known:
            report(_WARN, f"installed {stem}", "timer installed but NOT enabled")
        else:
            report(_WARN, f"installed {stem}",
                   "timer not installed — run `make install-timers`")


# -------------------------------------------------------------- data freshness

def check_data_freshness() -> None:
    """Has each named dataset actually produced a recent successful run?

    Deliberately reads ops.source_runs rather than the data tables: a table can
    hold rows forever while the job that fills it has been broken for weeks.
    """
    with get_conn() as conn:
        rows = dict((r[0], (r[1], r[2])) for r in conn.execute("""
            SELECT source_id, max(finished_at) FILTER (WHERE status='success'),
                   count(*) FILTER (WHERE status='failed'
                                      AND started_at > now() - interval '7 days')
            FROM ops.source_runs GROUP BY source_id
        """).fetchall())
    now = datetime.now(timezone.utc)
    for source_id, (label, budget) in DATA_SLO.items():
        entry = rows.get(source_id)
        if not entry or entry[0] is None:
            report(_WARN, f"data {source_id}",
                   f"{label}: no successful run on record yet")
            continue
        last, recent_failures = entry
        age = now - last
        detail = (f"{label}: last success {age.days}d "
                  f"{age.seconds // 3600}h ago (budget {budget.days}d)")
        if age > budget:
            report(_FAIL, f"data {source_id}", detail + " — STALE")
        elif recent_failures:
            report(_WARN, f"data {source_id}",
                   detail + f", but {recent_failures} failed run(s) in 7d")
        else:
            report(_OK, f"data {source_id}", detail)


def check_alerting_is_armed() -> None:
    """A source that still RUNS but is disabled has no staleness alerting.

    scripts/freshness_check.py only looks at enabled sources, so a disabled row
    with recent runs is a job quietly operating with its smoke detector taken
    out. Found for real on 2026-07-27: osm_truck_repair_overpass was seeded
    with kind='live_json', and truckintel-tick's registry sweep — which
    disables enabled sources that have no registry YAML unless their kind is
    'derived' — switched it off within a minute of its first run.

    The earlier freshness check could not catch this: it reads source_runs,
    which looked perfectly healthy the whole time.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.source_id, s.enabled, s.kind,
                   max(r.finished_at) FILTER (WHERE r.status = 'success')
            FROM ops.sources s JOIN ops.source_runs r USING (source_id)
            -- '_'-prefixed ids are test fixtures (tests/test_quality.py and
            -- friends seed and disable them on purpose). Flagging those would
            -- put four permanent FAILs in front of the one that matters.
            WHERE s.source_id NOT LIKE %s
            GROUP BY s.source_id, s.enabled, s.kind
            HAVING max(r.started_at) > now() - interval '14 days'
        """, ("\\_%",)).fetchall()
    for source_id, enabled, kind, last_ok in rows:
        if not enabled:
            report(_FAIL, f"alerting {source_id}",
                   f"ran within 14d (last success {last_ok}) but ops.sources "
                   f"is DISABLED (kind={kind}) — freshness_check skips it, so "
                   f"this job has no staleness alerting")
    report(_OK, "alerting armed", f"{len(rows)} recently-active source(s) checked")


def check_schedule_beats_slo() -> None:
    """A schedule slower than its own SLO can never satisfy it.

    This is a pure arithmetic contradiction and worth catching in CI rather
    than discovering it as a 3 a.m. page.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_id, schedule_minutes, slo_hours FROM ops.sources "
            "WHERE enabled").fetchall()
    checked = 0
    for source_id, sched_min, slo_h in rows:
        if slo_h is None:
            continue
        if sched_min is None:
            # Previously these were filtered out in SQL, which silently
            # exempted 11 of 23 enabled sources — including every derived one,
            # which is exactly where the cadence lives in a timer rather than
            # in this column. An unknowable cadence is not a passing check;
            # it is the reason osm_pois' monthly-vs-16.7d contradiction sat
            # here unreported. check_cadence_beats_budget() covers the ones
            # driven by a unit file; the rest are genuinely event-driven.
            continue
        checked += 1
        if sched_min / 60.0 > slo_h:
            report(_FAIL, f"schedule {source_id}",
                   f"runs every {sched_min}min but SLO is {slo_h}h — "
                   f"can never be met")
    unscheduled = [s for s, m, h in rows if m is None and h is not None
                   and s not in _FED_SOURCES]
    report(_OK, "schedule vs SLO", f"{checked} scheduled sources checked")
    if unscheduled:
        report(_WARN, "cadence unknown",
               f"{len(unscheduled)} enabled source(s) have an SLO but no "
               f"schedule_minutes and no unit in UNIT_FEEDS, so nothing can "
               f"prove their budget is reachable: {', '.join(sorted(unscheduled))}")


def check_cadence_beats_budget() -> None:
    """A job's freshness budget must be at least as long as its own cadence.

    check_schedule_beats_slo compares two ops.sources columns, which misses
    every source whose real cadence lives in a timer file instead — the
    derived ones. That gap is not hypothetical: truckintel-pois.timer moved to
    monthly on 2026-07-28, EXPECTED_UNITS was updated to 40d, DATA_SLO's 10d
    was not, and the result was a FAIL every day for a job that was working.

    Cadence comes from EXPECTED_UNITS, which is the interval the unit-file
    checks already hold the timer to, so the two cannot drift apart.
    """
    for unit, source_id in sorted(
            (u, sid) for u, sids in UNIT_FEEDS.items() for sid in sids):
        cadence = EXPECTED_UNITS.get(unit)
        entry = DATA_SLO.get(source_id)
        if cadence is None or entry is None:
            report(_WARN, f"cadence {unit}",
                   f"maps to {source_id} but one side is missing — "
                   f"EXPECTED_UNITS={cadence}, DATA_SLO={'set' if entry else None}")
            continue
        label, budget = entry
        if cadence > budget:
            report(_FAIL, f"cadence {source_id}",
                   f"{unit} fires at most every {cadence.days}d but the "
                   f"freshness budget is {budget.days}d — the budget is "
                   f"unmeetable, so it reports the calendar, not the data")
        else:
            report(_OK, f"cadence {source_id}",
                   f"fires every {cadence.days}d within {budget.days}d budget")


def check_fill_history() -> None:
    """The daily mechanic refresh must be leaving a trail.

    core.mechanic_fill_history is how "did today learn anything?" is answered;
    if it is empty the daily job has never completed even once.
    """
    with get_conn() as conn:
        n, last = conn.execute(
            "SELECT count(DISTINCT snapshot_at), max(snapshot_at) "
            "FROM core.mechanic_fill_history").fetchone()
    if not n:
        report(_WARN, "fill history",
               "no snapshots yet — daily mechanic refresh has not completed")
    else:
        age = datetime.now(timezone.utc) - last
        status = _OK if age < timedelta(days=2) else _WARN
        report(status, "fill history",
               f"{n} snapshot(s), newest {age.days}d {age.seconds // 3600}h old")


# ------------------------------------------------------------------ live run

def run_cheap_jobs() -> None:
    """Actually execute the two daily jobs. The only real proof they work.

    Both are idempotent and cost minutes, which is what makes running them in
    a smoke test reasonable — the monthly Overture pull is not, and is never
    run from here.
    """
    for label, cmd in (
        ("osm_overpass --job truck_repair",
         [sys.executable, "scripts/osm_overpass.py", "--job", "truck_repair"]),
        ("mechanic_list --refresh",
         [sys.executable, "scripts/mechanic_list.py", "--refresh"]),
    ):
        print(f"\n----- running {label}", flush=True)
        proc = subprocess.run(cmd, cwd=REPO, text=True)
        if proc.returncode == 0:
            report(_OK, f"live {label}", "exited 0")
        else:
            report(_FAIL, f"live {label}", f"exited {proc.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-cheap", action="store_true",
                    help="also execute the daily jobs end to end")
    # For CI, which has a checkout and nothing else: no database, no systemd
    # user manager, no ingested data. Only the two checks that are answerable
    # from the repo alone — every deploy/ unit resolves to a real script, and
    # every flag it passes is one that script accepts. Those are exactly the
    # failures a unit file cannot catch and a scheduled run finds at 05:00.
    #
    # This flag is why the CI step no longer ends in `|| true`. It used to,
    # because the full run needs a database and CI's fast job has none, so the
    # step crashed on every push and the discarded exit code hid it — the check
    # built to catch drift had been dead for its whole life.
    ap.add_argument("--units-only", action="store_true",
                    help="only the repo-answerable checks; no DB, no systemd, "
                         "no run row. Intended for CI.")
    args = ap.parse_args()
    load_dotenv()

    if args.units_only:
        print("=== pipeline smoke: unit files (units-only) ===", flush=True)
        check_units_are_all_known()
        check_unit_files()
        check_flags_exist()
        fails = [r for r in _results if r[0] == _FAIL]
        warns = [r for r in _results if r[0] == _WARN]
        print(f"\n=== {len(_results)} checks: "
              f"{len(_results) - len(fails) - len(warns)} pass, "
              f"{len(warns)} warn, {len(fails)} fail ===", flush=True)
        for status, check, detail in fails + warns:
            print(f"  [{status}] {check}: {detail}", flush=True)
        return 1 if fails else 0

    run_id = _start_run()

    print("=== pipeline smoke: unit files ===", flush=True)
    check_units_are_all_known()
    check_unit_files()
    check_flags_exist()
    print("\n=== pipeline smoke: installation ===", flush=True)
    check_installed()
    print("\n=== pipeline smoke: data freshness ===", flush=True)
    check_data_freshness()
    check_alerting_is_armed()
    check_schedule_beats_slo()
    check_cadence_beats_budget()
    check_fill_history()
    if args.run_cheap:
        print("\n=== pipeline smoke: live run ===", flush=True)
        run_cheap_jobs()

    fails = [r for r in _results if r[0] == _FAIL]
    warns = [r for r in _results if r[0] == _WARN]
    print(f"\n=== {len(_results)} checks: "
          f"{len(_results) - len(fails) - len(warns)} pass, "
          f"{len(warns)} warn, {len(fails)} fail ===", flush=True)
    for status, check, detail in fails + warns:
        print(f"  [{status}] {check}: {detail}", flush=True)

    if fails:
        _finish_run(run_id, "failed",
                    message=f"{len(fails)} FAIL, {len(warns)} WARN: "
                    + "; ".join(f"{c}: {d}" for _, c, d in fails))
    else:
        _finish_run(run_id, "success",
                    message=f"{len(_results)} checks, {len(warns)} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
