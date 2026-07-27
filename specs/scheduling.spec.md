# Spec — event-driven + resource-driven scheduling, and deploy safety

Status: **BUILT 2026-07-27**, except where marked. Raised and delivered the
same day.

| Requirement | Outcome |
|---|---|
| R3 event-driven | **Already existed.** The draft was wrong; the measurement corrected it — §3 |
| R4 resource-driven | **Built.** `truckintel/resources.py` + admission gate + kernel ceilings — §4 |
| R1 deploy safety | **Built.** SQL pinned at job start in `route_rebuild` — §5 |
| R2 deploy triggers nothing | **Already true.** Now stated and tested — §6 |

### What was actually built

- `truckintel/resources.py` — admission gate: free RAM, free disk, load per
  CPU, on-battery. Measured, env-overridable, with a kill switch.
- `truckintel/jobs.defer_job()` — releases a claimed job back to `queued` with
  `started_at` cleared. **Not** `finish_job('failed')`: a deferral must not burn
  the exponential backoff, must not count toward the circuit breaker's
  five-failure threshold, and must not alert.
- `engine._RESOURCE_GATED` — gate applied to every derived runner except the
  rescore (an in-database UPDATE that takes seconds).
- `MemoryMax` / `MemorySwapMax=0` / `CPUQuota` on the five heavy units.
- `ops_watch.check_stuck_deferrals()` — a job deferred continuously for 24 h is
  an outage wearing a deferral's clothes, and does alert.
- `route_rebuild._pin_sql()` — copies every SQL step to a private temp dir
  before the rebuild starts.
- `sql/schema_wave2.sql` — seeds `route_rebuild` in `ops.sources`.

### Two bugs found while building, both mine

1. **The route-rebuild hook could never have fired.** `ops.job_queue.source_id`
   has a foreign key to `ops.sources`, and `route_rebuild` was seeded only by
   its own script on first run. The post-swap hook is SAVEPOINT-guarded, so the
   FK violation would roll back to the savepoint and return False — the publish
   succeeds, the rebuild is never queued, and nothing reports it. The safety net
   protecting the publish would have hidden the failure it was meant to prevent.
2. **The kernel ceilings were ignored.** Appended after `[Install]`, so systemd
   parsed `MemoryMax` as an `[Install]` directive and dropped it. The unit file
   read `MemoryMax=6G` while `systemctl show` reported `infinity`. Both are
   pinned by tests now.

---

## 0. What was asked, restated

> The pipeline should run on **events** and on **resources**, both.
> If the pipeline is working and I push manually, my push must not interrupt it.
> If the pipeline is waiting for its next cycle and I push an update, the push
> should just land, and the cycle should still start at its own automated time.

---

## 1. One correction before anything is built

**`git push` cannot interrupt a running job.** It is a network operation to
GitHub; it does not touch the working tree, the database, systemd, or any
running process. Measured today: pushes were made while `tick` and `freshness`
were firing every minute and nothing was disturbed.

What *can* interrupt work is a **deploy** — `git pull` or an edit that rewrites
files on disk, plus `systemctl restart` / `daemon-reload`. That is a different
act with different risks, and naming it correctly changes what gets built:

| Act | Can it disturb a running job? | Why |
|---|---|---|
| `git push` | **No** | Nothing local changes |
| `git pull` / editing files | **Partly** | A running Python process already holds its modules in memory. But jobs that read files at RUNTIME re-read from disk mid-run |
| `systemctl restart truckintel-worker` | **Yes** | Kills the process, orphaning its claimed job as a stale `running` row |
| `make install-timers` (`daemon-reload`) | **No** to running jobs | Reloads unit definitions; running services continue |

So the requirement splits in two, and only one of them is about pushing:

- **R1** A deploy must never corrupt or interrupt in-flight work.
- **R2** A deploy must never *trigger* work, and must never *shift* a schedule.

R2 is already true today (nothing watches the working tree). It should be
stated and tested so it stays true, not built.

