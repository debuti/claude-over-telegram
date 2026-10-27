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

# This script is read anew on every start, so a bot from before 2.5 (still running until
# restarted) may not pass COT_MEMORY_SWAP: unlimited swap then, rather than failing.
swap="${COT_MEMORY_SWAP:--1}"

# Extra host paths (one per line, "path" or "path:rw"), seen at the same path, read-only
# unless :rw.
mounts="${COT_MOUNTS:-}"

# The folder's own home on the host (a bot from before 2.14 passes none: the image's).
# What Claude installs or sets up there (npm -g, pip --user, caches, tool logins)
# outlives the container.
home="${COT_HOME:-}"

# Same image, folder, home, claude binary and extra mounts as the running container, or start
# anew. Limits don't count: docker update changes them in place, without stopping anything.
spec="$COT_IMAGE $PWD $(stat -c %Y "$COT_CLAUDE")"
[[ -z "$home" ]] || spec+=" home:$home"
[[ -z "$mounts" ]] || spec+=" mounts:$(printf %s "$mounts" | md5sum | cut -c1-8)"
current="$(docker container inspect -f '{{.State.Running}} {{index .Config.Labels "cot.spec"}}' "$COT_NAME" 2>/dev/null || true)"
if [[ "$current" != "true $spec" ]]; then
    docker rm -f "$COT_NAME" >/dev/null 2>&1 || true
    args=(run -d --init --name "$COT_NAME"
        --label cot.agent=1 --label "cot.spec=$spec"
        --user "$(id -u):$(id -g)"
        --network host
        --cpus "$COT_CPUS" --memory "$COT_MEMORY" --memory-swap "$swap"
        --pids-limit 4096 --shm-size 1g
        -e HOME="$HOME"
        -v "$PWD:$PWD"
        -v "$COT_CLAUDE:/usr/local/bin/claude:ro"
        -v "$(dirname "$0")/nice-shell.sh:/usr/local/bin/cot-shell:ro")
    if [[ -n "$home" ]]; then
        mkdir -p "$home"
        args+=(-v "$home:$HOME")
    fi
    # A path mounted inside the home needs its mount point there first, made by us:
    # docker would make it as root, and the agent couldn't write next to it.
    bind() {  # bind PATH MODE
        if [[ -n "$home" && "$1" == "$HOME"/* ]]; then
            local point="$home/${1#"$HOME"/}"
            if [[ -d "$1" ]]; then mkdir -p "$point"
            else mkdir -p "$(dirname "$point")"; [[ -e "$point" ]] || touch "$point"; fi
        fi
        args+=(-v "$1:$1:$2")
    }
    mkdir -p "$HOME/.claude"
    bind "$HOME/.claude" rw
    [[ -f "$HOME/.claude.json" ]] && bind "$HOME/.claude.json" rw
    for p in .gitconfig .ssh .config/gh; do
        [[ -e "$HOME/$p" ]] && bind "$HOME/$p" ro
    done
    # Docker mounts parents first, so the conversation's folder stays writable inside one.
    while IFS= read -r m; do
        mode=ro
        [[ "$m" == *:rw ]] && { m="${m%:rw}"; mode=rw; }
        [[ -n "$m" && -e "$m" ]] && bind "$m" "$mode"
    done <<< "$mounts"
    docker "${args[@]}" "$COT_IMAGE" sleep infinity >/dev/null
else
    docker update --cpus "$COT_CPUS" --memory "$COT_MEMORY" --memory-swap "$swap" "$COT_NAME" >/dev/null
fi

# A Claude from an earlier connection may still run: docker exec doesn't stop it when
# the bot goes away. Two on the same conversation answer into the void, so end it.
docker exec "$COT_NAME" sh -c 'pkill -x claude || exit 0
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -x claude >/dev/null || exit 0; sleep 0.5; done
    pkill -9 -x claude' >/dev/null 2>&1 || true

# Only what Claude needs from the bot's environment: never the Telegram token.
exec_args=(exec -i -w "$PWD")
# Binaries from mounted folders first, then those installed in the home (npm -g goes
# there too), then the image's own.
path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
[[ -z "$home" ]] || { path="$HOME/.local/bin:$path"; exec_args+=(-e "NPM_CONFIG_PREFIX=$HOME/.local"); }
[[ -z "${COT_PATH:-}" ]] || path="$COT_PATH:$path"
[[ "$path" == /usr/local/sbin:* ]] || exec_args+=(-e "PATH=$path")
# Commands below Claude in priority (containers from before 2.7 lack the script).
docker exec "$COT_NAME" test -x /usr/local/bin/cot-shell \
    && exec_args+=(-e CLAUDE_CODE_SHELL_PREFIX=/usr/local/bin/cot-shell)
while IFS= read -r name; do
    case "$name" in
        CLAUDE*|ANTHROPIC*|MCP_*|DISABLE_*|MAX_*|BASH_*|OTEL_*|LANG|LC_*|TZ|TERM|*_PROXY|*_proxy)
            exec_args+=(-e "$name") ;;
    esac
done < <(compgen -e)

exec docker "${exec_args[@]}" "$COT_NAME" claude "$@"
