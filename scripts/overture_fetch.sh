#!/usr/bin/env bash
# Fetch the Overture places release, verifying every part.
#
# The previous attempt (data/overture_places/dl.sh) fired 16 parallel curls and
# logged "done" on exit 0 — but 14 of 16 files were silently truncated, and the
# log claimed success anyway. duckdb could not read them ("No magic bytes found
# at end of file"). This version:
#   - checks Content-Length up front and compares it to what landed on disk
#   - resumes (-C -) instead of restarting, and retries on partial transfer
#   - keeps concurrency low so a kill does not shred every file at once
#   - backs off hard: S3 throttles a burst of large GETs (503 SlowDown), which
#     shows up as attempts that transfer ZERO bytes and fail in ~20 s
#   - marks a part done ONLY after the byte count matches
#
# Readability is then confirmed separately by scripts/overture_verify.py, which
# is the real gate — a full-length file can still be corrupt.
#
#   ./scripts/overture_fetch.sh            # fetch whatever is missing/short
#   ./scripts/overture_fetch.sh --force    # re-fetch everything
set -uo pipefail
cd "$(dirname "$0")/.."

DIR=data/overture_places
KEYS="$DIR/keys.txt"
BASE="https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
LOG="$DIR/fetch.log"
PARALLEL=2
FORCE="${1:-}"

[ -f "$KEYS" ] || { echo "missing $KEYS" >&2; exit 1; }

fetch_one() {
    local key="$1" file remote local_size attempt
    file="$DIR/$(basename "$key")"

    remote=$(curl -sSI --connect-timeout 20 "$BASE/$key" \
             | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)
    if [ -z "$remote" ]; then
        echo "$(date +%T) SIZE-UNKNOWN $(basename "$key")" >> "$LOG"
        return 1
    fi

    if [ "$FORCE" = "--force" ]; then rm -f "$file"; fi
    local_size=$(stat -c%s "$file" 2>/dev/null || echo 0)
    if [ "$local_size" = "$remote" ]; then
        echo "$(date +%T) ALREADY-FULL $(basename "$key") ($remote bytes)" >> "$LOG"
        return 0
    fi

    for attempt in 1 2 3 4 5 6 7 8; do
        curl -sS -C - --retry 8 --retry-all-errors --retry-delay 20 \
             --connect-timeout 20 --speed-time 120 --speed-limit 1024 \
             -o "$file" "$BASE/$key"
        local_size=$(stat -c%s "$file" 2>/dev/null || echo 0)
        if [ "$local_size" = "$remote" ]; then
            echo "$(date +%T) OK $(basename "$key") ($remote bytes, attempt $attempt)" >> "$LOG"
            return 0
        fi
        echo "$(date +%T) SHORT $(basename "$key") ($local_size/$remote, attempt $attempt)" >> "$LOG"
        sleep 30   # S3 answers a burst of large GETs with 503 SlowDown
    done
    echo "$(date +%T) FAILED $(basename "$key") ($local_size/$remote)" >> "$LOG"
    return 1
}
export -f fetch_one
export DIR BASE LOG FORCE

: > "$LOG"
echo "$(date +%T) START $(wc -l < "$KEYS") parts -> $DIR" >> "$LOG"
xargs -P "$PARALLEL" -I{} bash -c 'fetch_one "$@"' _ {} < "$KEYS"
echo "$(date +%T) DONE" >> "$LOG"

grep -c '^.* OK\|ALREADY-FULL' "$LOG" 2>/dev/null | xargs -I{} echo "{} parts at full length"
grep 'FAILED' "$LOG" || true
