#!/usr/bin/env python
"""Daily diesel prices per state, from AAA.

Boss's decision (2026-07-24): this is a personal, non-commercial tool, so use
AAA. Recorded here in full so the basis is never lost.

WHY THIS IS A CHOICE AND NOT A DEFAULT
--------------------------------------
AAA is better data than EIA on every axis that matters to a driver — daily
instead of weekly, up to 120,000 stations surveyed a day instead of ~590 diesel
outlets, and state-level instead of 11 multi-state regions. Nothing else free
comes close: USDA AgTransport, FRED and BTS all just republish the same EIA
weekly series, so there is no third option to prefer.

What their terms say, verbatim (gasprices.aaa.com/about-aaa):

  "Users may not reproduce, distribute, create derivative works, display,
   modify, archive or otherwise exploit any or all portions of this website."

  "Users are granted a limited license to retrieve or print a copy of content on
   this website for personal, non-commercial use only so long as attribution is
   given to the American Automobile Association."

Two things are genuinely on our side. The personal non-commercial licence is
explicit, and a price is a fact — US law does not extend copyright to facts
(Feist v. Rural Telephone), only to original selection and arrangement.

One thing is not. "archive" sits in the prohibited list without a
commercial/non-commercial qualifier, and keeping a daily history in a table is
archiving. That is the open point, it is Boss's call, and it is written down here
rather than glossed over.

HOW THIS BEHAVES AS A RESULT
----------------------------
* OFF unless switched on. `AAA_PRICES_ENABLED=1` must be set. A build that ships
  to anyone else keeps EIA and never calls this.
* Retention is a window, not an archive. Only KEEP_DAYS of history is stored;
  older rows are deleted on every run.
* Attribution travels with the data. Every row carries the AAA/OPIS credit their
  licence asks for, and the UI prints it.
* Politeness is not optional. Requests go through truckintel.politeness.polite_get
  with a 10-second interval, matching the Crawl-delay in their robots.txt. A 403
  or a repeated 429 raises and this job records a failed run — it never works
  around a refusal.
* EIA is never removed. It stays as the fallback and as the source of record.
* Audited like any other feed. Every invocation writes EXACTLY one
  ops.source_runs row under source id 'aaa_daily', so a silently-dead price job
  trips the same freshness alarm as a dead federal feed — and the UI can tell
  the driver how old the number is instead of implying it is today's.

  AAA_PRICES_ENABLED=1 uv run python scripts/aaa_prices.py
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402
from truckintel.politeness import polite_get  # noqa: E402

STATE_PAGE = "https://gasprices.aaa.com/state-gas-price-averages/"
NATIONAL_PAGE = "https://gasprices.aaa.com/"

# gasprices.aaa.com/robots.txt: "Crawl-delay: 10".
CRAWL_DELAY_S = 10.0

# A window, not an archive: enough to see a trend, not a database of their data.
KEEP_DAYS = 30

ATTRIBUTION = "AAA Gas Prices (data © Oil Price Information Service / AAA)"

SOURCE_ID = "aaa_daily"
# Daily source; two missed days is a real problem worth alerting on.
SLO_HOURS = 48

_SEED_SQL = """
INSERT INTO ops.sources
    (source_id, name, owner, kind, load_pattern, schedule_minutes, slo_hours,
     enabled, verify_status)
VALUES
    (%(sid)s, 'AAA daily state diesel averages (personal, non-commercial)',
     'AAA / Oil Price Information Service', 'derived', 'derived', NULL,
     %(slo)s, TRUE, 'verified')
ON CONFLICT (source_id) DO NOTHING
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS core.fuel_prices_daily (
    state          char(2)  NOT NULL,
    product        text     NOT NULL,
    observed_on    date     NOT NULL,
    price_usd_gal  numeric  NOT NULL,
    source_id      text     NOT NULL,
    attribution    text     NOT NULL,
    note           text     NOT NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (state, product, observed_on)
);
CREATE INDEX IF NOT EXISTS fuel_prices_daily_state_ix
    ON core.fuel_prices_daily (state, observed_on DESC);
