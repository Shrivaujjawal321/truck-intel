"""The worker's own "is this machine online?" check — no DB, no network.

Overnight on 2026-08-18 the laptop lost DNS and the worker kept claiming jobs
and attempting fetches against nothing: 97 failed runs across eight sources,
every one "Max retries exceeded", none of them about the sources. The units'
ExecStartPre=wait_ready.sh gates the worker at START and cannot help when the
network dies hours later.

These pin the decision itself. The consequence of getting it wrong is not a
crash, it is a quiet flood of failures blamed on innocent feeds — and, past
five in a row, an open circuit breaker on every one of them.

Run: uv run pytest tests/test_worker_network_gate.py
"""
from __future__ import annotations

import socket

import pytest

from truckintel import engine


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    engine._net_probe = None
    yield
    engine._net_probe = None


def test_resolvable_host_means_online(monkeypatch):
    monkeypatch.setattr(engine.socket, "getaddrinfo",
                        lambda *a, **k: [("fake", "addr")])
    assert engine._network_up() is True


def test_resolution_failure_means_offline(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("Temporary failure in name resolution")
    monkeypatch.setattr(engine.socket, "getaddrinfo", boom)
    assert engine._network_up() is False


def test_any_oserror_counts_as_offline(monkeypatch):
    """gaierror is the common case, but a down interface raises other OSError
    subclasses and means the same thing to a job that is about to fetch."""
    def boom(*a, **k):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(engine.socket, "getaddrinfo", boom)
    assert engine._network_up() is False


def test_the_answer_is_cached(monkeypatch):
    """The queue can hand out many jobs a minute; the resolver should not be
    asked once per job."""
    calls = []
    monkeypatch.setattr(engine.socket, "getaddrinfo",
                        lambda *a, **k: (calls.append(1), [("fake", "addr")])[1])
    assert engine._network_up() is True
    for _ in range(20):
        engine._network_up()
    assert len(calls) == 1, "the probe should be cached, not re-run per call"


def test_the_cache_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(engine.socket, "getaddrinfo",
                        lambda *a, **k: (calls.append(1), [("fake", "addr")])[1])
    assert engine._network_up() is True
    # Age the cached answer past its TTL rather than sleeping through it.
    stamp, value = engine._net_probe
    engine._net_probe = (stamp - engine._NET_PROBE_TTL_S - 1, value)
    engine._network_up()
    assert len(calls) == 2, "a stale answer must be re-probed"


def test_recovery_is_noticed(monkeypatch):
    """Offline then online: the worker must start working again on its own,
    without anyone restarting it."""
    state = {"up": False}

    def probe(*a, **k):
        if not state["up"]:
            raise socket.gaierror("down")
        return [("fake", "addr")]

    monkeypatch.setattr(engine.socket, "getaddrinfo", probe)
    assert engine._network_up() is False
    state["up"] = True
    stamp, value = engine._net_probe
    engine._net_probe = (stamp - engine._NET_PROBE_TTL_S - 1, value)
    assert engine._network_up() is True
