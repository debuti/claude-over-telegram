"""Telegram side: authorization, commands and routing messages to sessions."""

import asyncio
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import (
    __version__,
    container,
    costguard,
    history,
    login,
    permissions,
    server,
    tg_tools,
    titles,
    triage,
    uploads,
    usage,
    usagelog,
)
from .config import REPO_DIR, Config
from .output import TelegramOutput
from .render import esc
from .session import Session, transcript_context, transcript_cost
from .store import ConvState, Key, Store

log = logging.getLogger(__name__)

MODES = {
    "default": "ask before edits and commands",
    "acceptEdits": "edit files freely, ask for commands",
    "plan": "read and plan only, no changes",
    "auto": "a classifier approves or denies each call",
    "bypassPermissions": "never ask (Bash is not confined!)",
}

TOOL_LEVELS = {
    "hidden": "🔇 only Claude's replies",
    "calls": "🔧 tool calls (and errors)",
    "full": "🔍 tool calls and their output",
}

COMMANDS = [
    ("menu", "settings and commands with buttons"),
    ("help", "list commands"),
    ("new", "start a fresh conversation"),
    ("sleep", "let this conversation sleep: no cache warnings or keep-alives"),
    ("list", "recent conversations in this folder"),
    ("older", "older conversations in this folder"),
    ("resume", "go back to a past conversation"),
    ("stop", "interrupt Claude and drop queued messages"),
    ("cwd", "directory this conversation works in"),
    ("tools", "how much tool activity to show"),
    ("mode", "permission mode"),
    ("model", "switch model"),
    ("catchup", "go on by itself when a plan limit lifts"),
    ("status", "what's going on"),
    ("tldr", "what this project is and what each part does"),
    ("cost", "estimated cost of this conversation"),
    ("usage", "plan usage limits (5-hour and weekly)"),
    ("login", "renew Claude's login from here"),
    ("server", "load of the machine the bot runs on"),
]

# /tldr: the summary Claude keeps at the root of each project, and how much of it fits a message.
TLDR_FILE = "TLDR.md"
TLDR_MAX = 3500

# What the CLI keeps between its auto-compact window and the size it compacts at: room for
# the summary (min(max output, 20k)) plus 13k; checked with get_context_usage on 0.2.168.
COMPACT_BUFFER = 33_000

# A plan limit that doesn't say when it resets: seconds until the bot looks again.
LIMIT_RETRY = 5 * 60

# How a message from another conversation's agent reaches Claude; never as a bare text, which could be a /command.
AGENT_MESSAGE = (
    "[From the agent of the project {sender}, through the Telegram bot. The user let it through but didn't "
    "write it. To answer: send_to_agents to {sender}.]\n\n{text}"
)
AGENT_SEND_OUTCOME = "[From the Telegram bot, about your message to other agents: {outcome}. It said:]\n\n{text}"
AGENT_TEXT_LIMIT = 3000  # with the prompt around it, what fits in a Telegram message once escaped
# What an alarm Claude asked for (set_alarm) brings it when it rings.
WAKEUP_MESSAGE = "[From the Telegram bot: the alarm you asked for {when} ago rang. You set it to: {reason}]"
# Keep-alives for an alarm keep the cache this long past it: the alarm loop looks every 15 s.
WAKEUP_SLACK = 2 * 60
USAGE_FRESH = 30  # seconds a plan-usage reading still does for /usage
WAKEUPS_MAX = 3  # asked for or set at once, per conversation
RESERVE_NAMES = {"session": "5-hour", "weekly_all": "weekly"}
VERSION_NOTICE_TRIES = 3  # conversations to try the "new version" notice in

# What goes to Claude, ahead of any waiting messages, to carry on with a turn a lost login cut short.
REFUSED_CONTINUE_PROMPT = (
    "[From the Telegram bot: an error reaching Anthropic's servers cut your last turn short. "
    "Carry on with what you were doing.]"
)
# Waits before trying again after an "authentication failed" with a login that looks fine; then it's /login.
REFUSED_RETRIES = (120, 300, 600, 900, 1800, 1800)  # about 1h30 in all: an outage of the login servers
LOGIN_CONTINUE_PROMPT = (
    "[From the Telegram bot: your login expired in the middle of your last turn and has now been renewed. "
    "Carry on with what you were doing.]"
)

# Texts of the latest messages, to tell an edit that changed nothing.
KEEP_TEXTS = 500

# What goes to Claude, ahead of any waiting messages, to carry on with a turn a limit cut short.
CONTINUE_PROMPT = (
    "[From the Telegram bot: a plan limit cut your last turn short and has now lifted. "
    "Carry on with what you were doing.]"
)

# /catchup's choices, as stored in ConvState.catchup.
CATCHUP = {
    "": "ask before going on",
    "send": "go on by itself",
    "compact": "compact, then go on by itself",
}
CATCHUP_BUTTONS = {"send": "⏰ Go on by itself", "compact": "🗜 Compact, then go on", "": "❓ Ask me"}
CATCHUP_ARGS = {"auto": "send", "compact": "compact", "ask": ""}

# /menu's buttons that run a command, as (label, command); the settings come before them.
MENU_COMMANDS = {
    "status": "status",
    "cost": "cost",
    "usage": "usage",
    "new": "new",
    "sleep": "sleep",
    "list": "resume",
    "server": "server",
    "stop": "stop",
    "help": "help",
    "tldr": "tldr",
}
MENU_ACTIONS = [
    [("📊 Status", "status"), ("💰 Cost", "cost"), ("📈 Usage", "usage")],
    [("📝 TLDR", "tldr"), ("🆕 New", "new"), ("📜 Past", "list"), ("😴 Sleep", "sleep")],
    [("🖥 Server", "server"), ("❔ Help", "help"), ("🛑 Stop", "stop"), ("✖️ Close", "close")],
]

MODELS = {
    "claude-fable-5-2": "Fable 5.2",
    "claude-fable-5-1": "Fable 5.1",
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}


