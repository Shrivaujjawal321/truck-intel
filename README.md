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

## Every number here is checked against the database

```bash
make verify-claims       # 43 claims re-derived from live data
```

Documentation drifts from data silently. `scripts/verify_claims.py` re-derives
every figure printed in this README, in the map viewer, and in the source
comments — row counts, the 74% clearance gap, component counts, connector
lengths, generalization loss, fuel-verification bands, and the quoted route
distances against published road distances. It also asserts the invariants the
product rests on:

- every routable edge traces back to `core.truck_routes` (no generic road can enter)
- no synthetic connector exceeds 50 m
- weight limits come only from **posted** structures, never from a rating alone
- no fuel station is `verified` without a source independent of OpenStreetMap
- a constrained route is never shorter than an unconstrained one
- the density grid accounts for every row in the tile

It exits non-zero on the first drift, so `make verify-claims` is the answer to
"is this still true?"

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

## See the data

```bash
make schema-viewer   # one-time: build core.truck_routes_gen (~11 s)
make viewer          # → http://127.0.0.1:8000/viewer
make viewer-stop
```

Every mapped dataset on one map, one toggle each, click any feature for its row.
Geometry is served as vector tiles straight out of PostGIS (`ST_AsMVT`), so 455k
route segments and 630k bridges render without the API materialising JSON.

**No silent caps** — the honesty rule applied to cartography:

| Zoom | Routes | Point layers |
|---|---|---|
| below 8 | — | density grid; one marker per cell carrying `n`, `sum(n)` = every row in view |
| below 9 | dissolved corridors: 3,282 rows holding 99.4% of network mileage | — |
| 8 / 9 and up | every raw segment in view | every raw row in view |

Neither level drops rows; only the level of detail changes, and the sidebar and
HUD say which one you are looking at. Each tile also reports it on the wire via
`X-Source-Table`, `X-Generalized` and `X-Clustered`.

`osm.ways` is deliberately absent from the map: it is the generic road graph,
not the truck-designated network, and must never be drawn as a truck route.
It is listed under "not on the map" with its row count.

### Two service layers, on the truck network only

Fuel and truck mechanics are the map's service layers, and both are filtered to
within **5 km of a truck-designated route** — the same buffer the route-side
service list uses, so the national map and a planned route never disagree about
whether a shop is reachable.

| Layer | Drawn | Held | Filter |
|---|---|---|---|
| Fuel stations | 85,364 | 108,056 | on truck route, minus pumps tagged `fuel:diesel=no` / `hgv=no` |
| Truck mechanics | 9,809 | 11,759 | on truck route |

A filtered layer states **both** numbers on its legend row ("9,809 of 11,759"),
and `/v1/viewer/inventory` returns `rows` (drawn), `rows_total` (held) and the
`row_filter` that separates them. A map showing fewer dots than the table holds
has to be visible as a number, not discovered by eye.

Diesel is **not** used as an inclusion test. OSM leaves `fuel:diesel` untagged on
100,075 of 108,056 stations and explicitly false on 328, so requiring
`has_diesel = true` would delete 93% of the layer because of a metadata gap
rather than anything about the road. Untagged stations stay and render as
"unknown"; only the explicit negatives are dropped.

**General POI (`core.businesses`) is not a map layer.** It held 2,981 rows —
1,093 restaurants, 628 cafes, 362 ATMs — against exactly 1 truck-repair row, and
the viewer's "Mechanics" toggle was aliased to it as a stand-in, so the dots on
screen were 99.7% not mechanics. The table and its pipeline are untouched and it
is still listed under "not on the map"; it is simply not drawn.

## Track your own trucks, live

Own-GPS ingest — no telematics vendor, no app to install, nothing paid.

```bash
uv run python scripts/track_device.py add truck-14 --label "Volvo VNL 760"
# token is printed ONCE; only its sha256 is stored
```

Open `http://<host>:8000/track` on the driver's phone, enter the device id and
token, tap **Start sharing**. The phone becomes a live marker on `/viewer`:
click a truck in the sidebar for its last 2 h trail.

| Endpoint | What |
|---|---|
| `POST /v1/track/ping` | one GPS fix — token auth, rate limited, sanity checked; returns the nearest truck route and straight-line distance to it |
| `GET /v1/track/live` | newest fix per device, each labelled `live` or `stale` with `age_seconds` |
| `GET /v1/track/{device}/trail?minutes=120` | recent path as a LineString + points, capped and flagged when capped |

