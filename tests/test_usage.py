import asyncio
import time

import httpx
import pytest

from cot import usage


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(usage, "_last", None)
    monkeypatch.setattr(usage, "_retry_at", 0.0)
    monkeypatch.setattr(usage, "_backoff", 0.0)
    monkeypatch.setattr(usage, "_lock", None)


def fake_get(monkeypatch, responses):
    calls = []

    async def get(cli):
        calls.append(cli)
        r = responses.pop(0)
        if isinstance(r, httpx.Response):
            raise usage.RateLimited(usage._rate_limited(r))
        return r

    monkeypatch.setattr(usage, "_get", get)
    return calls


def test_readers_share_a_fresh_reading(monkeypatch):
    calls = fake_get(monkeypatch, [{"limits": []}, {"limits": [{"kind": "session"}]}])
    assert asyncio.run(usage.fetch("c", 30)) == {"limits": []}
    assert asyncio.run(usage.fetch("c", 30)) == {"limits": []}
    assert len(calls) == 1
    assert asyncio.run(usage.fetch("c")) == {"limits": [{"kind": "session"}]}


def test_a_429_holds_every_reader_back(monkeypatch):
    calls = fake_get(monkeypatch, [{"limits": []}, httpx.Response(429, headers={"retry-after": "120"})])
    asyncio.run(usage.fetch("c"))
    with pytest.raises(usage.RateLimited) as e:
        asyncio.run(usage.fetch("c"))
    assert 115 < e.value.until - time.time() <= 120
    with pytest.raises(usage.RateLimited):
        asyncio.run(usage.fetch("c"))  # no request while waiting
    assert len(calls) == 2
    assert usage.last()[1] == {"limits": []}
    assert asyncio.run(usage.fetch("c", 60)) == {"limits": []}  # a recent reading still does


def test_backoff_doubles_without_retry_after(monkeypatch):
    fake_get(monkeypatch, [httpx.Response(429), httpx.Response(429)])
    with pytest.raises(usage.RateLimited):
        asyncio.run(usage.fetch("c"))
    assert usage._backoff == usage.BACKOFF_MIN
    monkeypatch.setattr(usage, "_retry_at", 0.0)
    with pytest.raises(usage.RateLimited) as e:
        asyncio.run(usage.fetch("c"))
    assert usage._backoff == 2 * usage.BACKOFF_MIN
    assert e.value.until - time.time() > usage.BACKOFF_MIN + 50
