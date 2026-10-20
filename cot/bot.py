"""Telegram side: authorization, commands and routing messages to sessions."""

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Config
from .output import TelegramOutput
from .session import Session
from .store import ConvState, Key, Store

log = logging.getLogger(__name__)


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

    async def _on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        # Logged so the owner can find their own id; nothing is sent back.
        log.warning("ignored update from user id=%s username=%s", user and user.id, user and user.username)

    async def _shutdown(self, app: Application) -> None:
        for sess in self.sessions.values():
            await sess.close()
