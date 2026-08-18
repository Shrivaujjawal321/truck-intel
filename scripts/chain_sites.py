#!/usr/bin/env python
"""National truck-chain store locators -> core.chain_sites.

WHY THIS SCRIPT EXISTS
----------------------
scripts/mechanic_list.py --chains has been fetching five truck-relevant All
The Places spiders every morning since 2026-07-27, loading them into

    CREATE TEMP TABLE atp (...) ON COMMIT DROP

stamping opening_hours onto core.mechanic_shops, and dropping the rest on
COMMIT. Measured from the 2026-08-17 05:00 run, the payload discarded was:

    loves_us                      731 features
    pilot_flying_j                722
    travelcenters_of_america_us   362
    fleetpride_us                 494
    penske                       1763

1,815 truck stops from Love's + Pilot + TA alone, refreshed with every ATP
run, against core.parking_sites' 1,915 rows whose FeatureServer still says
"compiled on April 09, 2019". The freshest truck-stop evidence in the system
was being created and destroyed inside one transaction, daily, for weeks.

WHY IT IS THE STRONGEST FREE LIVENESS SIGNAL WE HAVE
A chain's store locator is not a third-party observation. It is the operator
publishing which of its own branches are open, on a page whose entire purpose
is to send customers there. There is no aggregator lag and no survey cycle: if
Love's stops listing a location, Love's closed it. Gate 6 gives that a full
corroboration point (truckintel/liveness.py, component A) and it is the only
signal strong enough to move a 2019 survey row to live_state='open'.

LIMIT, stated up front: chains only. Independent truck stops and one-bay shops
— roughly half the market — get nothing from this table and fall back to
presence decay plus the state licence registries. This does not pretend to be
a national truck-stop census.

HONESTY
- observed_at is the ATP RUN's own end_time, never now(). The run read on
  2026-08-17 was run_id 2026-08-08-13-32-19, finished 2026-08-11T21:11:13Z —
  six days old. Stamping the fetch date would have inflated every liveness
  score in the system by six days of decay.
- A spider that fails to fetch does NOT delete its previous rows. Only spiders
  that returned data this run have their rows replaced; a Love's outage must
  not read as "Love's closed every location", which is exactly the false
  positive Gate 6 exists to avoid.
- Features without a usable `ref` are skipped rather than given a synthetic
  key: an unstable id would churn chain_site_id every run and destroy the
  presence history it feeds.

LICENCE: All The Places is CC0 (public domain dedication) — safe to store and
to serve, unlike the AAA feed. Attribution is courtesy, not obligation.

Audit: exactly one ops.source_runs row per invocation. Exit 0 = success,
1 = failure (recorded on the run row).

Usage:
  uv run python scripts/chain_sites.py
  uv run python scripts/chain_sites.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.config import load_dotenv  # noqa: E402
from truckintel.db import get_conn  # noqa: E402

SOURCE_ID = "chain_sites"
TARGET = "core.chain_sites"
RUN_URL = "https://data.alltheplaces.xyz/runs/latest.json"
OUTPUT_URL = "https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson"
UA = "truck-intel/1.0 (public-data enrichment; CC0 source)"
TIMEOUT_S = 180

# The truck-relevant spiders. Same five mechanic_list.py already pulls, kept
# deliberately in step — divergence between the two lists would mean a shop
# could carry a chain_brand badge that this table cannot corroborate.
#
#   loves_us / pilot_flying_j / travelcenters_of_america_us  truck stops
#   fleetpride_us                                            parts + service
#   penske                                                   rental + service
SPIDERS = ("loves_us", "travelcenters_of_america_us", "pilot_flying_j",
           "fleetpride_us", "penske")

# A spider returning far fewer rows than it should is usually a broken
# locator API rather than mass closure, so it is refused rather than
# published — the same min_rows reasoning the registry gates use. Floors are
# set at roughly half of the 2026-08-17 observed counts.
MIN_ROWS = {
    "loves_us": 350,
    "travelcenters_of_america_us": 180,
    "pilot_flying_j": 350,
    "fleetpride_us": 240,
    "penske": 850,
}

_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status, authority_class, base_trust, trust)
VALUES
    (%(sid)s,
     'National truck-chain store locators via All The Places (CC0)',
     'All The Places / the chains themselves', 'derived', 'snapshot_swap',
     NULL, 72, TRUE, 'verified', 'curated', 0.85, 0.85)
ON CONFLICT (source_id) DO UPDATE SET
    -- Self-healing, like route_rebuild.py's seed: DO NOTHING let the live row
    -- drift and stay drifted. On 2026-08-18 this row was found kind='bulk_http',
    -- schedule_minutes=1440, enabled=false — a combination with no correct
    -- behaviour available. enabled=false silenced ops_watch's disarmed check
    -- but freshness_check skips non-derived sources anyway, so the job had NO
    -- staleness alerting at all; and enabling it while schedule_minutes was set
    -- would have handed it to the queue worker, which would run it a second
    -- time alongside truckintel-liveness.service.
    --
    -- kind='derived' is what it actually is: a script on a timer, not a
    -- registry feed the engine fetches. schedule_minutes NULL keeps
    -- truckintel/jobs.py:55 from ever enqueuing it. enabled=TRUE lets
    -- freshness_check see it against its 72 h SLO.
    kind = 'derived', schedule_minutes = NULL, enabled = TRUE
"""


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def run_vintage() -> datetime:
    """The ATP run's own end_time — the moment the scrape finished.

    Fails loudly if absent rather than falling back to now(): an unknown
    vintage must not be laundered into a fresh one. Gate 6 multiplies this
    date through every score it produces.
    """
    meta = _get_json(RUN_URL)
    end = meta.get("end_time")
    if not end:
        raise RuntimeError(
            f"{RUN_URL} carries no end_time — refusing to invent a vintage")
    return datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(
        timezone.utc)


