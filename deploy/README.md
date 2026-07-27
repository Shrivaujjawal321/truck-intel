# deploy/ — systemd USER units for truck-intel

Per MASTER_PLAN §3.1-9: Python workers and timers run as systemd **user** units
on the host (PostGIS runs in Docker via `scripts/db_up.sh`).

| Unit | What | Cadence |
|---|---|---|
| `truckintel-tick.service` + `.timer` | registry sync + enqueue due jobs | every 1 min |
| `truckintel-worker.service` | queue worker: fetch → validate → publish | long-running |
| `truckintel-freshness.service` + `.timer` | freshness SLO check, then regenerate `status.html` | every 10 min |
| `truckintel-quality.service` + `.timer` | nightly quality ladder (gates 4-5 + confidence rescore) | daily 03:30 |
| `truckintel-businesses.service` + `.timer` | Overture + FSQ-mirror pull → conflate rebuild of `core.businesses` | monthly, 1st @ 04:30 |
| `truckintel-weekly-digest.service` + `.timer` | 7-day ops rollup → `status_weekly.md` (+ Telegram/ntfy) | Mondays 08:00 |
| `truckintel-aaa-prices.service` + `.timer` | AAA daily diesel price per state | daily 06:15 |
| `truckintel-pois.service` + `.timer` | md5-conditional Geofabrik fetch → OSM POI pass (fuel / rest / weigh) → re-derive truck-route columns | Sundays 02:00 |
| `truckintel-osm-truck-repair.service` + `.timer` | OSM truck/trailer-repair shops via **Overpass** → `osm.truck_repair` + route assign | daily 04:45 |
| `truckintel-mechanics-daily.service` + `.timer` | mechanic **detail**: licence registries + chain hours + OSM corroboration + re-verify + coverage + fill report + HTML | daily 05:00 |
| `truckintel-mechanics.service` + `.timer` | mechanic **discovery**: Overture Places truck categories → `core.mechanic_shops` (then every daily stage) | monthly, 2nd @ 05:00 |
| `truckintel-track-prune.service` + `.timer` | delete tracking pings past the retention window | daily 03:10 |
| `truckintel-ops-watch.service` + `.timer` | repeated failures, never-succeeded sources, stuck runs, disarmed alerting, queue backlog → Telegram/ntfy | hourly |
| `truckintel-nightly-checks.service` + `.timer` | route-graph staleness + pipeline smoke + published-claim drift | daily 03:50 |
| `truckintel-fuel-verify.service` + `.timer` | fuel verification against a non-OSM source, then enrichment | Sundays 06:00 |
| `truckintel-api.service` | uvicorn `api.main:app` on 127.0.0.1:8000 | long-running |

### Fuel freshness: what "daily" actually covers

Two different things live under "fuel data", and conflating them would be
freshness theatre:

| | Cadence | Unit |
|---|---|---|
| **Price** — changes daily | daily | `truckintel-aaa-prices.timer` (state-level, AAA) + `eia_diesel` polled daily by the engine (EIA publishes weekly, Mondays) |
| **Stations** — geometry/attributes | weekly | `truckintel-pois.timer` |

A petrol pump does not move overnight. Re-downloading a 12 GB PBF every 24 h to
change a handful of rows would be cost without benefit, so station geometry is
weekly and the UI reports its real `observed_at` rather than implying same-day.
An unchanged week costs one small HTTP GET: the job compares Geofabrik's
published `.md5` against a sidecar before downloading anything.

### Mechanics: discovery is monthly, detail is daily

Same split, same reasoning as fuel — and it was wrong until 2026-07-27, when
everything mechanic-related sat on the monthly timer.

| | Cadence | Why |
|---|---|---|
| **Which shops exist** (Overture Places) | monthly, 2nd @ 05:00 | Overture publishes monthly. Scanning the national parquet daily would burn ~3 h to rewrite identical rows *and* reset `observed_at`, making stale data look fresh. |
| **What we know about them** (NY/NJ licence registries, All The Places chain feeds, OpenStreetMap) | daily 05:00 | All three change continuously. On the monthly schedule a shop could gain a licence record, opening hours, or an independent corroboration and we would not notice for up to 31 days. |

