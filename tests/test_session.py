import asyncio
import json
import time

import pytest

from cot import session as session_mod
from cot.session import Session, transcript_context
from cot.store import ConvState

from .fakes import INTERRUPT, FakeClient, FakeOutput, answer, compact_failed, limit_event, limit_hit, login_lost, result


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


async def test_claude_compacting_mid_turn_is_logged():
    from claude_agent_sdk import SystemMessage

    sess = make()
    boundary = SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "auto", "pre_tokens": 151_000}})
    FakeClient.scripts = [[answer(read=150_000), boundary, answer(read=0, write=20_000), result()]]
    await sess.send("long task")
    await until(lambda: sess.entries)
    assert sess.entries[0]["compactions"] == [151_000]
    assert sess.state.after_compact == 20_005
    assert sess.out.notices == ["🗜 Claude compacted the conversation (151k tokens of context) to go on"]
    await sess.close()


async def test_keepalive_is_hidden_and_not_use():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=50_000, last_active=1000.0)
    sess = make(state, ttl=3600)
    FakeClient.scripts = [[answer(read=50_000, text="ok"), result()]]
    await sess.keepalive()
    await sess.keepalive()  # one is already queued
    await until(lambda: sess.entries)
    assert FakeClient.queries == [session_mod.KEEPALIVE_PROMPT]
    assert sess.entries[0]["by"] == "keepalive" and sess.out.texts == []
    assert sess.state.last_active == 1000.0 and state.pending == []
    assert sess.state.last_api_at == pytest.approx(time.time(), abs=5)
    await sess.close()


async def test_message_with_a_keepalive_replaces_it():
    sess = make(ttl=3600)
    FakeClient.scripts = [[answer(text="hello"), result()]]
    await sess.keepalive()
    await sess.send("hi")
    await until(lambda: sess.entries)
    assert FakeClient.queries == ["hi"] and sess.entries[0]["by"] == "user" and sess.out.texts == ["hello"]
    await sess.close()


def limited(sess):
    """Collect what the session hands back when a plan limit stops Claude."""
    sess.limits = []

    async def on_limited(items, resets, cut):
        sess.limits.append(([item["text"] for item in items], resets, cut))

    sess.on_limited = on_limited
    return sess


async def test_messages_during_a_turn_go_together_and_stay_saved_until_then():
    sess = make()
    FakeClient.scripts = [[answer(), INTERRUPT, result()]]
    await sess.send("one")
    await until(lambda: FakeClient.queries == ["one"])
    await sess.send("two")
    await sess.send("three")
    await asyncio.sleep(0.05)
    assert FakeClient.queries == ["one"]
    assert [p["text"] for p in sess.state.pending] == ["two", "three"]  # what a restart passes on
    await sess._client.interrupt()
    await until(lambda: len(FakeClient.queries) == 2)
    assert FakeClient.queries[1] == "two\n\nthree"
    await until(lambda: not sess.state.pending)
    await sess.close()


async def test_queued_messages_are_passed_on_after_a_restart():
    state = ConvState(cwd="/tmp", session_id="sess-1", pending=[{"text": "one", "ids": []}, {"text": "two", "ids": []}])
    sess = make(state)
    await until(lambda: FakeClient.queries == ["one\n\ntwo"])
    await until(lambda: not sess.state.pending)
    await sess.close()


async def test_a_turn_claude_started_makes_messages_wait_for_its_end():
    sess = make()
    await sess.send("first")
    await until(lambda: sess.entries)
    await until(lambda: not sess.pending)
    client = sess._client
    await client._out.put(answer(text="a job finished"))
    await until(lambda: sess.busy)
    await sess.send("two")
    await sess.send("three")
    await asyncio.sleep(0.05)
    assert FakeClient.queries == ["first"]
    await client._out.put(result())
    await until(lambda: len(FakeClient.queries) == 2)
    assert FakeClient.queries[1] == "two\n\nthree"
    await sess.close()


async def test_stop_during_a_turn_claude_started_does_not_lose_the_next_message():
    sess = make()
    await sess.send("first")
    await until(lambda: not sess.pending and sess.entries)
    client = sess._client
    await client._out.put(answer())
    await until(lambda: sess.busy)
    await sess.stop()
    await client._out.put(result())
    await until(lambda: not sess.busy)
    await sess.send("next")
    await until(lambda: FakeClient.queries == ["first", "next"])
    await sess.close()


