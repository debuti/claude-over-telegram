# claude-over-telegram

Talk to [Claude Code](https://code.claude.com) from Telegram. A small Python bot
drives Claude Code through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) and relays
its output to a Telegram chat, so you can keep working on a project from your phone.

Personal-use tool: it runs on your machine with your own Claude login, and only
the Telegram users you allow can talk to it.

## Setup

1. Log in to Claude Code on the machine (`claude`, then `/login`), or set
   `ANTHROPIC_API_KEY`.
2. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy its token.
3. Install:
   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # then set TELEGRAM_TOKEN
   ```
4. Find your Telegram user id: start the bot with `ALLOWED_USER_IDS` empty, send it a
   message, and read `ignored update from user id=…` in the log. Put the id in
   `ALLOWED_USER_IDS` and restart.

## Running

```sh
.venv/bin/python -m cot /path/to/project   # default: current directory
```

To keep it running after closing an SSH session:

```sh
setsid nohup .venv/bin/python -m cot /path/to/project >> cot.log 2>&1 &
```

## Using it

Write to the bot as you would in the terminal. Messages sent while Claude is
working are queued. Tool calls show up as a compact, live-updated list.

| Command    | What it does                                         |
|------------|------------------------------------------------------|
| `/new`     | Start a fresh conversation                           |
| `/verbose` | Show or hide tool results                            |
| `/mode`    | Permission mode (default, acceptEdits, plan, auto…)  |
| `/…`       | Anything else is passed to Claude (e.g. `/compact`)  |

When Claude needs permission (the same cases where the terminal would ask), you
get the request with ✅ Allow / ❌ Deny / ♾️ Always buttons. "Always" saves the
rule to the project's `.claude/settings.local.json`, like the terminal does.
Unanswered requests are denied after 15 minutes. In plan mode, the plan arrives
with Approve / Keep planning buttons.

Conversations survive restarts (they are resumed from `state/sessions.json`).

## Confinement

Claude works in the directory given at launch. Its file tools (Read, Write,
Edit, Glob, Grep…) are denied any path outside it, whatever the permission mode.
Bash is not covered by that check: either keep `PERMISSION_MODE=default` and
review commands, or set `SANDBOX_BASH=true` so Bash can only write inside the
directory. The sandbox needs unprivileged
[bubblewrap](https://github.com/containers/bubblewrap); Ubuntu 24.04+ blocks it
by default through AppArmor (`kernel.apparmor_restrict_unprivileged_userns=1`).

## License

MIT
