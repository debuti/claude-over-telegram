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
.venv/bin/python -m cot /path/to/workdir   # default: current directory
```

The plain chat with the bot works in the workdir itself. Topics (see
[Parallel conversations](#parallel-conversations)) each work in one first-level
folder of it, so point the workdir at your folder of projects to use them.

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
| `/help`    | List commands                                        |
| `/cwd`     | Folder of this topic (a first-level folder)          |
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

Claude can send you files too (images show inline), through a `send_file`
tool the bot gives it. Just ask: "send me the build log".

## Parallel conversations

Each Telegram topic is an independent conversation, with its own Claude
process, queue, directory, mode and model. Use either:

- **Topics in the private chat with the bot**: turn on *Threaded Mode* for the
  bot in @BotFather, then create topics in the chat.
- **A forum group**: create a group, enable Topics, add the bot as an admin
  (so it sees all messages).

Each topic works in one first-level folder of the workdir. A topic named like a
folder starts there; otherwise the bot lists the folders and holds your messages
until you pick one. `/cwd` changes it later (and starts a new conversation).
Writes are confined to the topic's folder. Idle
conversations stop their Claude process after 30 minutes and resume on the next
message.

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
- Local voice-note transcription (e.g. whisper), so no Claude tokens are spent on it.

## License

MIT
