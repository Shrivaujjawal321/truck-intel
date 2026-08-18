#!/usr/bin/env bash
# Every sql/schema*.sql must appear in sql/apply-order.txt, and every entry
# there must exist on disk. Needs no database, so it runs in the fast CI job.
#
# Exists because on 2026-08-18 a review found sql/schema_liveness.sql missing
# from all three places that applied the schema — the file was a day old and
# nothing anywhere noticed.
set -uo pipefail
cd "$(dirname "$0")/.."

order_file="sql/apply-order.txt"
fail=0

listed="$(grep -vE '^\s*(#|$)' "$order_file" | sed 's/[[:space:]]*$//')"

# on disk but not listed
for f in sql/schema*.sql; do
    base="$(basename "$f")"
    if ! printf '%s\n' "$listed" | grep -qxF "$base"; then
        echo "  FAIL  $f exists but is not listed in $order_file" >&2
        fail=1
    fi
done

# listed but not on disk
while IFS= read -r base; do
    [ -n "$base" ] || continue
    if [ ! -f "sql/$base" ]; then
        echo "  FAIL  $order_file lists $base, which does not exist" >&2
        fail=1
    fi
done <<< "$listed"

if [ "$fail" -ne 0 ]; then
    echo "schema order: FAILED" >&2
    exit 1
fi
echo "schema order: $(printf '%s\n' "$listed" | grep -c .) files, all present and listed"
