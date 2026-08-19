"""Retention sweep for the raw ingest zone — the sibling track_device.py prune
has always had and this never did.

data/raw/<source>/<date>/ holds every payload ever fetched, content-addressed
and replayable. Nothing pruned it: measured 2026-08-18 it was 6.0 GB across
8,560 files after four weeks, growing ~220 MB/day from live-event polling
(wzdx_az alone 2.9 GB). Disk death was still years away on this machine, so
the damage was subtler — tests/test_validate.py globs this tree as a
real-data canary and had stopped finishing in eight minutes.

WHAT IS KEPT, and why more than one rule
----------------------------------------
  * everything newer than --days (default 14)
  * the newest dated directory per source, ALWAYS, however old it is — a source
    that only publishes annually (nbi_annual, ntad_national_network) would
    otherwise have its only payload deleted by an age rule, and "replayable"
    is not the same as "small enough to re-fetch on a whim"
  * the payload behind each source's last SUCCESSFUL run, so the thing the
    live data was actually built from stays on disk and provenance survives

    uv run python scripts/raw_prune.py --dry-run     # show, delete nothing
    uv run python scripts/raw_prune.py --days 14
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from truckintel.config import load_dotenv          # noqa: E402
from truckintel.db import get_conn                 # noqa: E402

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def last_success_dates() -> dict[str, set[str]]:
    """source_id -> {YYYY-MM-DD} of its last successful run.

    Best effort: if the database is unreachable the sweep still runs, but it
    keeps MORE than it would otherwise, never less — a retention job must not
    become more destructive because a dependency was down.
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT source_id, max(started_at)::date::text
                   FROM ops.source_runs
                   WHERE status IN ('success', 'skipped_unchanged')
                   GROUP BY source_id""").fetchall()
        return {sid: {d} for sid, d in rows if d}
    except Exception as exc:                                    # noqa: BLE001
        print(f"  [warn] database unreachable ({type(exc).__name__}); "
              "keeping last-success payloads is skipped, nothing extra deleted",
              flush=True)
        return {}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    load_dotenv()

    if not RAW.is_dir():
        print(f"raw-prune: {RAW} does not exist, nothing to do")
        return 0

    cutoff = (date.today() - timedelta(days=args.days)).isoformat()
    keep_success = last_success_dates()

    freed = 0
    removed = 0
    kept = 0
    for src_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
        dated = sorted(p for p in src_dir.iterdir() if p.is_dir())
        if not dated:
            continue
        keep = {dated[-1].name}                       # newest, always
        keep |= keep_success.get(src_dir.name, set())  # last successful run
        for d in dated:
            if d.name in keep or d.name >= cutoff:
                kept += 1
                continue
            size = _dir_bytes(d)
            freed += size
            removed += 1
            if args.dry_run:
                print(f"  would remove {d.relative_to(RAW.parent.parent)} "
                      f"({size/1e6:.1f} MB)")
            else:
                shutil.rmtree(d)

    verb = "would free" if args.dry_run else "freed"
    print(f"raw-prune: {removed} dated dir(s) {'listed' if args.dry_run else 'removed'}, "
          f"{kept} kept, {verb} {freed/1e9:.2f} GB (cutoff {cutoff})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
