#!/usr/bin/env python
"""Label connected components on route.nodes.

Why this matters for honesty: the truck-designated network is not one connected
graph. Pickup and drop can sit in two different islands, in which case there is
NO truck-legal path — and the correct answer is to say so, not to quietly leak
onto a road that is not truck-designated. Storing the component id lets the API
detect that case in one indexed lookup, before any search runs.

Labels land in route.node_component rather than a column on route.nodes: a
455k-row UPDATE rewrites the whole table and was measured at 40 min under I/O
contention, while COPY into a fresh table is seconds and leaves no bloat.

  uv run python scripts/route_components.py           # label + report
  uv run python scripts/route_components.py --report  # report only
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from truckintel.db import get_conn  # noqa: E402


def label(conn) -> dict[int, int]:
    """Union-find over route.edges. Returns node_id -> component id."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        for p in (a, b):
            if p not in parent:
                parent[p] = p
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM route.nodes")
        for (n,) in cur:
            parent[n] = n
        cur.execute("SELECT source, target FROM route.edges")
        for s, t in cur:
            union(s, t)

    # Number components by size, largest first, so component 1 is the mainland.
    roots = collections.Counter(find(n) for n in parent)
    order = {root: i + 1 for i, (root, _) in enumerate(roots.most_common())}
    return {n: order[find(n)] for n in parent}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="report only, do not write")
    args = ap.parse_args()

    with get_conn() as conn:
        if not args.report:
            mapping = label(conn)
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS route.node_component")
                cur.execute(
                    "CREATE TABLE route.node_component "
                    "(node_id bigint PRIMARY KEY, component int NOT NULL)"
                )
                with cur.copy(
                    "COPY route.node_component (node_id, component) FROM STDIN"
                ) as cp:
                    for node_id, comp in mapping.items():
                        cp.write_row((node_id, comp))
                cur.execute(
                    "CREATE INDEX node_component_ix ON route.node_component (component)"
                )
                cur.execute("ANALYZE route.node_component")
            conn.commit()

        with conn.cursor() as cur:
            cur.execute("""
                SELECT component, count(*) AS nodes
                FROM route.node_component GROUP BY component ORDER BY nodes DESC
            """)
            sizes = cur.fetchall()
            cur.execute("SELECT kind, count(*) FROM route.edges GROUP BY kind ORDER BY 2 DESC")
            kinds = cur.fetchall()
            cur.execute("""
                SELECT count(*) FROM route.nodes n
                LEFT JOIN route.node_component c USING (node_id)
                WHERE c.component IS NULL
            """)
            unlabelled = cur.fetchone()[0]

    total = sum(n for _, n in sizes)
    print("edges by kind:")
    for kind, n in kinds:
        print(f"  {kind:<22} {n:>9,}")
    print(f"\nnodes            {total:>9,}")
    print(f"components       {len(sizes):>9,}")
    print(f"largest (id 1)   {sizes[0][1]:>9,}  {sizes[0][1] / total:.1%} of the network")
    print(f"outside it       {total - sizes[0][1]:>9,}  {1 - sizes[0][1] / total:.1%} — unroutable to/from the mainland")
    print(f"unlabelled       {unlabelled:>9,}")
    print(f"next largest     {[f'{n:,}' for _, n in sizes[1:6]]}")
    return 1 if unlabelled else 0


if __name__ == "__main__":
    sys.exit(main())
