"""Ops alert delivery — Telegram and/or ntfy, best effort, never raising.

WHY THIS MODULE EXISTS
----------------------
The delivery code already worked, but only inside scripts/weekly_digest.py.
Everything else that had something urgent to say could only print it:
scripts/freshness_check.py accepted a --telegram flag whose implementation was
the literal comment "sending is post-MVP — printing only (TODO: alert hook)".

The cost of that was measured, not theorised: osm_pois failed five times in
seven days and nobody found out until a smoke test was run by hand. A monitor
that prints into a journal nobody reads is not a monitor.

DESIGN RULES
------------
- NEVER raise. A delivery hiccup must not fail the job that had news; the work
  is already done and recorded. Callers get per-channel results and decide.
- NEVER silently truncate. Telegram's 4096-char cap is real, so an over-long
  body is cut WITH a visible marker saying how much was dropped and where the
  full text lives.
- Plain text, no parse_mode. Bodies are full of source ids like
  `osm_truck_repair_overpass`; legacy Markdown rejects the underscores with an
  HTTP 400 and the whole alert vanishes.
- Unconfigured is 'skipped', not 'error'. A laptop with no token is a valid
  deployment; it should print, not fail.

DEDUPLICATION lives in the callers, not here. This module sends what it is
given; deciding that a condition was already reported an hour ago is the
caller's judgement, and burying it here would make alerts silently disappear
for reasons the caller could not see.
"""
from __future__ import annotations

import os

import requests

from truckintel.config import load_dotenv

TELEGRAM_LIMIT = 4096          # Telegram sendMessage hard cap (characters)
HTTP_TIMEOUT = 15


def _fit(body: str, source: str) -> str:
    """Trim to Telegram's cap with an honest marker — never a silent cut."""
    if len(body) <= TELEGRAM_LIMIT:
        return body
    marker = f"\n…(truncated {{}} chars — full text in {source})"
    keep = TELEGRAM_LIMIT - len(marker.format(9_999_999))
    return body[:keep] + marker.format(len(body) - keep)


def deliver(body: str, *, title: str = "truck-intel",
            full_text_at: str = "the job log",
            priority: str = "default") -> list[dict]:
    """Send `body` to every configured channel.

    Returns one dict per channel: {channel, status, detail} with status
    'ok' | 'skipped' | 'error'. Never raises.
    """
    load_dotenv()
    results: list[dict] = []

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat,
                      "text": _fit(f"{title}\n\n{body}", full_text_at),
                      "disable_web_page_preview": True},
                timeout=HTTP_TIMEOUT)
            results.append({"channel": "telegram",
                            "status": "ok" if r.ok else "error",
                            "detail": f"HTTP {r.status_code}"
                                      + ("" if r.ok else f" — {r.text[:120]}")})
        except Exception as exc:                                # noqa: BLE001
            results.append({"channel": "telegram", "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
    else:
        results.append({"channel": "telegram", "status": "skipped",
                        "detail": "TELEGRAM_BOT_TOKEN/CHAT_ID unset — printed only"})

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            # ntfy's limit is far above Telegram's, so the FULL body goes —
            # cutting it here would be silent data loss.
            r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                              headers={"Title": title, "Priority": priority},
                              timeout=HTTP_TIMEOUT)
            results.append({"channel": "ntfy",
                            "status": "ok" if r.ok else "error",
                            "detail": f"HTTP {r.status_code}"})
        except Exception as exc:                                # noqa: BLE001
            results.append({"channel": "ntfy", "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
    else:
        results.append({"channel": "ntfy", "status": "skipped",
                        "detail": "NTFY_TOPIC unset"})
    return results


def report(results: list[dict]) -> bool:
    """Print per-channel outcomes; True when nothing errored.

    'skipped' counts as fine — an unconfigured channel is a deployment choice.
    An 'error' is not: the caller should exit non-zero so a supervisor does not
    read a failed delivery as a delivered alert.
    """
    ok = True
    for r in results:
        print(f"[notify] {r['channel']}: {r['status']} — {r['detail']}", flush=True)
        ok = ok and r["status"] != "error"
    return ok
