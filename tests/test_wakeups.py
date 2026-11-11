"""Alarms Claude asks for (set_alarm): nothing is set without the user's button, and it wakes Claude."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cot.bot import Bot
from cot.store import ConvState, Store

ME = (1, 10)


class Out:
    def __init__(self, bot):
        self.bot = bot

    async def send_html(self, html, reply_markup=None, silent=False):
        if self.bot.dead:
            return None
        self.bot.posted.append(html)
        if reply_markup:
            self.bot.asked.append(reply_markup)
        return 100 + len(self.bot.posted)

    async def notice(self, text):
        self.bot.posted.append(text)

    async def flush(self):
        pass


class Query:
    def __init__(self, data):
        self.data, self.from_user = data, SimpleNamespace(id=7)
        self.answered, self.edited, self.markup, self.cleared = [], None, None, False

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edited, self.markup = text, reply_markup

    async def edit_message_reply_markup(self, markup):
        self.cleared = True


@pytest.fixture
def bot(tmp_path, monkeypatch):
    (tmp_path / "me").mkdir()
    b = object.__new__(Bot)
    b.cfg = SimpleNamespace(workdir=tmp_path, allowed_user_ids={7})
    b.store = Store(tmp_path / "sessions.json")
    b.store.put(ME, ConvState(cwd=str(tmp_path / "me")))
    b.sessions = {}
    b.posted, b.asked, b.submitted, b.dead = [], [], [], False

    async def submit(self, key, text, ids=None, check=True, hold=True):
        self.submitted.append((key, text, ids, hold))

    monkeypatch.setattr(Bot, "_key", lambda self, update: ME)
    monkeypatch.setattr(Bot, "_submit", submit)
    monkeypatch.setattr(Bot, "_output", lambda self, key: Out(self))
    monkeypatch.setattr(Bot, "_assigned", lambda self, key: True)
    return b


def ask(bot, minutes=30, reason="check the tests in job.log"):
    return asyncio.run(bot._ask_wakeup(ME, minutes, reason))


def press(bot, data):
    query = Query(data)
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    return query


def button(markup, i=0):
    return markup.inline_keyboard[0][i].callback_data


def ring(bot):
    asyncio.run(bot._check_wakeups(ME, bot.store.get(ME)))


def test_asking_only_shows_the_buttons(bot):
    assert "End your turn" in ask(bot)
    assert "30 min" in bot.posted[0] and "job.log" in bot.posted[0]
    [wake] = bot.store.get(ME).wakeups
    assert not wake["at"]
    ring(bot)
    assert not bot.submitted


@pytest.mark.parametrize("minutes,reason", [(0, "x"), (24 * 60 + 1, "x"), (5, "  "), (5, "<" * 1000)])
def test_bad_requests_dont_reach_the_user(bot, minutes, reason):
    with pytest.raises(ValueError):
        ask(bot, minutes, reason)
    assert not bot.asked and not bot.store.get(ME).wakeups


def test_a_request_the_user_cant_see_sets_nothing(bot):
    bot.dead = True
    with pytest.raises(ValueError):
        ask(bot)
    assert not bot.store.get(ME).wakeups


def test_only_a_few_at_once(bot):
    for _ in range(3):
        ask(bot)
    with pytest.raises(ValueError):
        ask(bot)


def test_denied_nothing_rings_and_claude_isnt_woken(bot):
    ask(bot)
    query = press(bot, button(bot.asked[0], 1))
    assert "Denied" in query.edited and query.markup is None
    assert not bot.store.get(ME).wakeups and not bot.submitted


def test_allowed_it_rings_when_due_and_wakes_claude_past_the_warning(bot, monkeypatch):
    ask(bot, 30)
    query = press(bot, button(bot.asked[0]))
    assert "Allowed" in query.edited and "cancel" in button(query.markup)
    ring(bot)
    assert not bot.submitted  # not yet
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 31 * 60)
    ring(bot)
    [(key, text, ids, hold)] = bot.submitted
    assert key == ME and "check the tests in job.log" in text and "31 min" in text
    assert ids and not hold
    assert not bot.store.get(ME).wakeups
    ring(bot)
    assert len(bot.submitted) == 1


def test_approved_late_it_rings_at_once(bot, monkeypatch):
    ask(bot, 5)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 60 * 60)
    press(bot, button(bot.asked[0]))
    ring(bot)
    assert len(bot.submitted) == 1


def test_cancelled_it_doesnt_ring(bot, monkeypatch):
    ask(bot, 5)
    query = press(bot, button(bot.asked[0]))
    query = press(bot, button(query.markup))
    assert "Cancelled" in query.edited and query.markup is None
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 10 * 60)
    ring(bot)
    assert not bot.submitted
    again = press(bot, button(bot.asked[0]))
    assert "no longer active" in again.answered[0]


def test_it_survives_a_restart(bot, tmp_path, monkeypatch):
    ask(bot, 5)
    bot.store = Store(tmp_path / "sessions.json")
    press(bot, button(bot.asked[0]))
    bot.store = Store(tmp_path / "sessions.json")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 6 * 60)
    ring(bot)
    assert len(bot.submitted) == 1


def test_a_topic_moved_to_another_folder_isnt_woken(bot, tmp_path, monkeypatch):
    ask(bot, 5)
    press(bot, button(bot.asked[0]))
    state = bot.store.get(ME)
    state.cwd = str(tmp_path / "other")
    bot.store.put(ME, state)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 6 * 60)
    ring(bot)
    assert not bot.submitted and "didn't ring" in bot.posted[-1]


def test_someone_else_cant_press_it(bot):
    ask(bot)
    query = Query(button(bot.asked[0]))
    query.from_user.id = 8
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    assert not bot.store.get(ME).wakeups[0]["at"]


def test_allowed_it_ends_claudes_process(bot):
    slept = []

    class Sess:
        connected = True
        container = "cot-me-10"

        async def sleep(self):
            slept.append(True)
            return True

    sess = Sess()
    sess.state = bot.store.get(ME)
    bot.sessions = {ME: sess}
    bot._sleeping = set()

    async def go():
        await bot._ask_wakeup(ME, 5, "look at job.log")
        query = Query(button(bot.asked[0]))
        await bot._on_button(SimpleNamespace(callback_query=query), None)
        await asyncio.gather(*bot._sleeping)

    asyncio.run(go())
    assert slept and "ended" in bot.posted[-1]
    assert bot.store.get(ME).wakeups[0]["container"] == "cot-me-10"


def test_without_a_process_its_container_is_still_known(bot):
    ask(bot)
    press(bot, button(bot.asked[0]))
    assert bot.store.get(ME).wakeups[0]["container"] == "cot-me-10"


def test_denied_claude_is_left_alone(bot):
    class Sess:
        connected = True

        async def sleep(self):
            raise AssertionError("not to be ended")

    sess = Sess()
    sess.state = bot.store.get(ME)
    bot.sessions = {ME: sess}
    bot._sleeping = set()
    ask(bot)
    press(bot, button(bot.asked[0], 1))
    assert not bot._sleeping


def test_the_topic_shows_an_alarm_clock_while_it_waits(bot):
    from cot import titles

    bot.cfg.compact_at = 0
    state = bot.store.get(ME)
    state.title = "me"
    assert bot._title(ME, state).startswith(titles.ASLEEP)
    ask(bot)
    press(bot, button(bot.asked[0]))
    assert bot._title(ME, bot.store.get(ME)).startswith(titles.ALARM)


def test_keepalives_last_until_the_alarm_when_cheaper(bot, monkeypatch):
    bot.cfg.keepalive, bot.ttl, bot._reserve = 3600, 3600, None
    now = time.time()
    state = bot.store.get(ME)
    state.context_tokens, state.last_active = 100_000, now
    assert bot._keepalive_window(state) == 3600
    state.wakeups = [{"id": "a", "at": now + 3 * 3600}]
    assert bot._keepalive_window(state) == pytest.approx(3 * 3600 + 120, abs=5)
    state.wakeups = [{"id": "a", "at": now + 23 * 3600}]  # costs more than writing it again
    assert bot._keepalive_window(state) == 3600
    state.wakeups = [{"id": "a", "at": 0.0}]  # asked, not approved
    assert bot._keepalive_window(state) == 3600
