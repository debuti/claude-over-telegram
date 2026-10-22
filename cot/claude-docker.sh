#!/usr/bin/env bash
# Stands in for the claude CLI: runs it inside the conversation's container, which only
# sees the conversation's folder and the user's Claude/git settings. The SDK starts this
# in the conversation's folder and talks to it over stdin/stdout, as it would to claude.
#
# The container outlives this process: Claude is started in it with docker exec, so
# whatever it launched detached (a long test suite, a dev server) keeps running when
# Claude stops, on idle or when the bot restarts. The bot removes the container once
# nothing runs in it. COT_* variables come from cot/container.py.
set -euo pipefail

# Same image, folder, limits and claude binary as the running container, or start anew.
spec="$COT_IMAGE $PWD $COT_CPUS $COT_MEMORY $(stat -c %Y "$COT_CLAUDE")"
current="$(docker container inspect -f '{{.State.Running}} {{index .Config.Labels "cot.spec"}}' "$COT_NAME" 2>/dev/null || true)"
if [[ "$current" != "true $spec" ]]; then
    docker rm -f "$COT_NAME" >/dev/null 2>&1 || true
    args=(run -d --init --name "$COT_NAME"
        --label cot.agent=1 --label "cot.spec=$spec"
        --user "$(id -u):$(id -g)"
        --network host
        --cpus "$COT_CPUS" --memory "$COT_MEMORY" --pids-limit 4096 --shm-size 1g
        -e HOME="$HOME"
        -v "$PWD:$PWD"
        -v "$COT_CLAUDE:/usr/local/bin/claude:ro")
    mkdir -p "$HOME/.claude"
    args+=(-v "$HOME/.claude:$HOME/.claude")
    [[ -f "$HOME/.claude.json" ]] && args+=(-v "$HOME/.claude.json:$HOME/.claude.json")
    for p in .gitconfig .ssh .config/gh; do
        [[ -e "$HOME/$p" ]] && args+=(-v "$HOME/$p:$HOME/$p:ro")
    done
    docker "${args[@]}" "$COT_IMAGE" sleep infinity >/dev/null
fi

# Only what Claude needs from the bot's environment: never the Telegram token.
exec_args=(exec -i -w "$PWD")
while IFS= read -r name; do
    case "$name" in
        CLAUDE*|ANTHROPIC*|MCP_*|DISABLE_*|MAX_*|BASH_*|OTEL_*|LANG|LC_*|TZ|TERM|*_PROXY|*_proxy)
            exec_args+=(-e "$name") ;;
    esac
done < <(compgen -e)

exec docker "${exec_args[@]}" "$COT_NAME" claude "$@"
