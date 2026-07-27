"""Resource gate — refuse to START a heavy job when the machine cannot afford it.

WHY `Nice` WAS NOT ENOUGH
-------------------------
Eight of sixteen units already carry `Nice=10` and `IOSchedulingClass=idle`.
Those lower a job's PRIORITY once it is running; they never stop it starting.

Measured 2026-07-27: the US OSM pass ran idle-classed for its whole life and
still took the laptop from 11 GB free to 1.6 GB, pushed 1.1 GB into swap, drove
load average to 16 on 8 cores, and made the machine unusable. Boss asked for it
to stop. Priority was never the lever — admission was.

WHAT THIS IS NOT
----------------
Not a cgroup and not a replacement for one. `MemoryMax`/`CPUQuota` in the unit
files bound a job that is ALREADY running; this decides whether it should begin.
Both are wanted, and they answer different questions.

DEFER, NEVER FAIL
-----------------
A refusal means "not now", not "broken". The caller records
`status='deferred'` with the measurement that caused it, leaves the job queued
and lets the next tick retry. A deferral must not alert — an alert on every
busy laptop is noise. A job deferred CONTINUOUSLY for a day is a real finding,
and ops_watch raises that instead.

Thresholds default from the measurements above and are env-overridable, because
this file cannot know what machine it is on.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Defaults measured on Boss's 15 GB / 8-core laptop, 2026-07-27.
DEFAULT_MIN_FREE_RAM_GB = 3.0      # the OSM node index alone passed 1 GB
DEFAULT_MIN_FREE_DISK_GB = 60.0    # 12 GB PBF + a node cache that reached 25 GB
DEFAULT_MAX_LOAD_PER_CPU = 1.5     # unusable at 16 on 8 cores == 2.0/cpu
DEFAULT_REQUIRE_AC = True          # a 3-hour pass should not run on battery


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Reading:
    """One measured resource plus the limit it was judged against."""
    name: str
    value: float
    limit: float
    ok: bool
    unit: str

    def __str__(self) -> str:
        verdict = "ok" if self.ok else "BLOCKS"
        return f"{self.name}={self.value:.1f}{self.unit} (limit {self.limit:.1f}{self.unit}) {verdict}"


def free_ram_gb() -> float:
    """Available (not merely 'free') RAM. MemAvailable accounts for reclaimable
    page cache, which is what actually decides whether the next big allocation
    swaps — 'MemFree' alone reads near-zero on a healthy machine and would
    block everything forever."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1048576.0
    except OSError:
        pass
    return float("inf")            # unknown must not block work


def free_disk_gb(path: str | Path = ".") -> float:
    try:
        return shutil.disk_usage(Path(path)).free / 1073741824.0
    except OSError:
        return float("inf")


def load_per_cpu() -> float:
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        return 0.0


def on_battery() -> bool | None:
    """True on battery, False on mains, None when the machine has no battery
    (a desktop or a VM) — and None must never block."""
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        try:
            if (supply / "type").read_text().strip() == "Mains":
                return (supply / "online").read_text().strip() == "0"
        except OSError:
            continue
    return None


def measure(*, work_path: str | Path = ".") -> list[Reading]:
    """Every gated resource, measured now, with its verdict."""
    readings = [
        Reading("free_ram", free_ram_gb(),
                _f("TRUCKINTEL_MIN_FREE_RAM_GB", DEFAULT_MIN_FREE_RAM_GB),
                free_ram_gb() >= _f("TRUCKINTEL_MIN_FREE_RAM_GB",
                                    DEFAULT_MIN_FREE_RAM_GB), "GB"),
        Reading("free_disk", free_disk_gb(work_path),
                _f("TRUCKINTEL_MIN_FREE_DISK_GB", DEFAULT_MIN_FREE_DISK_GB),
                free_disk_gb(work_path) >= _f("TRUCKINTEL_MIN_FREE_DISK_GB",
                                              DEFAULT_MIN_FREE_DISK_GB), "GB"),
        Reading("load_per_cpu", load_per_cpu(),
                _f("TRUCKINTEL_MAX_LOAD_PER_CPU", DEFAULT_MAX_LOAD_PER_CPU),
                load_per_cpu() <= _f("TRUCKINTEL_MAX_LOAD_PER_CPU",
                                     DEFAULT_MAX_LOAD_PER_CPU), ""),
    ]
    require_ac = os.environ.get("TRUCKINTEL_REQUIRE_AC",
                                "1" if DEFAULT_REQUIRE_AC else "0") == "1"
    batt = on_battery()
    if require_ac and batt is not None:
        # 1.0 = on battery. Encoded numerically so it prints like the others.
        readings.append(Reading("on_battery", 1.0 if batt else 0.0, 0.0,
                                not batt, ""))
    return readings


def check(*, work_path: str | Path = ".") -> tuple[bool, str]:
    """(may_start, human-readable reason).

    The reason names every measurement, passing and failing both — a deferral
    log that says only "not enough memory" cannot be argued with later, and the
    numbers are the whole point of gating on them.
    """
    if os.environ.get("TRUCKINTEL_IGNORE_RESOURCE_GATE") == "1":
        return True, "resource gate disabled by TRUCKINTEL_IGNORE_RESOURCE_GATE=1"
    readings = measure(work_path=work_path)
    blockers = [r for r in readings if not r.ok]
    detail = "; ".join(str(r) for r in readings)
    if blockers:
        return False, (f"deferred — {', '.join(r.name for r in blockers)} "
                       f"below floor [{detail}]")
    return True, f"resources ok [{detail}]"
