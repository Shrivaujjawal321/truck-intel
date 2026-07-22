"""The single polite-HTTP choke point. ALL outbound HTTP goes through polite_get().

Contract (plan §9, engine-enforced):
- per-host token bucket, default 1 request/second
- descriptive User-Agent with contact email (config.user_agent(); NWS requires it)
- honor Retry-After on 429/503
- on 403/429: back off and STOP — never retry around a refusal, never spoof headers
- conditional requests (ETag / If-Modified-Since) where the server supports them
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PoliteResult:
    """What a fetch returned. not_modified=True means the caller should skip
    (record status='skipped_unchanged') and content is empty."""

    status_code: int
    content: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool


def polite_get(
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_s: float = 60.0,
    min_interval_s: float = 1.0,
) -> PoliteResult:
    """Fetch `url` politely.

    Behavior to implement:
    - Sleep as needed so requests to the same host are >= min_interval_s apart
      (module-level per-host timestamp map; single worker process by design).
    - Send config.user_agent() plus If-None-Match / If-Modified-Since when
      etag / last_modified are given; a 304 returns not_modified=True.
    - 429/503 with Retry-After: wait that long, retry once. 403 or a second
      429: raise PoliteRefusal — the caller records a failed run. NEVER loop.

    Raises:
        PoliteRefusal: server said no (403 / repeated 429). Do not work around.
        requests.RequestException: network-level failure.
    """
    raise NotImplementedError


class PoliteRefusal(Exception):
    """Server refused us (403 / repeated 429). Back off; a refusal is final."""
