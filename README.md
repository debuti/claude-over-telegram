# claude-over-telegram

[![check](https://github.com/debuti/claude-over-telegram/actions/workflows/check.yml/badge.svg)](https://github.com/debuti/claude-over-telegram/actions/workflows/check.yml)

Talk to [Claude Code](https://code.claude.com) from Telegram. A small Python bot
drives Claude Code through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) and relays
its output to a Telegram chat, so you can keep working on your projects from
your phone.

It is a personal tool: it runs on your own machine with your own Claude login,
and only the Telegram users you allow can talk to it.

## Features

- Full Claude Code sessions (same tools, permissions, `CLAUDE.md`, skills and
  MCP servers as in the terminal), resumed across restarts.
- Pick up past conversations, including ones started in the terminal (`/list`, `/resume`).
- Live, compact view of tool calls, todo lists and plans.
- Permission requests as ✅ Allow / ❌ Deny / ♾️ Always buttons.
- Several conversations at once, one per Telegram topic, each in its own project folder.
- Send photos and files to Claude; Claude can send files back.
- Writes confined to the workdir (and optionally Bash too, through a sandbox).
- `/usage` shows your plan limits, `/cost` the API-price estimate of a conversation.
  Every turn is logged to `state/usage.jsonl` with its cost and the plan limits
  before and after it, and with how the prompt cache stood (idle time, what the
  first call read from the cache and wrote to it, `cache_break` when the cache was
  lost before its TTL); `python -m cot.usagelog` sums it up per conversation.

## Requirements

- Linux (macOS should work but is untested; `install-service.sh` is Linux-only).
- Python 3.10 or newer.
- A Claude subscription (Pro/Max) or an Anthropic API key. The Claude Code CLI
  comes bundled with the SDK, but you need to log in once (see below).
- A Telegram account.
- Docker, usable by the user the bot runs as (or set `SANDBOX_DOCKER=false`).

## Installation

### 1. Get the code

```sh
git clone https://github.com/debuti/claude-over-telegram.git
cd claude-over-telegram
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Log in to Claude

If you already use Claude Code on this machine, you are done. Otherwise run
`claude` and then `/login` once (install it with
`curl -fsSL https://claude.ai/install.sh | bash` if you don't have it).

To pay per use instead of a subscription, set `ANTHROPIC_API_KEY` in `.env`
(`/usage` is then not available: API keys have no plan limits).

### 3. Create the Telegram bot

1. Open [@BotFather](https://t.me/BotFather), send `/newbot` and follow the steps.
2. Copy the token it gives you.
3. Optional, for [parallel conversations](#parallel-conversations) in the
   private chat: in BotFather, open your bot's settings and turn on *Threaded Mode*.

### 4. Configure

```sh
cp .env.example .env
```

Edit `.env` and set `TELEGRAM_TOKEN`. Leave `ALLOWED_USER_IDS` empty for now.

### 5. Allow yourself

The bot ignores everyone not in `ALLOWED_USER_IDS`. To find your user id:

```sh
.venv/bin/python -m cot ~/projects
```

Send any message to your bot and look for this line in the output:

```
ignored update from user id=123456789 username=you
```

Stop the bot (Ctrl+C), put the id in `.env` (`ALLOWED_USER_IDS=123456789`;
several ids are comma-separated) and start it again. Send `/help` to check it answers.

### 6. Keep it running

Pick one of the options in [Running](#running). The systemd service is the
recommended one: it starts on boot and restarts after a crash.

## Configuration

All settings live in `.env` next to the code (see `.env.example`):

| Variable           | Default    | What it does                                                    |
|--------------------|------------|-----------------------------------------------------------------|
| `TELEGRAM_TOKEN`   | (required) | Bot token from @BotFather                                       |
| `ALLOWED_USER_IDS` | (empty)    | Comma-separated Telegram user ids allowed to use the bot        |
| `PERMISSION_MODE`  | `auto`     | Initial permission mode: `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions` |
| `AUTO_COMPACT_TOKENS` | `200000` | Context size that compacts a conversation before its next message; `0` turns it off |
| `CACHE_TTL`        | `auto`     | Prompt cache lifetime: `auto` (1 hour on a subscription, 5 minutes with an API key), `5m` or `1h` (passed to Claude too) |
| `SANDBOX_DOCKER`   | `true`     | Run each conversation's Claude in its own container (see [Confinement](#confinement)) |
| `DOCKER_CPUS`      | `2`        | CPUs each container may use                                     |
| `DOCKER_MEMORY`    | `4g`       | Memory each container may use                                   |
| `DOCKER_LIMITS`    | (empty)    | Per-folder limits, `folder=cpus[:memory],…` (e.g. `web=4:6g`); applied to running containers too |
| `DOCKER_MOUNTS`    | (empty)    | Extra host paths containers see, read-only unless `:rw`: `folder=path[+path],…`, `*` for every folder (e.g. `*=~/tools+~/shared:rw,web=~/workspace`); containers are recreated to add them |
| `DOCKER_PATH`      | (empty)    | Directories put first on the containers' PATH, `:`-separated (e.g. `~/tools/node/bin:~/tools/jdk/bin`) |
| `DOCKER_EXCLUDE`   | (empty)    | Comma-separated folders whose conversations run on the host, outside Docker |
| `SANDBOX_BASH`     | `false`    | Without Docker: confine Bash writes to the workdir too          |
| `CLAUDE_CLI_PATH`  | (bundled)  | Use this `claude` binary instead of the one bundled with the SDK |
| `STATE_DIR`        | `./state`  | Where sessions, lock, pid and log files live                    |

The **workdir** is given on the command line, not in `.env`: it is the folder
Claude works in and may write to. Point it at your folder of projects to use
topics, each of which works in one project inside it.

## Running

Choose one.

### Foreground (for trying it out)

```sh
.venv/bin/python -m cot /path/to/workdir   # default: current directory
```

### Background, by hand

Keeps running after you close the SSH session, but not across reboots:

```sh
./run.sh /path/to/workdir
tail -f state/cot.log
./stop.sh
```

`run.sh` refuses to start a second instance and rotates the log past 10 MB.

### systemd user service (recommended)

```sh
./install-service.sh /path/to/workdir
```

This writes `~/.config/systemd/user/claude-over-telegram.service`, enables it
and starts it. Run it from the shell you normally work in: it copies its `PATH`
into the service, so Claude finds the same tools (node, git, …) as in your
terminal. Use `./install-service.sh --print /path/to/workdir` to see the unit
without installing anything. Stop `run.sh` first if it is running.

User services only run while you are logged in, unless lingering is on. To
start the bot at boot without logging in, run once:

```sh
sudo loginctl enable-linger $USER
```

Day-to-day commands:

```sh
systemctl --user status claude-over-telegram
systemctl --user restart claude-over-telegram    # e.g. after git pull
journalctl --user -u claude-over-telegram -f      # log
```

To change the workdir, run `install-service.sh` again with the new path. To remove it:

```sh
systemctl --user disable --now claude-over-telegram
rm ~/.config/systemd/user/claude-over-telegram.service
systemctl --user daemon-reload
```

## Using it

Write to the bot as you would in the terminal. Messages sent a few seconds
apart reach Claude together, as one, so a long text Telegram splits in pieces
isn't answered piece by piece; those sent while Claude is working are queued
and merged the same way. By default you only see Claude's replies; `/tools calls`
shows tool calls as a compact, live-updated list. While Claude works the chat
shows "typing…", also when it picks up something on its own (a background job
finishing). A 👀 reaction marks each message once Claude has it; messages still
waiting when the bot restarts are passed on when it is back.

| Command    | What it does                                         |
|------------|------------------------------------------------------|
| `/help`    | List commands                                        |
| `/cwd`     | Folder of this topic (a first-level folder)          |
| `/new`     | Start a fresh conversation                           |
| `/list`    | Recent conversations in this folder, to resume       |
| `/resume`  | `/resume <number or id>`: go back to one of them     |
| `/stop`    | Interrupt Claude and drop queued messages            |
| `/tools`   | Tool activity: hidden, calls, or calls with output   |
| `/mode`    | Permission mode (default, acceptEdits, plan, auto…)  |
| `/model`   | Switch model                                         |
| `/status`  | Workdir, mode, model, activity, context usage        |
| `/cost`    | Estimated cost of the conversation                   |
| `/usage`   | Plan usage: 5-hour and weekly limits                 |
| `/server`  | Machine health: load, CPU, memory, disk, busiest processes |
| `/…`       | Anything else is passed to Claude (e.g. `/compact`)  |

When Claude needs permission (the same cases where the terminal would ask), you
get the request with ✅ Allow / ❌ Deny / ♾️ Always buttons. "Always" saves the
rule to the project's `.claude/settings.local.json`, like the terminal does.
Unanswered requests are denied after 15 minutes. In plan mode, the plan arrives
with Approve / Keep planning buttons.

Photos, documents, videos and audio you send are saved to `.tg-uploads/` in the
working folder (ignored by git) and Claude is told where they are; it can view
images and read text files with its own tools. Albums arrive as a single
message. The Bot API caps downloads at 20 MB.

Claude can send you files too (images show inline), through a `send_file`
tool the bot gives it. Just ask: "send me the build log".

## Parallel conversations

Each Telegram topic is an independent conversation, with its own Claude
process, queue, folder, mode and model. Use either:

- **Topics in the private chat with the bot**: turn on *Threaded Mode* for the
  bot in @BotFather, then create topics in the chat.
- **A forum group**: create a group, enable Topics, add the bot as an admin
  (so it sees all messages).

The plain chat (outside any topic) works in the workdir itself. Each topic
works in one first-level folder of it: a topic named like a folder starts
there; otherwise the bot lists the folders and holds your messages until you
pick one. `/cwd` changes it later (and starts a new conversation). Writes are
confined to the topic's folder.

Idle conversations stop their Claude process after 55 minutes, just before the
subscription's 1-hour prompt cache expires (30 minutes with an `ANTHROPIC_API_KEY`,
whose cache lasts 5), or once the background commands Claude is waiting for end
(6 hours at most). The next message
after that doesn't just carry on: the bot holds it and asks whether to continue
that conversation, compact it and continue, switch to another one from the list
or start a new one, since every message to a long conversation re-sends all of
it. A conversation whose context passes `AUTO_COMPACT_TOKENS` (200k) is compacted
(`/compact`) before its next message, or before its process stops, while the cache
still holds it. Conversations survive
restarts (`state/sessions.json`).

### Past conversations

`/list` shows the last 10 conversations of the chat's folder, the same ones
`claude -r` offers there, whether they were started from Telegram or from the
terminal. Tap a number (or send `/resume 3`, or `/resume <id prefix>`) to switch
to it with its full context; `/new` starts a fresh one. Don't keep the same
conversation open in the terminal and in Telegram at once: both would append to
the same transcript.

## Confinement

By default (`SANDBOX_DOCKER=true`) each conversation's Claude runs in its own
Docker container, together with everything it runs (Bash, builds, tests…). The
container sees only:

- the conversation's folder (the topic's folder, or the whole workdir), at the
  same path and with your uid, so files keep their owner;
- `~/.claude` and `~/.claude.json`, for the login and for `/resume`;
- `~/.gitconfig`, `~/.ssh` and `~/.config/gh`, read-only, so it can push. This
  is a hole in the isolation: whatever your SSH key and `gh` can reach, Claude
  can too;
- a home of its own, `state/homes/<folder>` on the host, shared by the folder's
  conversations. What Claude installs there (`npm i -g`, `~/.local/bin`, which
  is on its PATH), tool settings and caches outlive the container. System
  packages (`apt`) don't: it isn't root; those go in the Dockerfile;
- the paths in `DOCKER_MOUNTS`, at the same path. Credentials you want every
  conversation to use go there, once, from your own home: they show up inside
  each folder's home (e.g. `*=~/.config/.wrangler:rw` to deploy to Cloudflare;
  `:rw` because wrangler refreshes its token). Same hole as above.

A conversation's container outlives its Claude: when Claude stops (after 30
minutes without messages, or when the bot restarts) whatever it started
detached, like a long test suite or a dev server, keeps running, and the next
message brings Claude back into the same container. The bot removes containers
with nothing left running in them, on idle and when it starts. Claude is told to
start long jobs detached (`setsid nohup … &`), since commands it runs in the
foreground stop with it, and to wait for them in one background command instead
of checking on them over and over (each check re-sends the whole conversation).

`./containers.sh` lists them with their limits and current use (so does
`/server`, in the chat). Containers are named `cot-<topic or folder>-<topic id>`, so `docker ps` shows
which is which (topics created or renamed before 2.2 show their folder instead).
Claude's commands, and whatever they start, run at a lower CPU priority than
Claude itself (`nice 10`), so a container busy with tests still answers.
Each is capped at `DOCKER_CPUS` and `DOCKER_MEMORY`, so a runaway test suite can't
make the machine (and the bot) unresponsive. It shares the host network, so dev
servers and local services are reachable as usual, but it has no Docker socket:
it can't start containers of its own. The image (`docker/Dockerfile`) is based
on Playwright's, with Node, Python, git, gh and browsers; it is built the first
time the bot starts, and again whenever the Dockerfile changes. Add there
whatever your projects need. The bot user must be able to run `docker`.

Without Docker (`SANDBOX_DOCKER=false`), Claude runs as your user on the host.
It can read anywhere but write only inside the workdir (or the topic's
folder). Its file-writing tools (Write, Edit, NotebookEdit) are denied any path
outside it, whatever the permission mode. Bash is not covered by that check:
set `SANDBOX_BASH=true` so Bash commands can only write inside it too.

The sandbox needs unprivileged [bubblewrap](https://github.com/containers/bubblewrap)
(`sudo apt install bubblewrap`), which Ubuntu 24.04+ blocks through AppArmor.
To allow it for bwrap only:

```sh
sudo tee /etc/apparmor.d/bwrap <<'AA'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
AA
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

`bypassPermissions` mode skips all of this for Bash: avoid it unless the
sandbox is on.

## Security

Read this before exposing the bot to anyone but yourself.

- **Allowed users run code on your machine.** Anyone in `ALLOWED_USER_IDS`
  can make Claude run commands with your user's rights and spend your Claude
  plan. Only add people you would hand your SSH session to.
- **Keep the token secret.** The bot token in `.env` is enough to impersonate
  the bot (though not to use it: messages from unknown users are ignored).
  If it leaks, revoke it in @BotFather (`/revoke`).
- **What you send goes through Telegram.** Messages, files and Claude's
  replies (code included) are stored on Telegram's servers like any other
  chat. Bot chats are not end-to-end encrypted.
- **Permission modes still apply.** In `default` mode every risky action asks
  for your OK; `auto` and `bypassPermissions` don't. With either, keep
  `SANDBOX_DOCKER` on, or turn on `SANDBOX_BASH` (see [Confinement](#confinement)).

## Updating

```sh
git pull
.venv/bin/pip install -r requirements.txt
systemctl --user restart claude-over-telegram   # or ./stop.sh && ./run.sh …
```

## Troubleshooting

- **The bot doesn't answer.** Check the log (`journalctl --user -u
  claude-over-telegram -f` or `state/cot.log`). `ignored update from user id=…`
  means that id is missing from `ALLOWED_USER_IDS`.
- **`409 Conflict` / "another instance is already running".** Only one copy can
  poll a bot token. Stop the other one (`./stop.sh`, or the service).
- **Claude says it is not logged in.** Run `claude` on the machine and `/login`.
- **`/usage`: "login expired".** The token is refreshed whenever Claude runs;
  send any message and try again.
- **Commands work in the terminal but not through the bot.** The service has a
  different `PATH`; rerun `install-service.sh` from a shell where they work.
- **Topics don't show up in the private chat.** Turn on *Threaded Mode* for the
  bot in @BotFather.

## Contributing

Issues and pull requests are welcome. The code is small:

| File                 | What it does                                               |
|----------------------|------------------------------------------------------------|
| `cot/bot.py`         | Telegram side: auth, commands, routing chats to sessions  |
| `cot/session.py`     | One Claude conversation through the Agent SDK             |
| `cot/output.py`      | Sends a conversation's output to its chat (live activity) |
| `cot/render.py`      | Markdown to Telegram HTML, one-line tool call summaries   |
| `cot/permissions.py` | Permission prompts as buttons                              |
| `cot/jail.py`        | Hook that keeps file writes inside the working folder     |
| `cot/usage.py`       | Plan usage for `/usage`                                    |
| `cot/usagelog.py`    | Per-turn log of cost and plan usage (`state/usage.jsonl`) |
| `cot/costguard.py`   | What the next message costs: cache TTL, cold/warm estimates |
| `cot/uploads.py`, `cot/tg_tools.py` | Files from and to Telegram                  |
| `cot/store.py`, `cot/config.py`     | Saved conversations, settings               |

Run `ruff check .` and `pytest` (`pip install pytest pytest-asyncio`) before sending
changes; CI runs both on Python 3.10 and 3.13. `COT_DEBUG_TTL_SECONDS` makes the bot
assume a shorter cache lifetime (Claude's own is untouched), to try out what depends
on it in minutes.

## Ideas for later

- Local voice-note transcription (e.g. whisper), so no Claude tokens are spent on it.

## License

MIT
