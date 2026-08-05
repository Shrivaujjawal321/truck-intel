#!/usr/bin/env bash
# Daily snapshot -> commit -> push. The GitHub-visible end of the pipeline.
#
# The data pipeline updates a local PostGIS database; nothing on GitHub moves
# unless someone pushes. This job closes that gap: regenerate snapshots/,
# commit if anything changed, and push — which also carries any code commits
# that were made locally but never pushed (on 2026-08-05 main was sitting
# 4 commits ahead of origin with nobody noticing).
#
# Refuses to touch anything outside snapshots/: a half-finished working tree
# must never get swept into an automated commit. Push failures are loud
# (exit 1) so ops-watch / freshness surfaces them.

set -euo pipefail
cd "$(dirname "$0")/.."

# Regenerate the snapshot from the database.
"$HOME/.local/bin/uv" run python scripts/daily_snapshot.py

git add snapshots/

if ! git diff --cached --quiet -- snapshots/; then
    # -- snapshots/ twice (add + commit): ONLY the snapshot is ever
    # auto-committed, whatever else the working tree holds.
    git commit -q -m "snapshot: daily data heartbeat $(date -u +%F)" -- snapshots/
    echo "[git-push] committed snapshot $(date -u +%F)"
else
    echo "[git-push] snapshot unchanged, nothing to commit"
fi

# Push regardless: local-only commits from dev sessions ride along daily.
if [ "$(git rev-list --count origin/main..main 2>/dev/null || echo 0)" -gt 0 ]; then
    git push -q origin main
    echo "[git-push] pushed to origin/main"
else
    echo "[git-push] origin/main already up to date"
fi
