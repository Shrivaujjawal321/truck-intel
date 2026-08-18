#!/usr/bin/env bash
# Block until the dependencies a scheduled job needs are actually up.
#
# WHY THIS EXISTS
# ---------------
# Every unit in deploy/ declares its dependencies the obvious way:
#     After=network-online.target docker.service
#     Wants=network-online.target
# and every one of those lines is a no-op. These are *user* units, and neither
# target exists in the systemd user manager:
#     $ systemctl --user status network-online.target
#     Unit network-online.target could not be found.
# A user unit cannot order itself against a system service. So the jobs start
# whenever the user manager reaches them, which on this machine is seconds
# after login.
#
# That is not a theoretical window. 13 of 14 timers set Persistent=true, the
# daily jobs are scheduled 03:10-06:15, and the laptop is never on then — ten
# consecutive boots put the earliest at 06:09. Every run therefore arrives as
# catch-up, and catch-up ignores RandomizedDelaySec: on 2026-08-04 eight units
# started inside 41 ms. Both dependencies lost that race, in production:
#
#   DNS   truckintel-aaa-prices, 6 failures against 3 successes --
#         "Failed to resolve 'gasprices.aaa.com' ([Errno -3] Temporary
#         failure in name resolution)"
#   DB    truckintel-businesses, 2026-08-03 09:25 -- "connection to server at
#         127.0.0.1, port 5432 failed: Connection refused", because the
#         truckintel-pg container had not started yet. That one cost the whole
#         monthly rebuild: the timer is OnCalendar=*-*-01, so nothing retried.
#
# Type=oneshot forbids Restart=, so nothing absorbs either miss.
#
# This is a script rather than an inline ExecStartPre on purpose. systemd
# expands $VAR in Exec lines and silently substitutes empty for anything it
# does not know, and treats % as a specifier prefix -- so the obvious inline
# one-liner (host=${url%%:*}) is quietly corrupted before bash ever sees it.
# In a file, the shell is the only thing parsing the shell.
#
# Usage:  wait_ready.sh [--dns] [--db] [--timeout SECONDS]
# Exits 0 as soon as every requested dependency answers, 1 if the budget runs
# out. Failing loudly beats hanging forever.

set -uo pipefail

TIMEOUT=120
WANT_DNS=0
WANT_DB=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dns)     WANT_DNS=1 ;;
        --db)      WANT_DB=1 ;;
        --timeout) TIMEOUT="$2"; shift ;;
        *) echo "wait_ready: unknown argument '$1'" >&2; exit 2 ;;
    esac
    shift
done

cd "$(dirname "$0")/.." || exit 1

# Host and port come from DATABASE_URL so this keeps working if the database
# moves. cut, not ${var%%...}, because a literal % is a minefield if this ever
# gets inlined back into a unit file.
db_host=127.0.0.1
db_port=5432
if [ -f .env ]; then
    hostport=$(sed -nE 's|^DATABASE_URL=[a-z+]+://[^@]*@([^/]*)/.*|\1|p' .env | head -1)
    if [ -n "$hostport" ]; then
        h=$(printf '%s' "$hostport" | cut -d: -f1)
        p=$(printf '%s' "$hostport" | cut -d: -f2)
        [ -n "$h" ] && db_host="$h"
        [ -n "$p" ] && [ "$p" != "$h" ] && db_port="$p"
    fi
fi
# /dev/tcp needs an address, and "localhost" can resolve before the stack is
# fully up -- the same race this script exists to close.
[ "$db_host" = "localhost" ] && db_host=127.0.0.1

# getent rather than ping or curl: it goes through NSS, which is the path
# requests/urllib actually take, so this tests what the job will do.
dns_ok() { getent hosts one.one.one.one >/dev/null 2>&1; }

# A real connection, for the same reason dns_ok uses getent: it tests what the
# job will actually do. A TCP handshake is not readiness. Postgres accepts
# connections on the socket while it is still replaying WAL and answers every
# one of them with "FATAL: the database system is starting up" -- so the old
# /dev/tcp probe reported ready and the job's own connect failed seconds later.
# 2026-08-18, in production, on one boot: git-push, mechanics-daily and
# track-prune all passed this gate at 12:20-12:26 and then died on
# "server closed the connection unexpectedly" / "the database system is
# starting up". Three units, one boot, one gap.
#
# Falls back to the TCP probe only if the venv is missing (a half-built
# checkout), because a gate that cannot run is worse than a coarse one.
db_ok() {
    if [ -x .venv/bin/python ]; then
        .venv/bin/python - <<'PYCHK' >/dev/null 2>&1
import psycopg
from truckintel.config import database_url
psycopg.connect(database_url(), connect_timeout=3).close()
PYCHK
    else
        (exec 3<>"/dev/tcp/$db_host/$db_port") 2>/dev/null
    fi
}

# Attempts, not a wall-clock deadline: bash SECONDS keeps counting through a
# suspend, so a laptop that sleeps mid-wait comes back with the whole budget
# already spent and gives up just as the network returns. 2026-08-05, in
# production: truckintel-aaa-prices started 08:38, the machine suspended,
# resumed ~09:12, and this script reported "still unavailable after 120s" at
# 09:12:55 — mechanics-daily resolved DNS fine at 09:16. Counting attempts
# survives the gap: an iteration that straddles a suspend still costs one.
attempts=$(( (TIMEOUT + 4) / 5 ))
missing=""
for ((i = 0; i < attempts; i++)); do
    missing=""
    [ "$WANT_DNS" = 1 ] && ! dns_ok && missing="$missing dns"
    [ "$WANT_DB" = 1 ]  && ! db_ok  && missing="$missing db($db_host:$db_port)"
    [ -z "$missing" ] && exit 0
    sleep 5
done

echo "wait_ready: still unavailable after ${TIMEOUT}s:$missing" >&2
exit 1