The genuinely new work is R1, plus the event/resource model in §3–§4.

---

## 2. What the system already has (do not rebuild)

| Capability | Where | State |
|---|---|---|
| Clock-driven triggers | 14 systemd timers | working |
| Event-driven triggers | 4 post-swap hooks calling `enqueue_rescore` (quality rescore; `route_rebuild` added today) | working, narrow |
| Conditional fetch | `osm_pois_refresh.py` compares Geofabrik's published md5 before downloading | working — this is already upstream-change-driven |
| Backoff on failure | `jobs.enqueue_due` — 5 min doubling per consecutive failure, capped 6 h | working |
| Circuit breaker | `ops.feed_health` open/half-open with cooldown | working |
| Concurrency safety | `ops.job_queue` with `FOR UPDATE SKIP LOCKED`; one advisory lock on `mechanic_list` | partial — see §5 |
| Crude resource courtesy | `Nice=10` / `IOSchedulingClass=idle` on 8 of 16 services | present, but advisory only |

**There is no resource GATING anywhere.** No `MemoryMax`, no `CPUQuota`, no
disk-headroom check, no "is the machine busy" test. `Nice` lowers priority; it
does not stop a job from starting on a laptop with 1 GB free. That is exactly
how the 12 GB OSM pass made the machine unusable on 2026-07-27 — it was
`IOSchedulingClass=idle` the whole time.

---

## 3. R3 — Event-driven: ALREADY BUILT. Do not rebuild it.

**This section's first draft was wrong and the measurement corrected it.**

The draft claimed most scheduled runs republish identical rows, citing
`wzdx_az`'s 27 successes in 12 hours. Checked against `raw_sha256` on the run
rows: those 8 most-recent runs carry **8 distinct payload hashes**, with row
counts moving 2211 → 3251. Every one was a real change. Live work-zone feeds
genuinely differ every 15 minutes; that is the data, not waste.

`truckintel/engine.py` already implements the full pattern:

1. conditional request with the previous success's `ETag` / `Last-Modified`
   (sidecar meta in the raw zone) → `304` → `status='skipped_unchanged'`
2. **and** a payload-hash fallback for servers that ignore conditional headers:
   `sha256(content) == prev_sha` → `skipped_unchanged`, before any parse

Measured over 24 h: `wzdx_wa` skipped 21 of 32 runs — **65 % of its pipeline
work avoided**. The sources showing 0 % skipped are the ones whose content
really does change every cycle.

`osm_pois_refresh.py` does the same thing at file level (Geofabrik md5), and
`route_rebuild --if-stale` at derived level.

**Remaining gap, and it is small:** the post-swap hook hard-codes its
dependants at the call site (`if target == "core.truck_routes"`). A source
should be able to declare its dependent jobs in its registry YAML instead. This
is a refactor for maintainability, not a missing capability — deferred until
something actually needs a third dependant.

| Event | Cheap detection | Should trigger |
|---|---|---|
| Upstream file changed | published md5 / ETag / `Last-Modified` differs | that source's ingest |
| Upstream feed content changed | row-hash of the fetched payload differs from last publish | publish; **skip** if identical |
| A source published | existing post-swap hook | its derived jobs |
| A derived input changed | e.g. `osm.truck_repair` swapped | `mechanic_list --osm-match` |
| Deploy landed | git HEAD moved | **nothing** (see R2) |

**The rule that matters: an unchanged upstream must cost a HEAD request, not a
pipeline run.** `osm_pois_refresh.py` already does this and it is the pattern to
generalise. Today most sources re-fetch and re-publish on the clock whether or
not anything changed — which is why `wzdx_az` recorded 27 successful runs in 12
hours, most of them republishing identical rows.

Clock triggers stay as the **floor**, not the mechanism: "if no event has fired
in N hours, run anyway", so a missed webhook or a silent upstream never means
indefinitely stale data.

---

## 4. R4 — Resource-driven: define the resource

"Run when resources allow" needs numbers or it is not testable. Proposed gate,
checked immediately before a HEAVY job starts (not for the 15-minute feeds):

