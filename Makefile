# truck-intel — common tasks. Run from the repo root.

.PHONY: db-up schema schema-phase2 sync ingest tick api status test status-page freshness

db-up:
	./scripts/db_up.sh

schema:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema.sql

# Phase-2 additive schema (idempotent; requires `make schema` applied first)
schema-phase2:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_phase2.sql

sync:
	uv run python -m truckintel.registry

# usage: make ingest SOURCE=nbi_annual
ingest:
	uv run python -m truckintel.engine ingest $(SOURCE)

tick:
	uv run python -m truckintel.engine tick

api:
	uv run uvicorn api.main:app --host 127.0.0.1 --port 8000

status:
	./scripts/db_psql.sh -c "SELECT run_id, source_id, status, started_at, rows_in, rows_published, rows_rejected, left(coalesce(message,''),60) AS message FROM ops.source_runs ORDER BY started_at DESC LIMIT 20;"

test:
	uv run pytest

# regenerate status.html from ops.sources + ops.source_runs
status-page:
	uv run python scripts/status_gen.py

# freshness SLO check: exit 1 on violations (same pair the 10-min timer runs)
freshness:
	uv run python scripts/freshness_check.py