What it will not fake:

- **A stale fix is never a current position.** Past 180 s a device is reported
  `stale` and drawn hollow — a phone that lost signal must not render as a truck
  parked there.
- **`route_dist_m` is straight-line**, not drive distance, and says so everywhere.
- **Rejections are counted, not hidden.** Bad token, wrong clock, impossible
  speed, swapped lat/lon and rate-limit refusals each return a stable error code
  and increment `reject_count` on the device.
- **Retention is a window.** Pings older than 30 days are deleted daily
  (`truckintel-track-prune.timer`).

Ingest is the only write path in the API, and it uses its own Postgres login that
can INSERT pings and update three columns of `core.truck_devices` — nothing else.
`DELETE FROM core.bridges` on that role is refused by Postgres, not by our code.
`/v1/health` reports `tracking_write_role` so the posture is observable.

## Plan a truck route

```bash
make route-graph     # one-time, ~50 min: graph -> noding -> components -> snap index
make viewer          # then click a pickup and a drop on the map
```

Or over HTTP:

```bash
curl "http://127.0.0.1:8000/v1/route?from=-96.797,32.777&to=-97.517,35.467"
```

Dallas to Oklahoma City comes back as **204 mi on I-35**, with **6 restrictions,
71 truck mechanics, 483 fuel stations, 5 truck parking, 20 rest areas and 15
weigh stations** along it — each itemised with how far into the trip it sits and
how far off the road it is.

### Why the answer is a truck answer

The search runs over `route.edges`, built only from `core.truck_routes`. A road
that is not truck-designated is not in the graph, so it cannot appear in a
result — the guarantee is structural, not a filter applied afterwards.

Getting there took three fixes the published data required:

| Problem | Evidence | Fix |
|---|---|---|
| Segments meet visually but share no node, so the router cannot turn | 1,053 of 1,053 sampled dead ends sat within ~11 m of an edge they shared no node with; Dallas→OKC returned 312 mi via US-81 | `sql/route_noding.sql` splits 3,287 edges at 3,394 interior incidences. Components 583 → **180**, routable network 88.7% → **97.1%**, Dallas→OKC → **207 mi on I-35** |
| Chains stop a few metres short of the next road | 4,906 dead ends; 2,339 within 50 m of another node | 2,107 `synthetic_connector` edges, dead-ends only so an overpass is never fused. Every route reports how many it used |
| The nearest truck route is often a disconnected stub | downtown OKC snaps to component 107 while Oklahoma has 8,614 mainland nodes | snap returns the best access point *per component* and picks the cheapest connected pair |

### What it refuses to do

- **No path invented.** 2.9% of the network is still in islands. If pickup and
  drop are in different components the answer is `422 no_truck_path`, not a
  detour onto a road trucks may not use.
- **Access distance stays separate.** City centres are not on truck routes;
  the walk-on distance is reported as `access_m`, never folded into the trip.
- **Restriction counts come with their own ignorance.** 468,598 of 629,710 NBI
  bridges have no recorded vertical clearance. Every route reports how many
  bridges it passes and how many of those could not be judged, so "6
  restrictions" reads as *known to restrict*, not *proven clear*.
- **Services are straight-line.** The 5 km service radius is as-the-crow-flies
  and labelled as such — it is not drive distance.

### Routing to a vehicle profile

Give the truck's dimensions and the restrictions stop being a report and start
being a **constraint on the search** — segments the vehicle may not use are never
considered, so the returned route already complies.

```bash
curl "http://127.0.0.1:8000/v1/route?from=-96.797,32.777&to=-97.517,35.467\
&height_in=180&weight_lb=105000&hazmat=true"
```

Dallas → Oklahoma City, same two points:

| Vehicle | Route | Segments excluded |
|---|---|---|
| no profile | 207 mi (I-35) | – |
| 13'6" · 80,000 lb | 207 mi (I-35) | 4 |
| 14'0" · 80,000 lb | 207 mi (I-35) | 5 |
| **15'0" · 105,000 lb** | **309 mi (I-40 / US-69 / US-75)** | **58** |

Chicago → Indianapolis at 15'0" · 105,000 lb returns `422 no_compliant_path`: a
truck route exists, but none this vehicle may legally use. That refusal is the
correct answer, not a failure.

**What each input actually does**

