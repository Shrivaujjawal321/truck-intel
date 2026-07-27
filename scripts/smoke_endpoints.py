"""Smoke-test every v1 endpoint against a running API (checklist item C2).

What this is FOR: after a backfill, "the API returns 200" is not the question
worth answering — an endpoint that serves an empty FeatureCollection is also
200, and reads as healthy on any dashboard. So this reports three states, not
two:

    OK     — responded, and returned at least one feature
    EMPTY  — responded correctly but has no data in the probed bbox
    FAIL   — wrong status, bad envelope, or missing required attribution

EMPTY is never folded into OK. Most of this project's data gaps look exactly
like a successful empty response (`osm.ways` = Delaware-only, `core.businesses`
= NYC-only), and a smoke test that hides that is worse than none.

Each probe carries the bbox it used, because "empty" only means something
relative to where you looked: an NYC bbox over a Delaware-only table is a
correct empty, not a bug. Probes default to a national bbox where the endpoint
allows one, and to a known-populated metro where a small bbox is required.

ODbL: responses derived from `osm.*` MUST carry attribution (the licence
isolation is the whole reason OSM lives in its own schema). Missing
attribution on those routes is a FAIL, not a warning.

Usage:
    make api                                   # in another shell
    uv run python scripts/smoke_endpoints.py
    uv run python scripts/smoke_endpoints.py --base-url http://127.0.0.1:8000
    uv run python scripts/smoke_endpoints.py --json

Exit codes: 0 = no FAILs (EMPTYs are reported, not fatal), 1 = at least one
FAIL, 2 = the API is not reachable at all.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"

# bboxes as 'minLon,minLat,maxLon,maxLat'.
#
# CRITICAL: the API caps a bbox at MAX_BBOX_DEG (4x4 degrees), so there is NO
# "just query the whole US" probe — a CONUS box is rejected as bbox_too_large,
# not served. That constraint is why coverage has to be proved by SWEEPING
# small regional boxes: one metro returning rows says nothing about whether a
# backfill is nationwide, which is exactly the claim being checked here.
REGIONS: dict[str, str] = {
    "DE": "-75.8,38.4,-74.9,39.9",        # the only state osm.ways had pre-backfill
    "NYC": "-74.05,40.65,-73.90,40.85",   # the only place core.businesses has rows
    "DEN": "-105.5,39.0,-104.5,40.0",     # Colorado front range
    "SIERRA": "-121.0,38.5,-119.6,39.9",  # Donner Pass — Caltrans chain controls
    "DFW": "-97.5,32.5,-96.5,33.2",       # Texas
    "SEA": "-122.5,47.0,-121.5,48.0",     # WZDx WA feed
    "PHX": "-112.5,33.0,-111.5,34.0",     # WZDx AZ feed
    "MSP": "-93.5,44.7,-92.9,45.2",       # WZDx MN feed
    "ICT": "-97.8,38.7,-97.0,39.2",       # WZDx KS feed
}

# Endpoints swept across every region: coverage claims live or die here.
SWEEP: list[tuple[str, dict, bool, str]] = [
    ("/v1/bridges", {"limit": 5}, False, "NBI bridges"),
    ("/v1/fuel", {"limit": 5}, True, "OSM fuel stations (ODbL)"),
    ("/v1/places", {"limit": 5}, False, "conflated businesses"),
]

# Single-shot probes: national tables, or feeds whose coverage is legitimately
# regional (chain controls only exist where Caltrans operates).
PROBES: list[tuple[str, dict, bool, str]] = [
    ("/v1/health", {}, False, "liveness"),
    ("/v1/meta/coverage", {}, False, "per-source coverage + freshness"),
    # Both require a bbox (the 4x4-degree cap applies to every spatial read), so
    # probing them without one asserted a 200 that the API is right to refuse —
    # two permanent FAILs that said nothing about coverage. NYC carries both
    # river tunnels and truck parking, so a real bbox exercises real rows.
    ("/v1/tunnels", {"bbox": REGIONS["NYC"], "limit": 5}, False, "NTI tunnels"),
    ("/v1/parking", {"bbox": REGIONS["NYC"], "limit": 5}, False,
     "NTAD truck parking"),
    ("/v1/fuel/prices", {"limit": 5}, False, "EIA diesel prices"),
    ("/v1/bridges", {"bbox": REGIONS["DE"], "limit": 5,
                     "max_clearance_lt_in": 162}, False,
     "low-clearance filter (<13'6\")"),
    ("/v1/live/weather-alerts", {"bbox": REGIONS["DEN"], "limit": 5}, False,
     "NWS alerts"),
    ("/v1/live/weather-alerts", {"bbox": REGIONS["DEN"], "limit": 5,
                                 "include_nongeo": "true"}, False,
     "NWS zone-only alerts included"),
    ("/v1/live/closures", {"bbox": REGIONS["SEA"], "limit": 5}, False,
     "WZDx work zones"),
    ("/v1/live/chain-controls", {"bbox": REGIONS["SIERRA"], "limit": 5}, False,
     "Caltrans CWWP2"),
    # Tracking reads. EMPTY is the correct answer with no devices registered —
    # the sweep reports EMPTY separately from OK, so a fleet-less install is not
    # mistaken for a broken endpoint.
    ("/v1/track/live", {}, False, "live truck positions (own-GPS ingest)"),
]

# Probes that must be REJECTED. An API that quietly serves a garbage or
# unbounded bbox is worse than one that errors, so 200 here is the bug.
# Validation is normalised to 400 with a stable error code (never FastAPI's
# raw 422), so the code is asserted too — the status alone would pass even if
# the error envelope regressed.
NEGATIVE_PROBES: list[tuple[str, dict, int, str, str]] = [
    ("/v1/bridges", {}, 400, "invalid_bbox", "bbox is required"),
    ("/v1/bridges", {"bbox": "not-a-bbox"}, 400, "invalid_bbox",
     "malformed bbox rejected"),
    ("/v1/bridges", {"bbox": "-125,24,-66,50"}, 400, "bbox_too_large",
     "CONUS-sized bbox rejected (4x4 deg cap)"),
    ("/v1/bridges", {"bbox": REGIONS["DE"], "limit": 0}, 400, "invalid_param",
     "limit below range rejected"),
]


def _get(base: str, path: str, query: dict) -> tuple[int, dict | str]:
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body[:200]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body[:200]


def _count_of(payload) -> int | None:
    """Feature count if this is a collection; None if the shape has no count
    (health, single-feature reads) — None is 'not applicable', never 0."""
    if isinstance(payload, dict):
        if "count" in payload:
            return payload["count"]
        if isinstance(payload.get("features"), list):
            return len(payload["features"])
        if isinstance(payload.get("sources"), list):
            return len(payload["sources"])
        # /v1/track/live — a fleet, not a feature collection. Counted so "no
        # trucks registered" reports as EMPTY rather than "not applicable".
        if isinstance(payload.get("devices"), list):
            return len(payload["devices"])
    return None


def run(base: str) -> dict:
    results: list[dict] = []
    swept = [(p, {**q, "bbox": bbox}, attr, f"{note} @ {name}")
             for p, q, attr, note in SWEEP
             for name, bbox in REGIONS.items()]
    for path, query, needs_attr, note in PROBES + swept:
        status, payload = _get(base, path, query)
        row = {"probe": path, "note": note, "http": status,
               "bbox": query.get("bbox")}
        if status != 200:
            row["state"] = "FAIL"
            row["why"] = f"expected 200, got {status}"
        else:
            n = _count_of(payload)
            row["count"] = n
            attr_ok = (not needs_attr
                       or bool(isinstance(payload, dict)
                               and payload.get("attribution")))
            if not attr_ok:
                row["state"] = "FAIL"
                row["why"] = "ODbL attribution missing on an osm.*-derived route"
            elif n is None:
                row["state"] = "OK"
            elif n == 0:
                row["state"] = "EMPTY"
                row["why"] = "responded correctly, no rows in this bbox"
            else:
                row["state"] = "OK"
        results.append(row)

    for path, query, want, want_code, note in NEGATIVE_PROBES:
        status, payload = _get(base, path, query)
        got_code = (payload.get("error", {}).get("code")
                    if isinstance(payload, dict) else None)
        if status != want:
            state, why = "FAIL", f"expected {want}, got {status}"
        elif got_code != want_code:
            state, why = "FAIL", (f"status {status} correct but error code is "
                                  f"{got_code!r}, expected {want_code!r}")
        else:
            state, why = "OK", None
        results.append({"probe": path, "note": f"negative: {note}",
                        "http": status, "state": state, "why": why})

    return {
        "base_url": base,
        "results": results,
        "ok": sum(r["state"] == "OK" for r in results),
        "empty": sum(r["state"] == "EMPTY" for r in results),
        "fail": sum(r["state"] == "FAIL" for r in results),
        "empty_regions": sorted({
            r["note"].split(" @ ")[-1] for r in results
            if r["state"] == "EMPTY" and " @ " in r["note"]}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        code, _ = _get(args.base_url, "/v1/health", {})
    except OSError as e:
        print(f"API unreachable at {args.base_url}: {e}\n"
              f"start it first:  make api", file=sys.stderr)
        return 2
    if code != 200:
        print(f"API at {args.base_url} answered /v1/health with {code}",
              file=sys.stderr)
        return 2

    out = run(args.base_url)
    if args.json:
        print(json.dumps(out, indent=2))
        return 1 if out["fail"] else 0

    mark = {"OK": "OK   ", "EMPTY": "EMPTY", "FAIL": "FAIL "}
    print(f"\nsmoke: {out['base_url']}\n")
    for r in out["results"]:
        cnt = "" if r.get("count") is None else f"{r['count']:>6,}"
        line = f"  {mark[r['state']]} {r['probe']:<28} {cnt:>8}  {r['note']}"
        print(line)
        if r.get("why"):
            print(f"        -> {r['why']}"
                  + (f" (bbox {r['bbox']})" if r.get("bbox") else ""))
    print(f"\n  OK {out['ok']}   EMPTY {out['empty']}   FAIL {out['fail']}")
    if out["empty"]:
        print("\n  EMPTY is not a pass. Each one is either a real coverage gap\n"
              "  or a probe looking in the wrong place — resolve which before\n"
              "  calling the backfill done.")
    return 1 if out["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
