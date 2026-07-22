"""Parser: EIA API v2 weekly on-highway diesel response (JSON) -> price rows.

HONESTY: these are survey-based REGIONAL weekly averages (US, PADDs, sub-PADDs,
CA only for diesel) — never station-level pump prices. The API labels them
"regional_weekly_estimate".
"""
from __future__ import annotations

from typing import Iterator


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
    raise NotImplementedError
