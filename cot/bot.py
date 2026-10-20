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

from . import permissions
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


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.state_dir / "sessions.json")
        self.sessions: dict[Key, Session] = {}
        self.typing: dict[Key, asyncio.Task] = {}
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
        self.app.add_handler(CommandHandler("verbose", self._cmd_verbose, filters=allowed))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode, filters=allowed))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        # Unknown /commands fall through to here and reach Claude (e.g. /compact).
        self.app.add_handler(MessageHandler(allowed & filters.TEXT, self._on_text))
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
        out.verbose = state.verbose
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

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self._session(key)
        await sess.send(update.effective_message.text)
        if key not in self.typing:
            self.typing[key] = asyncio.create_task(self._keep_typing(key, sess))

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        await self._output(key).notice(
            f"Claude Code over Telegram\n"
            f"workdir: {self.cfg.workdir}\n\n"
            "Write to talk to Claude.\n"
            "/new  start a fresh conversation\n"
            "/verbose  show or hide tool results\n"
            "/mode  permission mode\n"
            "Other /commands are passed to Claude (e.g. /compact)."
        )

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()
        state = self._state(key)
        state.session_id = None
        self.store.put(key, state)
        await self._output(key).notice(f"🆕 New conversation in {state.cwd}")

    async def _cmd_verbose(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = self._key(update)
        sess = self._session(key)
        sess.state.verbose = not sess.state.verbose
        sess.out.verbose = sess.state.verbose
        self.store.put(key, sess.state)
        await sess.out.notice("🔊 Tool results: shown" if sess.state.verbose else "🔇 Tool results: hidden")

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
