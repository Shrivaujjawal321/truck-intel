"""Tracking ingest + reads: auth, validation, rate limit, staleness, trails.

Same two-layer contract as the rest of the suite: pure-unit assertions always
run, DB-backed ones skip when PostGIS is unreachable. Devices created here use a
`pytest-` id prefix and are deleted afterwards, so no test row survives in
core.truck_devices.

Run: uv run pytest tests/test_track.py
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from api import common, routes_track
from api.main import app
from api.routes_track import (
    MAX_CLOCK_SKEW_S,
    MAX_PLAUSIBLE_KPH,
    MIN_PING_INTERVAL_S,
    STALE_AFTER_S,
    _Limiter,
)
from truckintel.db import get_conn

DEVICE = "pytest-track-1"


def _call(method: str, path: str, **kw):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            return await getattr(c, method)(path, **kw)

    return asyncio.run(go())


def _db_available() -> bool:
    try:
        with common.connect_ro() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostGIS unreachable")


# ------------------------------------------------------------------ unit tests
def test_limiter_allows_first_then_refuses_within_window():
    lim = _Limiter(10.0)
    ok, wait = lim.allow("a")
    assert ok and wait == 0.0
    ok, wait = lim.allow("a")
    assert not ok
    assert 0 < wait <= 10.0


def test_limiter_is_per_key():
    """One chatty device must not throttle a different truck."""
    lim = _Limiter(10.0)
    assert lim.allow("a")[0]
    assert lim.allow("b")[0]


def test_stale_threshold_is_longer_than_the_ping_interval():
    """A device pinging at the allowed rate can never be labelled stale.

    If these two ever cross, a healthy truck would flicker between live and
    stale — the sort of thing that reads as a data bug to a dispatcher.
    """
    assert STALE_AFTER_S > MIN_PING_INTERVAL_S * 3


def test_ping_rejects_missing_fields():
    r = _call("post", "/v1/track/ping", json={"device_id": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


def test_ping_rejects_out_of_range_latitude():
    r = _call("post", "/v1/track/ping", json={
        "device_id": "x", "token": "0123456789", "lat": 991.0, "lon": -96.8})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


def test_ping_rejects_position_outside_coverage():
    """Longitude in Asia is a swapped-coordinate bug, not a US truck."""
    r = _call("post", "/v1/track/ping", json={
        "device_id": "x", "token": "0123456789", "lat": 20.0, "lon": 77.0})
    assert r.status_code == 400
    assert "outside the covered area" in r.json()["error"]["message"]


def test_ping_rejects_impossible_speed():
    r = _call("post", "/v1/track/ping", json={
        "device_id": "x", "token": "0123456789", "lat": 32.7, "lon": -96.8,
        "speed_kph": MAX_PLAUSIBLE_KPH + 1})
    assert r.status_code == 400
    assert "not a truck" in r.json()["error"]["message"]


def test_ping_rejects_future_clock():
    future = datetime.now(timezone.utc) + timedelta(seconds=MAX_CLOCK_SKEW_S + 600)
    r = _call("post", "/v1/track/ping", json={
        "device_id": "x", "token": "0123456789", "lat": 32.7, "lon": -96.8,
        "observed_at": future.isoformat()})
    assert r.status_code == 400
    assert "future" in r.json()["error"]["message"]


def test_unknown_device_and_bad_token_are_indistinguishable():
    """Different messages here would turn the endpoint into a device-id oracle."""
    a = _call("post", "/v1/track/ping", json={
        "device_id": "definitely-not-registered", "token": "0123456789",
        "lat": 32.7, "lon": -96.8})
    assert a.status_code == 401
    assert a.json()["error"]["message"] == "unknown device_id or bad token"


def test_trail_rejects_absurd_window():
    r = _call("get", "/v1/track/x/trail?minutes=999999")
    assert r.status_code == 400


# -------------------------------------------------------------- DB-backed tests
@pytest.fixture
def device():
    """A real registered device, removed (with its pings) afterwards."""
    token = secrets.token_urlsafe(16)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with get_conn() as pg:
        pg.execute("DELETE FROM core.truck_devices WHERE device_id = %s", (DEVICE,))
        pg.execute(
            "INSERT INTO core.truck_devices (device_id, label, token_sha256) "
            "VALUES (%s, %s, %s)", (DEVICE, "pytest rig", digest))
    # The in-process limiter is module state and survives between tests; clear
    # this device's slot so an unrelated earlier test cannot cause a 429 here.
    routes_track._limiter._last.pop(DEVICE, None)
    yield DEVICE, token
    with get_conn() as pg:
        pg.execute("DELETE FROM core.truck_devices WHERE device_id = %s", (DEVICE,))


def _ping(dev: str, token: str, lat: float, lon: float, **extra):
    body = {"device_id": dev, "token": token, "lat": lat, "lon": lon, **extra}
    return _call("post", "/v1/track/ping", json=body)


@needs_db
def test_ping_stores_and_matches_a_truck_route(device):
    """A fix on I-35 through Dallas must resolve to a real route, not NULL."""
    dev, token = device
    r = _ping(dev, token, 32.7767, -96.8100, speed_kph=95.0, heading_deg=190.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stored"] is True
    assert body["duplicate"] is False
    # Straight-line distance to the nearest truck-designated route. A downtown
    # Dallas coordinate is on the network, so this must be a real number.
    assert body["route_ref"]
    assert isinstance(body["route_dist_m"], int)
    assert body["route_dist_m"] < 5000


@needs_db
def test_ping_without_observed_at_is_labelled_server_stamped(device):
    dev, token = device
    r = _ping(dev, token, 32.7767, -96.8100)
    assert r.json()["observed_at_source"] == "server"


@needs_db
def test_repeated_fix_time_is_a_duplicate_not_a_second_point(device):
    """A phone flushing its outbox must not draw the same position twice."""
    dev, token = device
    stamp = datetime.now(timezone.utc).isoformat()
    first = _ping(dev, token, 32.7767, -96.8100, observed_at=stamp)
    assert first.json()["stored"] is True

    routes_track._limiter._last.pop(dev, None)  # isolate from the rate limiter
    second = _ping(dev, token, 32.7767, -96.8100, observed_at=stamp)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["stored"] is False

    with get_conn() as pg:
        n = pg.execute(
            "SELECT count(*) FROM core.truck_positions WHERE device_id = %s",
            (dev,)).fetchone()[0]
    assert n == 1


@needs_db
def test_second_ping_inside_the_window_is_rate_limited(device):
    dev, token = device
    assert _ping(dev, token, 32.7767, -96.8100).status_code == 200
    r = _ping(dev, token, 32.7800, -96.8000)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"


@needs_db
def test_rejections_are_counted_on_the_device_row(device):
    """The counter must survive the raise that follows it.

    Bumping it on the request's own connection would be rolled back by the
    exception — this asserts the increment is really committed.
    """
    dev, token = device
    _ping(dev, token, 32.7767, -96.8100)          # consume the window
    _ping(dev, token, 32.7800, -96.8000)          # 429
    with get_conn() as pg:
        rejects = pg.execute(
            "SELECT reject_count FROM core.truck_devices WHERE device_id = %s",
            (dev,)).fetchone()[0]
    assert rejects >= 1


@needs_db
def test_bad_token_on_a_real_device_is_counted_and_refused(device):
    dev, _ = device
    r = _ping(dev, "wrong-token-entirely", 32.7767, -96.8100)
    assert r.status_code == 401
    with get_conn() as pg:
        rejects = pg.execute(
            "SELECT reject_count FROM core.truck_devices WHERE device_id = %s",
            (dev,)).fetchone()[0]
    assert rejects >= 1


@needs_db
def test_deactivated_device_is_refused(device):
    dev, token = device
    with get_conn() as pg:
        pg.execute("UPDATE core.truck_devices SET active = false "
                   "WHERE device_id = %s", (dev,))
    r = _ping(dev, token, 32.7767, -96.8100)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


@needs_db
def test_live_reports_fresh_device_as_live_with_an_age(device):
    dev, token = device
    assert _ping(dev, token, 32.7767, -96.8100, speed_kph=88.0).status_code == 200
    body = _call("get", "/v1/track/live").json()
    mine = [d for d in body["devices"] if d["device_id"] == dev]
    assert mine, body
    d = mine[0]
    assert d["status"] == "live"
    assert d["age_seconds"] is not None and d["age_seconds"] < STALE_AFTER_S
    assert body["stale_after_seconds"] == STALE_AFTER_S


@needs_db
def test_old_fix_is_reported_stale_and_can_be_excluded(device):
    """A device that stopped reporting is labelled, not drawn as current."""
    dev, token = device
    _ping(dev, token, 32.7767, -96.8100)
    with get_conn() as pg:
        pg.execute(
            "UPDATE core.truck_devices "
            "SET last_seen_at = now() - make_interval(secs => %s) "
            "WHERE device_id = %s", (STALE_AFTER_S + 600, dev))

    shown = _call("get", "/v1/track/live").json()["devices"]
    assert [d["status"] for d in shown if d["device_id"] == dev] == ["stale"]

    hidden = _call("get", "/v1/track/live?include_stale=false").json()
    assert dev not in [d["device_id"] for d in hidden["devices"]]


@needs_db
def test_out_of_order_ping_does_not_drag_the_live_marker_backwards(device):
    """A late-arriving old fix is history; the newest fix stays the position."""
    dev, token = device
    now = datetime.now(timezone.utc)
    _ping(dev, token, 32.7767, -96.8100, observed_at=now.isoformat())

    routes_track._limiter._last.pop(dev, None)
    old = (now - timedelta(minutes=20)).isoformat()
    _ping(dev, token, 33.5000, -96.5000, observed_at=old)

    with get_conn() as pg:
        row = pg.execute(
            "SELECT ST_Y(last_geom) AS lat, last_seen_at "
            "FROM core.truck_devices WHERE device_id = %s", (dev,)).fetchone()
    # Still the newer Dallas fix, not the older one 80 km north.
    assert abs(row[0] - 32.7767) < 0.01


@needs_db
def test_trail_returns_chronological_points_and_a_linestring(device):
    dev, token = device
    now = datetime.now(timezone.utc)
    for i in range(3):
        routes_track._limiter._last.pop(dev, None)
        stamp = (now - timedelta(minutes=3 - i)).isoformat()
        assert _ping(dev, token, 32.7767 - i * 0.02, -96.8100,
                     observed_at=stamp).status_code == 200

    t = _call("get", f"/v1/track/{dev}/trail?minutes=30").json()
    assert t["returned"] == 3
    assert t["available"] == 3
    assert t["truncated"] is False
    assert t["geometry"]["type"] == "LineString"
    assert len(t["geometry"]["coordinates"]) == 3
    stamps = [p["observed_at"] for p in t["points"]]
    assert stamps == sorted(stamps), "trail must be drawable in order"


@needs_db
def test_trail_of_unknown_device_is_404():
    r = _call("get", "/v1/track/pytest-no-such-device/trail")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@needs_db
def test_single_fix_yields_no_degenerate_linestring(device):
    dev, token = device
    _ping(dev, token, 32.7767, -96.8100)
    t = _call("get", f"/v1/track/{dev}/trail?minutes=30").json()
    assert t["returned"] == 1
    assert t["geometry"] is None


@needs_db
def test_health_reports_the_tracking_write_posture():
    body = _call("get", "/v1/health").json()
    assert body["tracking_write_role"] in {"narrow", "owner_fallback"}
