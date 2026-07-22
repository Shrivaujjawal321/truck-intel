"""The ingestion engine: tick (scheduler pass), worker loop, and run_source —
the one generic fetch->validate->publish path. Only parsers are per-source code.
"""
from __future__ import annotations

import sys


def run_source(source_id: str) -> None:
    """Run one source end-to-end. Every outcome writes EXACTLY one
    ops.source_runs row — success, skip, gate-abort, or failure. Never fake success.

    Steps to implement:
    1. Load source config from ops.sources; insert source_runs row (status='running').
    2. auth.env set but env var empty -> finish run status='skipped_no_key'
       with a clear message and return (EIA rule; no crash).
    3. polite_get() the url (conditional headers from the last successful run);
       not_modified -> status='skipped_unchanged'.
    4. Write raw bytes to data/raw/<source_id>/<date>/<sha256[:16]>.<ext>;
       store raw_sha256 + http_status on the run row.
    5. parsers.<source>.parse(raw) -> rows; gate1_schema + gate2_coords;
       rejects to quality.rejects with reasons.
    6. Registry gates (min_rows, max_row_delta_pct vs last success): failure ->
       status='gated', publish ABORTED, old table stays live.
    7. Load via the source's load_pattern (snapshot_swap | event_lifecycle |
       upsert); finish run status='success' with rows_in/published/rejected.
    """
    raise NotImplementedError


def tick() -> None:
    """One scheduler pass (systemd timer target): sync registry into
    ops.sources, then jobs.enqueue_due(). Cheap and idempotent."""
    raise NotImplementedError


def worker_loop() -> None:
    """Drain ops.job_queue forever: claim_job -> run_source -> finish_job.
    One job at a time in MVP; sleeps briefly when the queue is empty."""
    raise NotImplementedError


def main(argv: list[str]) -> int:
    """CLI: python -m truckintel.engine {tick | ingest <source_id> | worker}"""
    if len(argv) >= 1 and argv[0] == "tick":
        tick()
        return 0
    if len(argv) >= 2 and argv[0] == "ingest":
        run_source(argv[1])
        return 0
    if len(argv) >= 1 and argv[0] == "worker":
        worker_loop()
        return 0
    print(__doc__)
    print("usage: python -m truckintel.engine {tick | ingest <source_id> | worker}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
