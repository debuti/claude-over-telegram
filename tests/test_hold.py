"""Held messages and the nonce-guarded prompt, with Telegram and the sessions stubbed out."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from cot import bot as bot_mod
from cot.bot import Bot
from cot.store import ConvState, Store

KEY = (1, None)


class TgBot:
    def __init__(self):
        self.edits = []

    async def edit_message_reply_markup(self, chat_id, msg_id, reply_markup=None):
        self.edits.append(msg_id)


class Out:
    def __init__(self, fail=False):
        self.sent, self.notices, self.fail = [], [], fail

    async def send_html(self, html, reply_markup=None, silent=False):
        if self.fail:
            return None
        self.sent.append((html, reply_markup))
        return 100 + len(self.sent)

    async def notice(self, text):
        self.notices.append(text)


class Query:
    def __init__(self, data, markup):
        self.data = data
        self.message = SimpleNamespace(reply_markup=markup, text_html="prompt")
        self.answers, self.edited = [], []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup):
        self.edited.append(None)

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    b = object.__new__(Bot)
    b.cfg = SimpleNamespace(workdir=tmp_path)
    b.store = Store(tmp_path / "sessions.json")
    b.store.put(KEY, ConvState(cwd=str(tmp_path), session_id="s1"))
    b.sessions, b.no_folder = {}, {}
    b.app = SimpleNamespace(bot=TgBot())
    b.out = Out()
    b.delivered = []
    b.hold = True
    monkeypatch.setattr(Bot, "_output", lambda self, key: self.out)
    monkeypatch.setattr(Bot, "_should_hold", lambda self, key: self.hold)

    async def hold_prompt(self, key):
        return await self._prompt(key, "wake", "asleep", [[("Continue", "continue"), ("New", "new")]])

    async def new(self, key):
        self.delivered.append("NEW")

    def session(self, key):
        async def send(text, ids):
            self.delivered.append(text)
        return SimpleNamespace(send=send, compact_next=False, stop=None)

    monkeypatch.setattr(Bot, "_hold_prompt", hold_prompt)
    monkeypatch.setattr(Bot, "_new", new)
    monkeypatch.setattr(Bot, "_session", session)
    monkeypatch.setattr(Bot, "_ensure_typing", lambda self, key, sess: None)
    return b


def tap(bot, action):
    nonce = bot.store.get(KEY).prompt["nonce"]
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("New", callback_data=f"guard:{nonce}:new")]])
    return Query(f"guard:{nonce}:{action}", markup), f"{nonce}:{action}"


async def test_hold_then_choose(bot):
    await bot._submit(KEY, "one", [1])
    await bot._submit(KEY, "two", [2])
    assert len(bot.out.sent) == 1  # one prompt for both
    assert [m["text"] for m in bot.store.get(KEY).held] == ["one", "two"]
    query, rest = tap(bot, "new")
    await bot._on_guard(KEY, query, rest)
    assert bot.delivered == ["NEW", "one", "two"]
    assert bot.store.get(KEY).held == [] and bot.store.get(KEY).prompt is None
    assert query.edited == ["prompt\n\n→ New"]


async def test_stale_nonce_does_nothing(bot):
    await bot._submit(KEY, "one")
    old_query, old_rest = tap(bot, "continue")
    await bot._ask_hold(KEY)  # a newer prompt takes the buttons off the old one
    assert bot.app.bot.edits == [101]
    await bot._on_guard(KEY, old_query, old_rest)
    assert old_query.answers == ["This prompt has expired"]
    assert bot.delivered == [] and len(bot.store.get(KEY).held) == 1


async def test_prompt_that_cannot_be_sent_delivers(bot):
    bot.out.fail = True
    await bot._submit(KEY, "one")
    assert bot.delivered == ["one"] and bot.store.get(KEY).held == []


async def test_stop_drops_held(bot, monkeypatch):
    await bot._submit(KEY, "one")
    await bot._submit(KEY, "two")
    update = SimpleNamespace()
    monkeypatch.setattr(Bot, "_key", staticmethod(lambda u: KEY))
    await bot._cmd_stop(update, None)
    assert bot.out.notices == ["🛑 Dropped 2 queued message(s)"]
    assert bot.store.get(KEY).held == [] and bot.store.get(KEY).prompt is None
    assert bot.delivered == []


def test_callback_data_fits(bot):
    assert len(f"guard:{'a' * 6}:r:{'x' * 36}".encode()) <= 64
    assert bot_mod.secrets.token_hex(3).__len__() == 6
