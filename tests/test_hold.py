"""Held messages and the nonce-guarded prompt, with Telegram and the sessions stubbed out."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from cot import bot as bot_mod
from cot import login, triage, usage
from cot.bot import CONTINUE_PROMPT, Bot
from cot.session import KEEPALIVE_PROMPT
from cot.store import ConvState, Store

KEY = (1, None)
REAL_BLOCKED, REAL_HARD_BLOCKED = Bot._blocked, Bot._hard_blocked  # before the fixture stubs them


class TgBot:
    def __init__(self):
        self.edits, self.renames, self.rename_error = [], [], None

    async def edit_message_reply_markup(self, chat_id, msg_id, reply_markup=None):
        self.edits.append(msg_id)

    async def delete_message(self, chat_id, msg_id):
        pass

    async def edit_forum_topic(self, chat_id, thread_id, name):
        self.renames.append(name)
        if self.rename_error:
            raise self.rename_error


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

    async def text(self, md):
        self.sent.append((md, None))


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
        workdir=tmp_path, wake="ask", wake_small=30_000, eur_per_usd=0.9, state_dir=tmp_path, cli_path="claude",
        limit_reserve=False, api_key=False,
    )
    b.ttl = 3600
    b.store = Store(tmp_path / "sessions.json")
    b.store.put(KEY, ConvState(cwd=str(tmp_path), session_id="s1"))
    b.sessions, b.no_folder, b.texts, b.login = {}, {}, {}, None
    b.app = SimpleNamespace(bot=TgBot())
    b.out = Out()
    b.delivered = []
    b.sess = SimpleNamespace(compact_next=False, stop=None)
    b.hold = True
    b.limit, b._plan = False, (0.0, None)
    b._reserve, b._reserve_skip, b._rates = None, 0.0, {}

    async def blocked(self):
        return self.limit

    monkeypatch.setattr(Bot, "_blocked", blocked)
    monkeypatch.setattr(usage, "login_valid", lambda: False)
    monkeypatch.setattr(Bot, "_hard_blocked", blocked)
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


async def test_edit_that_changed_nothing_is_not_sent_again(bot):
    bot.hold = False
    bot._key = lambda update: KEY
    bot._learn_title = lambda key, msg: None
    msg = SimpleNamespace(chat_id=1, message_id=7, text="one")
    await bot._on_text(SimpleNamespace(effective_message=msg, edited_message=None, message=msg), None)
    await bot._on_text(SimpleNamespace(effective_message=msg, edited_message=msg, message=None), None)
    assert bot.delivered == ["one"]
    msg.text = "two"  # a real correction still goes
    await bot._on_text(SimpleNamespace(effective_message=msg, edited_message=msg), None)
    assert bot.delivered == ["one", "two"]


async def test_edit_of_a_message_held_for_a_while_is_not_sent_again(bot):
    bot._key = lambda update: KEY
    bot._learn_title = lambda key, msg: None
    await bot._submit(KEY, "one", [7])
    query, rest = tap(bot, "continue")
    await bot._on_guard(KEY, query, rest)
    msg = SimpleNamespace(chat_id=KEY[0], message_id=7, text="one")
    await bot._on_text(SimpleNamespace(effective_message=msg, edited_message=msg), None)
    assert bot.delivered == ["one"]


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

    async def recent(cwd, limit=5):
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
    assert len(limit.out.sent) == 1 and "Plan limit reached" in limit.out.sent[0][0]
    saved = Store(tmp_path / "sessions.json").get(KEY)  # what a restart finds
    assert [m["text"] for m in saved.held] == ["one", "two"] and saved.limit_until > 0


async def test_limit_lifted_asks_then_sends_them_all(limit):
    await limit._submit(KEY, "one")
    await limit._submit(KEY, "two")
    state = limit.store.get(KEY)
    await limit._check_limit(KEY, state)
    assert len(limit.out.sent) == 1  # still before the time it should end
    state.limit_until = 1.0
    await limit._check_limit(KEY, state)
    assert len(limit.out.sent) == 1  # past it, but the plan still says it's stopped
    limit.limit = False
    await limit._check_limit(KEY, state)
    assert limit.delivered == [] and "Send 2 waiting messages" in limit.out.sent[1][0]
    assert state.limit_until == 0 and state.prompt["kind"] == "limit"
    await limit._submit(KEY, "three")  # waits for the same answer
    await limit._check_limit(KEY, state)
    assert len(limit.out.sent) == 2
    query, rest = tap(limit, "send")
    await limit._on_guard(KEY, query, rest)
    assert limit.delivered == ["one", "two", "three"] and state.held == []


@pytest.mark.parametrize("action", ["send", "compact"])
async def test_limit_goes_on_by_itself_if_chosen_in_advance(limit, tmp_path, action):
    await limit._submit(KEY, "one")
    query, rest = tap(limit, action)
    await limit._on_guard(KEY, query, rest)
    assert limit.delivered == []  # not until it lifts
    state = limit.store.get(KEY)
    assert Store(tmp_path / "sessions.json").get(KEY).limit_auto == action
    await limit._submit(KEY, "two")
    state.limit_until = 1.0
    await limit._check_limit(KEY, state)
    assert limit.delivered == []  # the plan still says it's stopped
    limit.limit, limit.hold = False, True  # a cold cache asks nothing: that was answered
    await limit._check_limit(KEY, state)
    prefix = "COMPACT " if action == "compact" else ""
    assert limit.delivered == [prefix + "one", prefix + "two"]
    assert state.held == [] and state.limit_until == 0 and state.prompt is None


async def test_limit_chosen_in_advance_carries_on_with_the_cut_turn(limit):
    await limit._on_limited(KEY, [], 1.0, cut=True)
    query, rest = tap(limit, "send")
    await limit._on_guard(KEY, query, rest)
    limit.limit = False
    state = limit.store.get(KEY)
    await limit._check_limit(KEY, state)
    assert limit.delivered == [CONTINUE_PROMPT] and not state.limit_cut


async def test_limit_hit_again_keeps_the_choice_only_with_a_time_to_wait_for(limit):
    state = limit.store.get(KEY)
    state.limit_auto = "send"
    await limit._on_limited(KEY, [], 1.0, cut=True)
    assert state.limit_auto == "send" and "as you asked" in limit.out.sent[-1][0]
    state.limit_until = 0.0
    await limit._on_limited(KEY, [], None, cut=True)
    assert state.limit_auto == "" and state.prompt["kind"] == "schedule"


async def test_stop_cancels_going_on_by_itself(limit, monkeypatch):
    await limit._submit(KEY, "one")
    query, rest = tap(limit, "send")
    await limit._on_guard(KEY, query, rest)
    monkeypatch.setattr(Bot, "_key", staticmethod(lambda u: KEY))
    await limit._cmd_stop(SimpleNamespace(), None)
    assert limit.store.get(KEY).limit_auto == "" and limit.store.get(KEY).held == []


async def test_lost_login_holds_messages_until_it_is_back(bot, monkeypatch):
    bot.hold = False
    back = []
    monkeypatch.setattr(usage, "renewed_since", lambda when: bool(back))
    await bot._on_login_lost(KEY, [{"text": "one", "ids": [1]}], False)
    await bot._submit(KEY, "two", [2])
    assert len(bot.out.notices) == 1 and "/login" in bot.out.notices[0] and "1 message waiting" in bot.out.notices[0]
    state = bot.store.get(KEY)
    await bot._check_login(KEY, state)
    assert bot.delivered == [] and [m["text"] for m in state.held] == ["one", "two"]
    await bot._on_login_lost(KEY, [], False)  # a keep-alive finding out too: said once
    assert len(bot.out.notices) == 1
    back.append(True)
    await bot._check_login(KEY, state)
    assert bot.delivered == ["one", "two"] and state.login_lost_at == 0 and state.held == []


async def test_lost_login_that_cut_a_turn_carries_on_with_it(bot, monkeypatch):
    bot.hold = False
    monkeypatch.setattr(usage, "renewed_since", lambda when: True)
    await bot._on_login_lost(KEY, [], True)
    assert "cut short" in bot.out.notices[0] and "waiting" not in bot.out.notices[0]
    await bot._check_login(KEY, bot.store.get(KEY))
    assert bot.delivered == [bot_mod.LOGIN_CONTINUE_PROMPT]


async def test_refused_with_a_good_login_tries_again_then_asks_for_it(bot, monkeypatch):
    bot.hold = False
    monkeypatch.setattr(usage, "login_valid", lambda: True)
    monkeypatch.setattr(usage, "renewed_since", lambda when: False)
    await bot._on_login_lost(KEY, [], True)
    state = bot.store.get(KEY)
    assert "🌩" in bot.out.notices[-1] and "1/3" in bot.out.notices[-1] and "/login" not in bot.out.notices[-1]
    await bot._on_login_lost(KEY, [], False)  # a keep-alive finding out too: said once, no extra try
    assert len(bot.out.notices) == 1 and state.login_retries == 1
    await bot._check_login(KEY, state)
    assert bot.delivered == []  # not yet
    for _ in range(len(bot_mod.REFUSED_RETRIES)):
        state.login_retry_at = time.time() - 1
        await bot._check_login(KEY, state)
        assert bot.delivered[-1] == bot_mod.REFUSED_CONTINUE_PROMPT and state.login_lost_at == 0
        await bot._on_login_lost(KEY, [], True)
    assert "/login" in bot.out.notices[-1] and state.login_retry_at == 0 and state.login_lost_at


async def test_code_goes_to_the_login_and_not_to_claude(bot):
    bot.hold = False
    bot._key = lambda update: KEY
    bot._learn_title = lambda key, msg: None
    done = []

    class FakeLogin:
        key, waiting = KEY, True

        async def finish(self, code):
            done.append(code)

    bot.login = FakeLogin()

    async def send(text):
        msg = SimpleNamespace(chat_id=1, message_id=len(text), text=text)
        await bot._on_text(SimpleNamespace(effective_message=msg, edited_message=None, message=msg), None)

    await send("not a code at all")
    await send("abcdefghijklmnopqrstuvwxyz#123")
    assert bot.delivered == ["not a code at all"] and done == ["abcdefghijklmnopqrstuvwxyz#123"]
    assert "Logged in" in bot.out.notices[-1]
    bot.login = login.Login("claude", KEY)  # nothing running any more: a message like any other
    await send("abcdefghijklmnopqrstuvwxyz#456")
    assert bot.delivered[-1] == "abcdefghijklmnopqrstuvwxyz#456"


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
    assert "resets in" in bot.out.sent[0][0]


async def test_limit_with_nothing_waiting_ends_silently(bot):
    await bot._on_limited(KEY, [], None)
    state = bot.store.get(KEY)
    assert bot.out.notices == [] and state.limit_until > 0
    state.limit_until = 1.0
    await bot._check_limit(KEY, state)
    assert state.limit_until == 0 and bot.out.sent == []


async def test_cut_turn_with_nothing_waiting_offers_to_continue(bot):
    await bot._on_limited(KEY, [], 2_000_000_000.0, cut=True)
    assert "cut short" in bot.out.sent[0][0]
    state = bot.store.get(KEY)
    state.limit_until = 1.0
    bot.hold = False
    await bot._check_limit(KEY, state)
    text, markup = bot.out.sent[-1]
    assert "carry on" in text and state.limit_cut and state.limit_until == 0
    assert [b.text for row in markup.inline_keyboard for b in row] == ["▶️ Continue", "🗑 Leave it"]
    query, rest = tap(bot, "send")
    await bot._on_guard(KEY, query, rest)
    assert bot.delivered == [CONTINUE_PROMPT] and not state.limit_cut


async def test_continue_goes_ahead_of_the_waiting_messages_and_skips_the_triage(bot):
    await bot._on_limited(KEY, [{"text": "one", "ids": []}], None, cut=True)
    state = bot.store.get(KEY)
    state.limit_until = 1.0
    bot.hold = True  # a cold cache: the choice is in this prompt, none comes after it
    await bot._check_limit(KEY, state)
    assert "sending 1 waiting message too" in bot.out.sent[-1][0]
    query, rest = tap(bot, "send")
    await bot._on_guard(KEY, query, rest)
    assert bot.delivered == [CONTINUE_PROMPT, "one"] and state.prompt is None


async def test_cut_turn_with_a_big_cold_context_offers_to_compact(bot):
    await bot._on_limited(KEY, [], None, cut=True)
    state = bot.store.get(KEY)
    state.limit_until, state.context_tokens = 1.0, 180_000
    await bot._check_limit(KEY, state)
    text, markup = bot.out.sent[-1]
    assert "180k tokens" in text and "🗜 Compact it and continue" in [b.text for r in markup.inline_keyboard for b in r]
    query, rest = tap(bot, "compact")
    await bot._on_guard(KEY, query, rest)
    assert bot.delivered == ["COMPACT " + CONTINUE_PROMPT]


async def test_leaving_the_cut_turn_forgets_it_and_its_messages(bot):
    await bot._on_limited(KEY, [{"text": "one", "ids": []}], None, cut=True)
    state = bot.store.get(KEY)
    state.limit_until = 1.0
    await bot._check_limit(KEY, state)
    query, rest = tap(bot, "drop")
    await bot._on_guard(KEY, query, rest)
    assert bot.delivered == [] and state.held == [] and not state.limit_cut


async def test_message_sent_while_the_cut_turn_prompt_is_live_waits_for_it(bot):
    await bot._on_limited(KEY, [], None, cut=True)
    state = bot.store.get(KEY)
    state.limit_until, bot.hold = 1.0, False
    await bot._check_limit(KEY, state)
    await bot._submit(KEY, "one")
    assert bot.delivered == []
    query, rest = tap(bot, "send")
    await bot._on_guard(KEY, query, rest)
    assert bot.delivered == [CONTINUE_PROMPT, "one"]


async def test_cut_turn_survives_a_restart(bot, tmp_path):
    await bot._on_limited(KEY, [], 2_000_000_000.0, cut=True)
    saved = Store(tmp_path / "sessions.json").get(KEY)
    assert saved.limit_cut and saved.limit_until == 2_000_000_000.0


async def test_stop_forgets_the_cut_turn(bot, monkeypatch):
    await bot._on_limited(KEY, [], None, cut=True)
    monkeypatch.setattr(Bot, "_key", staticmethod(lambda u: KEY))
    await bot._cmd_stop(SimpleNamespace(), None)
    assert not bot.store.get(KEY).limit_cut and "won't carry on" in bot.out.notices[-1]


async def test_prompt_of_a_cut_turn_taken_off_is_asked_again(bot):
    await bot._on_limited(KEY, [{"text": "one", "ids": []}], None, cut=True)
    state = bot.store.get(KEY)
    state.limit_until, bot.hold = 1.0, False
    await bot._check_limit(KEY, state)
    await bot._release(KEY)  # /resume of this same conversation
    assert bot.delivered == [] and state.prompt is None
    await bot._check_limit(KEY, state)
    assert state.prompt["kind"] == "limit" and len(state.held) == 1


TOPIC = (1, 5)


@pytest.fixture
def topic(bot):
    bot.cfg.compact_at = 150_000
    bot.cfg.keepalive, bot.cfg.wake_small = 3600, 30_000
    bot._titles_pause = 0.0
    (bot.cfg.workdir / "web").mkdir()
    state = ConvState(cwd=str(bot.cfg.workdir / "web"), session_id="s1", title="web", context_tokens=45_000)
    bot.store.put(TOPIC, state)
    return bot, state


async def test_topic_name_shows_the_status_and_only_changes_when_it_does(topic):
    bot, state = topic
    await bot._check_title(TOPIC, state)
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == ["💤·7️⃣·web"]
    assert bot.store.get(TOPIC).shown_title == "💤·7️⃣·web"


async def test_topic_name_with_everything_on(topic):
    bot, state = topic
    state.last_api_at = bot_mod.time.time() - 600
    bot.sessions[TOPIC] = SimpleNamespace(state=state, waiting=False, pending=True, connected=True, queued=2)
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == ["⚙️·7️⃣·>45m·web·📨2"]


@pytest.mark.parametrize(
    "tokens, plan, shown",
    [
        (45_000, None, "💤·7️⃣·>90m·web"),  # 53 min to the keep-alive, and a whole cache after it
        (29_000, None, "💤·8️⃣·>45m·web"),  # too small for keep-alives
        (45_000, {"limits": [{"kind": "session", "percent": 100}]}, "💤·7️⃣·>45m·web"),  # the plan stops them
    ],
)
async def test_topic_name_counts_the_keep_alives_this_conversation_gets(topic, tokens, plan, shown):
    bot, state = topic
    bot.cfg.keepalive = 7200
    bot._plan = (0.0, plan)
    state.last_api_at = state.last_active = bot_mod.time.time()
    state.context_tokens = tokens
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == [shown]


async def test_topic_name_shows_no_cache_just_after_compacting(topic):
    bot, state = topic
    state.last_api_at = bot_mod.time.time() - 600
    state.context_tokens = 0
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == ["💤·🔟·web"]


async def test_topic_name_when_awake_and_idle(topic):
    bot, state = topic
    bot.sessions[TOPIC] = SimpleNamespace(state=state, waiting=False, pending=False, connected=True, queued=0)
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == ["🟢·7️⃣·web"]


async def test_topic_name_while_waiting_for_permission(topic):
    bot, state = topic
    bot.sessions[TOPIC] = SimpleNamespace(state=state, waiting=True, pending=True, connected=True, queued=0)
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == ["❓·7️⃣·web"]


async def test_topic_name_while_stopped_by_a_limit_counts_what_waits(topic):
    bot, state = topic
    state.limit_until, state.held = 2_000_000_000.0, [{"text": "one", "ids": []}]
    state.last_api_at = 1.0
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == ["⏸·7️⃣·🧊·web·📨1"]


async def test_topic_with_an_unknown_name_is_left_alone(topic):
    bot, state = topic
    state.title = None
    await bot._check_title(TOPIC, state)
    assert bot.app.bot.renames == []


async def test_name_learned_from_the_topic_creation_a_message_points_at(topic):
    bot, state = topic
    state.title = None
    created = SimpleNamespace(forum_topic_created=SimpleNamespace(name="Mi web"))
    bot._learn_title(TOPIC, SimpleNamespace(reply_to_message=created))
    assert state.title == "Mi web"
    bot._learn_title(TOPIC, SimpleNamespace(reply_to_message=SimpleNamespace(forum_topic_created=None)))
    assert state.title == "Mi web"


async def test_telegram_asking_to_slow_down_pauses_all_renames(topic):
    bot, state = topic
    bot.app.bot.rename_error = RetryAfter(30)
    await bot._check_title(TOPIC, state)
    assert state.shown_title is None and bot._titles_pause > bot_mod.time.time()
    bot.app.bot.rename_error = None
    await bot._check_title(TOPIC, state)
    assert len(bot.app.bot.renames) == 1


async def test_a_topic_that_refuses_the_rename_is_not_retried_until_it_changes(topic):
    bot, state = topic
    bot.app.bot.rename_error = BadRequest("Topic_id_invalid")
    await bot._check_title(TOPIC, state)
    await bot._check_title(TOPIC, state)
    assert len(bot.app.bot.renames) == 1


async def test_only_our_own_rename_notices_are_deleted(bot):
    bot.app.bot.id = 99
    deleted = []

    async def delete():
        deleted.append(True)

    def update(user_id):
        msg = SimpleNamespace(forum_topic_edited=SimpleNamespace(name="x"), forum_topic_created=None, delete=delete)
        return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="u"), effective_message=msg)

    await bot._on_stranger(update(7), None)
    assert deleted == []
    await bot._on_stranger(update(99), None)
    assert deleted == [True]


async def test_compacted_conversation_has_no_cache_alarms(bot, monkeypatch):
    monkeypatch.setattr(Bot, "_assigned", lambda self, key: True)
    bot.cfg.keepalive, bot.cfg.cache_alarms, bot.alarms = 3600, (5,), {}
    state = bot.store.get(KEY)
    state.last_api_at, state.context_tokens = time.time() - 3400, 40_000  # 2 min left
    state.last_active = state.last_api_at
    await bot._check_alarm(KEY, state)
    assert bot.alarms[KEY]["step"] == 5
    state.context_tokens = 0  # as compacting leaves it
    await bot._check_alarm(KEY, state)
    assert KEY not in bot.alarms


def test_excerpt_leaves_keepalives_out_and_keeps_how_messages_end(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "projects" / "x" / "s.jsonl"
    path.parent.mkdir(parents=True)
    said = [
        ("user", "hi"),
        ("assistant", "x" * 1000 + " shall I go on?"),
        ("user", KEEPALIVE_PROMPT),
        ("assistant", "ok"),
    ]
    lines = [json.dumps({"type": t, "message": {"content": c}}) for t, c in said]
    path.write_text("\n".join(lines))
    got = triage.excerpt("s").splitlines()
    assert len(got) == 2 and got[0] == "User: hi" and got[1].endswith("shall I go on?")


@pytest.fixture
def reserving(bot, monkeypatch):
    """At 98% of the 5-hour window with 5 hours left and one big warm conversation: the reserve is due."""
    monkeypatch.setattr(Bot, "_blocked", REAL_BLOCKED)
    monkeypatch.setattr(Bot, "_hard_blocked", REAL_HARD_BLOCKED)
    monkeypatch.setattr(Bot, "_assigned", lambda self, key: True)
    bot.hold = False
    bot.cfg.limit_reserve, bot.cfg.keepalive, bot.cfg.cache_alarms, bot.alarms = True, 3600, (), {}
    bot._rates, bot._plan_lock = {"session": 2.2e-5, "weekly_all": 1.5e-6}, asyncio.Lock()
    resets = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 3600))
    bot._plan = (time.time(), {"limits": [{"kind": "session", "percent": 98, "resets_at": resets}]})
    state = bot.store.get(KEY)
    state.last_api_at = state.last_active = time.time() - 60
    state.context_tokens = 100_000
    bot.store.put(KEY, state)
    return bot


async def test_the_reserve_holds_messages_and_says_what_for(reserving):
    await reserving._submit(KEY, "one", [1])
    assert reserving.delivered == []
    text, markup = reserving.out.sent[0]
    assert "Saving the last 3% of the 5-hour limit to keep 1 conversation warm" in text
    assert markup.inline_keyboard[0][0].text == "▶️ Send it anyway"
    query, rest = tap(reserving, "anyway")
    await reserving._on_guard(KEY, query, rest)
    assert reserving.delivered == ["one"]
    await reserving._submit(KEY, "two", [2])  # spent until it resets: no more holding
    assert reserving.delivered == ["one", "two"]


async def test_the_reserve_keeps_the_cache_warm_past_keepalive_hours(reserving):
    state = reserving.store.get(KEY)
    state.last_active = time.time() - 3 * 3600  # long past KEEPALIVE_HOURS
    state.last_api_at = time.time() - 3500  # its last keep-alive's cache is about to go
    state.limit_until = time.time() + 5 * 3600  # a message waits for the reset
    pings = []

    async def keepalive():
        pings.append(True)

    reserving.sess.keepalive, reserving.sess.pending = keepalive, False
    await reserving._check_alarm(KEY, state)
    assert pings == [True]
    reserving.cfg.limit_reserve, reserving._reserve = False, None  # without it the wait for the limit sends none
    await reserving._check_alarm(KEY, state)
    assert pings == [True]


async def test_catchup_goes_on_after_every_limit_without_asking(limit):
    state = limit.store.get(KEY)
    await limit._set_catchup(KEY, "auto")
    assert state.catchup == "send"
    await limit._on_limited(KEY, [], 1.0, cut=True)
    assert state.limit_auto == "send" and "as you asked (as after every limit)" in limit.out.sent[-1][0]
    limit.limit = False
    await limit._check_limit(KEY, state)
    assert limit.delivered == [CONTINUE_PROMPT]
    state.limit_auto, limit.limit = "", True  # the turn got through; a later limit goes on too
    await limit._on_limited(KEY, [], 2.0, cut=True)
    assert state.limit_auto == "send" and state.prompt["kind"] == "schedule"


async def test_catchup_set_during_a_limit_applies_to_it(limit):
    await limit._on_limited(KEY, [], 1.0, cut=True)
    state = limit.store.get(KEY)
    assert state.limit_auto == ""
    await limit._set_catchup(KEY, "compact")
    assert state.limit_auto == "compact"
    await limit._set_catchup(KEY, "ask")
    assert state.catchup == state.limit_auto == ""


async def test_limit_notice_sets_catchup_and_takes_it_back(limit):
    await limit._on_limited(KEY, [], 1.0, cut=True)
    state = limit.store.get(KEY)
    query, rest = tap(limit, "catchup-auto")
    await limit._on_guard(KEY, query, rest)
    assert state.catchup == state.limit_auto == "send"
    await limit._on_limited(KEY, [], 2.0, cut=True)  # hit again: the notice offers to go back to asking
    query, rest = tap(limit, "catchup-ask")
    await limit._on_guard(KEY, query, rest)
    assert state.catchup == state.limit_auto == ""


async def test_menu_opens_a_setting_sets_it_and_comes_back(bot, monkeypatch):
    monkeypatch.setattr(Bot, "_assigned", lambda self, key: True)
    monkeypatch.setattr(Bot, "_key", lambda self, update: KEY)
    bot.cfg.allowed_user_ids, bot.cfg.permission_mode = {1}, "auto"
    await bot._cmd_menu(None, None)
    html, markup = bot.out.sent[-1]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "⏰ ❓ Ask me" in labels and "🛑 Stop" in labels

    async def press(data):
        query = Query(data, None)
        query.from_user = SimpleNamespace(id=1)
        await bot._on_button(SimpleNamespace(callback_query=query), SimpleNamespace(args=None))
        return query

    query = await press("menu:catchup")
    assert "After a plan limit" in query.edited[-1]
    query = await press("menu:catchup=auto")
    assert bot.store.get(KEY).catchup == "send" and query.edited[-1].startswith("⚙️")
    await press("menu:close")


async def test_tldr_shows_the_projects_summary(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(Bot, "_key", lambda self, update: KEY)
    await bot._cmd_tldr(None, None)
    assert "No TLDR.md" in bot.out.notices[-1]
    (tmp_path / "TLDR.md").write_text("**App** does things.\n")
    await bot._cmd_tldr(None, None)
    assert bot.out.sent[-1][0] == "📝 **App** does things."  # Markdown, rendered by text()


async def test_sleep_lets_the_cache_go_until_the_next_message(bot, monkeypatch):
    monkeypatch.setattr(Bot, "_assigned", lambda self, key: True)
    monkeypatch.setattr(Bot, "_key", staticmethod(lambda u: KEY))

    async def ready(self, key):
        return True

    monkeypatch.setattr(Bot, "_ready", ready)
    bot.cfg.keepalive, bot.cfg.cache_alarms, bot.alarms = 3600, (5,), {}
    state = bot.store.get(KEY)
    state.last_api_at, state.context_tokens = time.time() - 3400, 40_000  # 2 min left
    state.last_active = state.last_api_at
    state.wakeups = [{"id": "w", "at": time.time() + 600}]
    await bot._cmd_sleep(SimpleNamespace(), None)
    assert state.asleep and state.wakeups == [] and "💤" in bot.out.notices[-1] and "alarm" in bot.out.notices[-1]
    await bot._check_alarm(KEY, state)
    assert KEY not in bot.alarms
    bot.hold = False
    await bot._submit(KEY, "back")
    assert not state.asleep
    await bot._check_alarm(KEY, state)
    assert bot.alarms[KEY]["step"] == 5
