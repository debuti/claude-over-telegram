"""Agents writing to each other: who can be reached, the user's button, and what arrives."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from cot import permissions
from cot.bot import Bot
from cot.store import ConvState, Store

CHAT = 1
ME, WEB, API = (CHAT, 10), (CHAT, 20), (CHAT, 30)


class Out:
    def __init__(self, bot, key):
        self.bot, self.key = bot, key

    async def send_html(self, html, reply_markup=None, silent=False):
        if self.key in self.bot.dead_topics:
            return None
        self.bot.posted.append((self.key, html))
        if reply_markup:
            self.bot.asked.append((html, reply_markup))
        return 100 + len(self.bot.posted)

    async def flush(self):
        pass


class TgBot:
    def __init__(self):
        self.created = []

    async def create_forum_topic(self, chat_id, name):
        if name == "noforum":
            raise TelegramError("not a forum")
        self.created.append(name)
        return SimpleNamespace(message_thread_id=99)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    for name in ("me", "web", "api", "new", "noforum", ".hidden"):
        (tmp_path / name).mkdir()
    b = object.__new__(Bot)
    b.cfg = SimpleNamespace(workdir=tmp_path, allowed_user_ids={7})
    b.store = Store(tmp_path / "sessions.json")
    b.store.put(ME, ConvState(cwd=str(tmp_path / "me")))
    b.store.put(WEB, ConvState(cwd=str(tmp_path / "web"), last_active=5.0))
    b.store.put(API, ConvState(cwd=str(tmp_path / "api")))
    b.sessions = {}
    b.app = SimpleNamespace(bot=TgBot())
    b.posted, b.submitted, b.asked, b.dead_topics, b.cwds = [], [], [], set(), []
    b.hold = False

    async def submit(self, key, text, ids=None, check=True):
        self.submitted.append((key, text, ids))
        if self.hold:
            self.store.get(key).held.append({"text": text, "ids": ids})

    async def set_cwd(self, key, target):
        self.cwds.append((key, target))
        self.store.put(key, ConvState(cwd=str(tmp_path / target)))

    monkeypatch.setattr(Bot, "_key", lambda self, update: ME)
    monkeypatch.setattr(Bot, "_submit", submit)
    monkeypatch.setattr(Bot, "_set_cwd", set_cwd)
    monkeypatch.setattr(Bot, "_output", lambda self, key: Out(self, key))
    monkeypatch.setattr(Bot, "_should_hold", lambda self, key: self.hold)
    return b


def request(bot, to, text="please update the footer"):
    """The agent's call: it only puts the button before the user."""
    return asyncio.run(bot._send_to_agents(ME, to, text))


class Query:
    def __init__(self, data):
        self.data, self.from_user = data, SimpleNamespace(id=7)
        self.answered, self.edited, self.cleared = [], None, False

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edited = text

    async def edit_message_reply_markup(self, markup):
        self.cleared = True


def press(bot, choice=permissions.ALLOW, data=None):
    """The user's button on the oldest request still open."""
    if data is None:
        data = bot.asked[0][1].inline_keyboard[0][choice == permissions.DENY].callback_data
    query = Query(data)
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    return query


def send(bot, to, text="please update the footer"):
    """Requested and approved: what the prompt ends up saying."""
    request(bot, to, text)
    bot.posted.clear()
    return press(bot).edited


def test_lists_other_projects_but_not_itself(bot):
    listed = bot._list_agents(ME)
    assert "web: available" in listed and "new: no topic yet" in listed
    assert "me:" not in listed and ".hidden" not in listed


def test_a_topic_of_another_chat_doesnt_count(bot, tmp_path):
    bot.store.put((2, 50), ConvState(cwd=str(tmp_path / "new")))
    assert bot._agent_topics(ME)["new"] is None


def test_the_latest_active_topic_speaks_for_a_project(bot, tmp_path):
    bot.store.put((CHAT, 21), ConvState(cwd=str(tmp_path / "web"), last_active=9.0))
    bot.store.put((CHAT, 22), ConvState(cwd=str(tmp_path / "web"), last_active=1.0))
    assert bot._agent_topics(ME)["web"] == (CHAT, 21)


@pytest.mark.parametrize("to", [["../etc"], ["me"], ["web", "nope"], [".hidden"], []])
def test_bad_recipients_dont_reach_the_user(bot, to):
    with pytest.raises(ValueError):
        request(bot, to)
    assert not bot.asked and not bot.submitted


def test_a_message_too_long_to_show_whole_is_refused(bot):
    with pytest.raises(ValueError):
        request(bot, ["web"], "<" * 1000)
    assert not bot.asked


def test_the_agent_doesnt_wait_and_nothing_goes_without_the_button(bot):
    assert "Waiting" in request(bot, ["web", "new"])
    assert [key for key, _ in bot.posted] == [ME]
    assert not bot.submitted and not bot.app.bot.created


