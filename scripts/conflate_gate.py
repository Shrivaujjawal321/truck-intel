"""Measure the conflation workload BEFORE running it at US scale.

`run_conflate()` is global (no bbox scoping) and holds three things in Python
RAM on a 15 GB box that also runs Postgres:

  * `pairs`            — every pair scoring >= MERGE_THRESHOLD (0.85)
  * `gray_o` / `gray_f`— every id in the 0.55-0.85 band, UNBOUNDED by any
                         threshold, then passed to SQL as `%(gray_o)s::bigint[]`
                         — one array parameter carrying the full id set, three
                         times (merged insert + two single inserts). Per the
                         Phase-2 risk note this is the likeliest failure point.

The only proof we have is a bounded NYC run that pulled **Overture only**, so
the cross-source pairing path has never actually been exercised at volume.
Calling it "proven" would be wrong, and rewriting proven scoring logic on a
guess is how correctness gets lost. So: measure first, then decide.

This script answers the question at O(1) memory — it streams the same blocking
join through the same scorer and spills gray ids to TEMP TABLES instead of
Python sets, so it can size a workload that might not fit in RAM without
itself falling over.

Read-only with respect to published data: it creates TEMP tables inside one
transaction and writes nothing to core/staging, and records no ops.source_runs
row (a measurement is not an ingest).

Decision rule (Phase-2 tracker):
  merge-band pairs + distinct gray ids  <  ~5M  -> run `--conflate` as-is
  otherwise                                     -> spill first: gray ids into a
                                                   TEMP TABLE instead of an
                                                   array parameter, and an
                                                   external sort for `pairs`,
                                                   preserving score_from()
                                                   semantics exactly.

Usage:
    uv run python scripts/conflate_gate.py
    uv run python scripts/conflate_gate.py --json      # machine-readable

Exit codes: 0 = measured (verdict on stdout), 2 = cannot measure (staging
empty). The verdict itself is NEVER an error — this script reports, it does
not gate the pipeline by exit code.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from truckintel.config import load_dotenv
from truckintel.db import get_conn

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from businesses_pipeline import (  # noqa: E402
    BLOCK_NAME_SIM,
    BLOCK_RADIUS_M,
    DISTINCT_THRESHOLD,
    MERGE_THRESHOLD,
    _COPY_BATCH,
    _PAIRS_SQL,
    _STAGE_TEMP_SQL,
    norm_name_sql,
    pair_bonus,
    score_from,
)

# The decision threshold from the Phase-2 risk note. Not a hard limit — the
# point at which "just run it" stops being the obviously-safe call.
SPILL_ADVICE_THRESHOLD = 5_000_000

# The row filter is TAKEN FROM the production statement rather than retyped, so
# this measurement can never silently drift from what conflate actually stages.
# Everything after `FROM {staging}` is the WHERE clause (incl. {closed_guard}).
_PROD_WHERE = _STAGE_TEMP_SQL.split("FROM {staging}", 1)[1]

# Same population and same `rid` numbering as production (identical
# row_number() ordering + identical filter), but only the columns _PAIRS_SQL
# reads — the production temp table also materializes a per-row `blob` jsonb,
# which costs hours at US scale and tells us nothing about pair volume.
_LIGHT_TEMP_SQL = """
CREATE TEMP TABLE {tmp} AS
SELECT row_number() OVER (ORDER BY source_record_id, name) AS rid,
       {norm} AS name_norm, brand, phone, address,
       ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography AS g
