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
}

# The data Boss named: "daily fuel prize update ho, truck routes, mechanic
# detail". Each maps to the source_id that must keep producing runs, and how
# old its newest SUCCESS may be before this is a problem.
DATA_SLO = {
    "aaa_daily": ("fuel prices (AAA, per state, daily)", timedelta(hours=48)),
    "eia_diesel": ("fuel prices (EIA, regional, weekly)", timedelta(days=9)),
    "ntad_national_network": ("truck routes (NTAD National Network)", timedelta(days=400)),
    "osm_truck_repair_overpass": ("mechanic corroboration (OSM via Overpass)", timedelta(days=3)),
    "osm_pois": ("fuel stations (OSM PBF)", timedelta(days=10)),
}

_OK, _WARN, _FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def report(status: str, check: str, detail: str) -> None:
    _results.append((status, check, detail))
    print(f"[{status:4}] {check}: {detail}", flush=True)


# ------------------------------------------------------------- unit file checks

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
            "WHERE enabled AND schedule_minutes IS NOT NULL "
            "AND slo_hours IS NOT NULL").fetchall()
    for source_id, sched_min, slo_h in rows:
        if sched_min / 60.0 > slo_h:
            report(_FAIL, f"schedule {source_id}",
                   f"runs every {sched_min}min but SLO is {slo_h}h — "
                   f"can never be met")
    report(_OK, "schedule vs SLO", f"{len(rows)} scheduled sources checked")


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
    args = ap.parse_args()
    load_dotenv()

    print("=== pipeline smoke: unit files ===", flush=True)
    check_unit_files()
    check_flags_exist()
    print("\n=== pipeline smoke: installation ===", flush=True)
    check_installed()
    print("\n=== pipeline smoke: data freshness ===", flush=True)
    check_data_freshness()
    check_alerting_is_armed()
    check_schedule_beats_slo()
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
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