| Input | Effect | Source |
|---|---|---|
| `height_in` | excludes segments whose recorded clearance is lower | NBI items 10/53 for structures carrying the road, item 54B for structures crossing above, plus tunnel clearance |
| `weight_lb` | excludes segments whose carrying structure is **posted** below it; closed structures always excluded | NBI item 41 posting + item 64 operating rating. A rating alone never blocks — an unposted bridge is legally open to legal loads |
| `hazmat` | excludes hazmat-restricted tunnels | NTI hazmat flags (135 nationally) |
| `length_ft`, `width_in` | checked against statute, **not** per-edge | 23 CFR 658: no state may impose a width limit other than 102 in, or a semitrailer length limit below 48 ft, on the National Network — which is this graph. There is no per-edge dataset because the regulation makes it uniform; `osm.ways` carries `maxlength_in`/`maxwidth_in` columns with **0** rows populated |

Beyond the statutory limits (over 102 in wide, over 80,000 lb gross) the answer
is a permit, not a route. The response says so rather than pretending.

**The honest limit of "compliant".** Only *recorded* limits exclude a segment.
468,598 of 629,710 NBI bridges record no clearance at all, so a compliant route
means no **known** restriction blocks it — not that every structure was measured.
Every response reports how many unmeasured structures the route passed.

```bash
make route-limits    # rebuild the per-edge limits (~15 min)
```

### Fuel stations, in full

```bash
make fuel-verify     # who else says this station exists (~40 min)
make fuel-enrich     # name, address, phone, website, regional price
```

| Field | Filled | Where it comes from |
|---|---|---|
| name | 101,081 (93.5%) | OSM, falling back to Overture |
| address | 81,506 (75.4%) | Overture |
| phone | 75,718 (70.1%) | OSM, falling back to Overture — OSM alone had 8.4% |
| website | 80,524 (74.5%) | OSM, falling back to Overture — OSM alone had 17.4% |
| email | 21,758 (20.1%) | Overture |
| opening hours | 9,176 (8.5%) | **OSM only** — Overture Places has no hours field |
| regional diesel price | 108,055 (100%) | EIA weekly, by PADD region |

**Two price sources, both honest about what they are.**

- **AAA — daily, per state.** Preferred where available. Refreshed overnight by
  OPIS; the UI says "today", "yesterday" or "N days ago" from the fetch date, and
  flags a stale number rather than dressing it up as current.
- **EIA — weekly, per region.** The fallback and the source of record. US public
  domain, one of 11 [PADD districts](https://www.eia.gov/petroleum/weekly/includes/padds.php).

Neither is a pump price. No free, legal source publishes per-pump prices in the
US — that is an OPIS commercial licence. Both stored notes say "not the price at
any individual pump", and the disclaimer rides every row so a query cannot strip
it.

**AAA is opt-in.** Their terms grant a limited licence for **personal,
non-commercial use only** (gasprices.aaa.com/about-aaa). `scripts/aaa_prices.py`
refuses to run without `AAA_PRICES_ENABLED=1`; retention is a 30-day window, not
an archive; every row carries the AAA/OPIS attribution their licence requires;
and fetches honour the site's `Crawl-delay: 10` through the project's
`polite_get` choke point. Enable with:

```bash
make aaa-prices                                  # one fetch, now
systemctl --user enable --now truckintel-aaa-prices.timer   # daily at 06:15
```

Each run writes one `ops.source_runs` row under `aaa_daily` (48 h SLO), so a
dead price job trips the same freshness alarm as a dead federal feed. Remove the
timer before serving the platform to anyone else — EIA keeps working alone.

Each station's state is resolved from OSM (31,875), else Overture (56,816), else
its nearest truck route (19,364) — the last only because a PADD district spans
many states, so a border misread almost never changes the number, while leaving
18% of stations priceless would.

**Licences stay separate.** Overture Places is CDLA-Permissive; `osm.*` is ODbL
and share-alike. The detail is never copied into `osm.fuel_stations` — it lives
in `core.fuel_places` and is joined through `ov_place_id` at query time. A test
asserts no Overture column has leaked into the ODbL table.

### Endpoint

`GET /v1/route?from=lon,lat&to=lon,lat`

| Parameter | Default | Meaning |
|---|---|---|
| `restriction_buffer_m` | 150 | how close a bridge/tunnel must be to count as *on* the route |
| `service_buffer_m` | 5000 | straight-line reach for mechanics, fuel, parking |
| `clearance_in` | 162 (13'6") | vehicle height; anything lower is a restriction |
| `include` | `all` | `route` skips corridor analysis, `counts` omits the item lists |

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
