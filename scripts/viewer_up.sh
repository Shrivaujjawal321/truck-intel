#!/usr/bin/env bash
# Start (or restart) the API that serves the data viewer, detached from this shell.
#
#   ./scripts/viewer_up.sh          -> http://127.0.0.1:8000/viewer
#   ./scripts/viewer_up.sh --stop   -> stop it
#
# Log: data/viewer-api.log   PID: data/viewer-api.pid
set -euo pipefail
cd "$(dirname "$0")/.."

PIDFILE=data/viewer-api.pid
LOGFILE=data/viewer-api.log
PORT="${PORT:-8000}"

stop() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill "$(cat "$PIDFILE")" && echo "viewer api: stopped (pid $(cat "$PIDFILE"))"
    else
        echo "viewer api: not running"
    fi
    rm -f "$PIDFILE"
}

if [ "${1:-}" = "--stop" ]; then stop; exit 0; fi
stop >/dev/null 2>&1 || true

# setsid: survives this shell exiting, and never shares its process group.
setsid uv run uvicorn api.main:app --host 127.0.0.1 --port "$PORT" \
    >"$LOGFILE" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$PORT/v1/health" >/dev/null 2>&1; then
        echo "viewer api: up (pid $(cat "$PIDFILE"))"
        echo "open -> http://127.0.0.1:$PORT/viewer"
        exit 0
    fi
    sleep 0.5
done
echo "ERROR: api did not become healthy in 20s — see $LOGFILE" >&2
tail -20 "$LOGFILE" >&2
exit 1
