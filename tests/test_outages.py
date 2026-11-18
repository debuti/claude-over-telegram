from cot.outages import Outages


def test_intervals_by_kind_reported_after_recovery(tmp_path):
    o = Outages(tmp_path / "o.json")
    o.failed("502 Bad Gateway", now=1000)
    o.failed("502 Bad Gateway", now=1060)
    o.failed("timed out", now=1100)
    assert o.report() is None  # still down
    o.answered(now=1150)
    assert [i[2] for i in o.intervals] == ["502 Bad Gateway", "timed out"]
    assert o.intervals[0][:2] == [1000, 1100] and o.intervals[1][:2] == [1100, 1150]
    assert "502 Bad Gateway" in o.report() and "timed out" in o.report()
    o.reported()
    assert o.report() is None


def test_short_blip_ignored_but_earlier_outage_kept(tmp_path):
    o = Outages(tmp_path / "o.json")
    o.failed("timed out", now=0)
    o.failed("timed out", now=100)
    o.answered(now=150)
    o.failed("timed out", now=1000)
    o.answered(now=1030)
    assert len(o.intervals) == 1


def test_survives_restart_and_bot_downtime(tmp_path):
    o = Outages(tmp_path / "o.json")
    o.failed("timed out", now=0)
    o.failed("timed out", now=100)
    o = Outages(tmp_path / "o.json")
    assert o.down
    o.answered(now=5000)  # the bot was off meanwhile: ends at the last failure seen
    assert o.intervals == [[0, 100, "timed out"]]
