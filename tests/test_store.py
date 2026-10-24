import json

from cot.store import ConvState, Store


def test_old_sessions_json_loads(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "1:0": {"cwd": "/w", "session_id": "s", "verbose": True},
        "1:5": {"cwd": "/w/a", "parked": [{"text": "hi", "ids": [3]}], "after_compact": 9},
    }))
    store = Store(path)
    old = store.get((1, None))
    assert old.tools == "full" and old.last_api_at == 0 and old.held == [] and old.prompt is None
    parked = store.get((1, 5))
    assert parked.held == [{"text": "hi", "ids": [3]}]
    store.save()
    assert "parked" not in path.read_text()
    assert Store(path).get((1, 5)).held == parked.held


def test_roundtrip(tmp_path):
    store = Store(tmp_path / "sessions.json")
    state = ConvState(cwd="/w", last_api_at=12.5, prompt={"nonce": "abcdef", "msg_id": 7, "kind": "wake"})
    store.put((2, 3), state)
    assert Store(tmp_path / "sessions.json").get((2, 3)) == state
