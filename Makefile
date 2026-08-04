# truck-intel — common tasks. Run from the repo root.

.PHONY: db-up schema schema-phase2 schema-routes schema-wave2 schema-viewer schema-tracking route-graph route-node route-components route-snap-index route-limits fuel-verify fuel-enrich fuel-routes aaa-prices pois-refresh mechanics verify-claims sync ingest tick api status test status-page freshness weekly-digest osm-ways osm-ways-resume viewer viewer-stop track-add track-list track-prune osm-truck-repair mechanics-refresh mechanics-fill ci ci-fast pipeline-smoke install-timers

db-up:
	./scripts/db_up.sh

schema:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema.sql

# Phase-2 additive schema (idempotent; requires `make schema` applied first)
schema-phase2:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_phase2.sql

# core.truck_routes and friends — the truck-designated route spine.
# This had NO target until 2026-07-27; it was applied by hand, so a fresh
# database built from the Makefile alone was missing core.truck_routes and
# schema_tracking.sql then failed on the dangling reference. CI on a clean
# PostGIS container is what surfaced it.
schema-routes:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_routes.sql

# Wave-2 additive schema (idempotent; requires schema + schema-phase2 first)
schema-wave2:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_wave2.sql

# Low-zoom generalization the map viewer needs (idempotent, ~11 s).
# Re-run after any core.truck_routes reload.
schema-viewer:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/viewer_generalized.sql

# Routable truck graph, end to end (~50 min: the build is the slow part).
# Order matters — noding changes the topology, so components and the snap index
# must both be rebuilt after it, in that order.
route-graph:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/route_graph.sql
	$(MAKE) route-node
	$(MAKE) route-components
	$(MAKE) route-snap-index

# Split edges at junctions the published geometry implies but does not node.
route-node:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/route_noding.sql

# Label connected components (route.node_component). Re-run after any topology change.
route-components:
	uv run python scripts/route_components.py

# Per-edge height / weight / hazmat limits, so a vehicle profile can constrain
# the search. Re-run after any topology change (~15 min).
route-limits:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/route_limits.sql

# Nearest-mainland-edge lookup used when snapping a pickup/drop onto the network.
route-snap-index:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/route_snap_index.sql

# Attach full detail to every fuel station (name, address, phone, website,
# hours, and the regional diesel price). Needs fuel-verify to have run first.
fuel-enrich:
	uv run python scripts/fuel_enrich.py

# Daily per-state diesel prices from AAA. OPT-IN: their terms grant a limited
# licence for PERSONAL, NON-COMMERCIAL use only, so this needs the flag set
# deliberately. EIA (weekly, regional, public domain) works without it.
aaa-prices:
	AAA_PRICES_ENABLED=1 uv run python scripts/aaa_prices.py

# Re-derive every published figure from the live database. Fails if any number
# in the README, the viewer, or a source comment has drifted from the data.
verify-claims:
	uv run python scripts/verify_claims.py

# Confirm fuel stations against a source independent of OpenStreetMap.
# ~40 min: scans the 11 GB Overture mirror, then matches 108k stations.
fuel-verify:
	uv run python scripts/fuel_verify.py

# Assign every fuel station its nearest truck-designated route (~2 min for 108k).
# The fuel MAP LAYER filters on the on_route_5km column this writes, so the
# layer is empty until this has run. Re-run after any fuel_stations reload —
# scripts/osm_extract.py does it automatically after a POI swap.
fuel-routes:
	uv run python scripts/fuel_routes.py

# Read-only: how many stations are on the truck network, and how far off.
fuel-routes-report:
	uv run python scripts/fuel_routes.py --report

# Tracking tables + the narrow ingest role (idempotent).
schema-tracking:
	./scripts/db_psql.sh -v ON_ERROR_STOP=1 < sql/schema_tracking.sql

# Weekly OSM POI refresh: fetch the Geofabrik extract only if its published md5
# changed, then re-run the POI pass and re-derive truck-route columns.
pois-refresh:
	uv run python scripts/osm_pois_refresh.py

# Is the upstream extract newer than ours? Writes nothing.
pois-check:
	uv run python scripts/osm_pois_refresh.py --check

# Monthly truck-mechanic refresh (Overture pull + route assign + verify + HTML).
mechanics:
	uv run python scripts/mechanic_list.py

# Tracking devices. The token prints ONCE — only its sha256 is stored.
#   make track-add DEVICE=truck-14 LABEL="Volvo VNL 760"
track-add:
	uv run python scripts/track_device.py add $(DEVICE) --label "$(LABEL)"

track-list:
	uv run python scripts/track_device.py list

# Retention is a window, not an archive (the daily timer runs this).
track-prune:
	uv run python scripts/track_device.py prune --days 30