FROM {staging}
""" + _PROD_WHERE


def measure(*, staging_overture: str = "staging.overture_places",
            staging_fsq: str = "staging.fsq_places",
            progress_every: int = 2_000_000) -> dict:
    """Stream the blocking join, bucket every pair by score band, and count
    distinct gray ids without holding any of it in RAM."""
    out: dict = {"threshold": SPILL_ADVICE_THRESHOLD}
    t0 = time.monotonic()
    with get_conn() as conn:
        for tmp, staging, closed in (("_bo", staging_overture, ""),
                                     ("_bf", staging_fsq,
                                      "AND date_closed IS NULL")):
            conn.execute(_LIGHT_TEMP_SQL.format(
                tmp=tmp, staging=staging, norm=norm_name_sql("name"),
                closed_guard=closed))
            conn.execute(f"CREATE INDEX ON {tmp} USING GIST (g)")
            out[tmp] = conn.execute(
                f"SELECT count(*) FROM {tmp}").fetchone()[0]
        if out["_bo"] == 0 or out["_bf"] == 0:
            out["error"] = (
                f"cannot measure the cross-source path: _bo={out['_bo']} "
                f"_bf={out['_bf']} — run --pull-overture / --pull-fsq first"
            )
            return out

        conn.execute("CREATE TEMP TABLE _gray (o_rid BIGINT, f_rid BIGINT)")

        # One connection can be in COPY mode for exactly one statement at a
        # time, and a named cursor's FETCH cannot interleave with an open COPY
        # — so gray pairs are buffered in a bounded list and flushed in
        # batches BETWEEN fetches. Memory stays O(batch), not O(gray set),
        # which is the whole point of measuring this way.
        def flush(buf: list[tuple[int, int]]) -> None:
            if not buf:
                return
            with conn.cursor() as wcur, wcur.copy(
                    "COPY _gray (o_rid, f_rid) FROM STDIN") as cp:
                for row in buf:
                    cp.write_row(row)
            buf.clear()

        blocked = merge_band = gray_band = distinct_band = 0
        buf: list[tuple[int, int]] = []
        with conn.cursor(name="gate_pairs") as pcur:
            pcur.itersize = _COPY_BATCH
            pcur.execute(_PAIRS_SQL, {"radius": BLOCK_RADIUS_M,
                                      "min_sim": BLOCK_NAME_SIM})
            for (o_rid, f_rid, dist_m, name_sim, b_o, b_f, p_o, p_f,
                 a_o, a_f) in pcur:
                blocked += 1
                score = score_from(float(name_sim), float(dist_m),
                                   pair_bonus(b_o, b_f, p_o, p_f, a_o, a_f))
                if score >= MERGE_THRESHOLD:
                    merge_band += 1
                elif score > DISTINCT_THRESHOLD:
                    gray_band += 1
                    buf.append((o_rid, f_rid))
                    if len(buf) >= _COPY_BATCH:
                        flush(buf)
                else:
                    distinct_band += 1
                if progress_every and blocked % progress_every == 0:
                    print(f"gate: {blocked:,} pairs scanned "
                          f"({time.monotonic() - t0:.0f}s)", flush=True)
        flush(buf)

        out["blocked_pairs"] = blocked
        out["merge_band_pairs"] = merge_band
        out["gray_band_pairs"] = gray_band
        out["distinct_band_pairs"] = distinct_band
        # The array-parameter size that actually worries us is the DISTINCT id
        # count, not the pair count — one id can appear in many gray pairs.
        out["gray_ids_overture"] = conn.execute(
            "SELECT count(DISTINCT o_rid) FROM _gray").fetchone()[0]
        out["gray_ids_fsq"] = conn.execute(
            "SELECT count(DISTINCT f_rid) FROM _gray").fetchone()[0]
        out["elapsed_s"] = round(time.monotonic() - t0, 1)

    out["ram_pressure_units"] = (out["merge_band_pairs"]
                                 + out["gray_ids_overture"]
                                 + out["gray_ids_fsq"])
    # CPython: a (float, int, int) tuple plus its list slot is ~120 B; a set of
    # ints is ~60 B/entry once load-factored. Deliberately rough — an order of
    # magnitude is the decision-relevant precision here, not a byte count.
    out["est_peak_python_mb"] = round(
        (out["merge_band_pairs"] * 120
         + (out["gray_ids_overture"] + out["gray_ids_fsq"]) * 60) / 1e6, 1)
    out["verdict"] = ("run_as_is"
                      if out["ram_pressure_units"] < SPILL_ADVICE_THRESHOLD
                      else "spill_first")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true",
                        help="emit the measurement as JSON only")
    args = parser.parse_args(argv)
    load_dotenv()
    m = measure()
    if args.json:
        print(json.dumps(m, indent=2))
        return 2 if "error" in m else 0
    if "error" in m:
        print(f"conflate gate: {m['error']}", file=sys.stderr)
        return 2
    print(f"""
conflate gate — measured in {m['elapsed_s']}s

  staged rows        overture {m['_bo']:>12,}   fsq {m['_bf']:>12,}
  blocked pairs      {m['blocked_pairs']:>12,}   (join output; the cursor's row count)
    merge  >= {MERGE_THRESHOLD}   {m['merge_band_pairs']:>12,}   -> held in the `pairs` list
    gray   >  {DISTINCT_THRESHOLD}    {m['gray_band_pairs']:>12,}   -> ids held in gray_o / gray_f
    distinct           {m['distinct_band_pairs']:>12,}   -> not held at all
  distinct gray ids  overture {m['gray_ids_overture']:>12,}   fsq {m['gray_ids_fsq']:>12,}
                     (this is the SQL array-parameter size, sent 3x)

  RAM-pressure units {m['ram_pressure_units']:>12,}   vs threshold {SPILL_ADVICE_THRESHOLD:,}
  est. peak python   {m['est_peak_python_mb']:>12,} MB  (rough, order-of-magnitude)

  VERDICT: {m['verdict']}
""".rstrip())
    if m["verdict"] == "spill_first":
        print("  -> do NOT run --conflate as-is: spill gray ids to a TEMP\n"
              "     TABLE instead of an array parameter, and sort `pairs`\n"
              "     externally, preserving score_from() semantics exactly.")
    else:
        print("  -> safe to run: uv run python scripts/businesses_pipeline.py "
              "--conflate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
