#!/usr/bin/env bash
# Start the bot in the background, detached from the terminal, so it keeps
# running after the SSH session closes. Usage: ./run.sh [workdir]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(realpath "${1:-$PWD}")"
STATE="${STATE_DIR:-$REPO/state}"
PIDFILE="$STATE/cot.pid"
LOG="$STATE/cot.log"

mkdir -p "$STATE"
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running (pid $(cat "$PIDFILE")); stop it with ./stop.sh" >&2
    exit 1
fi
[[ -d "$WORKDIR" ]] || { echo "not a directory: $WORKDIR" >&2; exit 1; }

# Keep one previous log; start a fresh one when it grows past 10 MB.
if [[ -f "$LOG" ]] && (( $(stat -c %s "$LOG") > 10485760 )); then
    mv "$LOG" "$LOG.1"
fi

cd "$REPO"
setsid nohup .venv/bin/python -m cot "$WORKDIR" >> "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

sleep 2
if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "failed to start, last log lines:" >&2
    tail -n 20 "$LOG" >&2
    rm -f "$PIDFILE"
    exit 1
fi
echo "running (pid $(cat "$PIDFILE")) in $WORKDIR"
echo "log: tail -f $LOG"
