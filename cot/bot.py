"""Telegram side: authorization, commands and routing messages to sessions."""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from . import __version__, permissions, uploads
from .config import Config
from .output import TelegramOutput
from .session import Session
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

MODELS = {
    "claude-fable-5-1": "Fable 5.1",
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.state_dir / "sessions.json")
        self.sessions: dict[Key, Session] = {}
        self.typing: dict[Key, asyncio.Task] = {}
        self.albums: dict[str, uploads.AlbumCollector] = {}
        self.app = (
            Application.builder()
            .token(cfg.telegram_token)
            .concurrent_updates(True)
            .connect_timeout(20)
            .get_updates_connect_timeout(20)
            .post_shutdown(self._shutdown)
            .build()
        )
        allowed = filters.User(user_id=cfg.allowed_user_ids)
        self.app.add_handler(CommandHandler("start", self._cmd_start, filters=allowed))
        self.app.add_handler(CommandHandler("new", self._cmd_new, filters=allowed))
        self.app.add_handler(CommandHandler("tools", self._cmd_tools, filters=allowed))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode, filters=allowed))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop, filters=allowed))
        self.app.add_handler(CommandHandler("model", self._cmd_model, filters=allowed))
        self.app.add_handler(CommandHandler("status", self._cmd_status, filters=allowed))
        self.app.add_handler(CommandHandler("cost", self._cmd_cost, filters=allowed))
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
        msg = update.effective_message
        return (update.effective_chat.id, msg.message_thread_id if msg and msg.is_topic_message else None)

    def _state(self, key: Key) -> ConvState:
        """Stored state for this chat, or a fresh one if it belongs to another workdir."""
        state = self.store.get(key)
        if state is None or state.cwd != str(self.cfg.workdir):
            state = ConvState(cwd=str(self.cfg.workdir))
        return state

    def _output(self, key: Key) -> TelegramOutput:
        sess = self.sessions.get(key)
        if sess:
            return sess.out
        return TelegramOutput(self.app.bot, *key, self.cfg.workdir)

    def _session(self, key: Key) -> Session:
        sess = self.sessions.get(key)
        if sess:
            return sess
        state = self._state(key)
        out = TelegramOutput(self.app.bot, *key, self.cfg.workdir)
        out.tools = state.tools
        sess = Session(
            state,
            out=out,
            on_state=lambda st: self.store.put(key, st),
            default_mode=self.cfg.permission_mode,
            cli_path=self.cfg.cli_path,
            sandbox_bash=self.cfg.sandbox_bash,
        )
        sess.start()
        self.sessions[key] = sess
        return sess

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

    async def _submit(self, key: Key, text: str) -> None:
        sess = self._session(key)
        await sess.send(text)
        if key not in self.typing:
            self.typing[key] = asyncio.create_task(self._keep_typing(key, sess))

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._submit(self._key(update), update.effective_message.text)

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        msg = update.effective_message
        try:
            line = await uploads.save(msg, self.cfg.workdir)
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
                album = uploads.AlbumCollector(lambda text: self._album_done(group, key, text))
                self.albums[group] = album
            album.add(line, msg.caption)
            return
        await self._submit(key, f"{line}\n{msg.caption}" if msg.caption else line)

    async def _album_done(self, group: str, key: Key, text: str) -> None:
        self.albums.pop(group, None)
        await self._submit(key, text)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        await self._output(key).notice(
            f"Claude Code over Telegram v{__version__}\n"
            f"workdir: {self.cfg.workdir}\n\n"
            "Write to talk to Claude.\n"
            "/new  start a fresh conversation\n"
            "/stop  interrupt Claude and drop queued messages\n"
            "/tools  how much tool activity to show\n"
            "/mode  permission mode\n"
            "/model  switch model\n"
            "/status  what's going on\n"
            "/cost  estimated cost of this conversation\n"
            "Other /commands are passed to Claude (e.g. /compact)."
        )

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()
        state = self._state(key)
        state.session_id = None
        state.cost_usd = 0.0
        self.store.put(key, state)
        await self._output(key).notice(f"🆕 New conversation in {state.cwd}")

    async def _cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self._session(key)
        if ctx.args:
            await self._set_tools(sess, ctx.args[0])
            return
        buttons = [
            [InlineKeyboardButton(f"{'● ' if lvl == sess.state.tools else ''}{desc}", callback_data=f"tools:{lvl}")]
            for lvl, desc in TOOL_LEVELS.items()
        ]
        await sess.out.send_html(f"Tool activity: <b>{sess.state.tools}</b>", reply_markup=InlineKeyboardMarkup(buttons))

    async def _set_tools(self, sess: Session, level: str) -> None:
        if level not in TOOL_LEVELS:
            await sess.out.notice(f"Unknown level {level}. Options: {', '.join(TOOL_LEVELS)}")
            return
        sess.state.tools = sess.out.tools = level
        sess.save()
        await sess.out.notice(f"Tool activity: {TOOL_LEVELS[level]}")

    async def _cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
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
        if not running:
            # When a turn was running, its end reports "Stopped" itself.
            msg = f"🛑 Dropped {dropped} queued message(s)" if dropped else "Nothing to stop"
            await self._output(key).notice(msg)
        elif dropped:
            await self._output(key).notice(f"Dropped {dropped} queued message(s)")

    async def _cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
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
            f"workdir: {state.cwd}",
            f"conversation: {state.session_id or 'new'}",
            f"mode: {state.mode or self.cfg.permission_mode}",
            f"model: {(sess and sess.model) or state.model or 'default'}",
            f"state: {activity}" + (f", {sess.queued} queued" if sess and sess.queued else ""),
            f"tools: {state.tools}",
        ]
        try:
            usage = await sess.context_usage() if sess else None
        except Exception:
            log.exception("context usage failed")
            usage = None
        if usage:
            lines.append(f"context: {usage['percentage']:.0f}% ({usage['totalTokens'] // 1000}k / {usage['maxTokens'] // 1000}k tokens)")
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
        for sess in self.sessions.values():
            await sess.close()
