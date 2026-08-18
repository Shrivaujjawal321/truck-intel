#!/usr/bin/env bash
# Start the PostGIS container for truck-intel. Idempotent:
#   - container exists          -> docker start (no-op if already running)
#   - container does not exist  -> docker run
# Data lives in the named volume truckintel_pgdata and survives container removal.
set -euo pipefail
cd "$(dirname "$0")/.."

# Password: POSTGRES_PASSWORD from env/.env wins, else parsed from DATABASE_URL.
# There is no fallback default on purpose — see the error below.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PASSWORD="${POSTGRES_PASSWORD:-}"
if [ -z "$PASSWORD" ] && [ -n "${DATABASE_URL:-}" ]; then
    PASSWORD="$(printf '%s' "$DATABASE_URL" | sed -nE 's|^[a-z+]+://[^:/@]+:([^@]+)@.*|\1|p')"
fi
if [ -z "$PASSWORD" ]; then
    echo "ERROR: no database password found." >&2
    echo "  Set DATABASE_URL (or POSTGRES_PASSWORD) in .env before starting the container." >&2
    echo "  There is deliberately no default: a default in a public repo is a published password." >&2
    exit 1
fi

# Published on 127.0.0.1 only. `-p 5432:5432` binds 0.0.0.0 and Docker's DNAT
# chain bypasses ufw, so a host firewall does NOT contain it — the bind address
# is the control. Changed 2026-08-18 after review found it reachable from the LAN.
if docker ps -a --format '{{.Names}}' | grep -qx truckintel-pg; then
    docker start truckintel-pg >/dev/null
    echo "container truckintel-pg: started (already existed)"
    # A container created before 2026-08-18 still has the 0.0.0.0 binding baked in;
    # port publishing cannot be changed without recreating it. Say so loudly.
    # Without a restart policy the container stays down after every reboot, and
    # every DB-backed timer then dies on wait_ready's 120 s budget. That is not
    # hypothetical: truckintel-quality missed 2026-08-17 and 2026-08-18 exactly
    # this way and went 110 h stale against a 36 h SLO before anyone noticed.
    # Unlike the port, this one is fixable in place.
    if [ "$(docker inspect truckintel-pg --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null)" = "no" ]; then
        echo "WARNING: truckintel-pg has no restart policy — it will stay down after a reboot." >&2
        echo "  Fix in place: docker update --restart unless-stopped truckintel-pg" >&2
    fi
    if docker port truckintel-pg 5432 2>/dev/null | grep -q '^0\.0\.0\.0'; then
        echo "WARNING: truckintel-pg is published on 0.0.0.0:5432 (reachable from the LAN)." >&2
        echo "  Recreate it: docker rm -f truckintel-pg && scripts/db_up.sh" >&2
        echo "  Data lives in the truckintel_pgdata volume and survives removal." >&2
    fi
else
    docker run -d --name truckintel-pg \
        -e POSTGRES_DB=truckintel \
        -e POSTGRES_USER=truckintel \
        -e POSTGRES_PASSWORD="$PASSWORD" \
        -p 127.0.0.1:5432:5432 \
        --restart unless-stopped \
        -v truckintel_pgdata:/var/lib/postgresql/data \
        postgis/postgis:16-3.4 >/dev/null
    echo "container truckintel-pg: created"
fi

# Wait until Postgres accepts connections (first boot also runs PostGIS init scripts).
for _ in $(seq 1 90); do
    if docker exec truckintel-pg pg_isready -U truckintel -d truckintel >/dev/null 2>&1; then
        echo "postgres ready on localhost:5432 (db=truckintel user=truckintel)"
        exit 0
    fi
    sleep 1
done
echo "ERROR: postgres did not become ready within 90s" >&2
exit 1
