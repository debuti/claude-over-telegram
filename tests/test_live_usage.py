"""The plan usage kept up to date in General while its Claude sleeps."""

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from cot.bot import Bot
from cot.store import ConvState, Store

GENERAL, TOPIC = (1, None), (1, 7)


class TgBot:
    def __init__(self):
        self.edits, self.deleted, self.gone = [], [], False

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        if self.gone:
            raise BadRequest("Message to edit not found")
        self.edits.append(message_id)

    async def delete_message(self, chat_id, msg_id):
        self.deleted.append(msg_id)


class Out:
    def __init__(self):
        self.sent = []

    async def send_html(self, html, reply_markup=None, silent=False):
        self.sent.append((html, silent))
        return 100 + len(self.sent)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    b = object.__new__(Bot)
    b.cfg = SimpleNamespace(live_usage=300.0, api_key=False)
    b.store = Store(tmp_path / "sessions.json")
    b.sessions = {}
    b.app = SimpleNamespace(bot=TgBot())
    b.out = Out()
    monkeypatch.setattr(Bot, "_output", lambda self, key: self.out)

    async def usage_text(self):
        return "📈 Plan usage"

    monkeypatch.setattr(Bot, "_usage_text", usage_text)
    return b


async def check(b, key=GENERAL):
    state = b.store.get(key) or ConvState(cwd="/w")
    await b._check_live_usage(key, state)
    return b.store.get(key) or state


async def test_sent_once_then_edited_when_due(bot):
    state = await check(bot)
    assert state.live_usage_msg == 101 and bot.out.sent[0][1]  # silent
    await check(bot)
    assert len(bot.out.sent) == 1 and not bot.app.bot.edits  # not due yet
    state.live_usage_at -= 301
    bot.store.put(GENERAL, state)
    await check(bot)
    assert bot.app.bot.edits == [101] and len(bot.out.sent) == 1


async def test_taken_down_while_awake_and_back_when_asleep(bot):
    await check(bot)
    bot.sessions[GENERAL] = SimpleNamespace(connected=True, pending=[])
    state = await check(bot)
    assert bot.app.bot.deleted == [101] and state.live_usage_msg is None
    bot.sessions[GENERAL].connected = False
    state = await check(bot)
    assert state.live_usage_msg == 102


async def test_resent_when_the_message_is_gone(bot):
    state = await check(bot)
    state.live_usage_at = 0
    bot.app.bot.gone = True
    state = await check(bot)
    assert state.live_usage_msg == 102


async def test_only_general_and_not_when_off(bot):
    await check(bot, TOPIC)
    bot.cfg.live_usage = 0
    await check(bot)
    assert not bot.out.sent
