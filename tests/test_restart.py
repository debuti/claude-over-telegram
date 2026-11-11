"""Taking up a new version on its own once everything sleeps."""

import subprocess
import time
from types import SimpleNamespace

import pytest

from cot import __version__, container
from cot import bot as bot_module
from cot.bot import Bot, newer_version
from cot.store import ConvState, Store

KEY = (1, 2)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    b = object.__new__(Bot)
    b.cfg = SimpleNamespace(docker=False)
    b.ttl = 3600
    b.store = Store(tmp_path / "sessions.json")
    b.store.put(KEY, ConvState(cwd=str(tmp_path), session_id="s1"))
    b.sessions, b.login, b._restarting = {}, None, False
    b.stopped = []
    b.app = SimpleNamespace(stop_running=lambda: b.stopped.append(True))
    monkeypatch.setattr(bot_module, "newer_version", lambda repo: "9.9.9")
    monkeypatch.setattr(container, "any_running", lambda: False)
    return b


def session(pending=False, connected=False):
    return SimpleNamespace(pending=pending, connected=connected)


async def test_restarts_when_all_sleep(bot):
    await bot._restart_if_outdated()
    await bot._restart_if_outdated()
    assert bot.stopped == [True]  # and only once
    assert bot._restarting


@pytest.mark.parametrize(
    "awake",
    [
        lambda b: b.sessions.update({KEY: session(connected=True)}),
        lambda b: b.sessions.update({KEY: session(pending=True)}),
        lambda b: setattr(b.store.get(KEY), "last_api_at", time.time()),
        lambda b: setattr(b, "login", SimpleNamespace(waiting=True)),
    ],
    ids=["claude up", "under way", "cache alive", "login half-way"],
)
async def test_waits_while_something_is_awake(bot, awake):
    awake(bot)
    await bot._restart_if_outdated()
    assert not bot.stopped


async def test_waits_for_a_running_container(bot, monkeypatch):
    bot.cfg.docker = True
    monkeypatch.setattr(container, "any_running", lambda: True)
    await bot._restart_if_outdated()
    assert not bot.stopped


async def test_stays_on_the_latest_version(bot, monkeypatch):
    monkeypatch.setattr(bot_module, "newer_version", lambda repo: None)
    await bot._restart_if_outdated()
    assert not bot.stopped


def checkout(path, version):
    (path / "cot").mkdir()
    init = path / "cot" / "__init__.py"
    init.write_text(f'__version__ = "{version}"\n')
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return init


def test_newer_version_only_when_committed(tmp_path):
    init = checkout(tmp_path, "9.9.9")
    assert newer_version(tmp_path) == "9.9.9"
    (tmp_path / "untracked.txt").write_text("not part of a change yet")
    assert newer_version(tmp_path) == "9.9.9"
    init.write_text('__version__ = "9.9.10"\n')
    assert newer_version(tmp_path) is None


def test_no_newer_version_on_the_same_one(tmp_path):
    checkout(tmp_path, __version__)
    assert newer_version(tmp_path) is None


@pytest.fixture
def announcer(bot, tmp_path):
    bot.cfg.state_dir = tmp_path
    bot.sent, bot.gone = [], set()

    def output(key):
        async def send_html(text, silent=False):
            bot.sent.append((key, text, silent))
            return None if key in bot.gone else 1

        return SimpleNamespace(send_html=send_html)

    bot._assigned, bot._output = lambda key: True, output
    return bot


async def test_a_new_version_is_announced_once_and_quietly(announcer):
    await announcer._announce_version()
    await announcer._announce_version()
    assert announcer.sent == [(KEY, f"🔄 Updated to v{__version__}", True)]


async def test_the_same_version_says_nothing(announcer, tmp_path):
    (tmp_path / "version").write_text(__version__)
    await announcer._announce_version()
    assert announcer.sent == []


async def test_it_goes_to_the_latest_conversation_or_the_next_if_its_topic_is_gone(announcer, tmp_path):
    announcer.store.put((1, 3), ConvState(cwd=str(tmp_path), last_active=20.0))
    announcer.store.put((1, 4), ConvState(cwd=str(tmp_path), last_active=10.0))
    announcer.gone = {(1, 3)}
    await announcer._announce_version()
    assert [key for key, _, _ in announcer.sent] == [(1, 3), (1, 4)]


async def test_undelivered_it_tries_again_on_the_next_start(announcer, tmp_path):
    announcer.gone = {KEY}
    await announcer._announce_version()
    assert not (tmp_path / "version").exists()


async def test_with_no_conversation_it_only_remembers_the_version(announcer, tmp_path):
    announcer.store = Store(tmp_path / "empty.json")
    await announcer._announce_version()
    assert announcer.sent == []
    assert (tmp_path / "version").read_text() == __version__


async def test_it_goes_first_to_the_conversation_working_on_the_bot(announcer, tmp_path):
    from cot.config import REPO_DIR

    announcer.store.put((1, 3), ConvState(cwd=str(tmp_path), last_active=20.0))
    announcer.store.put((1, 4), ConvState(cwd=str(REPO_DIR), last_active=10.0))
    await announcer._announce_version()
    assert [key for key, _, _ in announcer.sent] == [(1, 4)]
