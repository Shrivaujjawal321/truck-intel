# truck-intel

US-wide truck intelligence platform — the **MVP data spine**.

One registry, one worker, one PostGIS database, one FastAPI service.
Four datasets through four different load paths, honestly labeled:

| Dataset | Path | Target table |
|---|---|---|
| FHWA NBI bridges (annual bulk ZIP, ~624k rows) | `bulk_http` → `snapshot_swap` | `core.bridges` |
| NTAD Truck Stop Parking (ArcGIS FeatureServer, 1,915 points) | `arcgis` → `snapshot_swap` | `core.parking_sites` |
| NWS active weather alerts (live JSON, no key) | `live_json` → `event_lifecycle` | `core.live_events` |
| EIA weekly on-highway diesel prices (free key) | `api_keyed` → `upsert` | `core.fuel_prices` |

## Honesty rules (non-negotiable)

- `observed_at` = when the fact was true in the world, **never** the download date.
  NTAD parking amenities date to the ~2019 Jason's Law survey era and are shown as such.
- `NULL` renders as "unknown", never as "no".
- Every published row carries `(source_id, run_id, ingested_at, observed_at)`.
- Every fetch — success, skip, or failure — is one `ops.source_runs` row. Failures
  are reported, never faked as success.
- EIA needs a free API key. Without `EIA_API_KEY` in `.env` the connector records
  `status='skipped_no_key'` with a clear message and does not crash.
- All HTTP goes through one `polite_get()` choke point: per-host rate limit,
  descriptive User-Agent with contact email, honor `Retry-After`, back off on
  403/429 and never retry around them.

## Run

```bash
# 1. Start PostGIS (plain docker run, idempotent)
./scripts/db_up.sh

# 2. Apply schema
./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema.sql
# (same as: make schema)

# 3. Install dependencies (uv)
uv sync

# 4. Configure
cp .env.example .env        # EIA_API_KEY optional — see honesty rules above

# 5. Sync registry into ops.sources, then ingest
make sync
make ingest SOURCE=nbi_annual

# 6. Serve the API
make api                    # → http://127.0.0.1:8000/v1/health
```

## MVP scope — what this is and is not (by rule, plan §11)

**In:** the 4 datasets above, 5 endpoints (`/v1/bridges`, `/v1/parking`,
`/v1/live/weather-alerts`, `/v1/fuel/prices`, `/v1/meta/coverage`, plus
`/v1/health`), validation gates 1–2 (schema + coordinates with lat/lon-swap
detection), registry gates (`min_rows`, `max_row_delta_pct`), freshness SLOs,
`status.html`, `ops.source_runs` audit trail.

**Not in (later phases, by rule):** Valhalla/routing, OSM conflation/enrichment,
AI jobs, the confidence formula (the columns exist on every core table now; the
formula lands in Phase 2), state 511 adapters, businesses/POI conflation.

**Current state:** scaffold. `truckintel/db.py`, `config.py` and `/v1/health`
are implemented; everything else is an interface stub (`NotImplementedError`)
with full signatures and docstrings, so the shape of the system is reviewable
before the connectors land.

Canonical spec: `MASTER_PLAN.md` in the J.A.R.V.I.S. repo under
`data/projects/truck-intel/` — its §3.1 rulings and §11 MVP definition win over
the design docs.

## Layout

```
registry/            one YAML per source (git is the config audit trail)
sql/schema.sql       schemas ops/staging/core/osm/quality (PostGIS)
truckintel/          engine package: politeness, registry, jobs, loaders,
                     parsers/, validate, engine; db.py + config.py implemented
api/                 FastAPI app: /v1/health real, data routes stubbed
scripts/db_up.sh     start PostGIS container (idempotent)
scripts/db_psql.sh   psql into the container (pipes stdin, e.g. schema apply)
data/raw/            immutable raw downloads, sha256-named (gitignored)
```
