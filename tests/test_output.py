"""A note goes into the latest message: no new one, so nothing sounds or shows as unread."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from cot.output import TelegramOutput
from cot.render import TG_LIMIT


class TgBot:
    def __init__(self):
        self.sent, self.edits = 0, []
        self.gone = False

    async def send_message(self, chat_id, text, **kwargs):
        self.sent += 1
        return SimpleNamespace(message_id=self.sent)

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        if self.gone:
            raise BadRequest("Message to edit not found")
        self.edits.append((message_id, text))


@pytest.fixture
def out():
    return TelegramOutput(TgBot(), 1, None, Path("/tmp"))


async def test_a_note_edits_the_latest_message_and_sends_none(out):
    await out.send_html("first")
    await out.send_html("<b>second</b>")
    await out.note("a < b")
    await out.note("again")
    assert out.bot.sent == 2
    assert out.bot.edits == [(2, "<b>second</b>\n\na &lt; b"), (2, "<b>second</b>\n\na &lt; b\n\nagain")]


async def test_a_message_with_buttons_is_left_alone(out):
    await out.send_html("answer")
    await out.send_html("sure?", reply_markup=object())
    await out.note("note")
    assert out.bot.edits == [(1, "answer\n\nnote")]


async def test_it_adds_to_what_the_activity_message_says_by_then(out):
    out._activity = ["one"]
    await out._flush_activity()
    out._activity = ["one", "two"]
    await out._flush_activity()
    await out.note("note")
    assert out.bot.edits[-1] == (1, "one\ntwo\n\nnote")


async def test_without_a_message_or_room_nothing_shows(out):
    await out.note("note")
    await out.send_html("x" * TG_LIMIT)
    await out.note("note")
    assert out.bot.sent == 1 and not out.bot.edits


async def test_a_deleted_message_is_no_error(out):
    await out.send_html("answer")
    out.bot.gone = True
    await out.note("note")
    assert out.bot.sent == 1