def to_row(feat: dict, spider: str, observed_at: datetime) -> dict | None:
    """One ATP GeoJSON feature -> one core.chain_sites row, or None to skip.

    ATP publishes OSM-style keys ('addr:city', not 'city'). `ref` is the
    chain's OWN store number, which is why it anchors chain_site_id: it is
    stable across runs in a way a coordinate or a name is not.
    """
    props = feat.get("properties") or {}
    geom = feat.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates") or []
    if len(coords) != 2:
        return None
    lon, lat = coords[0], coords[1]
    if lon is None or lat is None:
        return None
    ref = props.get("ref")
    if ref in (None, ""):
        return None  # no stable key -> would churn the presence history
    state = (props.get("addr:state") or "").strip().upper() or None
    return {
        "chain_site_id": f"{spider}:{ref}",
        "spider": spider,
        "brand": props.get("brand") or props.get("name"),
        "name": props.get("name"),
        "street": props.get("addr:street_address"),
        "city": props.get("addr:city"),
        "state": state if state and len(state) == 2 else None,
        "phone": props.get("phone"),
        "website": props.get("website"),
        "opening_hours": props.get("opening_hours"),
        # Tri-state: NULL means the chain does not publish hours, which is the
        # case for Love's, TA and Penske. False would be a fabrication.
        "open_24h": True if (props.get("opening_hours") or "").strip() in
                    ("24/7", "Mo-Su 00:00-24:00") else None,
        "lat": float(lat),
        "lon": float(lon),
        "observed_at": observed_at,
    }


def fetch_spider(spider: str, observed_at: datetime) -> list[dict]:
    feats = _get_json(OUTPUT_URL.format(spider=spider)).get("features", [])
    rows = [r for r in (to_row(f, spider, observed_at) for f in feats) if r]
    floor = MIN_ROWS.get(spider, 0)
    if len(rows) < floor:
        raise RuntimeError(
            f"{spider}: {len(rows)} usable rows is below the {floor} floor — "
            f"refusing to publish (looks like a broken locator, not closures)")
    return rows


