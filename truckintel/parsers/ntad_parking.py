"""Parser: NTAD Truck Stop Parking (ArcGIS GeoJSON pages, concatenated) -> sites.

The fetcher concatenates all resultOffset pages into one GeoJSON
FeatureCollection so the raw artifact is one file, not many fragments.
"""
from __future__ import annotations

from typing import Iterator


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per parking site.

    Keys of each yielded dict:
        site_id       str        NTAD feature id (natural key)
        kind          str        'truck_stop' | 'public_rest_area' (from facility type)
        name          str|None
        state         str|None   2-letter USPS code
        lat, lon      float      decimal degrees (point geometry)
        truck_spaces  int|None   capacity; None = unknown (NEVER coerce 0/None)
        observed_at   str        ISO date of the ~2019 Jason's Law survey era —
                                 the honesty rule: NOT the download date
        props         dict       full cleaned attribute record
    """
    raise NotImplementedError
