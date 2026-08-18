"""Periodic pipeline health digest, delivered over Telegram.

WHY THIS IS NOT ops_watch
-------------------------
ops_watch.py answers "has something broken?" and stays silent when the answer
is no — correctly, because an alert that fires when nothing is wrong is an
alert people stop reading. This answers a different question: "is the pipeline
still working?", and says so even when the answer is yes. Silence and health
are indistinguishable to a reader; this exists to tell them apart.

ARMING
------
It sends nothing unless armed with a run budget:

    make status-push-arm N=5      # 5 updates, then it stops on its own

Each delivery decrements the budget. On the last one it says so and disables
its own timer, so an unattended machine cannot keep talking forever. Unarmed
(the default, including on a fresh install) the timer fires and the script
exits doing nothing — which is why enabling it by default is harmless.

    scripts/status_push.py --dry-run    # print, send nothing, decrement nothing
    scripts/status_push.py --disarm     # stop early
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from truckintel import notify                       # noqa: E402
from truckintel.config import load_dotenv           # noqa: E402
from truckintel.db import get_conn                  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "data" / "status_push_state.json"
TIMER = "truckintel-status-push.timer"


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"remaining": 0, "sent": 0}


def _write_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2) + "\n")


def _sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120).stdout.strip()
    except Exception:
        return ""


def _freshness() -> tuple[int, list[str]]:
    """Run the real alarm rather than reimplementing its SLO arithmetic.

    Duplicating the rule here is how the copy drifts and starts disagreeing
    with the thing it is reporting on.
    """
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "freshness_check.py")],
                       capture_output=True, text=True, timeout=180)
    lines = [l for l in (p.stdout + p.stderr).splitlines()
             if l.startswith("FRESHNESS VIOLATION")]
    names = [l.split("FRESHNESS VIOLATION ", 1)[1].split(":", 1)[0] for l in lines]
    return len(names), names


def build_report() -> str:
    load_dotenv()
    out: list[str] = []

    failed = _sh("systemctl", "--user", "list-units", "truckintel*",
                 "--state=failed", "--no-legend", "--no-pager")
    n_failed = len([l for l in failed.splitlines() if l.strip()])
    worker = _sh("systemctl", "--user", "is-active", "truckintel-worker.service")
    n_timers = len([l for l in _sh("systemctl", "--user", "list-timers",
                                   "truckintel-*", "--no-legend",
                                   "--no-pager").splitlines() if l.strip()])

    out.append(f"worker   {worker or 'unknown'}")
    out.append(f"timers   {n_timers} enabled, {n_failed} failed unit(s)")
    if n_failed:
        for line in failed.splitlines():
            unit = line.split()[0] if line.split() else line
            out.append(f"    ! {unit}")

    try:
        with get_conn() as c:
            counts = {}
            for t in ("core.truck_routes", "core.bridges",
                      "core.mechanic_shops", "core.businesses"):
                counts[t.split(".")[1]] = c.execute(
                    f"SELECT count(*) FROM {t}").fetchone()[0]
            ok24, bad24 = c.execute(
                "SELECT count(*) FILTER (WHERE status = 'success'), "
                "       count(*) FILTER (WHERE status = 'failed') "
                "FROM ops.source_runs "
                "WHERE started_at > now() - interval '24 hours'").fetchone()
            worst = c.execute(
                "SELECT source_id, count(*) FROM ops.source_runs "
                "WHERE status = 'failed' "
                "  AND started_at > now() - interval '24 hours' "
                "GROUP BY source_id ORDER BY 2 DESC LIMIT 3").fetchall()
        out.append("")
        out.append(f"runs 24h {ok24} ok, {bad24} failed")
        for sid, n in worst:
            out.append(f"    ! {sid} x{n}")
        out.append("")
        out.append("data     " + " · ".join(f"{k} {v:,}" for k, v in counts.items()))
    except Exception as exc:
        out.append("")
        out.append(f"DATABASE UNREACHABLE: {type(exc).__name__}")

    n_viol, names = _freshness()
    out.append("")
    out.append(f"freshness {n_viol} violation(s)"
               + ("" if not names else ": " + ", ".join(names)))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", type=int, metavar="N",
                    help="send the next N updates, then stop")
    ap.add_argument("--disarm", action="store_true", help="stop sending now")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report; send nothing, change nothing")
    args = ap.parse_args()

    if args.disarm:
        _write_state({"remaining": 0, "sent": _read_state().get("sent", 0)})
        print("status-push: disarmed")
        return 0

    if args.arm is not None:
        _write_state({"remaining": args.arm, "sent": 0,
                      "armed_at": datetime.now(timezone.utc).isoformat()})
        print(f"status-push: armed for {args.arm} update(s)")
        return 0

    st = _read_state()
    remaining = int(st.get("remaining", 0))

    if args.dry_run:
        print(build_report())
        print(f"\n[dry-run] armed: {remaining} remaining — nothing sent")
        return 0

    if remaining <= 0:
        print("status-push: not armed, nothing sent")
        return 0

    sent = int(st.get("sent", 0)) + 1
    total = sent + remaining - 1
    body = build_report()
    last = remaining == 1
    body += ("\n\nthis was the last of "
             f"{total} — timer disabled. re-arm: make status-push-arm N=5"
             if last else f"\n\nnext update in 2h ({remaining - 1} left)")

    # deliver() never raises — it returns a per-channel status and leaves the
    # judgement to the caller. Ignoring that return value would make this
    # script announce "sent" for an update nobody received, which is the exact
    # silent-failure shape this whole digest exists to catch. So: check it,
    # say what each channel did, and do NOT spend a run from the budget on an
    # update that was never delivered.
    results = notify.deliver(body, title=f"truck-intel status {sent}/{total}")
    for r in results:
        print(f"  {r['channel']}: {r['status']} — {r['detail']}")
    if not any(r["status"] == "ok" for r in results):
        print("status-push: NOT DELIVERED on any channel — budget unchanged",
              file=sys.stderr)
        return 1

    _write_state({"remaining": remaining - 1, "sent": sent,
                  "armed_at": st.get("armed_at")})
    print(f"status-push: sent {sent}/{total}")

    if last:
        # Stop talking without needing anyone to remember to stop it.
        subprocess.run(["systemctl", "--user", "disable", "--now", TIMER],
                       capture_output=True, text=True)
        print(f"status-push: disabled {TIMER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
