"""The single polite-HTTP choke point. ALL outbound HTTP goes through polite_get().

Contract (plan §9, engine-enforced):
- per-host token bucket, default 1 request/second
- descriptive User-Agent with contact email (config.user_agent(); NWS requires it)
- honor Retry-After on 429/503
- on 403/429: back off and STOP — never retry around a refusal, never spoof headers
- conditional requests (ETag / If-Modified-Since) where the server supports them
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from truckintel.config import user_agent

# host -> time.monotonic() of the last request; single worker process by design.
_last_hit: dict[str, float] = {}

_DEFAULT_RETRY_AFTER_S = 30.0
# A single-worker pipeline must never sleep for a server-chosen eternity: a
# Retry-After beyond this cap fails the run (PoliteRefusal) and the scheduler's
# backoff retries later — politeness preserved, pipeline never frozen.
_MAX_RETRY_AFTER_S = 300.0
# A hang or a DNS blip is not a refusal, and the feed that produced it is
# usually fine seconds later. mn.carsprogram.org failed 21 of 48 runs in 48 h
# on "Read timed out (60s)" while a hand-timed request to the same endpoint
# returned 1.5 MB in 3.5 s. Before 2026-08-18 a timeout raised straight out of
# here with no retry at all, so every blip cost a whole run.
_TRANSIENT_RETRY_WAIT_S = 10.0


@dataclass(frozen=True)
class PoliteResult:
    """What a fetch returned. not_modified=True means the caller should skip
    (record status='skipped_unchanged') and content is empty."""

    status_code: int
    content: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool


class PoliteRefusal(Exception):
    """Server refused us (403 / repeated 429). Back off; a refusal is final."""


def _retry_after_seconds(value: str | None) -> float:
    """Parse a Retry-After header: delta-seconds or HTTP-date. Missing/garbage
    falls back to a conservative default — we still wait, never hammer."""
    if not value:
        return _DEFAULT_RETRY_AFTER_S
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_S


def _throttle(host: str, min_interval_s: float) -> None:
    last = _last_hit.get(host)
    if last is not None:
        wait = last + min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)


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
    host = urlsplit(url).netloc
    send_headers = {"User-Agent": user_agent()}
    if headers:
        send_headers.update(headers)
    if etag:
        send_headers["If-None-Match"] = etag
    if last_modified:
        send_headers["If-Modified-Since"] = last_modified

    retried = False
    while True:
        _throttle(host, min_interval_s)
        try:
            resp = requests.get(url, params=params, headers=send_headers,
                                timeout=timeout_s)
        except (requests.Timeout, requests.ConnectionError):
            # One retry, never a loop — the same discipline the 429/503 path
            # uses, and it shares `retried`, so a host cannot cost more than
            # two attempts however it misbehaves. The throttle above still
            # applies on the way round, so this can never become hammering.
            # _last_hit is updated because the host WAS contacted: a request
            # that timed out still consumed its attention.
            _last_hit[host] = time.monotonic()
            if retried:
                raise
            retried = True
            time.sleep(_TRANSIENT_RETRY_WAIT_S)
            continue
        _last_hit[host] = time.monotonic()

        if resp.status_code == 403:
            raise PoliteRefusal(f"403 from {host} — a refusal is final, not retrying")
        if resp.status_code in (429, 503):
            if retried:
                if resp.status_code == 429:
                    raise PoliteRefusal(f"second 429 from {host} — backing off")
                break  # second 503: hand the failure to the caller, never loop
            retried = True
            wait = _retry_after_seconds(resp.headers.get("Retry-After"))
            if wait > _MAX_RETRY_AFTER_S:
                raise PoliteRefusal(
                    f"{resp.status_code} from {host} with Retry-After {wait:.0f}s "
                    f"(> {_MAX_RETRY_AFTER_S:.0f}s cap) — failing run; retry later"
                )
            time.sleep(wait)
            continue
        break

    not_modified = resp.status_code == 304
    return PoliteResult(
        status_code=resp.status_code,
        content=b"" if not_modified else resp.content,
        # on 304 servers may omit the validators — keep the ones we sent
        etag=resp.headers.get("ETag") or (etag if not_modified else None),
        last_modified=resp.headers.get("Last-Modified")
        or (last_modified if not_modified else None),
        not_modified=not_modified,
    )