# The map viewer: every dataset on one map. Needs schema-viewer applied once.
viewer:
	./scripts/viewer_up.sh

viewer-stop:
	./scripts/viewer_up.sh --stop

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

# weekly digest: 7-day ops rollup -> status_weekly.md (+ --deliver to send)
weekly-digest:
	uv run python scripts/weekly_digest.py --days 7

# OSM highways -> osm.ways. The US PBF pass runs 3-4 h: ALWAYS keep the
# workdir, so a load-time failure replays in minutes instead of re-scanning.
osm-ways:
	uv run python scripts/osm_ways_job.py --pbf $(PBF) --keep-workdir

# Replay phase B alone from a kept workdir (see the disk-headroom error text):
#   make osm-ways-resume PBF=data/pbf/us-latest.osm.pbf \
#        WORKDIR=data/pbf/.osmways-work-us-latest.osm-run1238
osm-ways-resume:
	uv run python scripts/osm_ways_job.py --pbf $(PBF) --from-spool $(WORKDIR)

# ---------------------------------------------------------------- daily layer
# OSM truck-repair via Overpass (763 US rows, ~2 min). NOT the PBF path: see
# deploy/truckintel-osm-truck-repair.service for the measurement that decided it.
osm-truck-repair:
	uv run python scripts/osm_overpass.py --job truck_repair

# Everything that fills mechanic DETAIL, minus the 3 h Overture pull. This is
# what the daily timer runs.
mechanics-refresh:
	uv run python scripts/mechanic_list.py --refresh

# Per-field coverage + what changed since the last snapshot. Read-only.
mechanics-fill:
	uv run python scripts/mechanic_list.py --fill-report

# ------------------------------------------------------------------------ CI
# The local gate. .github/workflows/ci.yml runs these same steps in the same
# order on every push, so keep the two in step — a change to one that is not
# mirrored in the other means CI and your laptop disagree about what "passing"
# means, and the disagreement will surface at the worst moment.
#
# Split into fast/slow deliberately. `ci-fast` needs no database and finishes in
# seconds, so it is the one worth running before every commit; `ci` adds the
# DB-backed suite (~11 min) and is the one to run before trusting a change.
# No `-m "not needs_db"`: needs_db is a skipif marker, not a registered one, so
# -m would match nothing. These files are DB-free by construction, and any
# DB-backed case inside them skips itself when PostGIS is unreachable.
ci-fast:
	uv run python -m compileall -q scripts truckintel api
	uv run pytest -q tests/test_mechanic_enrich.py tests/test_registry.py \
	  tests/test_validate.py tests/test_parsers.py tests/test_politeness.py

ci: ci-fast
	uv run pytest -q
	uv run python scripts/freshness_check.py || true
	@echo "ci: OK"

# Prove the scheduled pipeline actually works end to end, against the live DB,
# without waiting a day for the timers. See scripts/pipeline_smoke.py.
pipeline-smoke:
	uv run python scripts/pipeline_smoke.py

# The jobs that reach the network. These get the DNS gate; the rest are
# DB-only (quality, nightly-checks, track-prune, fuel-verify) or self-healing
# (api/worker carry Restart=on-failure) and would only pay the latency.
# ops-watch is here because truckintel.notify delivers over Telegram.
NET_UNITS := aaa-prices freshness osm-truck-repair mechanics-daily mechanics \
             pois weekly-digest businesses ops-watch

# Install/refresh the systemd user units from deploy/ and enable the timers.
install-timers:
	install -Dm644 deploy/truckintel-*.service deploy/truckintel-*.timer \
	  -t $(HOME)/.config/systemd/user/
	@# See deploy/dropins/10-wait-dns.conf: the After=network-online.target in
	@# every unit is a no-op in the user manager, so Persistent=true catch-up
	@# runs fire before DNS is up. This is the gate that actually holds.
	@for u in $(NET_UNITS); do \
	  install -Dm644 deploy/dropins/10-wait-dns.conf \
	    $(HOME)/.config/systemd/user/truckintel-$$u.service.d/10-wait-dns.conf; \
	done
	systemctl --user daemon-reload
	systemctl --user enable --now \
	  truckintel-tick.timer truckintel-freshness.timer truckintel-quality.timer \
	  truckintel-aaa-prices.timer truckintel-pois.timer \
	  truckintel-osm-truck-repair.timer truckintel-mechanics.timer \
	  truckintel-mechanics-daily.timer truckintel-businesses.timer \
	  truckintel-track-prune.timer truckintel-weekly-digest.timer \
	  truckintel-ops-watch.timer truckintel-nightly-checks.timer \
	  truckintel-fuel-verify.timer
	systemctl --user list-timers 'truckintel-*'
