"""polite_get() unit tests — no network; requests.get and time.sleep are stubbed."""
from __future__ import annotations

import time

import pytest

from truckintel import politeness
from truckintel.config import user_agent
from truckintel.politeness import PoliteRefusal, polite_get

URL = "https://api.example.gov/data"


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"ok", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


@pytest.fixture
def sleeps(monkeypatch):
    """Reset the per-host throttle map and capture (instead of perform) sleeps."""
    politeness._last_hit.clear()
    recorded: list[float] = []
    monkeypatch.setattr(politeness.time, "sleep", lambda s: recorded.append(s))
    return recorded


def _serve(monkeypatch, responses: list[FakeResponse]) -> list[dict]:
    calls: list[dict] = []
    it = iter(responses)

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        nxt = next(it)
        # An entry that IS an exception is raised rather than returned, so a
        # transient network failure can be expressed in the same response list.
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(politeness.requests, "get", fake_get)
    return calls


def test_sends_user_agent_and_conditional_headers(monkeypatch, sleeps):
    calls = _serve(monkeypatch, [FakeResponse(200, b"body", {"ETag": '"abc"'})])
    res = polite_get(URL, etag='"old"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    h = calls[0]["headers"]
    assert h["User-Agent"] == user_agent()
    assert h["If-None-Match"] == '"old"'
    assert h["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"
    assert res.status_code == 200 and res.content == b"body" and res.etag == '"abc"'
    assert not res.not_modified


def test_304_returns_not_modified_and_keeps_validators(monkeypatch, sleeps):
    _serve(monkeypatch, [FakeResponse(304, b"", {})])
    res = polite_get(URL, etag='"old"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    assert res.not_modified and res.content == b""
    assert res.etag == '"old"'
    assert res.last_modified == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_403_raises_refusal_immediately(monkeypatch, sleeps):
    calls = _serve(monkeypatch, [FakeResponse(403)])
    with pytest.raises(PoliteRefusal):
        polite_get(URL)
    assert len(calls) == 1  # never retried around the refusal


def test_429_honors_retry_after_once(monkeypatch, sleeps):
    calls = _serve(
        monkeypatch,
        [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, b"ok")],
    )
    res = polite_get(URL)
    assert res.status_code == 200
    assert len(calls) == 2
    assert 7.0 in sleeps


def test_second_429_raises_refusal(monkeypatch, sleeps):
    calls = _serve(
        monkeypatch,
        [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(429)],
    )
    with pytest.raises(PoliteRefusal):
        polite_get(URL)
    assert len(calls) == 2  # exactly one retry, never a loop


def test_second_503_returns_failure_to_caller(monkeypatch, sleeps):
    _serve(monkeypatch, [FakeResponse(503, headers={"Retry-After": "1"}), FakeResponse(503)])
    res = polite_get(URL)
    assert res.status_code == 503 and not res.not_modified


def test_huge_retry_after_fails_fast_instead_of_freezing(monkeypatch, sleeps):
    """A server-chosen Retry-After beyond the cap must fail the run, never
    block the single worker (and its DB session) for hours/days."""
    calls = _serve(monkeypatch, [FakeResponse(429, headers={"Retry-After": "864000"})])
    with pytest.raises(PoliteRefusal):
        polite_get(URL)
    assert len(calls) == 1 and sleeps == []  # no 10-day sleep ever started


def test_per_host_min_interval_throttles(monkeypatch, sleeps):
    _serve(monkeypatch, [FakeResponse(200)])
    politeness._last_hit["api.example.gov"] = time.monotonic()
    polite_get(URL, min_interval_s=10.0)
    assert sleeps and sleeps[0] > 8.0  # waited out the per-host interval


# ----------------------------------------------- transient network failures
#
# Added 2026-08-18. A read timeout used to raise straight out of polite_get
# with no retry, so one blip cost a whole scheduled run: wzdx_mn failed 21 of
# 48 runs in 48 h that way while the endpoint itself answered in 3.5 s when
# asked by hand.

def test_read_timeout_retries_once_then_succeeds(monkeypatch, sleeps):
    calls = _serve(monkeypatch, [
        politeness.requests.Timeout("Read timed out. (read timeout=60.0)"),
        FakeResponse(200, b"recovered"),
    ])
    res = polite_get(URL)
    assert res.status_code == 200 and res.content == b"recovered"
    assert len(calls) == 2, "a transient timeout must be retried exactly once"
    # Two sleeps, and the second one is the point: the throttle still charges
    # the per-host minimum on the way round, because a request that timed out
    # still consumed the host's attention. Retrying must not be a way to skip
    # the politeness interval.
    assert sleeps[0] == politeness._TRANSIENT_RETRY_WAIT_S
    assert len(sleeps) == 2 and 0 < sleeps[1] <= 1.0


def test_dns_failure_retries_once_then_succeeds(monkeypatch, sleeps):
    """wzdx_ks failed on NameResolutionError, which requests wraps in
    ConnectionError — the same transient class as a timeout."""
    calls = _serve(monkeypatch, [
        politeness.requests.ConnectionError("NameResolutionError"),
        FakeResponse(200, b"recovered"),
    ])
    assert polite_get(URL).content == b"recovered"
    assert len(calls) == 2


def test_second_timeout_gives_up_instead_of_looping(monkeypatch, sleeps):
    calls = _serve(monkeypatch, [
        politeness.requests.Timeout("first"),
        politeness.requests.Timeout("second"),
    ])
    with pytest.raises(politeness.requests.Timeout):
        polite_get(URL)
    assert len(calls) == 2, "two attempts is the cap; it must not loop"


def test_a_503_then_a_timeout_still_caps_at_two_attempts(monkeypatch, sleeps):
    """The retry budget is shared: a host cannot spend more than two attempts
    by alternating failure modes."""
    calls = _serve(monkeypatch, [
        FakeResponse(503, headers={"Retry-After": "1"}),
        politeness.requests.Timeout("then a hang"),
    ])
    with pytest.raises(politeness.requests.Timeout):
        polite_get(URL)
    assert len(calls) == 2


def test_a_timed_out_host_still_counts_as_contacted(monkeypatch, sleeps):
    """The throttle must treat a timed-out request as a hit — the host spent
    attention on it, so the next call still owes it the min interval."""
    _serve(monkeypatch, [
        politeness.requests.Timeout("hang"),
        FakeResponse(200),
    ])
    polite_get(URL, min_interval_s=5.0)
    assert "api.example.gov" in politeness._last_hit
