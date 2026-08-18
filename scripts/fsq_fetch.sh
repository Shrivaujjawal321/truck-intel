#!/usr/bin/env bash
# Mirror an FSQ OS Places release from source.coop to local disk, verifying
# every part — then let DuckDB scan local files instead of URLs.
#
# WHY THIS EXISTS
# ---------------
# DuckDB's httpfs reader against data.source.coop stalls. Measured 2026-08-18,
# twice, reproducibly: a run reached ~28-30 MB and then sat at 0-6 KB/s with
# sockets in CLOSE-WAIT, making no progress for 55 minutes. It is not the
# server — plain curl pulled an 11 MB part from the same host at 3.6 MB/s
# seconds later, on a fresh connection.
#
# scripts/mechanic_list.py hit exactly this against the Overture store and
# recorded the same conclusion in its own comments: "Mirroring the parquet
# files locally first with resumable parallel curl, then scanning them, is the
# reliable path." scripts/overture_fetch.sh is that path; this is its FSQ
# sibling and deliberately copies its shape:
#   - Content-Length checked up front and compared to what landed
#   - resume (-C -) rather than restart, with bounded retries
#   - low concurrency, so a kill does not shred every file at once
#   - a speed floor (--speed-limit/--speed-time): under 1 KB/s for 120 s is
#     the stall signature above, and aborting beats hanging until someone
#     notices tomorrow
#   - a part counts as done ONLY when the byte count matches
#
#   ./scripts/fsq_fetch.sh              # fetch whatever is missing/short
#   ./scripts/fsq_fetch.sh --force      # re-fetch everything
set -uo pipefail
cd "$(dirname "$0")/.."

DIR=data/fsq_places
BASE="https://data.source.coop/fused"
LISTING="https://data.source.coop/fused/fsq-os-places/"
LOG="$DIR/fetch.log"
KEYS="$DIR/keys.txt"
PARALLEL=2
FORCE="${1:-}"

mkdir -p "$DIR"

# Newest release in the listing, and its parquet keys. Same no-silent-
# truncation rule businesses_pipeline.py applies: an S3 listing caps at 1000
# keys, and a truncated page would silently drop parquet files, which means
# silently missing places. Refuse rather than mirror a partial release.
xml=$(curl -sS -A "truck-intel (+ops mirror)" --connect-timeout 30 "$LISTING")
if [ -z "$xml" ]; then echo "listing fetch failed" >&2; exit 1; fi
if printf '%s' "$xml" | grep -q "<IsTruncated>true</IsTruncated>"; then
    echo "listing is truncated (>1000 keys) — refusing a partial file list" >&2
    exit 1
fi
REL=$(printf '%s' "$xml" | grep -o '<Key>fsq-os-places/[0-9-]*/places/' \
      | sed 's|<Key>fsq-os-places/||; s|/places/||' | sort -u | tail -1)
if [ -z "$REL" ]; then echo "no release found in listing" >&2; exit 1; fi
# "key size" per line. The size comes from the listing we already have, NOT
# from a per-file HEAD. A HEAD can answer with a transient error body and no
# status check: on the first run 53.parquet was reported as 16 bytes, so a
# complete 197,197,846-byte file was declared SHORT six times and then FAILED.
# The listing is authoritative, it is one request instead of 81, and it cannot
# disagree with itself.
printf '%s' "$xml" | python3 -c '
import sys, re
xml = sys.stdin.read()
rel = sys.argv[1]
pat = r"<Key>(fsq-os-places/%s/places/[^<]*\.parquet)</Key>.*?<Size>(\d+)</Size>" % re.escape(rel)
for key, size in re.findall(pat, xml, re.S):
    print(key, size)
' "$REL" | sort > "$KEYS"
n=$(wc -l < "$KEYS")
[ "$n" -gt 0 ] || { echo "no parquet keys for release $REL" >&2; exit 1; }
echo "release $REL — $n parquet parts -> $DIR"

fetch_one() {
    local line="$1" key remote file local_size attempt
    key="${line%% *}"
    remote="${line##* }"
    file="$DIR/$(basename "$key")"

    case "$remote" in
        ''|*[!0-9]*) echo "$(date +%T) SIZE-UNKNOWN $(basename "$key")" >> "$LOG"; return 1 ;;
    esac
    if [ "$FORCE" = "--force" ]; then rm -f "$file"; fi
    local_size=$(stat -c%s "$file" 2>/dev/null || echo 0)
    if [ "$local_size" = "$remote" ]; then
        echo "$(date +%T) ALREADY-FULL $(basename "$key") ($remote bytes)" >> "$LOG"; return 0
    fi

    for attempt in 1 2 3 4 5 6; do
        curl -sS -C - -A "truck-intel (+ops mirror)" \
             --retry 6 --retry-all-errors --retry-delay 15 \
             --connect-timeout 20 --speed-time 120 --speed-limit 1024 \
             -o "$file" "$BASE/$key"
        local_size=$(stat -c%s "$file" 2>/dev/null || echo 0)
        if [ "$local_size" = "$remote" ]; then
            echo "$(date +%T) OK $(basename "$key") ($remote bytes, attempt $attempt)" >> "$LOG"
            return 0
        fi
        echo "$(date +%T) SHORT $(basename "$key") ($local_size/$remote, attempt $attempt)" >> "$LOG"
        sleep 20
    done
    echo "$(date +%T) FAILED $(basename "$key") ($local_size/$remote)" >> "$LOG"
    return 1
}
export -f fetch_one
export DIR BASE LOG FORCE

: > "$LOG"
echo "$(date +%T) START release=$REL parts=$n" >> "$LOG"
xargs -P "$PARALLEL" -I{} bash -c 'fetch_one "$@"' _ {} < "$KEYS"
echo "$(date +%T) DONE" >> "$LOG"

ok=$(grep -c ' OK \| ALREADY-FULL ' "$LOG" 2>/dev/null || echo 0)
echo "$ok/$n parts at full length -> $DIR"
if grep -q 'FAILED' "$LOG"; then grep 'FAILED' "$LOG"; exit 1; fi
