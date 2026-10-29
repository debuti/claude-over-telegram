"""Telegram side: authorization, commands and routing messages to sessions."""

import asyncio
import logging
import secrets
import time
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__, container, costguard, history, permissions, server, uploads, usage, usagelog
from .config import Config
from .output import TelegramOutput
from .render import esc
from .session import Session, idle_disconnect, transcript_context, transcript_cost
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
    ("help", "list commands"),
    ("new", "start a fresh conversation"),
    ("list", "recent conversations in this folder"),
    ("resume", "go back to a past conversation"),
    ("stop", "interrupt Claude and drop queued messages"),
    ("cwd", "directory this conversation works in"),
    ("tools", "how much tool activity to show"),
    ("mode", "permission mode"),
    ("model", "switch model"),
    ("status", "what's going on"),
    ("cost", "estimated cost of this conversation"),
    ("usage", "plan usage limits (5-hour and weekly)"),
    ("server", "load of the machine the bot runs on"),
]

# What the CLI keeps between its auto-compact window and the size it compacts at: room for
# the summary (min(max output, 20k)) plus 13k; checked with get_context_usage on 0.2.168.
COMPACT_BUFFER = 33_000

MODELS = {
    "claude-fable-5-1": "Fable 5.1",
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}


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
        self.ttl = costguard.cache_ttl(cfg)
        # The cache alarm last sent per conversation: {"at": last_api_at it counts from, "step", "msg_id"}.
        self.alarms: dict[Key, dict] = {}
        self._alarm_task: asyncio.Task | None = None
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
        self.app.add_handler(CommandHandler(["list", "resume"], self._cmd_resume, filters=allowed))
        self.app.add_handler(CommandHandler("tools", self._cmd_tools, filters=allowed))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode, filters=allowed))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop, filters=allowed))
        self.app.add_handler(CommandHandler("model", self._cmd_model, filters=allowed))
        self.app.add_handler(CommandHandler("status", self._cmd_status, filters=allowed))
        self.app.add_handler(CommandHandler("cost", self._cmd_cost, filters=allowed))
        self.app.add_handler(CommandHandler("usage", self._cmd_usage, filters=allowed))
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
            ttl=self.ttl,
            **self._runner(Path(state.cwd)),
        )
        sess.on_delivered = lambda ids: self._acknowledge(key, ids)
        sess.on_active = lambda: self._ensure_typing(key, sess)
        sess.on_turn_start = lambda: self.usage_log.turn_started(key)
        sess.on_turn_end = lambda entry: self._log_turn(key, sess, entry)
        sess.on_turn_failed = lambda: self.usage_log.turn_failed(key)
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

    async def _submit(self, key: Key, text: str, ids: list[int] | None = None, check: bool = True) -> None:
        """Hand a message to the conversation, unless a warning holds it back first.

        check=False delivers without asking: for messages the user already decided about.
        """
        if not self._assigned(key):
            first = key not in self.no_folder
            self.no_folder.setdefault(key, []).append((text, ids or []))
            if first:
                await self._folder_picker(
                    key, "Pick the folder for this topic; your message goes to Claude right after:"
                )
            return
        state = self._state(key)
        if state.held:  # a warning is already waiting for an answer: this one waits with it
            state.held.append({"text": text, "ids": ids or []})
            self.store.put(key, state)
            return
        if check and self._should_hold(key):
            state.held.append({"text": text, "ids": ids or []})
            self.store.put(key, state)
            await self._ask_hold(key)
            return
        sess = self._session(key)
        await sess.send(text, ids)
        self._ensure_typing(key, sess)

    def _should_hold(self, key: Key) -> bool:
        """A conversation whose Claude stopped for lack of use: where its next message goes is asked."""
        state = self._state(key)
        sess = self.sessions.get(key)
        if not state.session_id or (sess and (sess.connected or sess.pending)):
            return False
        return time.time() - state.last_active >= idle_disconnect()

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
        await self._submit(self._key(update), msg.text, [msg.message_id])

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        msg = update.effective_message
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
        # Messages that were waiting for Claude when the bot stopped.
        for key, state in self.store.items():
            if state.pending and self._assigned(key):
                n = len(state.pending)
                await self._output(key).notice(f"↩️ Passing on {n} message{'s' * (n > 1)} sent before the restart")
                self._session(key)
            if state.session_id and (not state.last_api_at or not state.context_tokens):
                await self._backfill(key, state)
            if state.held and self._assigned(key):
                await self._ask_hold(key)
        if self.cfg.cache_alarms:
            self._alarm_task = asyncio.create_task(self._alarm_loop())

    async def _alarm_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            for key, state in self.store.items():
                try:
                    await self._check_alarm(key, state)
                except Exception:
                    log.exception("cache alarm failed")

    async def _check_alarm(self, key: Key, state: ConvState) -> None:
        """Warn as the conversation's prompt cache nears its end (CACHE_ALARMS minutes before)."""
        sess = self.sessions.get(key)
        state = sess.state if sess else state
        alarm = self.alarms.get(key)
        if alarm and alarm["at"] != state.last_api_at:  # used again (or reset): the cache is fresh
            await self._delete_alarm(key)
            alarm = None
        if not state.session_id or not state.last_api_at or (sess and sess.pending) or not self._assigned(key):
            return
        left = costguard.cache_left(time.time(), state.last_api_at, self.ttl)
        if left <= 0 and alarm and alarm["step"]:
            await self._cache_expired(key, state)
            return
        step = costguard.alarm_step(left, self.ttl, self.cfg.cache_alarms, alarm and alarm["step"])
        if step is None:
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
        await self._output(key).send_html(esc(text), silent=True)

    async def _usage_text(self) -> str:
        try:
            return usage.render(await usage.fetch(self.cfg.cli_path or container.bundled_cli()))
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
            "Parallel conversations: one per Telegram topic, each with its own /cwd."
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
        state.held = old.held  # the new conversation gets them
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
            self._set_title(self._key(update), name.strip())

    def _set_title(self, key: Key, title: str) -> None:
        """Remembered to name the conversation's container (from its next start)."""
        state = self._state(key)
        state.title = title
        self.store.put(key, state)

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
        state.cost_usd = 0.0
        state.context_tokens = state.after_compact = 0
        state.last_api_at = 0.0
        self.store.put(key, state)
        await self._output(key).notice(f"🆕 New conversation in {state.cwd}")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/list shows past conversations of this folder; /resume <n|id> switches to one."""
        key = self._key(update)
        if not await self._ready(key):
            return
        state = self._state(key)
        sessions = await history.recent(state.cwd)
        if ctx.args:
            found = history.find(sessions, ctx.args[0])
            if found:
                await self._resume(key, found.session_id)
                await self._release(key)
            else:
                await self._output(key).notice(f"⚠️ No conversation {ctx.args[0]} here; /list shows them.")
            return
        if not sessions:
            await self._output(key).notice(f"No past conversations in {self._where(key)}.")
            return
        buttons = [
            InlineKeyboardButton(str(i), callback_data=f"resume:{s.session_id}") for i, s in enumerate(sessions, 1)
        ]
        rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        await self._output(key).send_html(
            history.render(sessions, state.session_id, self._where(key)), reply_markup=InlineKeyboardMarkup(rows)
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
        info = next((s for s in await history.recent(state.cwd) if s.session_id == session_id), None)
        if info is None:
            await out.notice("⚠️ That conversation is no longer here.")
            return
        sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()
        state.session_id = session_id
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
        buttons = [
            [InlineKeyboardButton(f"{'● ' if lvl == sess.state.tools else ''}{desc}", callback_data=f"tools:{lvl}")]
            for lvl, desc in TOOL_LEVELS.items()
        ]
        await sess.out.send_html(
            f"Tool activity: <b>{sess.state.tools}</b>", reply_markup=InlineKeyboardMarkup(buttons)
        )

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
        current = sess.state.mode or self.cfg.permission_mode
        buttons = [
            [InlineKeyboardButton(f"{'● ' if m == current else ''}{m}: {desc}", callback_data=f"mode:{m}")]
            for m, desc in MODES.items()
        ]
        await sess.out.send_html(f"Permission mode: <b>{current}</b>", reply_markup=InlineKeyboardMarkup(buttons))

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
        if state.held:  # held back by a warning: dropped too
            dropped += len(state.held)
            state.held = []
            self.store.put(key, state)
        await self._drop_prompt(key)
        if not running:
            # When a turn was running, its end reports "Stopped" itself.
            msg = f"🛑 Dropped {dropped} queued message(s)" if dropped else "Nothing to stop"
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
        current = sess.state.model or "default"
        buttons = [
            [InlineKeyboardButton(f"{'● ' if m == current else ''}{name}", callback_data=f"model:{m}")]
            for m, name in [*MODELS.items(), ("default", "Default (Claude Code's choice)")]
        ]
        await sess.out.send_html(f"Model: <b>{current}</b>", reply_markup=InlineKeyboardMarkup(buttons))

    async def _set_model(self, sess: Session, model: str) -> None:
        try:
            await sess.set_model(None if model == "default" else model)
        except Exception as e:
            log.exception("set_model failed")
            await sess.out.notice(f"⚠️ Could not change model: {e}")
            return
        await sess.out.notice(f"🧠 Model: {model} (applies from the next message)")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.get(key)
        state = sess.state if sess else self._state(key)
        if not sess or not sess.pending:
            activity = "idle"
        elif sess.waiting:
            activity = "waiting for your permission"
        else:
            activity = "working"
        lines = [
            f"📊 v{__version__}",
            f"working in: {self._where(key)}",
            f"conversation: {state.session_id or 'new'}",
            f"mode: {state.mode or self.cfg.permission_mode}",
            f"model: {(sess and sess.model) or state.model or 'default'}",
            f"state: {activity}" + (f", {sess.queued} queued" if sess and sess.queued else ""),
            f"tools: {state.tools}",
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

    async def _cmd_cost(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.get(key)
        state = sess.state if sess else self._state(key)
        last = f"\nlast turn: ${sess.last_turn_cost:.4f}" if sess and sess.last_turn_cost is not None else ""
        await self._output(key).notice(
            f"💰 This conversation: ${state.cost_usd:.4f}{last}\n"
            "API-price estimate; on a Claude subscription it counts against your plan limits instead."
        )

    async def _cmd_usage(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._output(self._key(update)).notice(await self._usage_text())

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
        elif kind == "cwd":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._set_cwd(key, rest)
        elif kind == "wake":  # the wake picker of 2.12 and before
            await query.answer("This prompt has expired")
            await query.edit_message_reply_markup(None)
        elif kind in ("tools", "model", "mode", "resume", "guard") and not await self._ready(key):
            await query.answer()
        elif kind == "resume":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._resume(key, rest)
            await self._release(key)
        elif kind == "guard":
            await self._on_guard(key, query, rest)
        elif kind == "tools":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._set_tools(self._session(key), rest)
        elif kind == "model":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._set_model(self._session(key), rest)
        elif kind == "mode":
            await query.answer()
            await query.edit_message_reply_markup(None)
            await self._set_mode(self._session(key), rest)
        else:
            await query.answer()

    async def _on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        # Logged so the owner can find their own id; nothing is sent back.
        log.warning("ignored update from user id=%s username=%s", user and user.id, user and user.username)

    async def _shutdown(self, app: Application) -> None:
        if self._alarm_task:
            self._alarm_task.cancel()
        for sess in self.sessions.values():
            await sess.close()
