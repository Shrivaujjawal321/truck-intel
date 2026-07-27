"""Resource-gate tests — admission control for heavy jobs.

The gate exists because `Nice=10` and `IOSchedulingClass=idle` were already on
the OSM units and the laptop still became unusable: priority never declines to
START a job. These tests pin the four behaviours that make the gate safe rather
than merely present:

  * it BLOCKS when a resource is below its floor
  * it never blocks on a resource it could not measure
  * a refusal DEFERS (job returns to 'queued'), it does not fail
  * unit files declare their kernel ceilings inside [Service]

Run: uv run pytest tests/test_resources.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from truckintel import resources

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ gating

def test_gate_passes_when_everything_is_comfortable(monkeypatch):
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 12.0)
    monkeypatch.setattr(resources, "free_disk_gb", lambda p=".": 200.0)
    monkeypatch.setattr(resources, "load_per_cpu", lambda: 0.3)
    monkeypatch.setattr(resources, "on_battery", lambda: False)
    ok, why = resources.check()
    assert ok and "ok" in why


@pytest.mark.parametrize("attr,value,name", [
    ("free_ram_gb", 0.5, "free_ram"),
    ("free_disk_gb", 5.0, "free_disk"),
    ("load_per_cpu", 9.0, "load_per_cpu"),
])
def test_each_resource_can_block_on_its_own(monkeypatch, attr, value, name):
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 12.0)
    monkeypatch.setattr(resources, "free_disk_gb", lambda p=".": 200.0)
    monkeypatch.setattr(resources, "load_per_cpu", lambda: 0.3)
    monkeypatch.setattr(resources, "on_battery", lambda: False)
    monkeypatch.setattr(resources, attr,
                        (lambda p=".": value) if "disk" in attr else (lambda: value))
    ok, why = resources.check()
    assert not ok
    assert why.startswith("deferred")
    assert name in why


def test_the_reason_names_every_measurement_not_only_the_failing_one(monkeypatch):
    """A deferral record that says only 'low memory' cannot be argued with
    afterwards. The numbers are the entire point of gating on them."""
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 0.5)
    monkeypatch.setattr(resources, "free_disk_gb", lambda p=".": 200.0)
    monkeypatch.setattr(resources, "load_per_cpu", lambda: 0.3)
    monkeypatch.setattr(resources, "on_battery", lambda: False)
    _, why = resources.check()
    for name in ("free_ram", "free_disk", "load_per_cpu"):
        assert name in why
    assert "BLOCKS" in why and "ok" in why


def test_battery_blocks_but_a_machine_without_one_does_not(monkeypatch):
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 12.0)
    monkeypatch.setattr(resources, "free_disk_gb", lambda p=".": 200.0)
    monkeypatch.setattr(resources, "load_per_cpu", lambda: 0.3)

    monkeypatch.setattr(resources, "on_battery", lambda: True)
    assert resources.check()[0] is False

    # A desktop or VM reports None. Unknown must never block work — otherwise
    # the gate would refuse every job on a server with no battery at all.
    monkeypatch.setattr(resources, "on_battery", lambda: None)
    assert resources.check()[0] is True


def test_unmeasurable_resources_never_block(monkeypatch):
    """free_ram_gb/free_disk_gb return inf when /proc or statvfs is unavailable.
    A gate that fails closed on a reading it could not take would stop the
    pipeline on any platform it does not recognise."""
    monkeypatch.setattr(resources, "free_ram_gb", lambda: float("inf"))
    monkeypatch.setattr(resources, "free_disk_gb", lambda p=".": float("inf"))
    monkeypatch.setattr(resources, "load_per_cpu", lambda: 0.0)
    monkeypatch.setattr(resources, "on_battery", lambda: None)
    assert resources.check()[0] is True


def test_kill_switch_wins_over_every_floor(monkeypatch):
    monkeypatch.setenv("TRUCKINTEL_IGNORE_RESOURCE_GATE", "1")
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 0.0)
    ok, why = resources.check()
    assert ok and "disabled by" in why


def test_thresholds_are_env_overridable(monkeypatch):
    monkeypatch.setattr(resources, "free_ram_gb", lambda: 5.0)
    monkeypatch.setattr(resources, "free_disk_gb", lambda p=".": 200.0)
    monkeypatch.setattr(resources, "load_per_cpu", lambda: 0.3)
    monkeypatch.setattr(resources, "on_battery", lambda: False)
    assert resources.check()[0] is True              # 5 GB clears the 3 GB default
    monkeypatch.setenv("TRUCKINTEL_MIN_FREE_RAM_GB", "8")
    assert resources.check()[0] is False             # ...but not an 8 GB floor


# --------------------------------------------------------- unit-file ceilings

HEAVY_UNITS = ["truckintel-pois", "truckintel-businesses", "truckintel-fuel-verify",
               "truckintel-mechanics", "truckintel-worker"]


@pytest.mark.parametrize("stem", HEAVY_UNITS)
def test_heavy_units_declare_kernel_ceilings_inside_the_service_section(stem):
    """systemd assigns a directive to the section it FOLLOWS. Appended after
    [Install], MemoryMax parses as an [Install] directive and is silently
    ignored — `systemctl show` then reports MemoryMax=infinity while the file
    plainly reads MemoryMax=6G. That happened on the first attempt here.
    """
    text = (REPO / "deploy" / f"{stem}.service").read_text()
    assert "MemoryMax=" in text, f"{stem} has no memory ceiling"

    # Everything from [Service] up to the next section header.
    m = re.search(r"^\[Service\]$(.*?)(?=^\[[A-Za-z]+\]$|\Z)", text,
                  re.M | re.S)
    assert m, f"{stem} has no [Service] section"
    service = m.group(1)
    assert "MemoryMax=" in service, f"{stem}: MemoryMax is outside [Service]"
    assert "CPUQuota=" in service, f"{stem}: CPUQuota is outside [Service]"


@pytest.mark.parametrize("stem", HEAVY_UNITS)
def test_heavy_units_forbid_swap(stem):
    """MemorySwapMax=0 is the point: the OSM pass pushed 1.1 GB into swap and
    that, not the RSS, is what made the machine stop responding."""
    assert "MemorySwapMax=0" in (REPO / "deploy" / f"{stem}.service").read_text()
