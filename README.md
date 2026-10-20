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
./run.sh /path/to/project   # detached; default: current directory
tail -f state/cot.log
./stop.sh
```

`run.sh` refuses to start a second instance and rotates the log past 10 MB.

## Using it

Write to the bot as you would in the terminal. Messages sent while Claude is
working are queued. Tool calls show up as a compact, live-updated list
(`/tools hidden` to see only Claude's replies).

| Command    | What it does                                         |
|------------|------------------------------------------------------|
| `/new`     | Start a fresh conversation                           |
| `/stop`    | Interrupt Claude and drop queued messages            |
| `/tools`   | Tool activity: hidden, calls, or calls with output   |
| `/mode`    | Permission mode (default, acceptEdits, plan, auto…)  |
| `/model`   | Switch model                                         |
| `/status`  | Workdir, mode, model, activity, context usage        |
| `/cost`    | Estimated cost of the conversation                   |
| `/…`       | Anything else is passed to Claude (e.g. `/compact`)  |

When Claude needs permission (the same cases where the terminal would ask), you
get the request with ✅ Allow / ❌ Deny / ♾️ Always buttons. "Always" saves the
rule to the project's `.claude/settings.local.json`, like the terminal does.
Unanswered requests are denied after 15 minutes. In plan mode, the plan arrives
with Approve / Keep planning buttons.

Photos, documents, videos and audio you send are saved to `.tg-uploads/` in the
workdir (ignored by git) and Claude is told where they are; it can view images
and read text files with its own tools. Albums arrive as a single message. The
Bot API caps downloads at 20 MB.

Conversations survive restarts (they are resumed from `state/sessions.json`).

## Confinement

Claude can read anywhere but write only inside the directory given at launch.
Its file-writing tools (Write, Edit, NotebookEdit) are denied any path outside
it, whatever the permission mode. Bash is not covered by that check: set
`SANDBOX_BASH=true` so Bash commands can only write inside the directory too.

The sandbox needs unprivileged [bubblewrap](https://github.com/containers/bubblewrap),
which Ubuntu 24.04+ blocks through AppArmor. To allow it for bwrap only:

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

## Ideas for later

- A systemd user service (with `loginctl enable-linger`) to start on boot and restart on failure.
- Parallel conversations, one per Telegram forum topic (state is already keyed by topic).
- Local voice-note transcription (e.g. whisper), so no Claude tokens are spent on it.
- Letting Claude send files back (a small MCP tool).

## License

MIT
