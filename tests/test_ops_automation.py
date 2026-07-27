"""Tests for the automation added on 2026-07-27: alerting, the ops watchdog,
and the derived-rebuild hook.

Each of these guards a failure that had already happened once:

  * ops_watch excluded EVERY source because '_' is a LIKE wildcard, so it
    reported "clean" against a week containing 19 real failures
  * nothing rebuilt the routing graph when core.truck_routes republished, so
    the router would answer from the previous network with no error anywhere
  * notify truncated silently at Telegram's cap
  * the WZDx liveness check read a capped 400 KB and called every large feed
    "not-json" — including four that were live and publishing that morning

Run: uv run pytest tests/test_ops_automation.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from truckintel import notify

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ops_watch = _load("ops_watch")
route_rebuild = _load("route_rebuild")


# ------------------------------------------------------------------- notify

def test_short_body_is_sent_whole():
    body = "two sources stale"
    assert notify._fit(body, "the log") == body


def test_over_long_body_is_cut_with_a_visible_marker():
    body = "x" * (notify.TELEGRAM_LIMIT + 5000)
    out = notify._fit(body, "status_weekly.md")
    assert len(out) <= notify.TELEGRAM_LIMIT
    # The reader must be able to tell something was dropped, how much, and
    # where the rest is. A silent cut in an ALERT is how you miss the finding
    # that mattered.
    assert "truncated" in out and "status_weekly.md" in out


def test_unconfigured_channel_is_skipped_not_an_error(monkeypatch):
    """A laptop with no token is a valid deployment. Reporting that as an
    error would make every clean run look broken."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notify, "load_dotenv", lambda: None)
    results = notify.deliver("anything")
    assert {r["status"] for r in results} == {"skipped"}
    assert notify.report(results) is True


def test_delivery_failure_never_raises(monkeypatch):
    """The job that had news has already finished its work. A delivery hiccup
    must not turn a successful run into a crash."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(notify, "load_dotenv", lambda: None)

    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(notify.requests, "post", boom)
    results = notify.deliver("body")
    tg = next(r for r in results if r["channel"] == "telegram")
    assert tg["status"] == "error" and "ConnectionError" in tg["detail"]
    assert notify.report(results) is False      # caller exits non-zero


# --------------------------------------------------------------- ops_watch

def test_test_fixture_pattern_escapes_the_like_wildcard():
    """THE bug. In SQL LIKE, '_' matches any single character, so the obvious
    '_%' matches every source id and the watchdog excludes the whole table —
    reporting "clean" forever. The backslash is what makes it a literal.
    """
    assert ops_watch.TEST_PREFIX_LIKE == r"\_%"
    assert ops_watch.TEST_PREFIX_LIKE.startswith("\\_")


@pytest.mark.parametrize("source_id,excluded", [
    ("_test_red", True),        # fixture — must be excluded
    ("_test_green", True),
    ("osm_pois", False),        # real source with an underscore INSIDE it
    ("ntad_national_network", False),
    ("aaa_daily", False),
])
def test_only_leading_underscore_ids_are_treated_as_fixtures(source_id, excluded):
    """Real source ids are full of underscores. Excluding on 'contains _'
    would silence the entire pipeline."""
    import re
    pattern = "^" + ops_watch.TEST_PREFIX_LIKE.replace("\\_", "_").replace("%", ".*")
    assert bool(re.match(pattern, source_id)) is excluded


def test_cooldown_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_watch, "STATE_FILE", tmp_path / "s.json")
    assert ops_watch.load_state() == {}          # missing file is not an error
    ops_watch.save_state({"failing/x": "2026-07-27T00:00:00+00:00"})
    assert ops_watch.load_state()["failing/x"].startswith("2026-07-27")


def test_corrupt_state_file_does_not_crash_the_watchdog(tmp_path, monkeypatch):
    """A half-written state file must not stop alerts. Losing the cooldown is
    a duplicate notification; losing the watchdog is a missed outage."""
    p = tmp_path / "s.json"
    p.write_text("{not json")
    monkeypatch.setattr(ops_watch, "STATE_FILE", p)
    assert ops_watch.load_state() == {}


# ------------------------------------------------------------ route_rebuild

def test_rebuild_order_is_the_dependency_order():
    """Noding CHANGES the topology, so components and the snap index are
    functions of it and must follow. Limits are per-edge and follow the edges.
    A wrong order builds a graph that looks fine and routes incorrectly —
    exactly the silent failure this job exists to prevent.
    """
    labels = [s[0] for s in route_rebuild.STEPS]
    assert labels.index("route graph") < labels.index("noding")
    assert labels.index("noding") < labels.index("components")
    assert labels.index("noding") < labels.index("snap index")
    assert labels.index("route graph") < labels.index("edge limits")


def test_every_rebuild_step_points_at_a_file_that_exists():
    """A renamed SQL file would otherwise fail at 3 a.m. inside a derived job."""
    for label, kind, payload in route_rebuild.STEPS:
        assert (REPO / payload).exists(), f"{label} -> missing {payload}"
        assert kind in ("sql", "py")


def test_viewer_geometry_is_rebuilt_too():
    """The map's low-zoom layer is derived from truck_routes as much as the
    graph is. Leaving it out would redraw the country from stale geometry."""
    assert any("viewer" in s[2] for s in route_rebuild.STEPS)


def test_rebuild_is_registered_as_a_derived_runner():
    """The hook enqueues a job; without the runner entry the job fails with
    'no derived-job runner' and the graph silently stays stale anyway."""
    from truckintel.engine import _DERIVED_RUNNERS, ROUTE_REBUILD_SOURCE_ID
    argv = _DERIVED_RUNNERS[ROUTE_REBUILD_SOURCE_ID]
    assert argv[0] == "scripts/route_rebuild.py"
    # --if-stale, or every routes swap costs ~50 min rebuilding an identical
    # graph even when the network did not change.
    assert "--if-stale" in argv
