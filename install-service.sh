#!/usr/bin/env bash
# Install the bot as a systemd user service: starts on boot, restarts on failure.
# Usage: ./install-service.sh [workdir]      (default: current directory)
#        ./install-service.sh --print [workdir]   only show the unit file
set -euo pipefail

PRINT=false
if [[ "${1:-}" == "--print" ]]; then PRINT=true; shift; fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(realpath "${1:-$PWD}")"
NAME=claude-over-telegram
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$NAME.service"

[[ -d "$WORKDIR" ]] || { echo "not a directory: $WORKDIR" >&2; exit 1; }
[[ -x "$REPO/.venv/bin/python" ]] || { echo "no .venv in $REPO; see the README" >&2; exit 1; }
[[ -f "$REPO/.env" ]] || { echo "no .env in $REPO; see the README" >&2; exit 1; }

# systemd expands % and splits on unquoted spaces: escape % and " for quoted values.
esc() { local s="${1//%/%%}"; printf '%s' "${s//\"/\\\"}"; }

# systemd starts services with a bare PATH; keep this shell's so the tools Claude
# runs (node, git, ~/.local/bin…) are found as they are in the terminal.
content="[Unit]
Description=Claude Code over Telegram
Wants=network-online.target
After=network-online.target

[Service]
WorkingDirectory=$(esc "$REPO")
ExecStart=\"$(esc "$REPO")/.venv/bin/python\" -m cot \"$(esc "$WORKDIR")\"
Environment=\"PATH=$(esc "$PATH")\"
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
# The bot closes its Claude sessions on SIGTERM; give it time before SIGKILL.
TimeoutStopSec=15

[Install]
WantedBy=default.target
"

if $PRINT; then
    printf '%s' "$content"
    exit 0
fi

if [[ -f "$REPO/state/cot.pid" ]] && kill -0 "$(cat "$REPO/state/cot.pid")" 2>/dev/null; then
    echo "the bot is running from run.sh; stop it first with ./stop.sh" >&2
    exit 1
fi

mkdir -p "$(dirname "$UNIT")"
printf '%s' "$content" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME"
echo "installed $UNIT and started it"

if [[ "$(loginctl show-user "$USER" -P Linger 2>/dev/null)" != "yes" ]]; then
    echo
    echo "To start it at boot without logging in, enable lingering (once):"
    echo "  sudo loginctl enable-linger $USER"
fi
