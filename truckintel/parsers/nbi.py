"""Parser: FHWA NBI annual bulk ZIP (comma-delimited flavor) -> bridge rows.

Units (NBI Coding Guide): clearances are METERS, ratings METRIC TONS — this
parser converts clearance to inches; rating codes pass through unconverted
(they are codes, not signed values, and are labeled as such downstream).
Coordinates (items 16/17) are packed degrees-minutes-seconds — must be parsed
and converted to decimal degrees.
"""
from __future__ import annotations

from typing import Iterator


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per bridge from the national delimited ZIP.

    Keys of each yielded dict:
        nbi_id                  str    state FIPS + structure number (natural key)
        name                    str|None  facility carried / feature crossed
        state                   str    2-letter USPS code (mapped from FIPS)
        lat, lon                float  decimal degrees (converted from DMS items 16/17)
        min_vert_clearance_in   float|None  inches, min of items 10/53/54 where
                                            coded; None when not applicable/unknown
        operating_rating        str|None  item 64 code (metric tons)
        inventory_rating        str|None  item 66 code
        posting_status          str|None  item 41 code (open/posted/closed)
        observed_at             str    ISO date — the file's data vintage
                                       (submission year), never the download date
        props                   dict   full cleaned record (all parsed NBI items)
    """
    raise NotImplementedError
