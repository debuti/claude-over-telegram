"""The plan usage history and its chart."""

import json

from cot import usagehist

DATA = {"limits": [{"kind": "session", "percent": 12.0}, {"kind": "weekly_all", "percent": 40.0}]}


def test_kept_at_most_every_few_minutes_and_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(usagehist, "_last_at", 0.0)
    path = tmp_path / "h.jsonl"
    old = 1_000_000_000
    assert usagehist.record(path, DATA, now=old)
    assert not usagehist.record(path, DATA, now=old + 60)
    now = old + 40 * 86400
    assert usagehist.record(path, DATA, now=now)  # a new day: drops what is over a month old
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["t"] for r in rows] == [now] and rows[0]["l"] == {"5-hour session": 12.0, "Week, all models": 40.0}


def test_picks_up_after_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "h.jsonl"
    path.write_text(json.dumps({"t": 5000, "l": {"x": 1}}) + "\n")
    monkeypatch.setattr(usagehist, "_last_at", 0.0)
    assert not usagehist.record(path, DATA, now=5100)


def test_charts(tmp_path, monkeypatch):
    monkeypatch.setattr(usagehist, "_last_at", 0.0)
    path = tmp_path / "h.jsonl"
    assert usagehist.weekly_chart(path, now=10_000) is None and usagehist.session_chart(path, 7, now=10_000) is None
    for i in range(10):
        usagehist.record(path, DATA, now=1000 + i * 300)
    assert usagehist.weekly_chart(path, now=4000)[:4] == b"\x89PNG"
    assert usagehist.session_chart(path, 7, now=4000)[:4] == b"\x89PNG"


def test_week_starts_at_its_reset_or_on_friday_evening():
    reset = 1_800_000_000
    row = {"t": reset - 3 * 86400, "l": {}, "r": {"Week, all models": reset + 40}}  # jitter: to the hour
    assert usagehist.week_start(row, "Week, all models") == round((reset - usagehist.WEEK) / 3600) * 3600
    start = usagehist.week_start({"t": reset, "l": {}, "r": {}}, "Week, all models")
    from datetime import datetime, timezone
    at = datetime.fromtimestamp(start, timezone.utc)
    assert (at.weekday(), at.hour, at.minute) == (4, 20, 0) and 0 <= reset - start < usagehist.WEEK
