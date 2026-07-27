"""WZDx feed discovery: federal Feed Registry CSV -> PROPOSED registry YAMLs.

Pulls the USDOT WZDx Feed Registry (public domain, data.transportation.gov),
filters to feeds that look ingestible today (active + no API key + plain URL +
WZDx v4.x + mappable US state), and drafts one PROPOSED registry YAML per feed
into data/wzdx_proposed/ plus a SUMMARY.md table of everything it saw.

HUMAN-IN-LOOP BY DESIGN (MASTER_PLAN 'Discover' + pipeline.md §8.4): this
script NEVER writes into registry/. Reading a feed's license/terms is a legal
judgment, not code — a human reviews each proposal, verifies the agency's
terms, then copies the YAML into registry/ (dropping the proposed_* keys and
adding license evidence to the comment header).

Run: uv run python scripts/wzdx_discover.py [--out data/wzdx_proposed]
Exit codes: 0 ok, 2 registry fetch/parse failed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import date
from pathlib import Path

import yaml

from truckintel.politeness import polite_get

REGISTRY_CSV_URL = (
    "https://data.transportation.gov/api/views/69qe-yiui/rows.csv?accessType=DOWNLOAD"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "wzdx_proposed"

# A URL that embeds a credential (even an agency-published one, like OK's
# access_token) is excluded: research/live-ops.md — "treat embedded tokens as
# agency-published, do not redistribute as your own". A human decides.
_EMBEDDED_CRED_RE = re.compile(r"(?i)[?&](api[_-]?key|app_key|access_token|token|key)=")

# The registry mixes WZDx RoadEventFeed rows with CWZ-spec rows ('CWZ 1.0');
# parsers/wzdx.py speaks WZDx v3/v4 only.
_WZDX_VERSION_RE = re.compile(r"^[34](\.\d+)?$")

STATE_TO_USPS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "puerto rico": "PR", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "yes", "1")


def fetch_registry_csv() -> bytes:
    """The registry CSV, via the one polite-HTTP choke point."""
    res = polite_get(REGISTRY_CSV_URL, timeout_s=120.0)
    if res.status_code >= 400:
        raise RuntimeError(f"registry CSV fetch failed: HTTP {res.status_code}")
    return res.content


def classify(row: dict) -> tuple[str | None, str | None]:
    """(usps_state, exclusion_reason). reason=None means: propose it.

    Filters (task contract): active, no API key required, license plausibly
    open (registry is public domain; per-feed terms are the human's judgment),
    plus practical gates — a real URL, WZDx (not CWZ) schema, a mappable US
    state (coverage honesty needs a state)."""
    state = STATE_TO_USPS.get((row.get("state") or "").strip().lower())
    url = (row.get("url") or "").strip()
    if not _truthy(row.get("active")):
        return state, "inactive in the federal registry"
    if not url.startswith("http"):
        return state, "no usable URL in the registry"
    if _truthy(row.get("needAPIKey")):
        return state, "requires an API key (free-issuance not auto-verifiable — human review)"
    if _EMBEDDED_CRED_RE.search(url):
        return state, "URL embeds an agency credential — do not redistribute (human review)"
    version = (row.get("version") or "").strip()
    if version and not _WZDX_VERSION_RE.match(version):
        return state, f"non-WZDx schema version {version!r} (parser speaks WZDx v3/v4)"
    if state is None:
        return None, f"no mappable US state ({row.get('state')!r}) — coverage honesty needs one"
    return state, None


def _proposal_yaml(row: dict, source_id: str, state: str) -> str:
    """One PROPOSED registry YAML. Same shape registry.load_registry() expects
    so promotion is copy + human edits, but flagged loudly as unapproved."""
    org = (row.get("issuingOrganization") or "").strip() or "unknown agency"
    today = date.today().isoformat()
    return f"""\
# PROPOSED — NOT APPROVED. Drafted by scripts/wzdx_discover.py on {today}
# from the federal WZDx Feed Registry (public domain,
# https://data.transportation.gov/d/69qe-yiui). A HUMAN must verify the
# agency's license/terms and liveness before copying this into registry/
# (MASTER_PLAN: license reading is a legal judgment, not code).
# registry row: state={row.get('state')!r} org={org!r} feed={row.get('feedName')!r}
#   version={row.get('version')!r} update_freq={row.get('DatafeedUpdateFrequency')!r}
#   active={row.get('active')!r} needAPIKey={row.get('needAPIKey') or 'false'!r}
id: {source_id}
name: WZDx work zones — {org} ({state})
owner: {org}
url: {row.get('url', '').strip()}
kind: live_json
load_pattern: event_lifecycle
parser: wzdx
schedule_minutes: 15
slo_hours: 24                  # work zones move slower than weather
license: "UNVERIFIED — registry row is public domain; confirm {org} feed terms before promotion"
attribution: "{org} (WZDx)"
gates:
  min_rows: 0                  # zero active work zones is a legitimate state
  max_row_delta_pct: null      # row-delta gate is not meaningful for event feeds
auth: null
proposed_by: scripts/wzdx_discover.py
proposed_on: "{today}"
"""


def discover(csv_bytes: bytes, out_dir: Path) -> list[dict]:
    """Parse the registry CSV, write proposals + SUMMARY.md into out_dir.

    Returns one summary dict per registry row. Refuses to write anywhere
    inside registry/ — promotion is the human's move, enforced, not advisory.
    """
    out_dir = out_dir.resolve()
    registry_dir = (REPO_ROOT / "registry").resolve()
    if out_dir == registry_dir or registry_dir in out_dir.parents:
        raise ValueError(
            f"refusing to write proposals into {out_dir} — registry/ is "
            "human-approved only (pipeline.md §8.4)"
        )
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    if not rows:
        raise ValueError("registry CSV parsed to zero rows — upstream format change?")

    out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []
    used_ids: set[str] = set()
    for row in rows:
        state, reason = classify(row)
        entry = {
            "state": state or (row.get("state") or "").strip() or "?",
            "org": (row.get("issuingOrganization") or "").strip(),
            "feed": (row.get("feedName") or "").strip(),
            "url": (row.get("url") or "").strip(),
            "version": (row.get("version") or "").strip(),
            "proposed": reason is None,
            "reason": reason,
        }
        if reason is None:
            source_id = f"wzdx_{state.lower()}"
            if source_id in used_ids:  # two feeds in one state (e.g. TX + Austin)
                source_id = f"wzdx_{state.lower()}_{_slug(entry['feed'] or entry['org'])}"
            used_ids.add(source_id)
            entry["source_id"] = source_id
            (out_dir / f"{source_id}.yaml").write_text(
                _proposal_yaml(row, source_id, state)
            )
        summary.append(entry)

    _write_summary(out_dir, summary)
    return summary


def check_liveness(out_dir: Path, timeout: int = 20) -> list[dict]:
    """Fetch every proposed feed and report whether it actually serves WZDx.

    WHY ONLY THIS HALF IS AUTOMATED
    -------------------------------
    Promotion into registry/ needs two answers: "does the feed work?" and "do
    the agency's terms permit use?". The first is a fact a script can settle.
    The second is a legal judgement, and every proposal this script writes says
    so in its own licence field: "UNVERIFIED — confirm feed terms before
    promotion". Automating that away would be forging the review, not doing it.

    So this narrows the human's job to the part only a human can do: instead of
    opening 21 URLs to see which are even alive, they get a list of the live
    ones and read terms for those alone.

    Returns one dict per proposal: {source_id, state, url, status, detail}.
    """
    import urllib.error
    import urllib.request

    from truckintel.config import user_agent

    results = []
    for path in sorted(out_dir.glob("wzdx_*.yaml")):
        doc = yaml.safe_load(path.read_text())
        url, sid = doc.get("url"), doc.get("id")
        entry = {"source_id": sid, "state": (sid or "")[-2:].upper(),
                 "url": url, "status": "unknown", "detail": ""}
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Read the WHOLE body. An earlier version capped this at 400 KB
                # to be frugal and then json.loads()'d the result — which
                # truncates any feed bigger than that into invalid JSON and
                # reports it as "not-json". It called AZ, KS, MN and WA dead
                # while all four were in registry/ and had published
                # successfully that morning. A check that lies about working
                # feeds is worse than no check.
                body = resp.read()
            data = json.loads(body)
            feats = data.get("features")
            if feats is None:
                entry.update(status="not-wzdx",
                             detail="200 but no 'features' key — not a WZDx GeoJSON")
            else:
                ver = ((data.get("road_event_feed_info") or {})
                       .get("version", "?"))
                entry.update(status="live",
                             detail=f"{len(feats)} active work zone(s), WZDx v{ver}")
        except urllib.error.HTTPError as exc:
            entry.update(status="http-error", detail=f"HTTP {exc.code}")
        except json.JSONDecodeError:
            entry.update(status="not-json", detail="200 but body is not JSON")
        except Exception as exc:                                # noqa: BLE001
            entry.update(status="unreachable",
                         detail=f"{type(exc).__name__}: {str(exc)[:80]}")
        results.append(entry)
        print(f"  {entry['status']:12} {sid:12} {entry['detail']}", flush=True)
    return results


def _write_summary(out_dir: Path, summary: list[dict]) -> None:
    proposed = sum(1 for e in summary if e["proposed"])
    lines = [
        "# WZDx feed discovery — summary",
        "",
        f"Generated {date.today().isoformat()} by scripts/wzdx_discover.py. "
        f"{proposed} of {len(summary)} registry feeds proposed. Proposals are "
        "drafts: a human verifies each feed's license/terms before anything "
        "reaches registry/.",
        "",
        "| state | organization | feed | version | proposed | reason / id |",
        "|---|---|---|---|---|---|",
    ]
    for e in summary:
        note = e.get("source_id") if e["proposed"] else e["reason"]
        lines.append(
            f"| {e['state']} | {e['org']} | {e['feed']} | {e['version']} "
            f"| {'YES' if e['proposed'] else 'no'} | {note} |"
        )
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="proposals directory (never registry/)")
    parser.add_argument("--check-live", action="store_true",
                        help="skip discovery; fetch each EXISTING proposal and "
                             "report whether it still serves WZDx. Narrows the "
                             "human licence review to feeds that actually work.")
    args = parser.parse_args()
    if args.check_live:
        print(f"checking liveness of proposals in {args.out} …", flush=True)
        res = check_liveness(args.out)
        live = [r for r in res if r["status"] == "live"]
        print(f"\n{len(live)}/{len(res)} proposal(s) live.")
        if live:
            print("Live and awaiting a LICENCE decision (terms are a human "
                  "call — see each YAML's licence field):")
            for r in live:
                print(f"  {r['state']}  {r['source_id']:12} {r['url']}")
        dead = [r for r in res if r["status"] != "live"]
        if dead:
            print(f"\nNot promotable right now ({len(dead)}):")
            for r in dead:
                print(f"  {r['state']}  {r['source_id']:12} {r['status']} — {r['detail']}")
        return 0
    try:
        summary = discover(fetch_registry_csv(), args.out)
    except Exception as exc:
        print(f"DISCOVERY FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for e in summary:
        mark = "PROPOSED" if e["proposed"] else "excluded"
        note = e.get("source_id") if e["proposed"] else e["reason"]
        print(f"{mark:9} {e['state']:>2} {e['org'][:40]:40} {note}")
    print(f"\n{sum(1 for e in summary if e['proposed'])} proposal(s) in {args.out} "
          "— human review required before promotion to registry/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
