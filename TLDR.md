claude-over-telegram: a Telegram bot to talk to Claude Code from the phone. Each topic is one conversation about one project folder, each in its own Docker container.

- cot/bot.py: the bot itself: commands (/menu, /model, /catchup, /tldr…), buttons, and messages that wait for plan limits, the login or your OK
- cot/session.py: one Claude Code session per topic, and what it streams back
- cot/output.py, render.py: turn Claude's replies and tool activity into Telegram messages
- cot/costguard.py, usage.py, triage.py: cache cost warnings, plan usage and its reserve, and a small model that picks where a message to a cold conversation goes
- cot/tg_tools.py, permissions.py, uploads.py: what Claude gets from the bot (send_file, agents, alarms), permission buttons, files you send
- cot/jail.py: keeps Claude's writes inside the workdir
- cot/container.py, server.py: the per-project containers and the machine's load
- cot/history.py, store.py, titles.py: past conversations, saved state, topic names
- claude-for-inner-agents.md: what every agent is told about the bot
