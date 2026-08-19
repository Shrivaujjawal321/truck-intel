"""Export every table to its own CSV — one file per table.

Geometry columns are written as WKT (ST_AsText), not the hex WKB that COPY
emits by default: a CSV full of "0101000020E6100000..." is not something a
person or a spreadsheet can use, which is the whole point of a CSV export.

    uv run python scripts/export_csv.py                  # everything
    uv run python scripts/export_csv.py --schema core    # one schema
    uv run python scripts/export_csv.py --out somewhere  # elsewhere

Writes <out>/<schema>.<table>.csv and prints a manifest with row counts and
sizes, so what landed can be checked against what was asked for.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from truckintel.config import load_dotenv          # noqa: E402
from truckintel.db import get_conn                 # noqa: E402

SCHEMAS = ("core", "ops", "osm", "route", "staging")


def tables(conn, schemas) -> list[tuple[str, str]]:
    return [(s, t) for s, t in conn.execute(
        """SELECT table_schema, table_name FROM information_schema.tables
           WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s)
           ORDER BY table_schema, table_name""", (list(schemas),)).fetchall()]


def select_list(conn, schema: str, table: str) -> str:
    """Column list with geometry rendered as WKT and everything else as-is."""
    cols = conn.execute(
        """SELECT column_name, udt_name FROM information_schema.columns
           WHERE table_schema = %s AND table_name = %s
           ORDER BY ordinal_position""", (schema, table)).fetchall()
    parts = []
    for name, udt in cols:
        q = f'"{name}"'
        if udt in ("geometry", "geography"):
            parts.append(f'ST_AsText({q}) AS {q}')
        else:
            parts.append(q)
    return ", ".join(parts)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/exports")
    ap.add_argument("--schema", action="append", dest="schemas")
    args = ap.parse_args(argv)
    load_dotenv()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    schemas = args.schemas or list(SCHEMAS)

    total_bytes = 0
    manifest: list[tuple[str, int, int]] = []
    with get_conn() as conn:
        for schema, table in tables(conn, schemas):
            cols = select_list(conn, schema, table)
            path = out / f"{schema}.{table}.csv"
            sql = f'COPY (SELECT {cols} FROM "{schema}"."{table}") TO STDOUT WITH CSV HEADER'
            with path.open("wb") as fh, conn.cursor().copy(sql) as cp:
                for chunk in cp:
                    fh.write(chunk)
            n = conn.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()[0]
            size = path.stat().st_size
            total_bytes += size
            manifest.append((f"{schema}.{table}", n, size))
            print(f"  {schema}.{table:34} {n:>10,} rows  {size/1e6:>9.1f} MB", flush=True)

    print(f"\n  {len(manifest)} files -> {out}  ({total_bytes/1e9:.2f} GB total)")
    big = [(t, s) for t, _, s in manifest if s > 100 * 1024 * 1024]
    if big:
        print("\n  NOTE: GitHub rejects any single file over 100 MB. These exceed it:")
        for t, s in big:
            print(f"    {t}  ({s/1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