`mechanic_list.py --refresh` is the daily door: every stage except the Overture
pull. Measured 12 s CPU / 145 MB / no swap, against ~3 h for the monthly job.

**Both write `core.mechanic_shops`, and on the 2nd of the month both are due.**
The script takes a Postgres **advisory lock** for its whole run and exits 0 with
a `[skip]` line if another run holds it, so a daily refresh can never interleave
its UPDATEs with the monthly job's TRUNCATE+reload. Timing them apart is not a
guarantee — a slow pull outlives any gap — so the lock is the guarantee and the
clock is only a courtesy.

Every refresh appends to `core.mechanic_fill_history` and prints the delta:

```
[fill]   opening_hours         409 / 11,759    3.5%  +34
[fill] newly filled this run: 34
```

That line is the point of running it daily. Without it, a refresh that silently
stopped finding new detail would look identical to one that is working — and
`+0  (nothing new upstream — not a failure)` is a real answer, not a bug.

### Why truck repair does NOT use the PBF path

`truckintel-pois` walks the 12 GB US extract, which is right for `amenity=fuel`
(108k rows) — no public API should be asked for that.

Truck repair is **763 rows nationally**. Measured 2026-07-27:

| | PBF pass | Overpass |
|---|---|---|
| Wall clock | 2 h 51 m (prior run) | **~2 min** |
| Local I/O | 12 GB + >1 GB node index | **357 KB** |
| Rows | 763 | **763** |
| Vintage | weekly snapshot | **minutes old** |

The PBF attempt was killed after 2 h 21 m for making the laptop unusable (node
index past 1 GB, 1.1 GB into swap). Both transports remain in the repo; choose
by result size, not by habit.

## Assumptions baked into the unit files

- Repo lives at `~/Documents/truck-intel` (`WorkingDirectory=%h/Documents/truck-intel`).
- `uv` is at `~/.local/bin/uv`. Edit both paths in every unit if your layout differs.
- The PostGIS container is already running (`make db-up`); units only order
  `After=docker.service`, they do not start the container.
- `.env` in the repo root is read by the app itself (no `EnvironmentFile=` needed).

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/truckintel-*.service deploy/truckintel-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload

# timers (they pull in their .service on each firing)
systemctl --user enable --now truckintel-tick.timer truckintel-freshness.timer \
    truckintel-quality.timer truckintel-businesses.timer truckintel-weekly-digest.timer \
    truckintel-aaa-prices.timer truckintel-pois.timer truckintel-track-prune.timer \
    truckintel-mechanics.timer
