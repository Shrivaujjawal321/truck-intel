#!/usr/bin/env bash
# Start the PostGIS container for truck-intel. Idempotent:
#   - container exists          -> docker start (no-op if already running)
#   - container does not exist  -> docker run
# Data lives in the named volume truckintel_pgdata and survives container removal.
set -euo pipefail
cd "$(dirname "$0")/.."

# Password: POSTGRES_PASSWORD from env/.env wins, else parsed from DATABASE_URL,
# else the dev default. Matches the DATABASE_URL in .env.example.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PASSWORD="${POSTGRES_PASSWORD:-}"
if [ -z "$PASSWORD" ] && [ -n "${DATABASE_URL:-}" ]; then
    PASSWORD="$(printf '%s' "$DATABASE_URL" | sed -nE 's|^[a-z+]+://[^:/@]+:([^@]+)@.*|\1|p')"
fi
PASSWORD="${PASSWORD:-truckintel_dev}"

if docker ps -a --format '{{.Names}}' | grep -qx truckintel-pg; then
    docker start truckintel-pg >/dev/null
    echo "container truckintel-pg: started (already existed)"
else
    docker run -d --name truckintel-pg \
        -e POSTGRES_DB=truckintel \
        -e POSTGRES_USER=truckintel \
        -e POSTGRES_PASSWORD="$PASSWORD" \
        -p 5432:5432 \
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