"""

NOTE = ("AAA daily state average, surveyed by OPIS/WEX — a state-wide average, "
        "still not the price at any individual pump")

# Full state names as AAA prints them, mapped to USPS codes.
NAME_TO_USPS = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

# AAA labels the column itself — <td class="diesel">$5.3110</td> — and puts the
# USPS code in the row's own link (?state=AL). Both are read directly rather than
# counting columns, so a re-ordered table cannot silently yield petrol prices
# labelled as diesel.
_STATE = re.compile(r"[?&]state=([A-Z]{2})\b")
_DIESEL = re.compile(r'<td[^>]*class="[^"]*\bdiesel\b[^"]*"[^>]*>\s*\$\s*([0-9]+\.[0-9]+)')


def parse_state_table(html: str) -> dict[str, float]:
    """State -> diesel price, read from the labelled cell in each table row."""
    out: dict[str, float] = {}
    for chunk in html.split("<tr")[1:]:
        state = _STATE.search(chunk)
        diesel = _DIESEL.search(chunk)
        if not state or not diesel:
            continue
        usps = state.group(1)
        if usps not in NAME_TO_USPS.values():
            continue                      # ignore anything that is not a US state
        # AAA prints tenths of a cent ($3.7720); keep three decimals like EIA.
        out[usps] = round(float(diesel.group(1)), 3)
    return out


def fetch() -> dict[str, float]:
    # polite_get enforces the per-host interval, sends a descriptive contact UA,
    # and raises PoliteRefusal on 403 or a repeated 429 — a refusal is recorded,
    # never worked around. (Measured: two back-to-back requests DO earn a 403
    # from their Cloudflare edge; honouring Crawl-delay: 10 returns 200.)
    res = polite_get(STATE_PAGE, min_interval_s=CRAWL_DELAY_S)
    html = getattr(res, "text", None) or getattr(res, "body", "") or getattr(res, "content", "")
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    prices = parse_state_table(html)
    if not prices:
        raise SystemExit(
            "parsed 0 states from AAA — their page layout changed. Fix the parser "
            "rather than loosening it; a wrong price is worse than no price."
        )
    return prices


def _start_run() -> int:
    with get_conn() as pg:
        pg.execute(_SEED_SQL, {"sid": SOURCE_ID, "slo": SLO_HOURS})
        return pg.execute(
            "INSERT INTO ops.source_runs (source_id, status) "
            "VALUES (%s, 'running') RETURNING run_id",
            (SOURCE_ID,),
        ).fetchone()[0]


def _finish_run(run_id: int, status: str, *, message: str | None = None,
                rows: int | None = None) -> None:
    with get_conn() as pg:
        pg.execute(
            "UPDATE ops.source_runs SET status = %s, finished_at = now(), "
            "message = %s, rows_published = %s WHERE run_id = %s",
            (status, message, rows, run_id),
        )


def store(prices: dict[str, float]) -> int:
    today = date.today()
    cutoff = today - timedelta(days=KEEP_DAYS)
    with get_conn() as pg:
        with pg.cursor() as cur:
            cur.execute(SCHEMA)
            cur.executemany(
                """
                INSERT INTO core.fuel_prices_daily
                    (state, product, observed_on, price_usd_gal,
                     source_id, attribution, note)
                VALUES (%s, 'diesel', %s, %s, 'aaa_daily', %s, %s)
                ON CONFLICT (state, product, observed_on)
                DO UPDATE SET price_usd_gal = EXCLUDED.price_usd_gal,
                              ingested_at = now()
                """,
                [(s, today, p, ATTRIBUTION, NOTE) for s, p in prices.items()],
            )
            # Window, not archive.
            cur.execute(
                "DELETE FROM core.fuel_prices_daily WHERE observed_on < %s", (cutoff,)
            )
            deleted = cur.rowcount
            cur.execute("ANALYZE core.fuel_prices_daily")
        pg.commit()
    print(f"[aaa] stored {len(prices)} states for {today}; "
          f"dropped {deleted} rows older than {KEEP_DAYS} days", flush=True)
    return len(prices)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and print, write nothing")
    args = ap.parse_args()

    if os.environ.get("AAA_PRICES_ENABLED") != "1":
        print(
            "AAA prices are OFF.\n"
            "  AAA's terms grant a limited licence for PERSONAL, NON-COMMERCIAL use\n"
            "  only, and list 'archive' among the prohibited uses. Enable it\n"
            "  deliberately, for a personal tool, with:\n\n"
            "      AAA_PRICES_ENABLED=1 uv run python scripts/aaa_prices.py\n\n"
            "  EIA (weekly, regional, public domain) stays available either way."
        )
        return 0

    if args.dry_run:
        prices = fetch()
        print(f"[aaa] parsed {len(prices)} state diesel averages", flush=True)
        for st, pr in sorted(prices.items())[:5]:
            print(f"       {st}  ${pr:.3f}", flush=True)
        print("[aaa] dry run — nothing written")
        return 0

    # One audited row per invocation, success or failure. A dead price job must
    # trip the same freshness alarm as a dead federal feed.
    run_id = _start_run()
    try:
        prices = fetch()
        print(f"[aaa] parsed {len(prices)} state diesel averages", flush=True)
        for st, pr in sorted(prices.items())[:5]:
            print(f"       {st}  ${pr:.3f}", flush=True)
        n = store(prices)
        _finish_run(run_id, "success", rows=n)
    except Exception as exc:
        # Including PoliteRefusal: a 403 is recorded as a failure, never
        # retried around.
        _finish_run(run_id, "failed", message=f"{type(exc).__name__}: {exc}"[:400])
        print(f"[aaa] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"\n{ATTRIBUTION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
