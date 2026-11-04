"""Held messages and the nonce-guarded prompt, with Telegram and the sessions stubbed out."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from cot import bot as bot_mod
from cot import triage
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
    b.cfg = SimpleNamespace(
        workdir=tmp_path, wake="ask", wake_small=30_000, eur_per_usd=0.9, state_dir=tmp_path, cli_path="claude"
    )
    b.ttl = 3600
    b.store = Store(tmp_path / "sessions.json")
    b.store.put(KEY, ConvState(cwd=str(tmp_path), session_id="s1"))
    b.sessions, b.no_folder = {}, {}
    b.app = SimpleNamespace(bot=TgBot())
    b.out = Out()
    b.delivered = []
    b.sess = SimpleNamespace(compact_next=False, stop=None)
    b.hold = True
    b.limit, b._plan = False, (0.0, None)

    async def blocked(self):
        return self.limit

    monkeypatch.setattr(Bot, "_blocked", blocked)
    monkeypatch.setattr(Bot, "_output", lambda self, key: self.out)
    monkeypatch.setattr(Bot, "_should_hold", lambda self, key: self.hold)

    async def hold_prompt(self, key):
        return await self._prompt(key, "wake", "asleep", [[("Continue", "continue"), ("New", "new")]])

    async def new(self, key):
        self.delivered.append("NEW")

    def session(self, key):
        async def send(text, ids):
            self.delivered.append(("COMPACT " if self.sess.compact_next else "") + text)
        self.sess.send = send
        return self.sess

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


@pytest.mark.parametrize(
    "idle, connected, held",
    [(56 * 60, False, False), (59 * 60, False, True), (2 * 3600, True, False), (10 * 60, False, False)],
)
def test_holds_only_once_the_cache_is_cold(tmp_path, idle, connected, held):
    b = object.__new__(Bot)
    b.ttl = 3600
    state = ConvState(cwd=str(tmp_path), session_id="s1", last_api_at=bot_mod.time.time() - idle)
    b.sessions = {KEY: SimpleNamespace(state=state, connected=connected, pending=False)}
    assert b._should_hold(KEY) is held


@pytest.fixture
def auto(bot, monkeypatch):
    """WAKE=auto with a big context, Haiku stubbed: set bot.verdict to what it answers."""
    bot.cfg.wake = "auto"
    state = bot.store.get(KEY)
    state.context_tokens = 180_000
    bot.store.put(KEY, state)
    bot.verdict, bot.asked = None, []

    async def recent(cwd):
        return [SimpleNamespace(session_id="s1", custom_title="Fix the parser", summary=None, first_prompt=None)]

    async def decide(cli, cwd, title, history, message):
        bot.asked.append((title, message))
        return bot.verdict

    monkeypatch.setattr(bot_mod.history, "recent", recent)
    monkeypatch.setattr(triage, "excerpt", lambda sid: "User: fix it")
    monkeypatch.setattr(triage, "decide", decide)
    return bot


async def test_auto_same_compacts_and_explains(auto):
    auto.verdict = triage.Verdict(True, "sigue con el parser", 0.002)
    await auto._submit(KEY, "y los tests?")
    assert auto.asked == [("Fix the parser", "y los tests?")]
    assert auto.delivered == ["COMPACT y los tests?"]
    text, markup = auto.out.sent[0]
    assert "Compacting" in text and "sigue con el parser" in text and "180k tokens" in text and markup is None
    assert "Haiku for 0.002 €" in text


async def test_auto_new_starts_one(auto):
    auto.verdict = triage.Verdict(False, "otro tema", 0.002)
    await auto._submit(KEY, "haz una web")
    assert auto.delivered == ["NEW", "haz una web"]
    assert "New conversation" in auto.out.sent[0][0] and "Fix the parser" in auto.out.sent[0][0]


async def test_auto_small_context_just_continues(auto):
    state = auto.store.get(KEY)
    state.context_tokens = 10_000
    auto.store.put(KEY, state)
    await auto._submit(KEY, "hola")
    assert auto.delivered == ["hola"] and auto.asked == [] and auto.out.sent == []


async def test_auto_without_verdict_asks(auto):
    await auto._submit(KEY, "hola")
    assert auto.delivered == [] and len(auto.out.sent) == 1 and auto.out.sent[0][1] is not None


@pytest.mark.parametrize(
    "answer, parsed",
    [("SAME: same task", (True, "same task")), ("new - otro tema", (False, "otro tema")),
     ("**NEW**: x", (False, "x")), ("maybe", None),
     ("Es un cambio de tema.\nNEW", (False, "Es un cambio de tema.")),
     ("sigue\ncon el parser\n**SAME**", (True, "sigue con el parser"))],
)
def test_parse(answer, parsed):
    assert triage.parse(answer) == parsed


@pytest.fixture
def limit(bot):
    """A plan limit is stopping Claude; what would ask about the cache stays out of the way."""
    bot.limit, bot.hold = True, False
    return bot


async def test_limit_holds_messages_and_says_so_once(limit, tmp_path):
    await limit._submit(KEY, "one", [1])
    await limit._submit(KEY, "two", [2])
    assert limit.delivered == []
    assert len(limit.out.notices) == 1 and "Plan limit reached" in limit.out.notices[0]
    saved = Store(tmp_path / "sessions.json").get(KEY)  # what a restart finds
    assert [m["text"] for m in saved.held] == ["one", "two"] and saved.limit_until > 0


async def test_limit_lifted_asks_then_sends_them_all(limit):
    await limit._submit(KEY, "one")
    await limit._submit(KEY, "two")
    state = limit.store.get(KEY)
    await limit._check_limit(KEY, state)
    assert limit.out.sent == []  # still before the time it should end
    state.limit_until = 1.0
    await limit._check_limit(KEY, state)
    assert limit.out.sent == []  # past it, but the plan still says it's stopped
    limit.limit = False
    await limit._check_limit(KEY, state)
    assert limit.delivered == [] and "Send 2 waiting messages" in limit.out.sent[0][0]
    assert state.limit_until == 0 and state.prompt["kind"] == "limit"
    await limit._submit(KEY, "three")  # waits for the same answer
    await limit._check_limit(KEY, state)
    assert len(limit.out.sent) == 1
    query, rest = tap(limit, "send")
    await limit._on_guard(KEY, query, rest)
    assert limit.delivered == ["one", "two", "three"] and state.held == []


async def test_limit_lifted_drop(limit):
    await limit._submit(KEY, "one")
    state = limit.store.get(KEY)
    state.limit_until, limit.limit = 1.0, False
    await limit._check_limit(KEY, state)
    query, rest = tap(limit, "drop")
    await limit._on_guard(KEY, query, rest)
    assert limit.delivered == [] and state.held == []


async def test_limit_lifted_with_a_cold_cache_goes_on_to_the_wake_choice(limit):
    await limit._submit(KEY, "one")
    state = limit.store.get(KEY)
    state.limit_until, limit.limit = 1.0, False
    await limit._check_limit(KEY, state)
    limit.hold = True
    query, rest = tap(limit, "send")
    await limit._on_guard(KEY, query, rest)
    assert limit.delivered == [] and state.prompt["kind"] == "wake" and len(state.held) == 1


async def test_prompt_not_sent_when_limit_lifts_tries_again(limit):
    await limit._submit(KEY, "one")
    state = limit.store.get(KEY)
    state.limit_until, limit.limit, limit.out.fail = 1.0, False, True
    await limit._check_limit(KEY, state)
    assert limit.delivered == [] and state.limit_until == 1.0
    limit.out.fail = False
    await limit._check_limit(KEY, state)
    assert state.prompt["kind"] == "limit"


async def test_new_conversation_during_a_limit_keeps_them_waiting(limit):
    await limit._submit(KEY, "one")
    await limit._release(KEY)
    assert limit.delivered == [] and len(limit.store.get(KEY).held) == 1


async def test_turned_down_messages_go_before_those_already_held(bot):
    state = bot.store.get(KEY)
    state.held = [{"text": "later", "ids": []}]
    await bot._on_limited(KEY, [{"text": "first", "ids": []}], 2_000_000_000.0)
    assert [m["text"] for m in state.held] == ["first", "later"] and state.limit_until == 2_000_000_000.0
    assert "resets in" in bot.out.notices[0]


async def test_limit_with_nothing_waiting_ends_silently(bot):
    await bot._on_limited(KEY, [], None)
    state = bot.store.get(KEY)
    assert bot.out.notices == [] and state.limit_until > 0
    state.limit_until = 1.0
    await bot._check_limit(KEY, state)
    assert state.limit_until == 0 and bot.out.sent == []
