"""Parser: NWS active alerts (api.weather.gov GeoJSON FeatureCollection) -> events."""
from __future__ import annotations

from typing import Iterator


def parse(raw: bytes) -> Iterator[dict]:
    """Yield one dict per active alert.

    Keys of each yielded dict:
        event_id     str        CAP alert identifier (feed's own id; upsert key)
        kind         str        always 'weather_alert' in MVP
        geom_wkt     str|None   WKT POLYGON/MULTIPOLYGON in EPSG:4326; None when
                                the alert carries zone references only (honest
                                NULL — no geometry is fabricated from zones in MVP)
        observed_at  str        ISO timestamp — the alert's sent/issued time,
                                never the fetch time
        props        dict       severity, headline, event type, onset, expires,
                                area description, full CAP properties
    """
    raise NotImplementedError
