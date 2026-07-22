#!/usr/bin/env bash
# psql into the truckintel-pg container. Arguments pass through; stdin is piped,
# so SQL files on the host apply with:
#   ./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema.sql
# Interactive shell: just ./scripts/db_psql.sh
set -euo pipefail

TTY_FLAGS="-i"
if [ -t 0 ]; then TTY_FLAGS="-it"; fi

exec docker exec "$TTY_FLAGS" truckintel-pg psql -U truckintel -d truckintel "$@"
