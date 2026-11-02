import json
from types import SimpleNamespace

import pytest

from cot import costguard
from cot.costguard import Calibration, estimate, format_estimate, is_cold, weighted


def cfg(cache_ttl="auto", api_key=False, debug_ttl=0):
    return SimpleNamespace(cache_ttl=cache_ttl, api_key=api_key, debug_ttl=debug_ttl)


def test_cache_ttl():
    assert costguard.cache_ttl(cfg()) == 3600
    assert costguard.cache_ttl(cfg(api_key=True)) == 300
    assert costguard.cache_ttl(cfg("1h", api_key=True)) == 3600
    assert costguard.cache_ttl(cfg("5m")) == 300
    assert costguard.cache_ttl(cfg("1h", debug_ttl=90)) == 90


@pytest.mark.parametrize(
    "idle, cold",
    [(0, False), (3539, False), (3540, True), (3600, True), (99999, True)],
)
def test_is_cold_at_the_edges(idle, cold):
    assert is_cold(10_000 + idle, 10_000, 3600) is cold


def test_is_cold_margin():
    assert not is_cold(1000 + 299, 1000, 300, margin=0)
    assert is_cold(1000 + 300, 1000, 300, margin=0)
    assert is_cold(1000 + 240, 1000, 300)  # 60 s early by default


def test_weighted():
    tokens = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
              "output_tokens": 2}
    assert weighted(tokens, 3600) == pytest.approx(10 + 200 + 100 + 10)
    assert weighted(tokens, 300) == pytest.approx(10 + 125 + 100 + 10)
    assert weighted({}) == 0


@pytest.mark.parametrize("ttl, write", [(300, costguard.WRITE_5M), (3600, costguard.WRITE_1H)])
def test_estimate_factors(ttl, write):
    warm = estimate(100_000, False, ttl)
    cold = estimate(100_000, True, ttl)
    assert warm.equivalent == pytest.approx(100_000 * costguard.READ)
    assert warm.factor == pytest.approx(1)
    assert cold.equivalent == pytest.approx(100_000 * write)
    assert cold.factor == pytest.approx(write / costguard.READ)
    assert cold.percent is None


def test_estimate_with_calibration():
    e = estimate(100_000, True, 3600, Calibration(1e-5, 20))
    assert e.percent == pytest.approx(2.0)
    assert format_estimate(e) == "100k tokens · ~20x a warm message · ~2% of your 5-hour window"
    assert format_estimate(estimate(180_000, False, 3600)) == "180k tokens"


def entry(before, after, parallel=0, read=1_000_000):
    return {"tokens": {"cache_read_input_tokens": read}, "parallel": parallel,
            "before": {"session": before}, "after": {"session": after}}


def write_log(tmp_path, entries):
    path = tmp_path / "usage.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return path


def test_calibration_needs_ten_samples(tmp_path):
    assert Calibration.from_log(write_log(tmp_path, [entry(10, 11)] * 9)) is None
    assert Calibration.from_log(tmp_path / "missing.jsonl") is None


def test_calibration_skips_parallel_and_flat_turns(tmp_path):
    entries = [entry(10, 11)] * 10 + [entry(10, 50, parallel=1)] * 30 + [entry(10, 10)] * 30
    cal = Calibration.from_log(write_log(tmp_path, entries))
    assert cal.samples == 10
    assert cal.percent_per_token == pytest.approx(1 / 100_000)  # 1M reads at 0.1


def test_calibration_takes_the_median_of_the_newest(tmp_path):
    entries = [entry(10, 90)] * 60 + [entry(10, 11)] * 50
    cal = Calibration.from_log(write_log(tmp_path, entries))
    assert cal.samples == costguard.CALIBRATION_ENTRIES
    assert cal.percent_per_token == pytest.approx(1 / 100_000)


STEPS = (30, 15, 10, 5, 2, 1)


@pytest.mark.parametrize(
    "left, last, step",
    [
        (3000, None, None),  # 50 min left: nothing yet
        (1800, None, 30),
        (1500, 30, None),  # 30 already rang
        (899, 30, 15),
        (700, None, 15),  # the bot was down at 30: only the closest rings
        (59, 2, 1),
        (30, 1, None),
        (0, None, None),  # expired
    ],
)
def test_alarm_step(left, last, step):
    assert costguard.alarm_step(left, 3600, STEPS, last) == step


def test_alarm_step_skips_steps_as_long_as_the_cache():
    assert costguard.alarm_step(240, 300, STEPS, None) is None  # 5 min with a 5-minute cache
    assert costguard.alarm_step(120, 300, STEPS, None) == 2


def test_cache_left():
    assert costguard.cache_left(1000 + 600, 1000, 3600) == 3600 - 60 - 600


def test_wake_costs():
    c = costguard.wake_costs(100_000, 0, 3600)
    assert c.keep == 200_000 and c.new == costguard.STARTUP * 2
    assert c.summary == costguard.SUMMARY and c.compact > c.keep
    assert costguard.wake_costs(100_000, 8_000, 300).summary == 8_000


def test_usd_per_token_and_format(tmp_path):
    log = tmp_path / "usage.jsonl"
    line = json.dumps({"cost": 0.5, "tokens": {"input_tokens": 100_000}, "ttl_s": 3600})
    log.write_text((line + "\n") * 12)
    usd = costguard.usd_per_token(log)
    assert usd == pytest.approx(5e-6)
    assert costguard.format_cost(200_000, usd, 0.9, None) == "~0.90 €"
    assert costguard.format_cost(1, None, 0.9, None) == ""


@pytest.mark.parametrize(
    "since_use, left, window_h, kept, due",
    [
        (54 * 60, 300, 3, True, True),  # first keep-alive, 5 minutes before expiry
        (54 * 60, 1200, 3, True, False),  # kept, but not yet
        (54 * 60, 300, 1, False, False),  # 1 hour: the cache's own life, nothing kept
        (2.5 * 3600, 300, 3, False, False),  # past the window: let it expire
        (54 * 60, 0, 3, False, False),  # already cold
    ],
)
def test_keepalive(since_use, left, window_h, kept, due):
    now = 100_000.0
    args = (now, left, now - since_use, 3600, window_h * 3600)
    assert costguard.keeps_alive(*args) is kept
    assert costguard.keepalive_due(*args) is due


@pytest.mark.parametrize(
    "limits, extra, out",
    [
        ([{"kind": "session", "percent": 40}], None, None),
        ([{"kind": "session", "percent": 100, "resets_at": "2026-11-02T18:00Z"}], None, "2026-11-02T18:00Z"),
        (
            [{"kind": "session", "percent": 100, "resets_at": "A"},
             {"kind": "weekly_all", "percent": 100, "resets_at": "B"}],
            None,
            "B",
        ),
        ([{"kind": "weekly_opus", "percent": 100}], None, None),
        ([{"kind": "session", "percent": 100}], {"is_enabled": True, "utilization": 20}, None),
        ([{"kind": "session", "percent": 100}], {"is_enabled": True, "utilization": 100}, ""),
    ],
)
def test_usage_blocked(limits, extra, out):
    from cot import usage

    assert usage.blocked({"limits": limits, "extra_usage": extra}) == out
