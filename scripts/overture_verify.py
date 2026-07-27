#!/usr/bin/env python
"""Verify every Overture parquet part actually reads — the real download gate.

Full byte length is necessary but not sufficient: a part can be the right size
and still be unreadable. This opens each file with duckdb and reports row counts,
so a load never starts against a half-download and reports a partial result as
if it were national coverage.

Exit 0 only when every expected part reads.

  uv run python scripts/overture_verify.py
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
DIR = REPO / "data" / "overture_places"
KEYS = DIR / "keys.txt"


def main() -> int:
    expected = [line.strip().rsplit("/", 1)[-1] for line in KEYS.read_text().splitlines() if line.strip()]
    present = {os.path.basename(p) for p in glob.glob(str(DIR / "*.parquet"))}

    con = duckdb.connect()
    ok, bad, missing = [], [], []
    total_rows = 0

    for name in expected:
        if name not in present:
            missing.append(name)
            continue
        path = DIR / name
        try:
            # count(*) alone is NOT a check: DuckDB answers it from the parquet
            # footer without touching a single data page, so a file whose pages
            # are truncated still "passes". That exact false pass let a corrupt
            # part through, and the loader died later with a Thrift
            # TProtocolException. Force a real scan of a real column instead.
            n, cats = con.execute(
                f"""
                SELECT count(*), count(categories.primary)
                FROM read_parquet('{path}')
                """
            ).fetchone()
            ok.append((name, n, cats))
            total_rows += n
        except Exception as exc:
            bad.append((name, str(exc).splitlines()[0][:70]))

    for name, n, cats in ok:
        print(f"  OK      {name[:20]:<22} {n:>12,} rows  {cats:>12,} categorised")
    for name, why in bad:
        print(f"  CORRUPT {name[:20]:<22} {why}")
    for name in missing:
        print(f"  MISSING {name[:20]:<22}")

    print(f"\n{len(ok)}/{len(expected)} parts readable · {total_rows:,} rows")
    if bad or missing:
        print(f"NOT COMPLETE — {len(bad)} corrupt, {len(missing)} missing. "
              f"Re-run: ./scripts/overture_fetch.sh")
        return 1
    print("complete — safe to load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
