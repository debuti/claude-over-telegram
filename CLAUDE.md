# claude-over-telegram

A Telegram bot that relays each chat topic to its own Claude conversation (Agent SDK),
one project folder per topic. Public on GitHub (MIT); the README is the user manual and
lists what each file does (Contributing).

## Releasing

Every change ships as: version bump in `cot/__init__.py` (feature → minor, fix → patch,
breaking → major), commit, tag `vX.Y.Z`, push to origin with the tag. Code, comments,
commits and README in English. The running bot restarts itself onto a new version once
every conversation is asleep and no agent container runs: don't restart it by hand.

## Checks

`.venv/bin/ruff check .` and `.venv/bin/python -m pytest -q tests` (CI: Python 3.10 and
3.13, so stay 3.10-compatible). `ruff format` is not applied to the codebase: don't
reformat files wholesale.

## Design rules

- **Cost first.** Every API call re-reads the whole context, so: keep what goes into
  every conversation's prompt (`APPEND_PROMPT`, `DOCKER_PROMPT`, tool descriptions in
  `tg_tools.py`) short; agents must never poll; the prompt cache's life (`costguard`)
  drives keep-alives, cache alarms, the wake warning and compaction. A keep-alive is only
  worth it while it costs less than re-sending the context cold (`costguard.worth_keeping`,
  `costguard.reserve`).
- **The user approves what agents do outward or on their own.** Tools that reach beyond
  the conversation (`send_to_agents`, `set_alarm`) ask by themselves with Telegram buttons,
  in every permission mode, and return at once: nothing waits on the button. Their
  pending state lives in `ConvState` (`store.py`) so buttons survive restarts.
- **Containers (SANDBOX_DOCKER).** Each conversation's Claude runs in its own container
  (`container.py`, `claude-docker.sh`), with no Docker socket on purpose. The container
  outlives Claude while something runs in it (detached jobs), and is never removed while
  an approved alarm is pending (`Session.alarm_set`, `container.prepare(keep=...)`).
- **Alarms (`set_alarm`).** Up to 24 h, at most 3 per conversation. Approved: the bot
  ends Claude's process (`Session.sleep`), keeps the container, keeps the cache warm
  until it rings when cheaper, and the topic shows ⏰. Ringing skips the wake warning but
  not plan limits.
- Telegram texts the bot writes are short and plain; topic names are a status line
  (`titles.py`).