async def test_limit_hands_back_the_message_it_turned_down_and_the_queue():
    sess = limited(make())
    FakeClient.scripts = [[limit_event(1234), INTERRUPT, limit_hit(), result(is_error=True)]]
    await sess.send("one")
    await until(lambda: FakeClient.queries == ["one"])
    await sess.send("two")
    await sess._client.interrupt()
    await until(lambda: sess.limits)
    assert sess.limits == [(["one", "two"], 1234, False)]
    assert sess.state.pending == [] and sess.queued == 0
    assert sess.out.notices == [] and sess.out.texts == ["You've hit your session limit · resets 6pm (UTC)"]
    await asyncio.sleep(0.05)
    assert FakeClient.queries == ["one"]  # nothing goes on trying
    await sess.close()


async def test_the_same_message_twice_goes_once():
    sess = make()
    FakeClient.scripts = [[answer(), result()]]
    await sess.send("one", [7])
    await sess.send("one", [7])
    await sess.send("corrected", [7])  # an edit that changed it does go
    await until(lambda: FakeClient.queries)
    assert FakeClient.queries == ["one\n\ncorrected"]
    await sess.close()


async def test_a_message_handed_back_can_come_again():
    sess = without_login(make())
    FakeClient.scripts = [[login_lost(), result(is_error=True)], [answer(), result()]]
    await sess.send("one", [7])
    await until(lambda: sess.lost)
    await sess.send("one", [7])
    await until(lambda: FakeClient.queries == ["one", "one"])
    await sess.close()


def without_login(sess):
    sess.lost = []

    async def on_login_lost(items, cut):
        sess.lost.append(([item["text"] for item in items], cut))

    sess.on_login_lost = on_login_lost
    return sess


async def test_lost_login_hands_back_the_message_and_the_queue():
    sess = without_login(make())
    FakeClient.scripts = [[INTERRUPT, login_lost(), result(is_error=True)]]
    await sess.send("one")
    await until(lambda: FakeClient.queries == ["one"])
    await sess.send("two")
    await sess._client.interrupt()
    await until(lambda: sess.lost)
    assert sess.lost == [(["one", "two"], False)]
    assert sess.state.pending == [] and sess.queued == 0 and sess.out.notices == []
    await asyncio.sleep(0.05)
    assert FakeClient.queries == ["one"]
    await sess.close()


async def test_lost_login_in_mid_turn_counts_as_cut_short():
    sess = without_login(make())
    FakeClient.scripts = [[answer(), login_lost(), result(is_error=True)]]
    await sess.send("one")
    await until(lambda: sess.lost)
    assert sess.lost == [([], True)]
    await sess.close()


async def test_compaction_stopped_by_the_login_keeps_the_message_and_the_context():
    sess = without_login(make(compact_at=100))
    sess.state.context_tokens = 500
    FakeClient.scripts = [[compact_failed(), result()]]
    await sess.send("one")
    await until(lambda: sess.lost)
    assert sess.lost == [(["one"], False)]
    assert FakeClient.queries == ["/compact"]
    assert sess.compact_next and sess.state.context_tokens == 500
    await sess.close()


async def test_compaction_that_failed_otherwise_keeps_the_context():
    sess = make(compact_at=100)
    sess.state.context_tokens = 500
    FakeClient.scripts = [[compact_failed("not enough messages"), result()], [answer(), result()]]
    await sess.send("one")
    await until(lambda: FakeClient.queries == ["/compact", "one"])
    assert not sess._measure_compacted
    await sess.close()


async def test_a_turn_that_gets_through_ends_going_on_by_itself():
    sess = limited(make())
    sess.state.limit_auto = "send"
    FakeClient.scripts = [[limit_hit(), result(is_error=True)], [answer(), result()]]
    await sess.send("one")
    await until(lambda: sess.limits)
    assert sess.state.limit_auto == "send"  # the limit hit again: the next reset goes on too
    await sess.send("two")
    await until(lambda: not sess.state.limit_auto)
    await sess.close()


