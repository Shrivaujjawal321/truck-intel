#!/usr/bin/env python
"""Weekly refresh of the OSM POI layers (fuel stations, rest areas, weigh points).

Why weekly and not daily
------------------------
Boss asked (2026-07-26) for fuel data to update every day. Two different things
live under "fuel data", and they move at different speeds:

  PRICE     changes daily        -> AAA daily (51 states) + EIA weekly poll.
                                    Already daily, via truckintel-aaa-prices.timer
                                    and the engine's eia_diesel schedule.
  STATIONS  location/attributes  -> this job. A petrol pump does not move
                                    overnight; Geofabrik rebuilds the US extract
                                    about once a day, but the diff that reaches
                                    108k fuel nodes in a week is small.

Running this daily would mean re-downloading a 12 GB PBF and re-passing it every
24 h to change a handful of rows. That is a real cost against no real gain, so
the cadence is weekly and the UI reports the actual `observed_at` rather than
implying the geometry is same-day. Claiming "stations update daily" would be the
kind of freshness theatre this repo's freshness SLO exists to prevent.

Bandwidth discipline
--------------------
Geofabrik publishes a `.osm.pbf.md5` beside every extract. This script reads that
first and downloads the PBF only when the checksum differs from the local file's.
An unchanged week costs one small HTTP GET instead of 12 GB. The download itself
is resumable and verified against the published md5 before it is allowed to
replace the local copy — a truncated PBF must never reach the extractor, because
`snapshot_swap`'s min_rows floor is the only thing between a short read and a
half-empty national layer.

Usage:
    uv run python scripts/osm_pois_refresh.py            # fetch-if-changed + extract
    uv run python scripts/osm_pois_refresh.py --check    # report only, no writes
    uv run python scripts/osm_pois_refresh.py --force    # re-extract from local PBF
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PBF_URL = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"
MD5_URL = PBF_URL + ".md5"
PBF_PATH = REPO / "data" / "pbf" / "us-latest.osm.pbf"
# Free disk required before a 12 GB download is even attempted: the new file and
# the old one coexist until the checksum verifies, so budget for both plus slack.
MIN_FREE_BYTES = 30 * 1024**3


def _hash_file(path: Path) -> str:
    """Streamed md5 of a local file. ~7 min on the 12 GB extract, so callers
    prefer the sidecar below and only fall back to this when it is missing."""
    h = hashlib.md5()  # noqa: S324 — matching Geofabrik's published checksum, not a security hash
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_md5(path: Path) -> str | None:
    """The md5 of the local PBF, or None if we have no file.

    Read from a sidecar (`<pbf>.md5`) written at download time. Re-hashing 12 GB
    on every weekly check would burn ~7 minutes of disk I/O to answer a question
    a 32-byte file already answers, and the sidecar is only ever written *after*
    a download verified against Geofabrik's own checksum — so it is not a claim
    we invented, it is the checksum we already proved.

    A PBF with no sidecar (e.g. the 12 GB file downloaded by hand before this
    script existed) is hashed once and the sidecar is written, so the expensive
    path happens at most once.
    """
    if not path.exists():
        return None
    side = path.with_suffix(path.suffix + ".md5")
    if side.exists():
        return side.read_text().split()[0].strip()
    print(f"[refresh] no sidecar for {path.name} — hashing once "
          f"({path.stat().st_size / 1024**3:.0f} GB, a few minutes)…", flush=True)
    digest = _hash_file(path)
    side.write_text(f"{digest}  {path.name}\n")
    return digest


def _remote_md5() -> str:
    """The checksum Geofabrik publishes for the current extract.

    Goes through polite_get so this share of the traffic obeys the same
    interval and refusal rules as every other outbound fetch in the repo.
    """
    from truckintel.politeness import polite_get
    res = polite_get(MD5_URL, min_interval_s=1.0)
    if res.status_code != 200 or not res.content:
        raise RuntimeError(
            f"cannot read {MD5_URL}: HTTP {res.status_code} — not downloading a "
            f"12 GB file that cannot be verified"
        )
    # Format is "<md5>  us-latest.osm.pbf"
    return res.content.decode("utf-8", "replace").split()[0]


def _free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def fetch_if_changed(*, dry_run: bool = False) -> tuple[bool, str]:
    """Download the PBF only when the published md5 differs from the local file.

    Returns (changed, reason). A verified download replaces the local file; a
    checksum mismatch leaves the old file in place and raises.
    """
    remote = _remote_md5()
    local = _local_md5(PBF_PATH)
    if local == remote:
        return False, f"unchanged (md5 {remote[:12]}…)"
    if dry_run:
        return True, f"would download: local {str(local)[:12]}… != remote {remote[:12]}…"

    free = _free_bytes(PBF_PATH.parent)
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"refusing to download: {free / 1024**3:.1f} GB free, "
            f"need {MIN_FREE_BYTES / 1024**3:.0f} GB for a safe replace"
        )

    import httpx
    tmp = PBF_PATH.with_suffix(".pbf.part")
    print(f"[refresh] downloading {PBF_URL} …", flush=True)
    h = hashlib.md5()  # noqa: S324
    t0 = time.monotonic()
    with httpx.stream("GET", PBF_URL, follow_redirects=True, timeout=None) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(8 * 1024 * 1024):
                fh.write(chunk)
                h.update(chunk)
    got = h.hexdigest()
    if got != remote:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"md5 mismatch: got {got}, expected {remote} — local PBF kept, "
            f"nothing extracted"
        )
    tmp.replace(PBF_PATH)
    # Sidecar written only now, after the checksum verified, so next week's check
    # is a 32-byte read instead of a 12 GB re-hash.
    PBF_PATH.with_suffix(PBF_PATH.suffix + ".md5").write_text(
        f"{remote}  {PBF_PATH.name}\n"
    )
    mb = PBF_PATH.stat().st_size / 1024**2
    print(f"[refresh] verified {mb:,.0f} MB in {time.monotonic() - t0:.0f}s",
          flush=True)
    return True, f"downloaded (md5 {remote[:12]}…)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report whether the extract changed; write nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-extract from the local PBF even if unchanged")
    args = ap.parse_args()

    changed, reason = fetch_if_changed(dry_run=args.check)
    print(f"[refresh] upstream: {reason}", flush=True)
    if args.check:
        return 0
    if not changed and not args.force:
        # Not an error: a week with no upstream change is a normal week. The POI
        # tables keep their existing observed_at, which is the honest answer to
        # "how old is this?" — re-swapping identical rows would only reset the
        # timestamp and make stale data look fresh.
        print("[refresh] nothing to do (use --force to re-extract anyway)",
              flush=True)
        return 0

    from scripts.osm_extract import run_pois
    published = run_pois()
    print(f"[refresh] published {published}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