def test_a_request_the_user_cant_see_is_an_error(bot):
    bot.dead_topics.add(ME)
    with pytest.raises(ValueError):
        request(bot, ["web"])
    assert not bot.store.get(ME).agent_sends


def test_denied_nothing_goes_and_the_sender_is_told(bot):
    request(bot, ["web", "new"])
    bot.posted.clear()
    query = press(bot, permissions.DENY)
    assert "Denied" in query.edited
    assert not bot.posted and not bot.app.bot.created
    [(key, text, _)] = bot.submitted
    assert key == ME and "refused" in text and "web, new" in text and text.endswith("please update the footer")


def test_the_button_survives_a_restart(bot, tmp_path):
    request(bot, ["web"])
    bot.store = Store(tmp_path / "sessions.json")
    press(bot)
    assert [key for key, _, _ in bot.submitted] == [WEB]


def test_the_button_works_once(bot):
    request(bot, ["web"])
    press(bot)
    again = press(bot)
    assert "no longer active" in again.answered[0] and again.cleared
    assert len(bot.submitted) == 1


def test_someone_else_cant_press_it(bot):
    request(bot, ["web"])
    query = Query(bot.asked[0][1].inline_keyboard[0][0].callback_data)
    query.from_user.id = 8
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    assert not bot.submitted and bot.store.get(ME).agent_sends


def test_approved_it_shows_in_each_topic_and_reaches_claude_marked(bot):
    edited = send(bot, ["web", "api", "web"])
    assert "Allowed" in edited and "web: delivered\napi: delivered" in edited
    html = bot.asked[0][0]
    assert "me" in html and "web, api" in html and "update the footer" in html
    assert [key for key, _ in bot.posted] == [WEB, API]
    assert [key for key, _, _ in bot.submitted] == [WEB, API]  # and nothing back to the sender
    key, text, ids = bot.submitted[0]
    assert ids == [101]
    assert text.startswith("[From the agent of the project me") and text.endswith("please update the footer")


def test_a_slash_command_stays_a_message(bot):
    send(bot, ["web"], "/compact")
    assert not bot.submitted[0][1].startswith("/")


def test_html_in_the_message_is_shown_as_text(bot):
    send(bot, ["web"], "<b>{x}</b>")
    assert "&lt;b&gt;{x}&lt;/b&gt;" in bot.posted[0][1] and "&lt;b&gt;" in bot.asked[0][0]
    assert bot.submitted[0][1].endswith("<b>{x}</b>")


def test_a_project_without_topic_gets_one(bot):
    assert "new: delivered" in send(bot, ["new"])
    assert "new (new topic)" in bot.asked[0][0]
    assert bot.app.bot.created == ["new"] and bot.cwds == [((CHAT, 99), "new")]
    assert bot.submitted[0][0] == (CHAT, 99)


def test_a_sleeping_conversation_waits_and_the_user_sees_it(bot):
    bot.hold = True
    assert "held until the user decides" in send(bot, ["web"])
    assert [key for key, _, _ in bot.submitted] == [WEB]


def test_a_deleted_topic_is_reported_and_the_rest_still_go(bot):
    bot.dead_topics.add(WEB)
    edited = send(bot, ["web", "api"])
    assert "web: not sent" in edited and "api: delivered" in edited
    assert [key for key, _, _ in bot.submitted[:1]] == [API]
    key, text, _ = bot.submitted[1]
    assert key == ME and "web: not sent" in text and "api" not in text.split("It said")[0]


def test_a_chat_without_topics_is_reported(bot):
    edited = send(bot, ["noforum", "web"])
    assert "noforum: not sent (not a forum)" in edited and "web: delivered" in edited


def test_a_project_gone_by_the_time_of_the_button_is_reported(bot, tmp_path):
    request(bot, ["new"])
    (tmp_path / "new").rmdir()
    assert "new: not sent" in press(bot).edited


def test_a_conversation_moved_elsewhere_isnt_told(bot, tmp_path):
    request(bot, ["web"])
    bot.store.get(ME).cwd = str(tmp_path / "api")
    press(bot, permissions.DENY)
    assert not bot.submitted


def test_ask_waits_for_the_button():
    class Marked(Out):
        def __init__(self):
            self.sent = []
            self.bot = SimpleNamespace(edit_message_text=self._edit)
            self.chat_id = 1

        async def send_html(self, html, reply_markup=None, silent=False):
            self.sent.append(reply_markup)
            return 7

        async def _edit(self, *args, **kwargs):
            pass

    async def run():
        out, waits = Marked(), []
        task = asyncio.create_task(permissions.ask(out, "sure?", waits.append))
        await asyncio.sleep(0)
        allow = out.sent[0].inline_keyboard[0][0].callback_data
        _, req_id, choice = allow.split(":")
        assert await permissions.resolve(out, req_id, choice)
        assert await task == permissions.ALLOW and waits == [True, False]
        assert len(out.sent[0].inline_keyboard[0]) == 2  # no "always"

    asyncio.run(run())
