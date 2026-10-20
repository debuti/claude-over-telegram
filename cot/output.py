"""Sends one conversation's output to its Telegram chat (or forum topic).

Consecutive tool calls are grouped into a single "activity" message that is
edited as new calls arrive, instead of flooding the chat with one message per
call. Any text from Claude closes the current group.
"""

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError

from .render import TG_LIMIT, esc, html_to_plain, markdown_to_html, tool_line, tool_result_text

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.5  # seconds between edits of the activity message
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_MAX = 10 * 1024 * 1024  # larger images go as documents
RESULT_PREVIEW = 1500  # "full" mode: longer tool results are sent as a file


class TelegramOutput:
    def __init__(self, bot: Bot, chat_id: int, thread_id: int | None, cwd: Path):
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.cwd = cwd
        self.tools = "calls"  # hidden | calls | full
        self._activity_id: int | None = None
        self._activity: list[str] = []
        self._shown = ""
        self._last_edit = 0.0
        self._flush_task: asyncio.Task | None = None
        self._flush_lock = asyncio.Lock()

    # --- low level --------------------------------------------------------

    async def _call(self, fn, *args, **kwargs):
        for _ in range(3):
            try:
                return await fn(*args, **kwargs)
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
        return await fn(*args, **kwargs)

    async def send_html(self, text: str, reply_markup=None) -> int | None:
        try:
            msg = await self._call(
                self.bot.send_message,
                self.chat_id,
                text,
                parse_mode=ParseMode.HTML,
                message_thread_id=self.thread_id,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except BadRequest as e:
            log.warning("HTML rejected (%s), sending plain text", e)
            try:
                msg = await self._call(
                    self.bot.send_message,
                    self.chat_id,
                    html_to_plain(text)[:4096],
                    message_thread_id=self.thread_id,
                    reply_markup=reply_markup,
                )
            except TelegramError:
                log.exception("send failed")
                return None
        except TelegramError:
            log.exception("send failed")
            return None
        return msg.message_id

    async def send_file(self, name: str, content: str, caption: str = "") -> None:
        try:
            await self._call(
                self.bot.send_document,
                self.chat_id,
                document=io.BytesIO(content.encode()),
                filename=name,
                caption=caption[:1024] or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=self.thread_id,
            )
        except TelegramError:
            log.exception("send_document failed")

    async def send_path(self, path: Path, caption: str = "") -> None:
        """Send a local file; images small enough are shown as photos."""
        await self._close_activity()
        kwargs = dict(caption=caption[:1024] or None, message_thread_id=self.thread_id)
        with path.open("rb") as f:
            if path.suffix.lower() in PHOTO_EXTS and path.stat().st_size <= PHOTO_MAX:
                await self._call(self.bot.send_photo, self.chat_id, photo=f, **kwargs)
            else:
                await self._call(self.bot.send_document, self.chat_id, document=f, filename=path.name, **kwargs)

    # --- activity message ---------------------------------------------------

    def _activity_html(self) -> str:
        text = "\n".join(self._activity)
        if len(text) > TG_LIMIT:
            # Keep the most recent calls visible.
            text = "…\n" + text[-(TG_LIMIT - 10) :].split("\n", 1)[-1]
        return text

    async def _flush_activity(self) -> None:
        # A delayed flush past its sleep may overlap with _close_activity.
        async with self._flush_lock:
            await self._flush_activity_locked()

    async def _flush_activity_locked(self) -> None:
        text = self._activity_html()
        if not text or text == self._shown:
            return
        self._last_edit = time.monotonic()
        if self._activity_id is None:
            self._activity_id = await self.send_html(text)
        else:
            try:
                await self._call(
                    self.bot.edit_message_text,
                    text,
                    chat_id=self.chat_id,
                    message_id=self._activity_id,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest as e:
                if "not modified" not in str(e):
                    log.warning("activity edit failed: %s", e)
            except TelegramError:
                log.exception("activity edit failed")
        self._shown = text

    async def _delayed_flush(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._flush_task = None
        await self._flush_activity()

    async def _schedule_flush(self) -> None:
        wait = EDIT_INTERVAL - (time.monotonic() - self._last_edit)
        if wait <= 0 and self._flush_task is None:
            await self._flush_activity()
        elif self._flush_task is None:
            self._flush_task = asyncio.create_task(self._delayed_flush(wait))

    async def flush(self) -> None:
        """Show pending tool calls now and start a new group after them."""
        await self._close_activity()

    async def _close_activity(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        await self._flush_activity()
        self._activity_id = None
        self._activity = []
        self._shown = ""

    # --- events from the session --------------------------------------------

    async def text(self, md: str) -> None:
        await self._close_activity()
        for part in markdown_to_html(md):
            await self.send_html(part)

    async def tool_use(self, name: str, inp: dict[str, Any], nested: bool) -> None:
        # ToolSearch only loads tool definitions: noise for the user.
        if self.tools == "hidden" or name == "ToolSearch":
            return
        line = tool_line(name, inp, self.cwd)
        self._activity.append(f"  ↳ {line}" if nested else line)
        await self._schedule_flush()

    async def tool_result(self, content: Any, is_error: bool) -> None:
        if self.tools == "hidden" or (self.tools == "calls" and not is_error):
            return
        text = tool_result_text(content).strip()
        if not text:
            return
        mark = "❌" if is_error else "↩️"
        if len(text) > RESULT_PREVIEW and self.tools == "full":
            await self._close_activity()
            await self.send_file("result.txt", text, caption=f"{mark} result ({len(text)} chars)")
            return
        if len(text) > RESULT_PREVIEW:
            text = text[:RESULT_PREVIEW] + "…"
        self._activity.append(f"{mark} <pre>{esc(text)}</pre>")
        await self._schedule_flush()

    async def notice(self, text: str) -> None:
        """A short plain-text status line from the bot itself."""
        await self._close_activity()
        await self.send_html(esc(text))

    async def end_turn(self) -> None:
        await self._close_activity()