| Resource | Refuse to start when | Why this number |
|---|---|---|
| Free RAM | < 3 GB available | The OSM node index grew past 1 GB and pushed 1.1 GB into swap on a 15 GB machine |
| Free disk | < 60 GB | The PBF pass alone needs 12 GB + a node cache that reached 25 GB |
| Load average (1 min) | > CPU count × 1.5 | Boss reported the laptop unusable at load 16 on 8 cores |
| Battery | on battery, not mains | A 3-hour pass should not run on battery |

Behaviour when the gate refuses: **defer, do not fail.** Record
`status='deferred'` with the measurement that caused it, leave the job queued,
and retry on the next tick. A deferral is not an error and must not alert; a job
deferred repeatedly for 24 h *is* a finding and must.

Jobs classified as HEAVY (measured today): `osm_pois` ~3 h, `osm_ways` ~4 h,
`route_rebuild` ~50 min, `businesses` Overture pull, `fuel_verify` ~40 min,
`mechanics` monthly pull ~3 h. Everything else is seconds-to-minutes and is not
gated — gating a 200 ms weather fetch adds risk for no benefit.

Additionally: give the heavy units real ceilings (`MemoryMax`, `CPUQuota`), so a
runaway job is throttled by the kernel rather than by Boss noticing his laptop
has stopped responding.

---

## 5. R1 — Deploy safety: what must be guaranteed

1. **A running job survives a code change.** Python holds its modules; the risk
   is jobs that read files at RUNTIME. `route_rebuild.py` shells out to
   `sql/*.sql` between steps — a `git pull` mid-rebuild can feed it a half-old,
   half-new schema. Fix: snapshot the SQL it will run at job start, or record
   the git SHA at claim time and refuse to continue across a change.

2. **A restart must not orphan a claimed job.** `Restart=on-failure` plus a
   killed worker leaves `status='running'` forever. A reaper exists (it
   produced the "reaped: stale running row" messages seen today), so this is
   mostly handled — it needs a test, and the reaper's window needs stating.

3. **A deploy during a heavy job should be allowed but visible.** Boss should
   not have to wait 3 hours to commit. The push lands; the job finishes on the
   code it started with; the NEXT run picks up the new code. That is the
   correct semantic and mostly true already — it should be made explicit and
   tested rather than assumed.

4. **Never auto-restart services on deploy.** No file watcher, no
   `systemctl restart` in a hook. Restarting is a human act, and doing it
   automatically is precisely how a push would start interrupting jobs.

---

## 6. R2 — What a deploy must NOT do

Stated so it can be tested and stays true:

- must not enqueue a job
- must not advance, delay or re-arm any timer
- must not kill, pause or signal a running job
- must not run migrations implicitly

**The cycle starts at its own scheduled time, regardless of when code landed.**

---

## 7. Acceptance tests

1. Push during a running heavy job → job completes; its `ops.source_runs` row
   shows success; no new job enqueued; next timer time unchanged.
2. Push while idle → `ops.job_queue` stays empty; `systemctl list-timers` shows
   the same NEXT as before the push.
3. Simulate low memory below the floor → heavy job records `deferred` with the
   measured value, stays queued, runs when the gate clears.
4. Unchanged upstream → HEAD/md5 request only, no publish, no new run row.
5. Kill a worker mid-job → the stale `running` row is reaped and re-queued, not
   left claimed.
6. A job deferred continuously for 24 h → `ops_watch` alerts.

---

## 8. Open decisions for Boss

1. **The RAM/disk/load numbers in §4** are my proposals from today's
   measurements. Confirm or set your own.
2. **Battery gate** — worth having, or does the laptop mostly stay plugged in?
3. **Deferral visibility** — should a deferred heavy job appear in the morning
   digest, or stay silent until the 24 h threshold?
4. **Scope** — §3 (event-driven) is the larger piece and touches every source.
   Do it all, or start with the heavy sources where a wasted run actually costs
   something?