async def test_limit_in_mid_turn_does_not_repeat_the_message_being_worked_on():
    sess = limited(make())
    FakeClient.scripts = [[answer(), INTERRUPT, limit_hit(), result(is_error=True)]]
    await sess.send("one")
    await until(lambda: FakeClient.queries == ["one"])
    await sess.send("two")
    await sess._client.interrupt()
    await until(lambda: sess.limits)
    assert sess.limits == [(["two"], None, True)]
    await sess.close()


async def test_limit_during_compaction_compacts_again_later():
    sess = limited(make(compact_at=100))
    sess.state.context_tokens = 500
    FakeClient.scripts = [[limit_hit(), result(is_error=True)]]
    await sess.send("one")
    await until(lambda: sess.limits)
    await until(lambda: not sess.busy)
    assert FakeClient.queries == ["/compact"]
    assert sess.limits == [(["one"], None, False)]
    assert sess.state.context_tokens == 500 and sess.compact_next
    FakeClient.scripts = [[answer(), result()], [answer(), result()]]
    await sess.send("two")  # the limit has lifted
    await until(lambda: len(FakeClient.queries) == 3)
    assert FakeClient.queries[1:] == ["/compact", "two"]
    await sess.close()


async def test_limit_on_a_turn_claude_started_counts_as_cut_short():
    sess = limited(make())
    await sess.send("first")
    await until(lambda: not sess.pending and sess.entries)
    await sess._client._out.put(limit_hit())
    await until(lambda: sess.busy)
    await sess.send("two")
    await sess._client._out.put(result(is_error=True))
    await until(lambda: sess.limits)
    assert sess.limits == [(["two"], None, True)]
    await until(lambda: not sess.pending)
    assert FakeClient.queries == ["first"]
    await sess.close()


async def test_limit_message_leaves_the_context_and_cache_figures_alone():
    sess = limited(make())
    sess.state.context_tokens, sess.state.last_api_at = 90_000, 123.0
    FakeClient.scripts = [[limit_hit(), result(is_error=True)]]
    await sess.send("one")
    await until(lambda: sess.limits)
    assert sess.state.context_tokens == 90_000 and sess.state.last_api_at == 123.0
    await sess.close()


@pytest.mark.parametrize(
    "context, ttl, compacts",
    [(40_000, 3600, True), (10_000, 3600, False), (40_000, 30, False)],  # small, and already cold (margin 60 s)
)
async def test_compacts_before_sleeping_only_big_and_warm(monkeypatch, context, ttl, compacts):
    monkeypatch.setattr("cot.session.idle_disconnect", lambda: 0.05)
    sess = make(compact_asleep=30_000, ttl=ttl)
    FakeClient.scripts = [[answer(read=context, write=0), result()], [result()]]
    await sess.send("hi")
    await until(lambda: sess.entries and not sess.connected)
    assert FakeClient.queries == (["hi", "/compact"] if compacts else ["hi"])
    assert sess.state.context_tokens == (0 if compacts else context + 5)
    assert sess.out.notices == []  # no new message to leave the chat unread
    assert len(sess.out.notes) == compacts and all("before it goes to sleep" in n for n in sess.out.notes)
    await sess.close()


async def test_compacting_before_a_message_says_so():
    state = ConvState(cwd="/tmp", session_id="sess-1", context_tokens=5000)
    sess = make(state, compact_at=1000)
    FakeClient.scripts = [[result()], [answer(), result()]]
    await sess.send("hi")
    await until(lambda: len(sess.entries) == 2)
    assert sess.out.notices == ["🗜 Compacting the conversation (5k tokens of context) first…"]
    await sess.close()


def test_transcript_context_starts_over_at_a_compaction(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "projects" / "x" / "s.jsonl"
    path.parent.mkdir(parents=True)
    said = {"type": "assistant", "message": {"usage": {"input_tokens": 500}}}
    lines = [said, {"type": "system", "subtype": "compact_boundary"}]
    path.write_text("\n".join(json.dumps(e) for e in lines))
    assert transcript_context("s")[0] == 0
    path.write_text("\n".join(json.dumps(e) for e in [*lines, said]))
    assert transcript_context("s")[0] == 500