# long-running services
systemctl --user enable --now truckintel-worker.service truckintel-api.service
```

Enabling the units is the step that is easy to skip and expensive to miss: on
2026-07-26 only `truckintel-aaa-prices` had ever been installed, so the whole
ingest engine had never run on a schedule and EIA prices sat six days stale while
every script still reported success on demand. `systemctl --user list-timers
'truckintel-*'` should list **9** timers — if it lists fewer, the data is only as
fresh as the last time someone ran a script by hand.

To keep everything running after logout / across reboots without a login session:

```bash
loginctl enable-linger "$USER"
```

### Two watchers, because they answer different questions

`truckintel-freshness` asks **"is the DATA old?"** — the SLO question.

`truckintel-ops-watch` asks **"is the JOB working?"** — and a source can fail
every single run while its last good publish is still inside the SLO. Measured
2026-07-27: `osm_pois` had failed 5 times in 7 days and `osm_ways` 10 times,
with freshness reporting PASS throughout. Neither was noticed until a smoke
test was run by hand.

Both now deliver through `truckintel/notify.py` (Telegram + ntfy). Before this,
`freshness_check --telegram` was a documented no-op whose implementation was
the comment *"sending is post-MVP — printing only (TODO: alert hook)"*. A
monitor that prints into a journal nobody reads is not a monitor.

ops-watch keeps a **12-hour per-finding cooldown** in
`data/ops_watch_state.json`. An hourly watchdog on a persistently broken source
otherwise becomes a notification you learn to swipe away, which is worse than
silence. Findings still true but suppressed are counted in the message, never
dropped.

### Derived rebuilds now follow their source

`core.truck_routes` refreshes weekly through the engine. Five things are
derived from it — `route.edges`, `route.node_component`, `route_snap_index`,
`route.edge_limits`, `viewer_generalized` — and **none were rebuilt when it
changed**. A new NTAD vintage would land and the router would keep answering
from a graph built on the previous network: no error, no failed run, no stale
alert, because every *source* was fresh. Only the derivatives were wrong.

A successful swap of `core.truck_routes` now enqueues `route_rebuild` in the
same transaction as the publish (same hook `quality_rescore` has always used).
It runs `--if-stale`, so a swap that republished an unchanged network does not
spend 50 minutes rebuilding an identical graph. The nightly checks re-assert
staleness independently — a hook can only fire if it was reached.

## Prove it works — without waiting a day

`make test` proves the code is correct. It does not prove the *pipeline* is,
and those fail differently: a timer with a typo'd unit name is valid systemd
and never fires; a service whose `ExecStart` uses a renamed flag fails once a
day at 05:00, into a journal nobody reads.

```bash
make pipeline-smoke          # checks only, writes nothing
uv run python scripts/pipeline_smoke.py --run-cheap   # also EXECUTES the daily jobs
```

37 checks across four groups, each printing the measurement behind its verdict:

1. **Unit files** — every expected `.service`/`.timer` exists and its
   `ExecStart` resolves to a real script.
2. **Flags** — every `--flag` a unit passes is one the script's `--help`
   actually accepts. This is what catches a rename before 05:00 does.
3. **Installation** — is the timer installed *and enabled on this machine*? A
   correct file in `deploy/` proves nothing until `make install-timers`.
4. **Data freshness** — read from `ops.source_runs`, not the data tables,
   because a table can hold rows forever while the job filling it has been
   broken for weeks. Plus an arithmetic check that no source's schedule is
   slower than its own SLO.

`FAIL` is a contradiction (missing script, unmeetable SLO) and exits 1. `WARN`
is a fact worth seeing that may be fine (a timer not yet enabled, a source added
today). Current state: **36 pass, 1 warn, 0 fail** — the warn being `osm_pois`,
which has 5 failed runs in 7 days because of the `lane6.v_enriched` blocker
below.

### Known blocker: `osm_pois` cannot publish

An untracked view, `lane6.v_enriched`, depends on `osm.fuel_stations`. The
snapshot swap renames the live table to `…_old` and drops it, and that drop
fails with `DependentObjectsStillExist`. The view exists in the database but
**nowhere in this repository** — a leftover from a hand-run experiment.

It has been left in place rather than dropped: it is not reproducible from
source, so dropping it destroys it, and that is Boss's call. Two ways out:
drop the orphan view (fast, irreversible), or teach `snapshot_swap` to capture,
drop and recreate dependent view definitions around the swap (correct, protects
every future swap, touches loader machinery every source uses).

## Observe

```bash
systemctl --user list-timers 'truckintel-*'
systemctl --user status truckintel-worker
journalctl --user -u truckintel-worker -f       # live worker log
journalctl --user -u truckintel-freshness -e    # last freshness run (violations included)
```

`status.html` is written to the repo root every 10 minutes; open it in a browser.
Note: `truckintel-freshness.service` runs `freshness_check.py` with a leading
`-` on its `ExecStart` line — an SLO violation (exit 1) is logged but does not
stop `status_gen.py`, so the page stays current even while a source is late.

## Uninstall

```bash
systemctl --user disable --now truckintel-tick.timer truckintel-freshness.timer \
    truckintel-worker.service truckintel-api.service
rm ~/.config/systemd/user/truckintel-*.{service,timer}
systemctl --user daemon-reload
```
