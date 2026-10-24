import asyncio
import time

import pytest

from cot import session as session_mod
from cot.session import Session
from cot.store import ConvState

from .fakes import INTERRUPT, FakeClient, FakeOutput, answer, result


@pytest.fixture(autouse=True)
def fake_cli(monkeypatch):
    FakeClient.scripts, FakeClient.queries = [], []
    monkeypatch.setattr(session_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(session_mod, "GATHER_SECONDS", 0.01)
    monkeypatch.setattr(Session, "_options", lambda self: None)


async def until(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)


def make(state=None, **kwargs):
    state = state or ConvState(cwd="/tmp", session_id="sess-1")
    sess = Session(state, out=FakeOutput(), on_state=lambda st: None, default_mode="auto", cli_path=None,
                   cli_env={}, sandbox_bash=False, **kwargs)
    sess.entries = []

    async def on_turn_end(entry):
        sess.entries.append(entry)

    sess.on_turn_end = on_turn_end
    sess.start()
    return sess


async def test_turn_logs_the_cache_probe():
    sess = make(ttl=3600)
    FakeClient.scripts = [[answer(read=0, write=40_000), answer(read=40_000, write=500), result()]]
    await sess.send("hi")
    await until(lambda: sess.entries)
    e = sess.entries[0]
    assert FakeClient.queries == ["hi"]
    assert e["turns"] == 1 and e["by"] == "user" and e["fresh_process"] is True
    assert e["first_call"] == {"read": 0, "write": 40_000, "input": 5}
    assert e["context_tokens"] == 40_505 and e["idle_s"] is None and e["cache_break"] is False
    assert sess.state.last_api_at == pytest.approx(time.time(), abs=5)
    await sess.close()


async def test_cache_break_within_the_ttl():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=50_000, last_api_at=time.time() - 600)
    sess = make(state, ttl=3600)
    FakeClient.scripts = [[answer(read=0, write=50_000), result()], [answer(read=50_000, write=100), result()]]
    await sess.send("hi")
    await until(lambda: sess.entries)
    first = sess.entries[0]
    assert first["cache_break"] is True and 595 <= first["idle_s"] <= 605
    assert first["estimated"] == 50_000 * 0.1  # warm, as predicted
    await sess.send("again")
    await until(lambda: len(sess.entries) == 2)
    assert sess.entries[1]["cache_break"] is False and sess.entries[1]["fresh_process"] is False
    await sess.close()


async def test_no_cache_break_past_the_ttl():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=50_000, last_api_at=time.time() - 7200)
    sess = make(state, ttl=3600)
    FakeClient.scripts = [[answer(read=0, write=50_000), result()]]
    await sess.send("hi")
    await until(lambda: sess.entries)
    assert sess.entries[0]["cache_break"] is False
    assert sess.entries[0]["estimated"] == 50_000 * 2  # cold, 1-hour writes
    await sess.close()


async def test_stop_during_compaction_drops_the_message():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=5000)
    sess = make(state, compact_at=1000)
    FakeClient.scripts = [[INTERRUPT, result(terminal_reason="aborted_streaming")]]
    await sess.send("hi")
    await until(lambda: FakeClient.queries == ["/compact"])
    running, dropped = await sess.stop()
    assert running
    await until(lambda: not sess.busy)
    await asyncio.sleep(0.05)
    assert FakeClient.queries == ["/compact"]  # "hi" never went
    assert sess.state.context_tokens == 5000  # a stopped compaction left it as it was
    assert sess.state.pending == []
    assert sess.out.notices[-1] == "🛑 Stopped"
    await sess.close()


async def test_failed_compaction_does_not_count():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=5000, after_compact=0)
    sess = make(state, compact_at=1000)
    FakeClient.scripts = [[result(is_error=True)], [answer(read=5000), result()]]
    await sess.send("hi")
    await until(lambda: len(sess.entries) == 2)
    assert FakeClient.queries == ["/compact", "hi"]
    assert sess.state.after_compact == 0
    assert [e["by"] for e in sess.entries] == ["compact", "user"]
    await sess.close()


async def test_compaction_measures_what_it_left():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=5000)
    sess = make(state, compact_at=1000)
    FakeClient.scripts = [[result()], [answer(read=0, write=800), result()]]
    await sess.send("hi")
    await until(lambda: len(sess.entries) == 2)
    assert sess.state.after_compact == 805 and sess.state.context_tokens == 805
    # The answer after a compaction writes the new summary: not a broken cache.
    assert sess.entries[1]["just_compacted"] is True and sess.entries[1]["cache_break"] is False
    await sess.close()


async def test_failed_turn_tells_the_usage_log():
    sess = make()
    failed = []
    sess.on_turn_failed = lambda: failed.append(True)
    await sess.send("hi")
    await until(lambda: sess.entries)

    async def broken(text):
        raise RuntimeError("boom")

    sess._client.query = broken
    await sess.send("again")
    await until(lambda: failed)
    assert sess.out.notices[-1] == "⚠️ Error: boom"
    await sess.close()


def test_transcript_context(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "projects" / "-w").mkdir(parents=True)
    lines = [
        '{"type":"assistant","isSidechain":false,"message":{"usage":{"input_tokens":1,'
        '"cache_creation_input_tokens":2,"cache_read_input_tokens":3,"output_tokens":9}}}',
        '{"type":"assistant","isSidechain":true,"message":{"usage":{"input_tokens":100}}}',
        "garbage",
    ]
    (tmp_path / "projects" / "-w" / "abc.jsonl").write_text("\n".join(lines))
    context, mtime = session_mod.transcript_context("abc")
    assert context == 6 and mtime > 0
    assert session_mod.transcript_context("missing") == (0, 0.0)
