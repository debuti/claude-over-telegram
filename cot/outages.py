"""Watches whether Telegram's Bot API answers, and keeps the intervals it was down for, to report them."""

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)

# Shorter outages (one long poll timing out now and then) aren't worth a message.
MIN_OUTAGE = 60
# A failure older than this when Telegram answers again means the bot itself was off meanwhile:
# the outage ends at that last failure, not now.
GAP = 120


class Outages:
    """Intervals [start, end, kind] in which requests to Telegram failed, stored until reported.

    A new interval starts whenever the kind of failure changes (502 Bad Gateway, timed out...).
    """

    def __init__(self, path: Path):
        self.path = path
        self.intervals: list[list] = []  # of the outage under way, or recovered and not yet reported
        self.down = False  # the last request failed
        self.first = 0  # index of the outage under way's first interval
        try:
            data = json.loads(path.read_text())
            self.intervals, self.down, self.first = data["intervals"], data["down"], data["first"]
        except (OSError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        try:
            data = {"intervals": self.intervals, "down": self.down, "first": self.first}
            self.path.write_text(json.dumps(data))
        except OSError:
            log.exception("could not save Telegram's outages")

    def failed(self, kind: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        last = self.intervals[-1] if self.intervals else None
        if not self.down or last is None:
            self.first = len(self.intervals)
            self.intervals.append([now, now, kind])
            log.warning("Telegram is not answering: %s", kind)
        elif last[2] == kind:
            last[1] = now
        else:
            last[1] = now
            self.intervals.append([now, now, kind])
            log.warning("Telegram is not answering: %s", kind)
        self.down = True
        self._save()

    def answered(self, now: float | None = None) -> None:
        if not self.down:
            return
        now = time.time() if now is None else now
        last = self.intervals[-1]
        last[1] = now if now - last[1] < GAP else last[1]
        self.down = False
        log.warning("Telegram answers again")
        # Earlier outages may still be waiting to be reported: only this one's length counts.
        if last[1] - self.intervals[self.first][0] < MIN_OUTAGE:
            del self.intervals[self.first:]
        self._save()

    def report(self) -> str | None:
        """What to tell the user, once Telegram answers and there was an outage worth telling."""
        if self.down or not self.intervals:
            return None
        today = datetime.now().date()

        def hhmm(t: float) -> str:
            d = datetime.fromtimestamp(t)
            return d.strftime("%H:%M") if d.date() == today else d.strftime("%d/%m %H:%M")

        lines = [f"{hhmm(start)} – {hhmm(end)}  {kind}" for start, end, kind in self.intervals]
        return "⚠️ Telegram was down:\n" + "\n".join(lines)

    def reported(self) -> None:
        self.intervals = []
        self._save()


def kind_of(error: BaseException) -> str:
    if isinstance(error, TimedOut):
        return "timed out"
    return str(error) or type(error).__name__


class WatchedRequest(HTTPXRequest):
    """An HTTPXRequest that tells the watcher whether each request got an answer from Telegram."""

    def __init__(self, outages: Outages, on_answer: Callable[[], None], **kwargs):
        super().__init__(**kwargs)
        self.outages = outages
        self.on_answer = on_answer

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        try:
            code, payload = await super().do_request(*args, **kwargs)
        except NetworkError as e:  # TimedOut included
            self.outages.failed(kind_of(e))
            raise
        if code >= 500:
            try:
                phrase = HTTPStatus(code).phrase
            except ValueError:
                phrase = "server error"
            self.outages.failed(f"{code} {phrase}")
        else:
            self.outages.answered()
            self.on_answer()
        return code, payload
