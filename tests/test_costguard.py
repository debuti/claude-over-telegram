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