def publish(conn, rows: list[dict], spiders_ok: list[str]) -> int:
    """Replace rows for the spiders that succeeded, leave the rest untouched.

    Scoped to spiders_ok on purpose: a blanket DELETE would let one spider's
    outage erase a chain that is perfectly healthy, and Gate 6 would then read
    the absence as loss of corroboration across every one of its sites.
    """
    conn.execute("DELETE FROM core.chain_sites WHERE spider = ANY(%s)",
                 (spiders_ok,))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO core.chain_sites
                (chain_site_id, spider, brand, name, street, city, state,
                 phone, website, opening_hours, open_24h, lat, lon, geom,
                 observed_at, run_id)
            VALUES (%(chain_site_id)s, %(spider)s, %(brand)s, %(name)s,
                    %(street)s, %(city)s, %(state)s, %(phone)s, %(website)s,
                    %(opening_hours)s, %(open_24h)s, %(lat)s, %(lon)s,
                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                    %(observed_at)s, %(run_id)s)
            ON CONFLICT (chain_site_id) DO UPDATE SET
                brand = EXCLUDED.brand, name = EXCLUDED.name,
                street = EXCLUDED.street, city = EXCLUDED.city,
                state = EXCLUDED.state, phone = EXCLUDED.phone,
                website = EXCLUDED.website,
                opening_hours = EXCLUDED.opening_hours,
                open_24h = EXCLUDED.open_24h,
                lat = EXCLUDED.lat, lon = EXCLUDED.lon, geom = EXCLUDED.geom,
                observed_at = EXCLUDED.observed_at, run_id = EXCLUDED.run_id
            """,
            rows,
        )
    return len(rows)


def _start_run() -> int:
    with get_conn() as conn:
        conn.execute(_SEED_SQL, {"sid": SOURCE_ID})
        return conn.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id", (SOURCE_ID,)
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows_published: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, message, rows_published, run_id))


def run(*, dry_run: bool = False) -> int:
    observed_at = run_vintage()
    age_days = (datetime.now(timezone.utc) - observed_at).days
    print(f"[chain-sites] ATP run vintage {observed_at:%Y-%m-%dT%H:%M:%SZ} "
          f"({age_days}d old)", flush=True)

    if dry_run:
        total = 0
        for spider in SPIDERS:
            try:
                rows = fetch_spider(spider, observed_at)
            except Exception as exc:
                print(f"[chain-sites] {spider}: FAILED ({exc})", flush=True)
                continue
            hours = sum(1 for r in rows if r["opening_hours"])
            print(f"[chain-sites] {spider}: {len(rows)} sites "
                  f"({hours} with hours)", flush=True)
            total += len(rows)
        print(f"[dry-run] {total} sites; nothing written", flush=True)
        return total

    run_id = _start_run()
    try:
        all_rows: list[dict] = []
        spiders_ok: list[str] = []
        failures: list[str] = []
        for spider in SPIDERS:
            try:
                rows = fetch_spider(spider, observed_at)
            except Exception as exc:
                # One chain's locator being down is not a reason to lose the
                # other four. Recorded in the run message, not swallowed.
                print(f"[chain-sites] {spider}: FAILED ({exc}) — kept previous "
                      f"rows", flush=True)
                failures.append(f"{spider}: {exc}")
                continue
            hours = sum(1 for r in rows if r["opening_hours"])
            print(f"[chain-sites] {spider}: {len(rows)} sites "
                  f"({hours} with hours)", flush=True)
            for r in rows:
                r["run_id"] = run_id
            all_rows.extend(rows)
            spiders_ok.append(spider)

        if not spiders_ok:
            raise RuntimeError("every spider failed: " + "; ".join(failures))

        with get_conn() as conn:
            published = publish(conn, all_rows, spiders_ok)

        msg = (f"vintage={observed_at:%Y-%m-%d}; spiders_ok="
               f"{len(spiders_ok)}/{len(SPIDERS)}; sites={published}")
        if failures:
            msg += "; failed=" + " | ".join(failures)
        _finish_run(run_id, "success", message=msg, rows_published=published)
        print(f"[chain-sites] published {published} sites from "
              f"{len(spiders_ok)} chains", flush=True)
        return published
    except Exception as exc:
        _finish_run(run_id, "failed", message=f"{type(exc).__name__}: {exc}")
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    a = ap.parse_args()
    load_dotenv()
    try:
        run(dry_run=a.dry_run)
    except Exception as exc:
        print(f"[chain-sites] FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
