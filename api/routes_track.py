"""Real-time truck tracking: ingest pings, serve live positions and trails.

Boss's ask (2026-07-26): see trucks moving on the map in real time. Source
decided the same day — our own GPS ingest, not a telematics vendor: free, legal,
and provable today from a phone browser. `sql/schema_tracking.sql` holds the
tables and the narrow write role.

The three things this module refuses to fake
--------------------------------------------
1. **A stale fix is never a current position.** Every read reports
   `age_seconds` from the device's own `observed_at`, and a device past
   STALE_AFTER_S is labelled `stale` rather than drawn as if it were live. A
   phone that lost signal in Nebraska must not render as a truck parked in
   Nebraska.
2. **Ping ≠ trusted.** A ping is authenticated (device token), rate-limited, and
   sanity-checked (US bounds, plausible timestamp, plausible speed). Anything
   that fails is counted against the device and rejected with a stable code —
   never silently dropped, never silently accepted.
3. **`route_dist_m` is straight-line.** It says "this truck is within X m of a
   truck route", not "X m of driving". The same measurement, and the same 5 km
   buffer, as the fuel and mechanic layers.

Why the write path is separate
------------------------------
The rest of the API holds a `default_transaction_read_only=on` session so a bug
cannot write. Ingest needs to write, so it uses its own login role that can
INSERT pings and UPDATE three columns of core.truck_devices — and is refused by
Postgres on everything else (verified: `DELETE FROM core.bridges` -> permission
denied). `/v1/health` reports whether that narrow role is actually in use.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from api import common
from truckintel.config import track_database_url

router = APIRouter(tags=["tracking"])

# A device quiet for longer than this is reported `stale`, not `live`. 3 minutes
# covers a phone that missed a few 30-second pings through a tunnel or a dead
# cell without pretending a truck that stopped reporting an hour ago is moving.
STALE_AFTER_S = 180
# Trail default/ceiling. A 24 h trail at one ping per 10 s is 8,640 points, which
# is a fine query and a terrible map layer, so the read is capped and says so.
TRAIL_DEFAULT_MIN = 60
TRAIL_MAX_POINTS = 5000

# Rate limit: the minimum gap between accepted pings from one device. 5 s allows
# a genuinely real-time feed (a truck at 100 km/h moves 139 m in 5 s) while
# making a runaway client cheap to refuse.
MIN_PING_INTERVAL_S = 5.0
# Continental-US-plus-AK/HI envelope. Deliberately generous: this rejects null
# island and swapped lat/lon, not a truck near a border.
US_BOUNDS = (-180.0, 15.0, -64.0, 72.0)
# Above this, the "fix" is not a truck. Kept high enough not to argue with a bad
# GPS-derived speed on a genuine highway run.
MAX_PLAUSIBLE_KPH = 200.0
# A fix from the future is a clock problem; a small skew is tolerated because
# phone clocks drift. Older than this and it is history, not a live position —
# still stored (it is real data), just never counted as current.
MAX_CLOCK_SKEW_S = 120


class Ping(BaseModel):
    """One GPS fix from one device."""

    device_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=8, max_length=256)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # Device clock, ISO-8601. Omitted -> the server stamps arrival time and says
    # so in the response, rather than inventing a fix time.
    observed_at: datetime | None = None
    speed_kph: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    accuracy_m: float | None = Field(default=None, ge=0)


# --------------------------------------------------------------- rate limiter
class _Limiter:
    """Per-device minimum interval, in-process.

    Deliberately not distributed: one uvicorn process serves this API, and a
    limiter that silently does nothing behind a load balancer would be worse
    than none. If this is ever run multi-process, move it to Postgres or Redis
    and delete this class — do not leave it looking like it still works.
    """

    def __init__(self, min_interval_s: float) -> None:
        self._min = min_interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            prev = self._last.get(key)
            if prev is not None and (now - prev) < self._min:
                return False, self._min - (now - prev)
            self._last[key] = now
            return True, 0.0


_limiter = _Limiter(MIN_PING_INTERVAL_S)

_TRACK_DSN, _TRACK_NARROW = track_database_url()


def track_role_is_narrow() -> bool:
    """True when ingest uses the restricted login rather than the owner."""
    return _TRACK_NARROW


def _connect_rw() -> psycopg.Connection:
    """Writable session for ingest only. Every other route uses connect_ro()."""
    return psycopg.connect(_TRACK_DSN, row_factory=dict_row)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.post("/v1/track/ping")
def ping(p: Ping) -> dict:
    """Record one GPS fix. Auth by device token; rejects are counted, not hidden.

    Returns the stored fix's age and its distance to the nearest truck route, so
    a driver's phone can show "you are on I-80, 41 m off centreline" without a
    second call.
    """
    lon_min, lat_min, lon_max, lat_max = US_BOUNDS
    if not (lon_min <= p.lon <= lon_max and lat_min <= p.lat <= lat_max):
        raise common.ApiError(
            "invalid_param",
            f"({p.lat}, {p.lon}) is outside the covered area — check for swapped "
            f"lat/lon",
        )
    if p.speed_kph is not None and p.speed_kph > MAX_PLAUSIBLE_KPH:
        raise common.ApiError(
            "invalid_param", f"speed_kph {p.speed_kph} exceeds "
                             f"{MAX_PLAUSIBLE_KPH:g} — not a truck")

    now = datetime.now(timezone.utc)
    observed = p.observed_at or now
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    server_stamped = p.observed_at is None
    if observed > now + timedelta(seconds=MAX_CLOCK_SKEW_S):
        raise common.ApiError(
            "invalid_param",
            f"observed_at is {(observed - now).total_seconds():.0f}s in the "
            f"future — device clock is wrong",
        )

    try:
        with _connect_rw() as conn:
            dev = conn.execute(
                "SELECT device_id, token_sha256, active FROM core.truck_devices "
                "WHERE device_id = %s",
                (p.device_id,),
            ).fetchone()
            # Same error for unknown device and bad token: telling a caller which
            # half was wrong turns this into a device-id oracle.
            if dev is None or dev["token_sha256"] != _sha256(p.token):
                if dev is not None:
                    _bump_reject(p.device_id)
                raise common.ApiError(
                    "unauthorized", "unknown device_id or bad token", status=401
                )
            if not dev["active"]:
                _bump_reject(p.device_id)
                raise common.ApiError(
                    "forbidden", f"device '{p.device_id}' is deactivated", status=403
                )

            # Rate limit AFTER the token verifies, on purpose. The device_id is
            # attacker-supplied, so limiting before auth would let anyone send
            # junk pings carrying a real truck's id and eat that truck's budget —
            # locking out the legitimate device. Costs one indexed SELECT per
            # bad request, which is the right trade against a trivial DoS.
            allowed, retry_in = _limiter.allow(p.device_id)
            if not allowed:
                # Counted on the device row so a chatty client is visible in the
                # data, then refused. 429 with the wait, not a silent drop.
                _bump_reject(p.device_id)
                raise common.ApiError(
                    "rate_limited",
                    f"one ping per {MIN_PING_INTERVAL_S:g}s per device; retry in "
                    f"{retry_in:.1f}s",
                    status=429,
                )

            row = conn.execute(
                """
                WITH near AS (
                  SELECT k.route_id, k.route_ref, k.d
                  FROM (
                    SELECT t.route_id, t.route_ref,
                           ST_Distance(
                             t.geom::geography,
                             ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
                           ) AS d
                    FROM core.truck_routes t
                    ORDER BY t.geom <-> ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)
                    LIMIT 10
                  ) k
                  ORDER BY k.d
                  LIMIT 1
                )
                INSERT INTO core.truck_positions
                  (device_id, observed_at, geom, speed_kph, heading_deg,
                   accuracy_m, route_id, route_ref, route_dist_m)
                SELECT %(device_id)s, %(observed_at)s,
                       ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                       %(speed)s, %(heading)s, %(accuracy)s,
                       near.route_id, near.route_ref, round(near.d)::int
                FROM near
                -- A phone retrying the same fix must not draw a second point.
                ON CONFLICT (device_id, observed_at) DO NOTHING
                RETURNING ping_id, route_ref, route_dist_m
                """,
                {
                    "device_id": p.device_id, "observed_at": observed,
                    "lon": p.lon, "lat": p.lat, "speed": p.speed_kph,
                    "heading": p.heading_deg, "accuracy": p.accuracy_m,
                },
            ).fetchone()

            duplicate = row is None
            if not duplicate:
                # last_* is the newest fix only. An out-of-order ping (a phone
                # flushing its outbox) is stored as history but must not drag the
                # live marker backwards.
                conn.execute(
                    """
                    UPDATE core.truck_devices SET
                      last_seen_at   = GREATEST(coalesce(last_seen_at,
                                                         %(observed_at)s),
                                                %(observed_at)s),
                      last_geom      = CASE WHEN last_seen_at IS NULL
                                              OR %(observed_at)s >= last_seen_at
                                            THEN ST_SetSRID(
                                                   ST_MakePoint(%(lon)s, %(lat)s), 4326)
                                            ELSE last_geom END,
                      last_speed_kph = CASE WHEN last_seen_at IS NULL
                                              OR %(observed_at)s >= last_seen_at
                                            THEN %(speed)s
                                            ELSE last_speed_kph END,
                      ping_count     = ping_count + 1
                    WHERE device_id = %(device_id)s
                    """,
                    {
                        "device_id": p.device_id, "observed_at": observed,
                        "lon": p.lon, "lat": p.lat, "speed": p.speed_kph,
                    },
                )
    except psycopg.Error as exc:
        raise common.ApiError(
            "upstream_unavailable",
            f"database unavailable ({type(exc).__name__})",
            status=503,
        ) from exc

    return {
        "stored": not duplicate,
        "duplicate": duplicate,
        "device_id": p.device_id,
        "observed_at": observed,
        # Says so when we stamped the time ourselves, so a client cannot mistake
        # arrival time for fix time.
        "observed_at_source": "server" if server_stamped else "device",
        "age_seconds": round((now - observed).total_seconds(), 1),
        "route_ref": (row or {}).get("route_ref"),
        # Straight-line to the nearest truck route, not drive distance.
        "route_dist_m": (row or {}).get("route_dist_m"),
    }


def _bump_reject(device_id: str) -> None:
    """Count a refused ping on the device row, in its OWN transaction.

    Deliberately not reusing the caller's connection: every caller raises
    immediately afterwards, and psycopg rolls back the `with` block on an
    exception — so an increment written on the request's own connection would be
    discarded exactly in the cases it exists to record. One extra short-lived
    connection per rejected ping is the price of the counter being real.

    Never raises: failing to record a rejection must not turn a 401 into a 500.
    """
    try:
        with _connect_rw() as own:
            own.execute(
                "UPDATE core.truck_devices SET reject_count = reject_count + 1 "
                "WHERE device_id = %s",
                (device_id,),
            )
    except psycopg.Error:
        pass


@router.get("/v1/track/live")
def live(
    include_stale: bool = Query(
        True, description="keep devices quiet for more than "
                          f"{STALE_AFTER_S}s in the response, labelled 'stale'"),
) -> dict:
    """Every active device's newest fix, each labelled live or stale.

    Reads the denormalised last_* columns, so this is a small-table scan rather
    than a top-1-per-device over the whole ping history.
    """
    rows = common.q_all(
        f"""
        SELECT d.device_id, d.label, d.last_seen_at, d.last_speed_kph,
               ST_X(d.last_geom) AS lon, ST_Y(d.last_geom) AS lat,
               d.ping_count, d.reject_count,
               EXTRACT(EPOCH FROM (now() - d.last_seen_at)) AS age_s,
               p.route_ref, p.route_dist_m, p.heading_deg
        FROM core.truck_devices d
        -- Newest ping for the route/heading detail the summary columns omit.
        LEFT JOIN LATERAL (
          SELECT route_ref, route_dist_m, heading_deg
          FROM core.truck_positions
          WHERE device_id = d.device_id
          ORDER BY observed_at DESC
          LIMIT 1
        ) p ON true
        WHERE d.active AND d.last_geom IS NOT NULL
        ORDER BY d.last_seen_at DESC NULLS LAST
        """
    )
    out = []
    for r in rows:
        age = float(r["age_s"]) if r["age_s"] is not None else None
        stale = age is None or age > STALE_AFTER_S
        if stale and not include_stale:
            continue
        out.append({
            "device_id": r["device_id"],
            "label": common.unknown(r["label"]),
            "lon": r["lon"], "lat": r["lat"],
            "observed_at": r["last_seen_at"],
            "age_seconds": round(age, 1) if age is not None else None,
            # The whole point: a fix this old is not a live position, and the
            # API says which it is rather than leaving it to the map's colour.
            "status": "stale" if stale else "live",
            "speed_kph": common.unknown(r["last_speed_kph"]),
            "heading_deg": common.unknown(r["heading_deg"]),
            "route_ref": common.unknown(r["route_ref"]),
            "route_dist_m": common.unknown(r["route_dist_m"]),
            "ping_count": r["ping_count"],
            "reject_count": r["reject_count"],
        })
    live_n = sum(1 for d in out if d["status"] == "live")
    return {
        "devices": out,
        "counts": {"total": len(out), "live": live_n, "stale": len(out) - live_n},
        "stale_after_seconds": STALE_AFTER_S,
    }


@router.get("/v1/track/{device_id}/trail")
def trail(
    device_id: str = Path(min_length=1, max_length=64),
    minutes: int = Query(TRAIL_DEFAULT_MIN, ge=1, le=60 * 24 * 7),
) -> dict:
    """One device's recent path as a GeoJSON LineString plus its points.

    Capped at TRAIL_MAX_POINTS. When the cap bites, `truncated` is true and
    `returned`/`available` both appear — a shortened trail must be visible as a
    cap, not mistaken for a truck that stopped moving.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    available = common.q_all(
        "SELECT count(*) AS n FROM core.truck_positions "
        "WHERE device_id = %s AND observed_at >= %s",
        (device_id, since),
    )[0]["n"]
    if available == 0:
        exists = common.q_all(
            "SELECT 1 AS ok FROM core.truck_devices WHERE device_id = %s",
            (device_id,),
        )
        if not exists:
            raise common.ApiError(
                "not_found", f"unknown device '{device_id}'", status=404)
    pts = common.q_all(
        """
        SELECT observed_at, ST_X(geom) AS lon, ST_Y(geom) AS lat,
               speed_kph, heading_deg, accuracy_m, route_ref, route_dist_m
        FROM core.truck_positions
        WHERE device_id = %s AND observed_at >= %s
        ORDER BY observed_at DESC
        LIMIT %s
        """,
        (device_id, since, TRAIL_MAX_POINTS),
    )
    pts.reverse()  # chronological for drawing
    coords = [[p["lon"], p["lat"]] for p in pts]
    return {
        "device_id": device_id,
        "window_minutes": minutes,
        "returned": len(pts),
        "available": available,
        "truncated": available > len(pts),
        "max_points": TRAIL_MAX_POINTS,
        # A LineString needs two points; one fix is a point, and saying so beats
        # emitting a degenerate geometry.
        "geometry": ({"type": "LineString", "coordinates": coords}
                     if len(coords) >= 2 else None),
        "points": [
            {
                "observed_at": p["observed_at"],
                "lon": p["lon"], "lat": p["lat"],
                "speed_kph": common.unknown(p["speed_kph"]),
                "heading_deg": common.unknown(p["heading_deg"]),
                "accuracy_m": common.unknown(p["accuracy_m"]),
                "route_ref": common.unknown(p["route_ref"]),
                "route_dist_m": common.unknown(p["route_dist_m"]),
            }
            for p in pts
        ],
    }
