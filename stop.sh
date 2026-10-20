#!/usr/bin/env bash
# Stop the bot started by run.sh, letting it close its Claude sessions first.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="${STATE_DIR:-$REPO/state}/cot.pid"

if [[ ! -f "$PIDFILE" ]] || ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "not running"
    rm -f "$PIDFILE"
    exit 0
fi

PID="$(cat "$PIDFILE")"
kill -TERM "$PID"
for _ in $(seq 20); do
    kill -0 "$PID" 2>/dev/null || { rm -f "$PIDFILE"; echo "stopped"; exit 0; }
    sleep 0.5
done
# Still alive after 10 s: kill the whole process group (bot + Claude processes).
kill -KILL -- "-$PID" 2>/dev/null || true
rm -f "$PIDFILE"
echo "killed"
