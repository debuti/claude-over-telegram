#!/usr/bin/env bash
# Stands in for the claude CLI: runs it in a throwaway container that only sees the
# conversation's folder and the user's Claude/git settings. The SDK starts this in the
# conversation's folder and talks to it over stdin/stdout, as it would to claude itself.
# COT_* variables come from cot/container.py.
set -euo pipefail

# One container per conversation: drop one a previous connection may have left.
docker rm -f "$COT_NAME" >/dev/null 2>&1 || true

args=(run -i --rm --init --name "$COT_NAME"
    --label cot.agent=1
    --user "$(id -u):$(id -g)"
    --network host
    --cpus "$COT_CPUS" --memory "$COT_MEMORY" --pids-limit 4096 --shm-size 1g
    -e HOME="$HOME" -w "$PWD"
    -v "$PWD:$PWD"
    -v "$COT_CLAUDE:/usr/local/bin/claude:ro")

mkdir -p "$HOME/.claude"
args+=(-v "$HOME/.claude:$HOME/.claude")
[[ -f "$HOME/.claude.json" ]] && args+=(-v "$HOME/.claude.json:$HOME/.claude.json")
for p in .gitconfig .ssh .config/gh; do
    [[ -e "$HOME/$p" ]] && args+=(-v "$HOME/$p:$HOME/$p:ro")
done

# Only what Claude needs from the bot's environment: never the Telegram token.
while IFS= read -r name; do
    case "$name" in
        CLAUDE*|ANTHROPIC*|MCP_*|DISABLE_*|MAX_*|BASH_*|OTEL_*|LANG|LC_*|TZ|TERM|*_PROXY|*_proxy)
            args+=(-e "$name") ;;
    esac
done < <(compgen -e)

exec docker "${args[@]}" "$COT_IMAGE" claude "$@"
