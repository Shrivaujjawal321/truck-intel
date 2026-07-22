"""Parser: EIA API v2 weekly on-highway diesel response (JSON) -> price rows.

HONESTY: these are survey-based REGIONAL weekly averages (US, PADDs, sub-PADDs,
CA only for diesel) — never station-level pump prices. The API labels them
"regional_weekly_estimate".
"""
from __future__ import annotations

import json
from typing import Iterator

# EIA duoarea -> our region labels (research/fuel.md: diesel = national + PADDs
# + sub-PADDs + California only). Unknown codes pass through verbatim rather
# than being dropped or guessed.
DUOAREA_TO_REGION: dict[str, str] = {
    "NUS": "US",
    "R10": "PADD1",
    "R1X": "PADD1A",   # New England
    "R1Y": "PADD1B",   # Central Atlantic
    "R1Z": "PADD1C",   # Lower Atlantic
    "R20": "PADD2",
    "R30": "PADD3",
    "R40": "PADD4",
    "R50": "PADD5",
    "R5XCA": "PADD5_EX_CA",  # West Coast less California
    "SCA": "CA",
}


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per (region, week) price observation.

    Keys of each yielded dict:
        region         str    normalized region code: 'US', 'PADD1', 'PADD1A',
                              ..., 'CA' (mapped from EIA duoarea/series area)
        product        str    'diesel' in MVP
        week_of        str    ISO date of the survey week (Monday)
        price_usd_gal  float  dollars per gallon
        observed_at    str    same as week_of — the week the price was true,
                              never the fetch date
        props          dict   full source record (series id, area name, units)
    """
    body = json.loads(raw)
    for record in (body.get("response") or {}).get("data", []):
        value = record.get("value")
        if value is None:  # weeks with no survey value carry no observation
            continue
        duoarea = (record.get("duoarea") or "").strip()
        week_of = record.get("period")
        yield {
            "region": DUOAREA_TO_REGION.get(duoarea, duoarea),
            "product": "diesel",
            "week_of": week_of,
            "price_usd_gal": float(value),
            "observed_at": week_of,
            "props": dict(record),
        }
