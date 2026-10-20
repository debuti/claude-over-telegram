"""Files sent from Telegram: saved into the workdir and described to Claude.

Claude reads them with its own Read tool (which also views images), so they
must live inside the jail. Albums arrive as one message per item; they are
collected briefly and handed to Claude as a single turn.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from telegram import Message

log = logging.getLogger(__name__)

UPLOAD_DIR = ".tg-uploads"
MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API getFile limit
ALBUM_WAIT = 1.5  # seconds to wait for the rest of an album


class TooLarge(Exception):
    pass


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.-]+", "_", Path(name).name).strip("._")
    return name[:80] or "file"


def _kb(size: int | None) -> str:
    return f"{(size or 0) / 1024:.0f} KB"


async def save(msg: Message, workdir: Path) -> str:
    """Download the message's attachment; return a line describing it to Claude."""
    if msg.photo:
        att, name, kind = msg.photo[-1], f"photo_{msg.message_id}.jpg", "image/jpeg"
    elif msg.document:
        att, name, kind = msg.document, msg.document.file_name or f"file_{msg.message_id}", msg.document.mime_type
    elif msg.video:
        att, name, kind = msg.video, msg.video.file_name or f"video_{msg.message_id}.mp4", msg.video.mime_type
    elif msg.audio:
        att, name, kind = msg.audio, msg.audio.file_name or f"audio_{msg.message_id}", msg.audio.mime_type
    elif msg.voice:
        att, name, kind = msg.voice, f"voice_{msg.message_id}.ogg", msg.voice.mime_type
    else:
        raise ValueError("no attachment")
    if att.file_size and att.file_size > MAX_DOWNLOAD:
        raise TooLarge(f"{name} is {att.file_size // (1024 * 1024)} MB; Telegram bots can only download up to 20 MB")

    folder = workdir / UPLOAD_DIR
    if not folder.exists():
        folder.mkdir()
        # Keep uploads out of the project's git status.
        (folder / ".gitignore").write_text("*\n")
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{_safe_name(name)}"
    file = await att.get_file()
    await file.download_to_drive(path)
    log.info("saved upload %s", path)
    return f"[The user sent a file: {path.relative_to(workdir)} ({kind or 'unknown type'}, {_kb(att.file_size)})]"


class AlbumCollector:
    """Groups the items of a Telegram album (same media_group_id) into one turn."""

    def __init__(self, submit: Callable[[str], Awaitable[None]]):
        self._submit = submit
        self._parts: list[str] = []
        self._captions: list[str] = []
        self._task: asyncio.Task | None = None

    def add(self, line: str, caption: str | None) -> None:
        self._parts.append(line)
        if caption:
            self._captions.append(caption)
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(ALBUM_WAIT)
        text = "\n".join(self._parts + self._captions)
        self._parts, self._captions, self._task = [], [], None
        await self._submit(text)
