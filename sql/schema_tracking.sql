-- Real-time truck tracking: devices, their pings, and a write role narrow
-- enough that the API cannot use it to touch anything else.
--
-- Boss's ask (2026-07-26): live truck positions on the map. Decision the same
-- day: our own GPS ingest rather than a telematics vendor — free, legal, and
-- testable today with nothing but a phone browser. A Samsara/Motive adapter can
-- be added later behind the same tables.
--
-- WHY A SEPARATE ROLE
-- The read API connects with `default_transaction_read_only=on` so that a buggy
-- route physically cannot write (api/common.py). Tracking ingest is the first
-- write path in the API, and reusing the owner's credentials to get it would
-- turn that guarantee back into a comment. So ingest gets its own login role
-- with INSERT on one table and UPDATE on three columns of another. If the
-- tracking route were ever tricked into running `DELETE FROM core.bridges`,
-- Postgres refuses it — not our code.

CREATE TABLE IF NOT EXISTS core.truck_devices (
    device_id     TEXT PRIMARY KEY,           -- caller-chosen, e.g. 'truck-14'
    label         TEXT,                       -- what a human calls it
    token_sha256  TEXT NOT NULL,              -- sha256 of the shared secret; the
                                              -- secret itself is never stored
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Denormalised newest fix. Kept so "where is my fleet right now" is one
    -- indexed scan of a small table instead of a top-1-per-device over history.
    last_seen_at  TIMESTAMPTZ,
    last_geom     geometry(Point, 4326),
    last_speed_kph DOUBLE PRECISION,
    -- Counters, not a rate limiter. The limiter lives in the API (per-process,
    -- per-device); these exist so a misbehaving device is visible after the fact.
    ping_count    BIGINT NOT NULL DEFAULT 0,
    reject_count  BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS core.truck_positions (
    ping_id     BIGSERIAL PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES core.truck_devices(device_id)
                ON DELETE CASCADE,
    -- When the FIX was taken on the device, not when it reached us. A ping that
    -- sat in a phone's outbox for 20 minutes must not read as a current
    -- position, so both timestamps are kept and the API reports the age of
    -- `observed_at`.
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    geom        geometry(Point, 4326) NOT NULL,
    speed_kph   DOUBLE PRECISION,             -- NULL = device did not report
    heading_deg DOUBLE PRECISION,             -- NULL = unknown, never 0
    accuracy_m  DOUBLE PRECISION,             -- GPS-reported horizontal accuracy
    -- Straight-line distance to the nearest truck route at ping time. Answers
    -- "is this truck on the network?" without re-running KNN on every read.
    route_id      BIGINT,
    route_ref     TEXT,
    route_dist_m  INTEGER,
    UNIQUE (device_id, observed_at)           -- idempotent retries: a phone that
                                              -- resends the same fix does not
                                              -- create a second point
);

CREATE INDEX IF NOT EXISTS truck_positions_device_time_ix
    ON core.truck_positions (device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS truck_positions_geom_gix
    ON core.truck_positions USING GIST (geom);
-- Retention sweeps delete by age across all devices; this serves that scan.
CREATE INDEX IF NOT EXISTS truck_positions_observed_ix
    ON core.truck_positions (observed_at);
CREATE INDEX IF NOT EXISTS truck_devices_last_seen_ix
    ON core.truck_devices (last_seen_at DESC) WHERE active;

-- ---------------------------------------------------------------- write role
-- Password is a local-dev value, consistent with DATABASE_URL in .env.example.
-- Rotate it (and TRACK_DATABASE_URL) before this reaches a shared host.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'truckintel_track') THEN
        CREATE ROLE truckintel_track LOGIN PASSWORD 'truckintel_track_dev';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA core TO truckintel_track;

-- Exactly what ingest needs and nothing more:
GRANT INSERT, SELECT ON core.truck_positions TO truckintel_track;
GRANT USAGE, SELECT ON SEQUENCE core.truck_positions_ping_id_seq TO truckintel_track;
-- SELECT to authenticate the device; UPDATE only on the newest-fix columns, so
-- ingest cannot rewrite a device's token, its id, or its active flag.
GRANT SELECT ON core.truck_devices TO truckintel_track;
GRANT UPDATE (last_seen_at, last_geom, last_speed_kph, ping_count, reject_count)
    ON core.truck_devices TO truckintel_track;
-- Nearest-route lookup on ingest needs to read the network. Read only.
GRANT SELECT ON core.truck_routes TO truckintel_track;