def _span(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return " ".join(part for part in (f"{hours} h" * bool(hours), f"{mins} min" * bool(mins)) if part)


def _clock(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


def _inside(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root)
    except OSError:
        return False


def newer_version(repo: Path) -> str | None:
    """The checkout's version if it isn't the one running and it is all committed: not half-way through a change."""
    try:
        found = re.search(r'__version__ = "(.+)"', (repo / "cot" / "__init__.py").read_text())
        if not found or found[1] == __version__:
            return None
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found[1] if status.returncode == 0 and not status.stdout.strip() else None


class Bot:
    def __init__(self, cfg: Config, cli_path: str | None, cli_env: dict[str, str]):
        self.cfg = cfg
        self.cli_path = cli_path
        self.cli_env = cli_env
        self.store = Store(cfg.state_dir / "sessions.json")
        self.sessions: dict[Key, Session] = {}
        self.typing: dict[Key, asyncio.Task] = {}
        self.albums: dict[str, uploads.AlbumCollector] = {}
        # Messages sent to a topic before it has a folder; delivered once one is picked.
        self.no_folder: dict[Key, list[tuple[str, list[int]]]] = {}
        self.texts: dict[tuple[int, int], str] = {}
        self.login: login.Login | None = None
        self.ttl = costguard.cache_ttl(cfg)
        self._restarting = False
        # The cache alarm last sent per conversation: {"at": last_api_at it counts from, "step", "msg_id"}.
        self.alarms: dict[Key, dict] = {}
        self._alarm_task: asyncio.Task | None = None
        # The plan usage the alarms last fetched: (time, data or None if unavailable).
        self._plan: tuple[float, dict | None] = (0.0, None)
        self._usage_later: dict[Key, asyncio.Task] = {}
        self._plan_lock = asyncio.Lock()  # messages arriving during a fetch keep their order
        self._rates = dict(costguard.PLAN_RATES)  # % of each window per equivalent token
        # The reserve _blocked last saw (None: none due), and the reset whose reserve the user spent.
        self._reserve: costguard.Reserve | None = None
        self._reserve_skip = 0.0
        self._sleeping: set[asyncio.Task] = set()  # Claude processes being ended for an alarm
        self._titles_pause = 0.0  # no renames before this: Telegram asked to slow down
        self.usage_log = usagelog.UsageLog(cfg.state_dir / "usage.jsonl", cfg.cli_path or container.bundled_cli())
        self.app = (
            Application.builder()
            .token(cfg.telegram_token)
            .concurrent_updates(True)
            .connect_timeout(20)
            # The 5 s defaults time out when the machine is overloaded.
            .read_timeout(30)
            .write_timeout(30)
            .media_write_timeout(120)
            .get_updates_connect_timeout(20)
            .post_init(self._post_init)
            .post_shutdown(self._shutdown)
            .build()
        )
        allowed = filters.User(user_id=cfg.allowed_user_ids)
        self.app.add_handler(CommandHandler(["start", "help"], self._cmd_help, filters=allowed))
        self.app.add_handler(CommandHandler("cwd", self._cmd_cwd, filters=allowed))
        self.app.add_handler(
            MessageHandler(allowed & filters.StatusUpdate.FORUM_TOPIC_CREATED, self._on_topic_created)
        )
        self.app.add_handler(
            MessageHandler(allowed & filters.StatusUpdate.FORUM_TOPIC_EDITED, self._on_topic_edited)
        )
        self.app.add_handler(CommandHandler("new", self._cmd_new, filters=allowed))
        self.app.add_handler(CommandHandler("sleep", self._cmd_sleep, filters=allowed))
        self.app.add_handler(CommandHandler(["list", "older", "resume"], self._cmd_resume, filters=allowed))
        self.app.add_handler(CommandHandler("menu", self._cmd_menu, filters=allowed))
        self.app.add_handler(CommandHandler("tools", self._cmd_tools, filters=allowed))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode, filters=allowed))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop, filters=allowed))
        self.app.add_handler(CommandHandler("model", self._cmd_model, filters=allowed))
        self.app.add_handler(CommandHandler("catchup", self._cmd_catchup, filters=allowed))
        self.app.add_handler(CommandHandler("status", self._cmd_status, filters=allowed))
        self.app.add_handler(CommandHandler("cost", self._cmd_cost, filters=allowed))
        self.app.add_handler(CommandHandler("tldr", self._cmd_tldr, filters=allowed))
        self.app.add_handler(CommandHandler("usage", self._cmd_usage, filters=allowed))
        self.app.add_handler(CommandHandler("login", self._cmd_login, filters=allowed))
        self.app.add_handler(CommandHandler("server", self._cmd_server, filters=allowed))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        # Unknown /commands fall through to here and reach Claude (e.g. /compact).
        self.app.add_handler(MessageHandler(allowed & filters.TEXT, self._on_text))
        media = filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE
        self.app.add_handler(MessageHandler(allowed & media, self._on_file))
        self.app.add_handler(MessageHandler(~allowed, self._on_stranger))

    def run(self) -> None:
        # Retry forever: the bot runs unattended and must survive network blips at startup.
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)
        if self._restarting:
            os.execv(sys.executable, sys.orig_argv)

    @staticmethod
    def _key(update: Update) -> Key:
        msg, chat = update.effective_message, update.effective_chat
        # In groups, message_thread_id also marks reply threads; only forum topics count.
        # Private chats only carry it when the bot has topics (threaded mode) on.
        in_topic = msg and msg.message_thread_id and (msg.is_topic_message or chat.type == chat.PRIVATE)
        return (chat.id, msg.message_thread_id if in_topic else None)

    def _valid_cwd(self, key: Key, path: Path) -> bool:
        """Topics work in a first-level folder of the workdir; the plain chat works in the workdir."""
        if key[1] is None:
            return path == self.cfg.workdir
        return path.is_dir() and path.parent == self.cfg.workdir and not path.name.startswith(".")

    def _assigned(self, key: Key) -> bool:
        if key[1] is None or key in self.sessions:
            return True
        state = self.store.get(key)
        return state is not None and self._valid_cwd(key, Path(state.cwd))

    def _state(self, key: Key) -> ConvState:
        """Stored state for this chat, or a fresh one if its directory is no longer valid."""
        sess = self.sessions.get(key)
        if sess:
            return sess.state
        state = self.store.get(key)
        if state is None or not self._valid_cwd(key, Path(state.cwd)):
            state = ConvState(cwd=str(self.cfg.workdir), title=state and state.title)
        return state

    async def _ready(self, key: Key) -> bool:
        """False (after showing the folder picker) while a topic has no folder yet."""
        if self._assigned(key):
            return True
        await self._folder_picker(key, "This topic has no folder yet. Pick one:")
        return False

    def _output(self, key: Key) -> TelegramOutput:
        sess = self.sessions.get(key)
        if sess:
            return sess.out
        return TelegramOutput(self.app.bot, *key, Path(self._state(key).cwd))

    def _where(self, key: Key) -> str:
        return self._rel(self._state(key).cwd) if self._assigned(key) else "no folder yet (/cwd)"

    def _rel(self, cwd: str) -> str:
        rel = Path(cwd).relative_to(self.cfg.workdir)
        return f"{self.cfg.workdir.name}/{rel}" if rel != Path(".") else f"{self.cfg.workdir.name}/"

    def _runner(self, cwd: Path) -> dict:
        """How the conversation's Claude starts: in its container, or on the host for excluded folders."""
        if self.cfg.docker and cwd.name not in self.cfg.docker_exclude:
            cpus, memory = self.cfg.docker_limits.get(cwd.name, (self.cfg.docker_cpus, self.cfg.docker_memory))
            env = {**self.cli_env, **container.limits(cpus, memory)}
            env["COT_HOME"] = str(self.cfg.state_dir / "homes" / cwd.name)
            mounts = self.cfg.docker_mounts.get("*", []) + self.cfg.docker_mounts.get(cwd.name, [])
            if mounts:
                env["COT_MOUNTS"] = "\n".join(mounts)
            if self.cfg.docker_path:
                env["COT_PATH"] = self.cfg.docker_path
            return {"cli_path": self.cli_path, "cli_env": {**env, **self._cli_settings()}, "sandbox_bash": False}
        return {"cli_path": self.cfg.cli_path, "cli_env": self._cli_settings(), "sandbox_bash": self.cfg.sandbox_bash}

    def _cli_settings(self) -> dict[str, str]:
        """Settings the bot shares with the CLI, so both act on the same numbers."""
        env = {}
        # An explicit CACHE_TTL goes to the CLI too, so the bot's estimates match its cache.
        if self.cfg.cache_ttl != "auto":
            env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = self.cfg.cache_ttl
        # Claude compacts by itself only near the model's window (967k of Opus's 1M), so a long
        # turn re-reads an ever larger context on each of its hundreds of calls. The CLI fires at
        # its window minus a buffer: give it the window whose threshold is AUTO_COMPACT_TOKENS.
        if self.cfg.compact_at:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self.cfg.compact_at + COMPACT_BUFFER)
        return env

    def _session(self, key: Key) -> Session:
        sess = self.sessions.get(key)
        if sess:
            return sess
        state = self._state(key)
        out = TelegramOutput(self.app.bot, *key, Path(state.cwd))
        out.tools = state.tools
        sess = Session(
            state,
            out=out,
            on_state=lambda st: self.store.put(key, st),
            default_mode=self.cfg.permission_mode,
            compact_at=self.cfg.compact_at,
            # With WAKE=ask whether to compact is the user's to say, when they come back.
            compact_asleep=self.cfg.wake_small if self.cfg.compact_at and self.cfg.wake == "auto" else 0,
            ttl=self.ttl,
            **self._runner(Path(state.cwd)),
        )
        sess.on_delivered = lambda ids: self._acknowledge(key, ids)
        sess.on_active = lambda: self._ensure_typing(key, sess)
        sess.on_turn_start = lambda: self.usage_log.turn_started(key)
        sess.on_turn_end = lambda entry: self._log_turn(key, sess, entry)
        sess.on_turn_failed = lambda: self.usage_log.turn_failed(key)
        sess.on_limited = lambda items, resets, cut: self._on_limited(key, items, resets, cut)
        if not self.cfg.api_key:  # a wrong API key won't come back on its own: its error says so
            sess.on_login_lost = lambda items, cut: self._on_login_lost(key, items, cut)
        sess.agents = tg_tools.Agents(
            overview=lambda: self._list_agents(key),
            send=lambda to, text: self._send_to_agents(key, to, text),
        )
        sess.alarm = lambda minutes, reason: self._ask_wakeup(key, minutes, reason)
        sess.start()
        self.sessions[key] = sess
        if state.pending:
            self._ensure_typing(key, sess)
        return sess

    async def _log_turn(self, key: Key, sess: Session, entry: dict) -> None:
        parallel = sum(1 for k, s in self.sessions.items() if k != key and s.busy)
        name = sess.state.title or Path(sess.state.cwd).name
        await self.usage_log.turn_ended(key, {"conversation": name, **entry, "parallel": parallel})

    def _ensure_typing(self, key: Key, sess: Session) -> None:
        if key not in self.typing:
            self.typing[key] = asyncio.create_task(self._keep_typing(key, sess))

    async def _acknowledge(self, key: Key, ids: list[int]) -> None:
        """👀 on each message once Claude has it: one without it never arrived."""
        for msg_id in ids:
            try:
                await self.app.bot.set_message_reaction(key[0], msg_id, reaction="👀")
            except TelegramError as e:  # reactions off in this chat, message deleted…
                log.debug("reaction failed: %s", e)

    async def _keep_typing(self, key: Key, sess: Session) -> None:
        chat_id, thread_id = key
        while sess.pending:
            if sess.waiting:  # the permission prompt is on screen; Claude isn't "typing"
                await asyncio.sleep(1)
                continue
            try:
                await self.app.bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
            except TelegramError:
                pass
            await asyncio.sleep(4)
        self.typing.pop(key, None)

    def _remember(self, key: Key, text: str, ids: list[int] | None) -> None:
        for msg_id in ids or []:
            self.texts[key[0], msg_id] = text
        while len(self.texts) > KEEP_TEXTS:
            del self.texts[next(iter(self.texts))]

    async def _submit(
        self, key: Key, text: str, ids: list[int] | None = None, check: bool = True, hold: bool = True
    ) -> None:
        """Hand a message to the conversation, unless a warning holds it back first.

        check=False delivers without asking: for messages the user already decided about.
        hold=False skips only the wake warning; plan limits still stop it.
        """
        if not self._assigned(key):
            first = key not in self.no_folder
            self.no_folder.setdefault(key, []).append((text, ids or []))
            if first:
                await self._folder_picker(
                    key, "Pick the folder for this topic; your message goes to Claude right after:"
                )
            return
        self._remember(key, text, ids)
        state = self._state(key)
        if state.asleep:  # a message wakes it up
            state.asleep = False
            self.store.put(key, state)
        # A warning or a plan limit is already holding some, or asking about the cut turn: this one waits too.
        if state.held or state.login_lost_at or (state.limit_cut and not state.limit_until):
            state.held.append({"text": text, "ids": ids or []})
            self.store.put(key, state)
            return
        if check and (state.limit_until or await self._blocked()):
            reserve = None if await self._hard_blocked() else self._reserve
            await self._on_limited(key, [{"text": text, "ids": ids or []}], self._plan_resets(), reserve=reserve)
            return
        if check and hold and self._should_hold(key):
            state.held.append({"text": text, "ids": ids or []})
            self.store.put(key, state)
            await self._wake(key)
            return
        sess = self._session(key)
        await sess.send(text, ids)
        self._ensure_typing(key, sess)

    def _agent_topics(self, key: Key) -> dict[str, Key | None]:
        """The other projects, with the topic that speaks for each in this chat: its latest active, or None."""
        topics: dict[str, Key | None] = dict.fromkeys(self._subdirs())
        for other, state in self.store.items():
            name = Path(state.cwd).name
            if other[0] != key[0] or not other[1] or name not in topics or not self._valid_cwd(other, Path(state.cwd)):
                continue
            current = topics[name]
            if current is None or state.last_active >= self._state(current).last_active:
                topics[name] = other
        topics.pop(Path(self._state(key).cwd).name, None)
        return topics

    def _list_agents(self, key: Key) -> str:
        lines = []
        for name, topic in self._agent_topics(key).items():
            if topic is None:
                status = "no topic yet: writing to it creates one"
            elif (sess := self.sessions.get(topic)) and sess.busy:
                status = "working"
            elif self._should_hold(topic):
                status = "asleep: waking it is up to the user"
            else:
                status = "available"
            lines.append(f"{name}: {status}")
        return "\n".join(lines) or "There are no other projects."

    async def _send_to_agents(self, key: Key, to: list[str], text: str) -> str:
        """Puts the send before the user. It waits for the button in the state, so no turn hangs on it."""
        names = list(dict.fromkeys(to))
        topics = self._agent_topics(key)
        unknown = [n for n in names if n not in topics]
        if unknown or not names:
            listed = ", ".join(unknown) or "(none given)"
            raise ValueError(f"Not a project you can write to: {listed}. See list_agents.")
        if not text.strip() or len(esc(text)) > AGENT_TEXT_LIMIT:
            # Not cut to fit: what the user approves has to be all that goes.
            raise ValueError(f"The message must have some text and fit in {AGENT_TEXT_LIMIT} characters.")
        sender = Path(self._state(key).cwd).name
        shown = ", ".join(esc(n) + ("" if topics[n] else " (new topic)") for n in names)
        out = self._output(key)
        await out.flush()
        html = f"📨 <b>{esc(sender)} wants to write to:</b> {shown}\n<pre>{esc(text)}</pre>"
        send_id = secrets.token_hex(6)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Allow", callback_data=f"agent:{send_id}:{permissions.ALLOW}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"agent:{send_id}:{permissions.DENY}"),
                ]
            ]
        )
        if await out.send_html(html, reply_markup=keyboard) is None:
            raise ValueError("The request couldn't be shown to the user: nothing will be sent.")
        state = self._state(key)
        state.agent_sends.append({"id": send_id, "sender": sender, "to": names, "text": text, "html": html})
        self.store.put(key, state)
        return "Waiting for the user's approval."

    async def _on_agent_send(self, key: Key, query, rest: str) -> None:
        """The button of a send to other agents: it delivers, and tells the sender only what went wrong."""
        send_id, _, choice = rest.partition(":")
        state = self._state(key)
        item = next((i for i in state.agent_sends if i["id"] == send_id), None)
        if item is None:
            await query.answer("This request is no longer active")
            await query.edit_message_reply_markup(None)
            return
        await query.answer()
        state.agent_sends.remove(item)
        self.store.put(key, state)
        names, sender, text = item["to"], item["sender"], item["text"]
        report, failed = [], []
        if choice == permissions.ALLOW:
            topics = self._agent_topics(key)
            for name in names:
                try:
                    if name not in topics:
                        raise ValueError("it can't be written to any more")
                    report.append(f"{name}: {await self._deliver_to_agent(key, name, topics[name], sender, text)}")
                except Exception as e:
                    log.exception("message to the agent of %s failed", name)
                    failed.append(f"{name}: not sent ({e})")
        outcome = f"<b>{permissions.OUTCOME[choice]}</b>"
        lines = "\n".join(report + failed)
        try:
            await query.edit_message_text(
                f"{item['html']}\n\n{outcome}\n{esc(lines)}"[:4096], parse_mode="HTML", reply_markup=None
            )
        except TelegramError as e:
            log.warning("could not update the send's message: %s", e)
        if choice != permissions.ALLOW:
            news = f"the user refused it, nothing was sent to {', '.join(names)}"
        elif failed:
            news = "; ".join(failed)
        else:
            return
        # A conversation moved to another project since isn't the one that wrote it.
        if Path(self._state(key).cwd).name == sender:
            await self._submit(key, AGENT_SEND_OUTCOME.format(outcome=news, text=text))

    async def _deliver_to_agent(self, key: Key, name: str, topic: Key | None, sender: str, text: str) -> str:
        if topic is None:
            created = await self.app.bot.create_forum_topic(key[0], name)
            topic = (key[0], created.message_thread_id)
            self._set_title(topic, name)
            await self._set_cwd(topic, name)
        # Posted first: it's what the user sees of it, and without it (a topic deleted since) nothing goes.
        msg_id = await self._output(topic).send_html(f"📨 <b>From {esc(sender)}</b>\n{esc(text)}")
        if msg_id is None:
            raise ValueError(f"its topic doesn't take messages (deleted? the user can open a new one for {name})")
        await self._submit(topic, AGENT_MESSAGE.format(sender=sender, text=text), [msg_id])
        if any(item["ids"] == [msg_id] for item in self._state(topic).held):
            return "held until the user decides (its conversation is asleep, or a limit stops it)"
        return "delivered"

    async def _ask_wakeup(self, key: Key, minutes: int, reason: str) -> str:
        """Puts the alarm before the user; it's set only once they approve it."""
        reason = reason.strip()
        if not 1 <= minutes <= tg_tools.ALARM_MAX_MINUTES:
            raise ValueError(f"minutes must be between 1 and {tg_tools.ALARM_MAX_MINUTES}.")
        if not reason or len(esc(reason)) > AGENT_TEXT_LIMIT:
            raise ValueError(f"The reason must have some text and fit in {AGENT_TEXT_LIMIT} characters.")
        state = self._state(key)
        if len(state.wakeups) >= WAKEUPS_MAX:
            raise ValueError(f"There are already {WAKEUPS_MAX} alarms asked for or set here; wait for one to ring.")
        out = self._output(key)
        await out.flush()
        now = time.time()
        html = (
            f"⏰ <b>Claude asks to be woken up in {_span(minutes)}</b> (at {_clock(now + minutes * 60)})\n"
            f"<pre>{esc(reason)}</pre>"
        )
        wake_id = secrets.token_hex(6)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Allow", callback_data=f"wakeup:{wake_id}:{permissions.ALLOW}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"wakeup:{wake_id}:{permissions.DENY}"),
                ]
            ]
        )
        if await out.send_html(html, reply_markup=keyboard) is None:
            raise ValueError("The request couldn't be shown to the user: no alarm is set.")
        state = self._state(key)
        state.wakeups.append(
            {
                "id": wake_id,
                "reason": reason,
                "minutes": minutes,
                "cwd": state.cwd,
                "asked_at": now,
                "at": 0.0,
                "html": html,
            }
        )
        self.store.put(key, state)
        return (
            "Asked the user. If they approve it, the bot ends your process (what isn't detached stops) and "
            "a message wakes you when it rings; if they refuse it, nothing rings and you aren't told. "
            "End your turn now; don't check meanwhile."
        )

    async def _on_wakeup(self, key: Key, query, rest: str) -> None:
        """The buttons of an alarm: allow or deny it while asked, cancel it once set."""
        wake_id, _, choice = rest.partition(":")
        state = self._state(key)
        item = next((i for i in state.wakeups if i["id"] == wake_id), None)
        if item is None:
            await query.answer("This alarm is no longer active")
            await query.edit_message_reply_markup(None)
            return
        await query.answer()
        keyboard = None
        if choice == permissions.ALLOW and not item["at"]:
            # Counted from when Claude asked: approved late, it rings at once.
            item["at"] = max(item["asked_at"] + item["minutes"] * 60, time.time())
            # Kept while it's set, even across restarts: the job it waits for may run there.
            sess = self.sessions.get(key)
            label = state.title or Path(state.cwd).name
            item["container"] = sess.container if sess else container.agent_name(label, *key)
            note = f"✅ Allowed: rings at {_clock(item['at'])}"
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔕 Cancel", callback_data=f"wakeup:{wake_id}:cancel")]]
            )
        elif choice == "cancel":
            state.wakeups.remove(item)
            note = "🔕 Cancelled"
        elif choice == permissions.DENY and not item["at"]:
            state.wakeups.remove(item)
            note = permissions.OUTCOME[permissions.DENY]
        else:
            return
        self.store.put(key, state)
        try:
            await query.edit_message_text(
                f"{item['html']}\n\n<b>{note}</b>"[:4096], parse_mode="HTML", reply_markup=keyboard
            )
        except TelegramError as e:
            log.warning("could not update the alarm's message: %s", e)
        if keyboard and (sess := self.sessions.get(key)) and sess.connected:
            # Not left to Claude: whatever it would keep polling with goes with its process.
            task = asyncio.create_task(self._put_to_sleep(key, sess))
            self._sleeping.add(task)
            task.add_done_callback(self._sleeping.discard)

    async def _put_to_sleep(self, key: Key, sess: Session) -> None:
        try:
            if await sess.sleep():
                await self._output(key).notice("⏰ Claude's process ended until the alarm rings; its container stays")
        except Exception:
            log.exception("putting Claude to sleep for its alarm failed")

    async def _check_wakeups(self, key: Key, state: ConvState) -> None:
        """Rings the approved alarms that are due: each reaches Claude as a message."""
        now = time.time()
        due = [i for i in state.wakeups if i["at"] and i["at"] <= now]
        if not due:
            return
        state.wakeups = [i for i in state.wakeups if i not in due]
        self.store.put(key, state)
        for item in due:
            out = self._output(key)
            if item.get("cwd") != state.cwd or not self._assigned(key):
                await out.notice(f"🔕 An alarm for {Path(item.get('cwd', '')).name} didn't ring: the topic moved on")
                continue
            msg_id = await out.send_html(f"⏰ <b>Alarm</b>\n{esc(item['reason'])}")
            when = _span(max(1, round((now - item["asked_at"]) / 60)))
            text = WAKEUP_MESSAGE.format(when=when, reason=item["reason"])
            # The user approved waking it: no wake warning, but plan limits still hold it.
            await self._submit(key, text, [msg_id] if msg_id else None, hold=False)

    def _should_hold(self, key: Key) -> bool:
        """A conversation asleep with its cache gone: where its next message goes is asked.

        Asleep with the cache still warm (Claude sleeps a few minutes early, to compact first),
        going on with it is the cheap choice, so the message goes straight there.
        """
        state = self._state(key)
        sess = self.sessions.get(key)
        if not state.session_id or (sess and (sess.connected or sess.pending)):
            return False
        return costguard.is_cold(time.time(), state.last_api_at or state.last_active, self.ttl)

    async def _on_limited(
        self,
        key: Key,
        items: list[dict],
        resets: float | None,
        cut: bool = False,
        reserve: costguard.Reserve | None = None,
    ) -> None:
        """A plan limit stops Claude, or its reserve: messages wait, in order, until _check_limit sees it gone."""
        state = self._state(key)
        state.held = items + state.held
        if not resets and not state.limit_until and state.limit_auto:
            state.limit_auto = ""  # going on by itself hit it again, with no time to wait for
        elif state.catchup and not state.limit_auto:
            state.limit_auto = state.catchup
        state.limit_until = max(state.limit_until, resets or 0.0) or time.time() + LIMIT_RETRY
        state.limit_cut = state.limit_cut or cut
        self.store.put(key, state)
        self._plan = (0.0, None)  # it didn't foresee this, or the next look is near the reset
        n = len(state.held)
        waiting = [f"{n} message{'s' * (n > 1)} waiting"] if n else []
        what = " and ".join((["Claude's work was cut short"] if state.limit_cut else []) + waiting)
        if items or cut:
            when = f", {usage.until(resets)}" if resets else ""
            text = f"⏸ Plan limit reached{when}. {what}"
            if reserve:
                n = len(reserve.contexts)
                text = (
                    f"⏸ Saving the last {reserve.percent:.0f}% of the {RESERVE_NAMES[reserve.kind]} limit to keep "
                    f"{n} conversation{'s' * (n > 1)} warm until it resets{when}: coming back then costs a tenth. "
                    f"{what}"
                )
            if state.limit_auto:
                always = " (as after every limit)" if state.catchup else ""
                text += f"; once it lifts I'll go on, as you asked{always}. /stop drops it."
                rows = [[("❓ Ask me instead", "catchup-ask")]]
            else:
                text += "; once it lifts I'll ask for your OK to go on, unless you choose now. /stop drops it."
                rows = [[("⏰ Go on this time", "send"), ("🔁 After every limit", "catchup-auto")]]
                if state.context_tokens >= self.cfg.wake_small:
                    kilo = costguard.kilo(state.context_tokens)
                    text += f"\n\nIf its cache has expired by then, going on re-sends {kilo} tokens."
                    rows.append([("🗜 Compact this time", "compact"), ("🗜🔁 After every limit", "catchup-compact")])
            if reserve:
                rows.insert(0, [("▶️ Send it anyway", "anyway")])
            await self._prompt(key, "schedule", text, rows)

    async def _on_login_lost(self, key: Key, items: list[dict], cut: bool) -> None:
        """Claude's login is gone: messages wait, in order, until _check_login sees it back."""
        state = self._state(key)
        first = not state.login_lost_at
        if first and time.time() - state.login_retried_at > 3600:  # a new run of failures
            state.login_retries = 0
        # The login looks fine, so it's the request that was turned down (Cloudflare's "Attention Required"):
        # it's tried again a few times before asking for a /login.
        refused = (first or state.login_retry_at) and state.login_retries < len(REFUSED_RETRIES) and usage.login_valid()
        if state.login_retry_at and not refused:
            first = True  # the retries are over: now it's the login
        state.held = items + state.held
        prompt = REFUSED_CONTINUE_PROMPT if refused else LOGIN_CONTINUE_PROMPT
        if cut and not any(m["text"] in (REFUSED_CONTINUE_PROMPT, LOGIN_CONTINUE_PROMPT) for m in state.held):
            state.held.insert(0, {"text": prompt, "ids": []})
        state.login_lost_at = time.time()
        if refused and (first or not state.login_retry_at):
            wait = REFUSED_RETRIES[state.login_retries]
            state.login_retries += 1
            state.login_retry_at = time.time() + wait
            self.store.put(key, state)
            await self._output(key).notice(
                f"🌩 Anthropic's servers turned the request down (often Cloudflare blocking it for a while), "
                f"though Claude's login looks fine. Trying again in {wait // 60} min "
                f"({state.login_retries}/{len(REFUSED_RETRIES)}); /stop drops it."
            )
            return
        if refused:
            self.store.put(key, state)
            return
        state.login_retry_at = 0.0
        self.store.put(key, state)
        if first:
            n = len(state.held) - cut
            waiting = f" {n} message{'s' * (n > 1)} waiting." if n else ""
            work = " Claude's work was cut short." if cut else ""
            await self._output(key).notice(
                f"🔑 Claude's login has expired.{work}{waiting} Send /login to renew it from here; "
                "all this goes on by itself once it's back. /stop drops it."
            )

    async def _check_login(self, key: Key, state: ConvState) -> None:
        """Once Claude has a login again (from /login or on the machine), what waited for it goes."""
        sess = self.sessions.get(key)
        state = sess.state if sess else state
        if not state.login_lost_at:
            return
        if state.login_retry_at and time.time() >= state.login_retry_at:  # turned down a while ago: try again
            state.login_retried_at = time.time()
        elif not usage.renewed_since(state.login_lost_at):
            return
        else:
            state.login_retries = 0
        retry = bool(state.login_retry_at)
        state.login_lost_at = state.login_retry_at = 0.0
        self.store.put(key, state)
        if state.held:
            await self._output(key).notice("🌩 Trying again" if retry else "🔑 Claude's login is back: going on")
            await self._release(key)

    async def _check_limit(self, key: Key, state: ConvState) -> None:
        """Once the plan limit is gone, ask whether the messages that waited for it still go."""
        sess = self.sessions.get(key)
        state = sess.state if sess else state
        if not state.limit_until or time.time() < state.limit_until or await self._blocked():
            return
        if state.limit_auto and (state.held or state.limit_cut):
            await self._go_on(key, state)
            return
        state.limit_auto = ""
        if (state.held or state.limit_cut) and not await self._prompt(key, "limit", *self._limit_prompt(key, state)):
            return  # looks again in a moment: going on takes the user's OK
        state.limit_until = 0.0
        self.store.put(key, state)

    async def _go_on(self, key: Key, state: ConvState) -> None:
        """The limit lifted and the user had already said what to do. limit_auto stays until a
        turn gets through: if the limit hits again, the next reset goes on too."""
        if state.limit_cut:
            state.limit_cut = False
            state.held.insert(0, {"text": CONTINUE_PROMPT, "ids": []})
        state.limit_until = 0.0
        self.store.put(key, state)
        if state.limit_auto == "compact":
            self._session(key).compact_next = True
        await self._output(key).notice("✅ The plan limit has lifted: going on, as you asked")
        await self._release(key)

    def _limit_prompt(self, key: Key, state: ConvState) -> tuple[str, list[list[tuple[str, str]]]]:
        """The question once the limit is gone. Going on with a cut turn stays in this conversation,
        so what the cold cache costs is decided here, instead of after it."""
        n = len(state.held)
        waiting = f"{n} waiting message{'s' * (n > 1)}"
        previews = "\n".join(f"• <i>{esc(m['text'][:80])}</i>" for m in state.held[:5])
        if not state.limit_cut:
            text = f"✅ The plan limit has lifted. Send {waiting} to Claude?\n\n{previews}"
            return text, [[("▶️ Send", "send"), ("🗑 Drop", "drop")]]
        text = "✅ The plan limit has lifted. It had cut Claude's work short: carry on with it"
        text += f", sending {waiting} too?\n\n{previews}" if n else "?"
        rows = [[("▶️ Continue", "send"), ("🗑 Leave it", "drop")]]
        if self._should_hold(key) and state.context_tokens >= self.cfg.wake_small:
            kilo = costguard.kilo(state.context_tokens)
            text += f"\n\nIts cache expired: continuing re-sends {kilo} tokens, and every later step re-reads them."
            rows.insert(1, [("🗜 Compact it and continue", "compact")])
        return text, rows

    async def _wake(self, key: Key) -> None:
        """Held messages to a conversation whose cache went cold: decide where they go (WAKE=auto)
        and say why, or ask. Haiku decides; if it can't, the user is asked."""
        decided = False
        if self.cfg.wake == "auto":
            try:
                decided = await self._triage(key)
            except Exception:
                log.exception("triage failed")
        if not decided:
            await self._ask_hold(key)

    async def _triage(self, key: Key) -> bool:
        """Small context: just continue. Otherwise compact and continue when the message goes on
        with the conversation, or start a new one when it doesn't. False if Haiku gave no answer."""
        state = self._state(key)
        context = state.context_tokens or (await asyncio.to_thread(transcript_context, state.session_id))[0]
        if context < self.cfg.wake_small:  # re-sending it costs little: no need to decide anything
            await self._release(key)
            return True
        sessions = await history.recent(state.cwd)
        title = next((history.title(s) for s in sessions if s.session_id == state.session_id), "")
        recent = await asyncio.to_thread(triage.excerpt, state.session_id)
        message = "\n".join(m["text"] for m in state.held)
        cli = self.cfg.cli_path or container.bundled_cli()
        verdict = await triage.decide(cli, self.cfg.state_dir, title, recent, message)
        if verdict is None:
            return False
        state = self._state(key)
        if not state.held:  # delivered or dropped meanwhile (/new, /resume, /stop)
            return True
        await self._output(key).send_html(await self._wake_text(state, context, title, verdict))
        if verdict.same:
            self._session(key).compact_next = True
        else:
            await self._new(key)
        await self._release(key)
        return True

    async def _wake_text(self, state: ConvState, context: int, title: str, verdict: triage.Verdict) -> str:
        """Why the message went where it did, with what each way costs."""
        log_path = self.cfg.state_dir / "usage.jsonl"
        calibration = await asyncio.to_thread(costguard.Calibration.from_log, log_path)
        usd = await asyncio.to_thread(costguard.usd_per_token, log_path)
        costs = costguard.wake_costs(context, state.after_compact, self.ttl)
        kilo = costguard.kilo

        def price(equivalent: float) -> str:
            text = costguard.format_cost(equivalent, usd, self.cfg.eur_per_usd, calibration)
            return f" ({esc(text)})" if text else ""

        reason = f": <i>{esc(verdict.reason)}</i>" if verdict.reason else ""
        keep = f"Its cache expired, so continuing it as it is would re-send {kilo(context)} tokens{price(costs.keep)}"
        if verdict.same:
            text = (
                f"🗜 <b>Compacting, then your message</b>: it goes on with this conversation{reason}\n\n"
                f"{keep}, and every later step would re-read them. Compacting costs about that once"
                f"{price(costs.compact)}; after it, each step re-reads ~{kilo(costs.summary)}."
            )
        else:
            old = f" (<i>{esc(title)}</i>)" if title else ""
            text = (
                f"🆕 <b>New conversation for your message</b>: it looks like a different task{reason}\n\n"
                f"{keep}; a new one starts at ~{kilo(costguard.STARTUP)} tokens{price(costs.new)}. "
                f"/list takes you back to the old one{old}."
            )
        haiku = verdict.cost_usd * self.cfg.eur_per_usd
        return text + f"\n\n<i>Decided by Haiku for {haiku:.3f} €. WAKE=ask asks instead.</i>"

    async def _ask_hold(self, key: Key) -> None:
        """The warning for held messages; without it nothing would deliver them, so they go on."""
        try:
            sent = await self._hold_prompt(key)
        except Exception:
            log.exception("hold prompt failed")
            sent = False
        if not sent:
            await self._release(key)

    async def _hold_prompt(self, key: Key) -> bool:
        state = self._state(key)
        sessions = await history.recent(state.cwd)
        rows = [[(str(i), f"r:{s.session_id}") for i, s in enumerate(sessions, 1)]]
        rows = [rows[0][i : i + 5] for i in range(0, len(rows[0]), 5)]
        rows.append([("▶️ Continue this one", "continue"), ("🆕 New one", "new")])
        # Its cache has expired, or the restarted process misses it: the next message re-sends
        # the whole context at full price.
        rows.append([("🗜 Compact it and continue", "compact")])
        idle = f" (last used {history.ago(state.last_active)})" if state.last_active else ""
        footer = (
            f"💤 This conversation was asleep{idle}. Continue it, pick another one or start a new one: "
            "your message goes there right after."
        )
        text = history.render(sessions, state.session_id, self._where(key), footer)
        return await self._prompt(key, "wake", text, rows)

    async def _on_prompt(self, key: Key, kind: str, action: str) -> None:
        """A button of the live warning was tapped (its buttons are already gone)."""
        state = self._state(key)
        if kind == "schedule" and action == "anyway":  # spends the reserve: for every conversation, until it resets
            self._reserve_skip = max(self._reserve_skip, state.limit_until)
            self._reserve = None
            state.limit_until = 0.0
            self.store.put(key, state)
            await self._release(key)
            return
        if kind == "schedule" and action.startswith("catchup-"):  # what /catchup sets, this limit included
            await self._set_catchup(key, action.removeprefix("catchup-"))
            return
        if kind == "schedule":  # _check_limit acts on it once the limit lifts
            state.limit_auto = action
            self.store.put(key, state)
            return
        if kind == "limit":
            if action == "drop":
                state.held, state.limit_cut = [], False
                self.store.put(key, state)
                return
            if state.limit_cut:
                state.limit_cut = False
                state.held.insert(0, {"text": CONTINUE_PROMPT, "ids": []})
                self.store.put(key, state)
                if action == "compact":
                    self._session(key).compact_next = True
            elif self._should_hold(key):  # the wait left the cache cold: where they go is still open
                await self._wake(key)
                return
        if kind == "wake":
            if action == "new":
                await self._new(key)
            elif action == "compact":
                self._session(key).compact_next = True
            elif action.startswith("r:") and action[2:] != state.session_id:
                await self._resume(key, action[2:])
                if self._state(key).session_id != action[2:]:  # it could not be resumed: ask again
                    await self._ask_hold(key)
                    return
        await self._release(key)

    async def _release(self, key: Key) -> None:
        """Deliver the held messages where the conversation now stands, without asking again."""
        await self._drop_prompt(key)
        state = self._state(key)
        if state.limit_cut and not state.limit_until:  # the question is still open: asked again
            state.limit_until = time.time()
            self.store.put(key, state)
        if state.limit_until:  # nothing can reach Claude yet: _check_limit asks when it can
            return
        held, state.held = state.held, []
        self.store.put(key, state)
        for item in held:
            await self._submit(key, item["text"], item["ids"], check=False)

    async def _prompt(
        self, key: Key, kind: str, html: str, rows: list[list[tuple[str, str]]], silent: bool = False
    ) -> bool:
        """Send a warning with buttons (label, action); False if it could not be sent.

        Its buttons carry a fresh nonce (guard:<nonce>:<action>): only the conversation's latest
        warning answers taps, and a new one takes the buttons off the one before.
        """
        await self._drop_prompt(key)
        nonce = secrets.token_hex(3)
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=f"guard:{nonce}:{action}") for label, action in row]
             for row in rows if row]
        )
        msg_id = await self._output(key).send_html(html, reply_markup=markup, silent=silent)
        if msg_id is None:
            return False
        state = self._state(key)
        state.prompt = {"nonce": nonce, "msg_id": msg_id, "kind": kind}
        self.store.put(key, state)
        return True

    async def _drop_prompt(self, key: Key) -> None:
        """Take the buttons off the live warning, if any: it no longer decides anything."""
        state = self._state(key)
        if not state.prompt:
            return
        msg_id, state.prompt = state.prompt["msg_id"], None
        self.store.put(key, state)
        try:
            await self.app.bot.edit_message_reply_markup(key[0], msg_id, reply_markup=None)
        except TelegramError as e:  # deleted, too old…
            log.debug("could not take the buttons off: %s", e)

    async def _on_guard(self, key: Key, query, rest: str) -> None:
        nonce, _, action = rest.partition(":")
        state = self._state(key)
        if not state.prompt or state.prompt["nonce"] != nonce:
            await query.answer("This prompt has expired")
            try:
                await query.edit_message_reply_markup(None)
            except TelegramError:
                pass
            return
        await query.answer()
        kind = state.prompt["kind"]
        state.prompt = None
        self.store.put(key, state)
        # The warning keeps what was picked, in place of its buttons.
        markup = query.message and query.message.reply_markup
        buttons = [b for row in markup.inline_keyboard for b in row] if markup else []
        label = next((b.text for b in buttons if b.callback_data == query.data), action)
        try:
            await query.edit_message_text(
                f"{query.message.text_html}\n\n→ {esc(label)}", parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            log.debug("could not mark the choice: %s", e)
            try:
                await query.edit_message_reply_markup(None)
            except TelegramError:
                pass
        try:
            await self._on_prompt(key, kind, action)
        except Exception:
            log.exception("prompt action %s:%s failed", kind, action)
            await self._release(key)  # never leave the messages stuck

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        self._learn_title(self._key(update), msg)
        if self.login and self.login.key == self._key(update) and self.login.waiting:
            if update.message and login.CODE.fullmatch(msg.text.strip()):
                await self._finish_login(self._key(update), msg.text.strip())
                return
        # An edit that changed nothing: it was sent already.
        unchanged = update.edited_message and self.texts.get((msg.chat_id, msg.message_id)) == msg.text
        if update.edited_message:
            log.info("message %s edited (%s)", msg.message_id, "same text, ignored" if unchanged else "text changed")
        if unchanged:
            return
        await self._submit(self._key(update), msg.text, [msg.message_id])

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        msg = update.effective_message
        self._learn_title(key, msg)
        if not await self._ready(key):
            await self._output(key).notice("Send the file again once the folder is picked.")
            return
        try:
            line = await uploads.save(msg, Path(self._state(key).cwd))
        except uploads.TooLarge as e:
            await self._output(key).notice(f"⚠️ {e}")
            return
        except Exception as e:
            log.exception("upload failed")
            await self._output(key).notice(f"⚠️ Could not save the file: {e}")
            return
        if msg.voice or msg.audio:
            line += " (you cannot listen to audio; tell the user if you need a transcript)"
        if msg.media_group_id:
            album = self.albums.get(msg.media_group_id)
            if album is None:
                group = msg.media_group_id
                album = uploads.AlbumCollector(lambda text, ids: self._album_done(group, key, text, ids))
                self.albums[group] = album
            album.add(line, msg.caption, msg.message_id)
            return
        await self._submit(key, f"{line}\n{msg.caption}" if msg.caption else line, [msg.message_id])

    async def _album_done(self, group: str, key: Key, text: str, ids: list[int]) -> None:
        self.albums.pop(group, None)
        await self._submit(key, text, ids)

    async def _post_init(self, app: Application) -> None:
        # Fills Telegram's command menu (the "/" button).
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])
        await self._announce_version()
        # Messages that were waiting for Claude when the bot stopped.
        for key, state in self.store.items():
            for item in state.pending + state.held:  # sent before the restart
                self._remember(key, item["text"], item["ids"])
            if state.pending and self._assigned(key):
                if await self._blocked():
                    pending, state.pending = state.pending, []
                    reserve = None if await self._hard_blocked() else self._reserve
                    await self._on_limited(key, pending, self._plan_resets(), reserve=reserve)
                else:
                    n = len(state.pending)
                    await self._output(key).notice(f"↩️ Passing on {n} message{'s' * (n > 1)} sent before the restart")
                    self._session(key)
            if state.session_id and (not state.last_api_at or not state.context_tokens):
                await self._backfill(key, state)
            # Held for a plan limit, they go on waiting for it or for the answer to its prompt.
            limit = state.limit_until or (state.prompt and state.prompt["kind"] == "limit")
            if state.held and self._assigned(key) and not limit and not state.login_lost_at:
                await self._ask_hold(key)
        self._alarm_task = asyncio.create_task(self._alarm_loop())

    async def _announce_version(self) -> None:
        """Says once, quietly, that a new version is running; restarts on the same one say nothing."""
        seen = self.cfg.state_dir / "version"
        if seen.exists() and seen.read_text().strip() == __version__:
            return
        known = [(key, state) for key, state in self.store.items() if self._assigned(key)]
        # The one working on the bot itself made the update: it goes there, then to the most recent.
        known.sort(key=lambda item: (_inside(item[1].cwd, REPO_DIR), item[1].last_active), reverse=True)
        # The next ones in case its topic was deleted.
        for key, _ in known[:VERSION_NOTICE_TRIES]:
            if await self._output(key).send_html(f"🔄 Updated to v{__version__}", silent=True):
                break
        else:
            if known:  # not delivered: the next start tries again
                return
        seen.write_text(__version__)

    async def _alarm_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            for key, state in self.store.items():
                try:
                    await self._check_login(key, state)
                    await self._check_wakeups(key, state)
                    await self._check_limit(key, state)
                    if self.cfg.topic_titles:
                        await self._check_title(key, state)
                    if self.cfg.cache_alarms or self.cfg.keepalive > self.ttl or self.cfg.limit_reserve:
                        await self._check_alarm(key, state)
                except Exception:
                    log.exception("login or plan limit check, cache alarm or keep-alive failed")
            try:
                await self._restart_if_outdated()
            except Exception:
                log.exception("restart check failed")

    def _all_asleep(self) -> bool:
        """No Claude process up, no cache left to lose and nothing under way: a restart costs nothing."""
        if self.login and self.login.waiting:
            return False
        if any(sess.pending or sess.connected for sess in self.sessions.values()):
            return False
        now = time.time()
        return all(costguard.cache_left(now, state.last_api_at, self.ttl) <= 0 for _, state in self.store.items())

    async def _restart_if_outdated(self) -> None:
        """Takes up a new version as soon as everything sleeps, rather than at the daily restart."""
        if self._restarting or not self._all_asleep():
            return
        version = await asyncio.to_thread(newer_version, REPO_DIR)
        if not version:
            return
        # One still running was kept for what the user left in it: it isn't asleep yet.
        if self.cfg.docker and await asyncio.to_thread(container.any_running):
            return
        log.info("all asleep: restarting from v%s to v%s", __version__, version)
        self._restarting = True
        self.app.stop_running()

    def _title(self, key: Key, state: ConvState) -> str:
        """The topic's name as it should stand now."""
        sess = self.sessions.get(key)
        if sess and sess.waiting:  # first: it's the one that needs the user
            status = titles.ASKING
        elif state.login_lost_at:
            status = titles.LOGIN
        elif state.limit_until or state.limit_cut:
            status = titles.LIMITED
        elif sess and sess.pending:
            status = titles.WORKING
        elif sess and sess.connected:
            status = titles.IDLE
        else:
            status = titles.ALARM if any(w.get("at") for w in state.wakeups) else titles.ASLEEP
        room = ""
        if self.cfg.compact_at:  # the same size Session._needs_compact goes by
            room = titles.room(state.context_tokens, max(self.cfg.compact_at, 2 * state.after_compact))
        # Just compacted, the old context's cache is no use: until the next answer there's none to lose.
        cache = titles.cache(self._cache_lasts(state)) if state.last_api_at and state.context_tokens else ""
        queued = (sess.queued if sess else len(state.pending)) + len(state.held)
        model = titles.model(state.model or state.last_model)
        return titles.compose(state.title, status, room, cache, queued, model)

    def _cache_lasts(self, state: ConvState) -> float:
        """Seconds until this conversation's cache is really lost, with the keep-alives _check_alarm
        would send it. A plan limit stops those; _blocked's last look is enough, it looks again as one falls due."""
        now = time.time()
        left = costguard.cache_left(now, state.last_api_at, self.ttl)
        data = self._plan[1]
        if (
            not state.session_id
            or state.asleep
            or state.login_lost_at
            or (state.limit_until and not self._reserve)
            or state.limit_cut
            or state.context_tokens < self.cfg.wake_small
            or (data and usage.blocked(data) is not None)
        ):
            return left
        return costguard.warm_left(now, left, state.last_active, self.ttl, self._keepalive_window(state))

    async def _check_title(self, key: Key, state: ConvState) -> None:
        """Keep the topic's name showing its status. Only with its real name known (Telegram has no
        way to ask for it): a guessed one would overwrite what the user called it."""
        sess = self.sessions.get(key)
        state = sess.state if sess else state
        if not key[1] or not state.title or not self._assigned(key) or time.time() < self._titles_pause:
            return
        title = self._title(key, state)
        if title == state.shown_title:
            return
        try:
            await self.app.bot.edit_forum_topic(key[0], key[1], name=title)
        except RetryAfter as e:
            self._titles_pause = time.time() + float(e.retry_after)
            return
        except BadRequest as e:  # not modified, topic deleted, no permission: not worth insisting
            log.debug("could not rename topic %s: %s", key, e)
        state.shown_title = title
        self.store.put(key, state)

    async def _check_alarm(self, key: Key, state: ConvState) -> None:
        """Warn as the conversation's prompt cache nears its end (CACHE_ALARMS minutes before),
        or keep it warm instead, for a while after the conversation's last use (KEEPALIVE_HOURS)."""
        sess = self.sessions.get(key)
        state = sess.state if sess else state
        alarm = self.alarms.get(key)
        # Used again or reset: the cache is fresh. Compacted: there's nothing left to warn about.
        if alarm and (alarm["at"] != state.last_api_at or not state.context_tokens):
            await self._delete_alarm(key)
            alarm = None
        if state.asleep:  # /sleep: its cache may go
            if alarm:
                await self._delete_alarm(key)
            return
        if not state.session_id or not state.last_api_at or (sess and sess.pending) or not self._assigned(key):
            return
        if not state.context_tokens:
            return
        if state.login_lost_at or state.limit_cut:
            return
        now = time.time()
        left = costguard.cache_left(now, state.last_api_at, self.ttl)
        if self.cfg.limit_reserve and left > 0 and state.context_tokens >= self.cfg.wake_small:
            await self._reserving()  # it may keep this one warm until the reset
        if state.limit_until and not self._reserve:  # waiting for the limit itself
            return
        window = self._keepalive_window(state)
        if state.context_tokens >= self.cfg.wake_small and costguard.keeps_alive(
            now, left, state.last_active, self.ttl, window
        ):  # the cache won't expire yet: no alarms
            due = costguard.keepalive_due(now, left, state.last_active, self.ttl, window)
            if due and not await self._hard_blocked():
                await self._session(key).keepalive()
            return
        if left <= 0 and alarm and alarm["step"]:
            if await self._blocked():  # nothing to do about it until the limit resets
                await self._delete_alarm(key)
                self.alarms[key] = {"at": state.last_api_at, "step": 0, "msg_id": None}
                return
            await self._cache_expired(key, state)
            return
        step = costguard.alarm_step(left, self.ttl, self.cfg.cache_alarms, alarm and alarm["step"])
        if step is None or await self._blocked():  # no message could reach Claude in time anyway
            return
        await self._delete_alarm(key)
        calibration = await asyncio.to_thread(costguard.Calibration.from_log, self.cfg.state_dir / "usage.jsonl")
        est = costguard.estimate(state.context_tokens, True, self.ttl, calibration)
        mins = max(round(left / 60), 1)
        text = (
            f"⏳ <b>{mins} min</b> of cache left. After that the next message re-sends "
            f"{esc(costguard.format_estimate(est))}"
        )
        msg_id = await self._output(key).send_html(text)
        self.alarms[key] = {"at": state.last_api_at, "step": step, "msg_id": msg_id}

    async def _cache_expired(self, key: Key, state: ConvState) -> None:
        """The cache the alarms counted down is gone: say so, with the plan usage (free to fetch)."""
        await self._delete_alarm(key)
        self.alarms[key] = {"at": state.last_api_at, "step": 0, "msg_id": None}
        sess = self.sessions.get(key)
        asleep = "" if sess and sess.connected else ", Claude is asleep"
        text = f"🧊 Cache expired{asleep}. The next message re-sends {costguard.kilo(state.context_tokens)} tokens."
        if not self.cfg.api_key:
            text += "\n\n" + await self._usage_text()
        await self._output(key).send_html(esc(text))

    async def _plan_data(self) -> dict | None:
        """The plan usage, fetched at most once a minute; None if unknown."""
        if self.cfg.api_key:
            return None
        async with self._plan_lock:
            at, data = self._plan
            if time.time() - at > 60:
                try:
                    data = await usage.fetch(self.cfg.cli_path or container.bundled_cli(), usagelog.FRESH)
                except Exception as e:
                    log.debug("plan usage not available: %s", e)
                    data = None
                if data and self.cfg.limit_reserve:
                    self._rates = await asyncio.to_thread(costguard.plan_rates, self.cfg.state_dir / "usage.jsonl")
                self._plan = (time.time(), data)
        return data

    async def _hard_blocked(self) -> bool:
        """Whether a plan limit itself stops Claude now: keep-alives can't go either."""
        data = await self._plan_data()
        return bool(data) and usage.blocked(data) is not None

    async def _blocked(self) -> bool:
        """Whether real work stops now: a plan limit, or the reserve kept for keep-alives before it."""
        return await self._hard_blocked() or await self._reserving() is not None

    async def _reserving(self) -> costguard.Reserve | None:
        """The reserve due now (see costguard.reserve), unless the user chose to spend it."""
        data = await self._plan_data()
        self._reserve = None
        if not self.cfg.limit_reserve or not data or usage.blocked(data) is not None:
            return None
        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled") and (extra.get("utilization") or 0) < 100:  # going past it doesn't stop Claude
            return None
        limits = [
            (lim["kind"], lim.get("percent") or 0, usage.epoch(lim["resets_at"]))
            for lim in data.get("limits") or []
            if lim.get("kind") in costguard.RESERVE_KINDS and lim.get("resets_at")
        ]
        now = time.time()
        contexts = []
        for key, state in self.store.items():
            sess = self.sessions.get(key)
            state = sess.state if sess else state
            if (
                state.session_id
                and state.last_api_at
                and state.context_tokens >= self.cfg.wake_small
                and not state.login_lost_at
                and not state.limit_cut
                and not state.asleep
                and costguard.cache_left(now, state.last_api_at, self.ttl) > 0
                and self._assigned(key)
            ):
                contexts.append(state.context_tokens)
        found = costguard.reserve(now, limits, contexts, self.ttl, self._rates)
        if found and found.resets <= self._reserve_skip + 60:  # the user chose to spend this one
            return None
        self._reserve = found
        return found

    def _plan_resets(self) -> float | None:
        """When the limit _blocked last saw resets, or the one its reserve is for; None if unknown."""
        data = self._plan[1]
        iso = usage.blocked(data) if data else None
        if iso:
            return usage.epoch(iso)
        return self._reserve.resets if self._reserve else None

    def _keepalive_window(self, state: ConvState) -> float:
        """KEEPALIVE_HOURS, or up to the reset while the reserve keeps this conversation warm, or up to
        its alarm while one is set."""
        window = self.cfg.keepalive
        if self._reserve and state.context_tokens in self._reserve.contexts:  # not the ones it left out
            window = max(window, self._reserve.resets - state.last_active + self.ttl)
        # Asleep until an alarm: warm until it rings, if that costs less than writing the context then.
        now = time.time()
        rings = min((w["at"] for w in state.wakeups if w.get("at")), default=0.0)
        if rings > now and costguard.worth_keeping(now, rings, state.context_tokens, self.ttl):
            window = max(window, rings + WAKEUP_SLACK - state.last_active)
        return window

    async def _usage_text(self) -> str:
        try:
            return usage.render(await usage.fetch(self.cfg.cli_path or container.bundled_cli(), USAGE_FRESH))
        except usage.RateLimited as e:
            last = usage.last()
            if not last:
                return f"⚠️ Usage not available: {e}"
            ago = usage.ago(last[0])
            return f"{usage.render(last[1])}\n\n🕰 Reading from {ago}: {e}"
        except usage.Unavailable as e:
            return f"⚠️ Usage not available: {e}"
        except Exception as e:
            log.exception("usage failed")
            return f"⚠️ Could not fetch usage: {e}"

    async def _delete_alarm(self, key: Key) -> None:
        """Take down the conversation's last cache alarm: only the latest one says how things stand."""
        alarm = self.alarms.pop(key, None)
        if not alarm or not alarm["msg_id"]:
            return
        try:
            await self.app.bot.delete_message(key[0], alarm["msg_id"])
        except TelegramError as e:
            log.debug("could not delete the cache alarm: %s", e)

    async def _backfill(self, key: Key, state: ConvState) -> None:
        """Context size and last call time from the transcript, for state that lacks them."""
        context, mtime = await asyncio.to_thread(transcript_context, state.session_id)
        state.context_tokens = state.context_tokens or context
        state.last_api_at = state.last_api_at or mtime
        self.store.put(key, state)

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        cmds = "\n".join(f"/{name}  {desc}" for name, desc in COMMANDS)
        await self._output(key).notice(
            f"Claude Code over Telegram v{__version__}\n"
            f"working in: {self._where(key)}\n\n"
            "Write to talk to Claude; send files and it gets them too.\n\n"
            f"{cmds}\n\n"
            "Other /commands are passed to Claude (e.g. /compact).\n"
            "Parallel conversations: one per Telegram topic, each with its own /cwd.\n\n"
            f"{titles.LEGEND}"
        )

    def _subdirs(self) -> list[str]:
        return sorted(
            p.name for p in self.cfg.workdir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    async def _cmd_cwd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        if key[1] is None:
            await self._output(key).notice(
                f"This chat works in {self.cfg.workdir}. Folders are picked per topic "
                "(one first-level folder of the workdir each)."
            )
            return
        if ctx.args:
            await self._set_cwd(key, " ".join(ctx.args))
            return
        current = self._state(key).cwd if self._assigned(key) else None
        where = f"Working in <b>{self._rel(current)}</b>. " if current else ""
        await self._folder_picker(key, f"{where}Pick a folder (starts a new conversation):", current)

    async def _folder_picker(self, key: Key, prompt: str, current: str | None = None) -> None:
        # callback_data is capped at 64 bytes: longer names must be typed with /cwd <dir>.
        names = [n for n in self._subdirs() if len(f"cwd:{n}".encode()) <= 64]
        if not names:
            await self._output(key).notice(f"⚠️ {self.cfg.workdir} has no folders to pick from.")
            return
        mark = lambda n: "● " if str(self.cfg.workdir / n) == current else ""  # noqa: E731
        buttons = [InlineKeyboardButton(f"{mark(n)}📁 {n}", callback_data=f"cwd:{n}") for n in names[:90]]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        await self._output(key).send_html(prompt, reply_markup=InlineKeyboardMarkup(rows))

    async def _set_cwd(self, key: Key, target: str) -> None:
        path = self.cfg.workdir / target.strip().strip("/")
        if "/" in target.strip().strip("/") or not self._valid_cwd(key, path):
            await self._output(key).notice(f"⚠️ {target} is not a first-level folder of {self.cfg.workdir}")
            return
        old = self._state(key)
        sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()
        # A new directory means a new conversation; display preferences carry over.
        state = ConvState(cwd=str(path), mode=old.mode, model=old.model, tools=old.tools, title=old.title)
        # The new conversation gets them.
        state.held, state.limit_until, state.limit_auto = old.held, old.limit_until, old.limit_auto
        state.login_lost_at, state.agent_sends = old.login_lost_at, old.agent_sends
        state.login_retry_at, state.login_retries = old.login_retry_at, old.login_retries
        self.store.put(key, state)
        if old.prompt:
            try:
                await self.app.bot.edit_message_reply_markup(key[0], old.prompt["msg_id"], reply_markup=None)
            except TelegramError:
                pass
        msg = f"📁 Working in {self._rel(state.cwd)} (new conversation)"
        if any(s.state.cwd == state.cwd for k, s in self.sessions.items() if k != key):
            msg += "\n⚠️ Another conversation is also working in this directory."
        await self._output(key).notice(msg)
        for text, ids in self.no_folder.pop(key, []):
            await self._submit(key, text, ids)
        await self._release(key)

    async def _on_topic_created(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A new topic named like a folder of the workdir works there; otherwise ask for one."""
        key = self._key(update)
        name = update.effective_message.forum_topic_created.name.strip()
        self._set_title(key, name)
        match = next((n for n in self._subdirs() if n.lower() == name.lower()), None)
        if match:
            await self._set_cwd(key, match)
        elif key not in self.no_folder:
            await self._ready(key)

    async def _on_topic_edited(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        name = update.effective_message.forum_topic_edited.name
        if name:  # None when only the icon changed
            self._set_title(self._key(update), titles.plain(name.strip()))

    def _set_title(self, key: Key, title: str) -> None:
        """Remembered to name the conversation's container (from its next start) and its topic."""
        state = self._state(key)
        state.title = title
        state.shown_title = None
        self.store.put(key, state)

    def _learn_title(self, key: Key, msg) -> None:
        """Topics older than the bot's knowing their names: messages in one point at its creation."""
        created = msg.reply_to_message and msg.reply_to_message.forum_topic_created
        if created and key[1] and not self._state(key).title:
            self._set_title(key, created.name)

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        if not await self._ready(key):
            return
        await self._new(key)
        await self._release(key)

    async def _new(self, key: Key) -> None:
        sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()
        state = self._state(key)
        state.session_id = None
        state.limit_cut = state.asleep = False  # that work stays in the old conversation
        state.cost_usd = 0.0
        state.context_tokens = state.after_compact = 0
        state.last_api_at = 0.0
        self.store.put(key, state)
        await self._output(key).notice(f"🆕 New conversation in {state.cwd}")

    async def _cmd_sleep(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/sleep: done with this conversation for now. Its process stops and its cache is let go
        (no warnings, keep-alives or alarms); the next message wakes it up as any asleep one."""
        key = self._key(update)
        if not await self._ready(key):
            return
        out = self._output(key)
        sess = self.sessions.get(key)
        state = sess.state if sess else self._state(key)
        if sess and sess.pending:
            await out.notice("⚠️ Claude is still working: /stop it first, or wait.")
            return
        if not state.session_id:
            await out.notice("Nothing to put to sleep: there's no conversation here yet.")
            return
        self.sessions.pop(key, None)
        if sess:
            await sess.close()
        await self._delete_alarm(key)
        dropped = len(state.wakeups)
        state.asleep, state.wakeups = True, []
        self.store.put(key, state)
        alarms = f" Its {dropped} alarm{'s' * (dropped > 1)} won't ring." if dropped else ""
        await out.notice(
            f"💤 Asleep: no more cache warnings or keep-alives here, its cache may go.{alarms} "
            "Your next message wakes it up."
        )

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/list shows the last few conversations here, /older more of them; /resume <n|id> switches to one."""
        key = self._key(update)
        if not await self._ready(key):
            return
        older = bool(update.effective_message and (update.effective_message.text or "").startswith("/older"))
        await self._list(key, ctx.args or [], older)

    async def _list(self, key: Key, args: list[str], older: bool = False) -> None:
        state = self._state(key)
        sessions = await history.recent(state.cwd, history.LIMIT if older or args else history.SHORT)
        if args:
            found = history.find(sessions, args[0])
            if found:
                await self._resume(key, found.session_id)
                await self._release(key)
            else:
                await self._output(key).notice(f"⚠️ No conversation {args[0]} here; /older shows them.")
            return
        if not sessions:
            await self._output(key).notice(f"No past conversations in {self._where(key)}.")
            return
        buttons = [
            InlineKeyboardButton(str(i), callback_data=f"resume:{s.session_id}") for i, s in enumerate(sessions, 1)
        ]
        rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        footer = history.OLDER_FOOTER
        if not older and len(sessions) >= history.SHORT:  # maybe more: offer them
            rows.append([InlineKeyboardButton("📜 Older ones", callback_data="older:")])
            footer = history.FOOTER
        await self._output(key).send_html(
            history.render(sessions, state.session_id, self._where(key), footer),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _resume(self, key: Key, session_id: str) -> None:
        out = self._output(key)
        state = self._state(key)
        if session_id == state.session_id:
            await out.notice("Already in that conversation.")
            return
        if any(s.state.session_id == session_id for k, s in self.sessions.items() if k != key):
            await out.notice("⚠️ That conversation is open in another topic; /new there first.")
            return
        info = next((s for s in await history.recent(state.cwd, history.LIMIT) if s.session_id == session_id), None)
        if info is None:
            await out.notice("⚠️ That conversation is no longer here.")
            return
        sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()
        state.session_id = session_id
        state.limit_cut = state.asleep = False  # that work stays in the old conversation
        state.cost_usd = await asyncio.to_thread(transcript_cost, session_id)
        state.context_tokens, state.last_api_at = await asyncio.to_thread(transcript_context, session_id)
        state.after_compact = 0
        state.last_active = time.time()
        self.store.put(key, state)
        await self._output(key).notice(
            f"⏪ Resumed: {history.title(info)}\n"
            "Your next message continues it. Don't use it in the terminal at the same time."
        )

    async def _cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        if not await self._ready(key):
            return
        sess = self._session(key)
        if ctx.args:
            await self._set_tools(sess, ctx.args[0])
            return
        await self._send_picker(key, "tools")

    async def _set_tools(self, sess: Session, level: str) -> None:
        if level not in TOOL_LEVELS:
            await sess.out.notice(f"Unknown level {level}. Options: {', '.join(TOOL_LEVELS)}")
            return
        sess.state.tools = sess.out.tools = level
        sess.save()
        await sess.out.notice(f"Tool activity: {TOOL_LEVELS[level]}")

    async def _cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        if not await self._ready(key):
            return
        sess = self._session(key)
        if ctx.args:
            await self._set_mode(sess, ctx.args[0])
            return
        await self._send_picker(key, "mode")

    async def _set_mode(self, sess: Session, mode: str) -> None:
        if mode not in MODES:
            await sess.out.notice(f"Unknown mode {mode}. Options: {', '.join(MODES)}")
            return
        try:
            await sess.set_mode(mode)
        except Exception as e:
            log.exception("set_mode failed")
            await sess.out.notice(f"⚠️ Could not change mode: {e}")
            return
        await sess.out.notice(f"🔐 Permission mode: {mode}")

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.get(key)
        running, dropped = await sess.stop() if sess else (False, 0)
        state = self._state(key)
        cut = state.limit_cut
        state.limit_auto, state.login_lost_at = "", 0.0  # with nothing waiting, the next message finds out
        state.login_retry_at, state.login_retries = 0.0, 0
        if state.held or cut:  # held back by a warning, or waiting for a plan limit: dropped too
            dropped += len(state.held)
            state.held, state.limit_cut = [], False
            self.store.put(key, state)
        await self._drop_prompt(key)
        if not running:
            # When a turn was running, its end reports "Stopped" itself.
            msg = f"🛑 Dropped {dropped} queued message(s)" if dropped else "Nothing to stop"
            if cut:
                msg = "🛑 Claude won't carry on with the work the limit cut short" + (
                    f"\nDropped {dropped} queued message(s)" if dropped else ""
                )
            await self._output(key).notice(msg)
        elif dropped:
            await self._output(key).notice(f"Dropped {dropped} queued message(s)")

    async def _cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        if not await self._ready(key):
            return
        sess = self._session(key)
        if ctx.args:
            await self._set_model(sess, ctx.args[0])
            return
        await self._send_picker(key, "model")

    async def _set_model(self, sess: Session, model: str) -> None:
        try:
            await sess.set_model(None if model == "default" else model)
        except Exception as e:
            log.exception("set_model failed")
            await sess.out.notice(f"⚠️ Could not change model: {e}")
            return
        await sess.out.notice(f"🧠 Model: {model} (applies from the next message)")

    async def _cmd_catchup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/catchup [auto|compact|ask]: whether the conversation goes on by itself when a plan limit lifts."""
        key = self._key(update)
        if not await self._ready(key):
            return
        if ctx.args:
            await self._set_catchup(key, ctx.args[0].lower())
            return
        await self._send_picker(key, "catchup")

    def _picker(self, key: Key, what: str) -> tuple[str, list[tuple[str, str]]]:
        """The choices of /tools, /mode, /model or /catchup: a heading and (label, value), the current one marked."""
        state = self._state(key)
        if what == "tools":
            heading, current, options = "Tool activity", state.tools, TOOL_LEVELS.items()
        elif what == "mode":
            current = state.mode or self.cfg.permission_mode
            heading, options = "Permission mode", [(m, f"{m}: {desc}") for m, desc in MODES.items()]
        elif what == "model":
            current = state.model or "default"
            heading, options = "Model", [*MODELS.items(), ("default", "Default (Claude Code's choice)")]
        else:
            heading, current = "After a plan limit", state.catchup
            options = [(arg, CATCHUP_BUTTONS[value]) for arg, value in CATCHUP_ARGS.items()]
            current = next(arg for arg, value in CATCHUP_ARGS.items() if value == current)
        shown = dict(options).get(current, current)
        choices = [(f"{'● ' if v == current else ''}{label}", v) for v, label in options]
        return f"{heading}: <b>{esc(shown)}</b>", choices

    async def _send_picker(self, key: Key, what: str) -> None:
        heading, choices = self._picker(key, what)
        buttons = [[InlineKeyboardButton(label, callback_data=f"{what}:{v}")] for label, v in choices]
        await self._output(key).send_html(heading, reply_markup=InlineKeyboardMarkup(buttons))

    async def _set(self, key: Key, what: str, value: str) -> None:
        """What a button of a picker (/tools, /mode, /model, /catchup or /menu) chose."""
        if what == "catchup":
            await self._set_catchup(key, value)
        else:
            setter = {"tools": self._set_tools, "mode": self._set_mode, "model": self._set_model}[what]
            await setter(self._session(key), value)

    def _menu(self, key: Key) -> tuple[str, InlineKeyboardMarkup]:
        """/menu's main page: the settings, each opening its choices, then the commands."""
        settings = []
        for what, icon in (("model", "🧠"), ("tools", "🛠"), ("mode", "🔐"), ("catchup", "⏰")):
            heading, _ = self._picker(key, what)
            value = re.sub(r"<[^>]+>", "", heading.partition(": ")[2])
            settings.append(InlineKeyboardButton(f"{icon} {value}", callback_data=f"menu:{what}"))
        rows = [settings[:2], settings[2:]] + [
            [InlineKeyboardButton(label, callback_data=f"menu:{cmd}") for label, cmd in row] for row in MENU_ACTIONS
        ]
        return f"⚙️ {esc(self._where(key))}", InlineKeyboardMarkup(rows)

    async def _cmd_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        text, markup = self._menu(key)
        await self._output(key).send_html(text, reply_markup=markup)

    async def _on_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, key: Key, query, rest: str) -> None:
        """A /menu button: a setting's page and back, a choice (then back to the main page), or a command."""
        await query.answer()
        page, _, value = rest.partition("=")
        if page == "close":
            await query.edit_message_reply_markup(None)
            return
        if page in ("model", "tools", "mode", "catchup"):
            if not await self._ready(key):
                return
            if value:
                await self._set(key, page, value)
            else:
                heading, choices = self._picker(key, page)
                rows = [[InlineKeyboardButton(label, callback_data=f"menu:{page}={v}")] for label, v in choices]
                rows.append([InlineKeyboardButton("‹ Back", callback_data="menu:main")])
                markup = InlineKeyboardMarkup(rows)
                await query.edit_message_text(heading, parse_mode=ParseMode.HTML, reply_markup=markup)
                return
        elif page in MENU_COMMANDS:
            await getattr(self, f"_cmd_{MENU_COMMANDS[page]}")(update, ctx)
        text, markup = self._menu(key)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:  # unchanged: Telegram refuses an edit to the same thing
            log.debug("menu not edited: %s", e)

    async def _set_catchup(self, key: Key, arg: str) -> None:
        value = {**CATCHUP_ARGS, "on": "send", "off": "", "no": ""}.get(arg)
        if value is None:
            await self._output(key).notice("Unknown choice. Options: auto, compact, ask")
            return
        state = self._state(key)
        state.catchup = value
        if state.limit_until:  # a limit already stops it: this one too
            state.limit_auto = value
        self.store.put(key, state)
        await self._output(key).notice(f"⏰ After a plan limit: {CATCHUP[value]}")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.get(key)
        state = sess.state if sess else self._state(key)
        if state.login_retry_at:
            mins = max(0, int(state.login_retry_at - time.time()) // 60)
            activity = f"turned down by Anthropic's servers, trying again in {mins} min"
        elif state.login_lost_at:
            activity = "waiting for /login"
        elif state.limit_until:
            activity = "stopped by a plan limit" + (", goes on when it lifts" if state.limit_auto else "")
        elif not sess or not sess.pending:
            activity = "idle"
        elif sess.waiting:
            activity = "waiting for your permission"
        else:
            activity = "working"
        queued = (sess.queued if sess else 0) + len(state.held)
        lines = [
            f"📊 v{__version__}",
            f"working in: {self._where(key)}",
            f"conversation: {state.session_id or 'new'}",
            f"mode: {state.mode or self.cfg.permission_mode}",
            f"model: {(sess and sess.model) or state.model or 'default'}",
            f"state: {activity}" + (f", {queued} queued" if queued else ""),
            f"tools: {state.tools}",
            f"after a plan limit: {CATCHUP[state.catchup]}",
        ]
        try:
            context = await sess.context_usage() if sess else None
        except Exception:
            log.exception("context usage failed")
            context = None
        if context:
            used, total = context["totalTokens"] // 1000, context["maxTokens"] // 1000
            lines.append(f"context: {context['percentage']:.0f}% ({used}k / {total}k tokens)")
        await self._output(key).notice("\n".join(lines))

    async def _cmd_tldr(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/tldr: the TLDR.md Claude keeps at the root of this conversation's folder."""
        key = self._key(update)
        path = Path(self._state(key).cwd) / TLDR_FILE
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            text = ""
        if len(text) > TLDR_MAX:
            text = text[:TLDR_MAX].rstrip() + "\n…"
        if text:
            await self._output(key).text(f"📝 {text}")  # it's Markdown: shown formatted
        else:
            await self._output(key).notice(f"No {TLDR_FILE} in {self._where(key)} yet: ask Claude to write one.")

    async def _cmd_cost(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.get(key)
        state = sess.state if sess else self._state(key)
        last = f"\nlast turn: ${sess.last_turn_cost:.4f}" if sess and sess.last_turn_cost is not None else ""
        await self._output(key).notice(
            f"💰 This conversation: ${state.cost_usd:.4f}{last}\n"
            "API-price estimate; on a Claude subscription it counts against your plan limits instead."
        )

    async def _cmd_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        if self.cfg.api_key:
            await self._output(key).notice("⚠️ This bot uses an API key, not a Claude login.")
            return
        if self.login:
            self.login.close()
        self.login = login.Login(self.cfg.cli_path or container.bundled_cli(), key)
        try:
            url = await self.login.start()
        except (OSError, login.LoginError) as e:
            await self._output(key).notice(f"⚠️ Could not start the login: {e}")
            return
        await self._output(key).send_html(
            f'🔑 <a href="{esc(url)}">Sign in to Claude here</a>, then send me the code the page shows '
            "(in this topic, within 10 minutes). It goes to the login, not to Claude."
        )

    async def _finish_login(self, key: Key, code: str) -> None:
        try:
            await self.login.finish(code)
        except login.LoginError as e:
            await self._output(key).notice(f"⚠️ The login failed: {e}. /login tries again.")
            return
        await self._output(key).notice("✅ Logged in to Claude. What was waiting for it goes in a moment.")

    async def _cmd_usage(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        text = await self._usage_text()
        wait = usage.retry_at() - time.time()
        if wait > 0 and not usage.fresh(USAGE_FRESH):
            if key not in self._usage_later:
                self._usage_later[key] = asyncio.create_task(self._send_usage_later(key))
            text += "\n\n⏳ I'll send a fresh reading here as soon as the endpoint allows it."
        await self._output(key).notice(text)

    async def _send_usage_later(self, key: Key) -> None:
        """/usage while the endpoint was rate-limited: the reading, once it lets the bot ask."""
        try:
            while True:
                await asyncio.sleep(max(usage.retry_at() - time.time(), 0) + 1)
                try:
                    await usage.fetch(self.cfg.cli_path or container.bundled_cli(), USAGE_FRESH)
                except usage.RateLimited:
                    continue
                except Exception:
                    pass  # the reading below says what went wrong
                break
            await self._output(key).notice(await self._usage_text())
        finally:
            self._usage_later.pop(key, None)

    async def _cmd_server(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        try:
            text = await server.render([Path("/"), self.cfg.workdir, Path(self._state(key).cwd)])
        except server.Unavailable as e:
            text = f"⚠️ Server status not available: {e}"
        except Exception as e:
            log.exception("server status failed")
            text = f"⚠️ Could not read server status: {e}"
        await self._output(key).notice(text)

    async def _on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query.from_user or query.from_user.id not in self.cfg.allowed_user_ids:
            await query.answer()
            return
        key = self._key(update)
        kind, _, rest = (query.data or "").partition(":")
        if kind == "perm":
            req_id, _, choice = rest.partition(":")
            ok = await permissions.resolve(self._output(key), req_id, choice)
            await query.answer(None if ok else "This request is no longer active")
            if not ok:
                await query.edit_message_reply_markup(None)
        elif kind == "agent":
            await self._on_agent_send(key, query, rest)
        elif kind == "wakeup":
            await self._on_wakeup(key, query, rest)
        elif kind == "cwd":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._set_cwd(key, rest)
        elif kind == "menu":
            await self._on_menu(update, ctx, key, query, rest)
        elif kind == "wake":  # the wake picker of 2.12 and before
            await query.answer("This prompt has expired")
            await query.edit_message_reply_markup(None)
        elif kind in ("tools", "model", "mode", "resume", "older", "guard", "catchup") and not await self._ready(key):
            await query.answer()
        elif kind == "older":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._list(key, [], older=True)
        elif kind == "resume":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._resume(key, rest)
            await self._release(key)
        elif kind == "guard":
            await self._on_guard(key, query, rest)
        elif kind in ("tools", "model", "catchup", "mode"):
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._set(key, kind, rest)
        else:
            await query.answer()

    async def _on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user, msg = update.effective_user, update.effective_message
        if user and user.id == self.app.bot.id and msg and msg.forum_topic_created:
            return  # a topic of ours, for a message to an agent: set up when it was created
        if user and user.id == self.app.bot.id and msg and msg.forum_topic_edited:
            # The service message for a rename of ours: the topic's name already says it all.
            try:
                await msg.delete()
            except TelegramError as e:
                log.debug("could not delete the rename notice: %s", e)
            return
        # Logged so the owner can find their own id; nothing is sent back.
        log.warning("ignored update from user id=%s username=%s", user and user.id, user and user.username)

    async def _shutdown(self, app: Application) -> None:
        if self._alarm_task:
            self._alarm_task.cancel()
        for sess in self.sessions.values():
            await sess.close()
