"""What the next message will cost, for the cost warnings: pure functions, no Telegram, no SDK.

Every message re-sends the whole conversation. While the prompt cache holds it, the context
is read at a tenth of the input price; once the cache expires it is written again in full,
at 1.25x the input price with the 5-minute cache or 2x with the 1-hour one. Costs here are
"equivalent tokens": tokens weighted by those multipliers, in units of an input token. No
dollar prices: on a subscription what counts is the share of the plan, which comes from
calibrating against the usage log.
"""

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

# Price of each kind of token relative to an uncached input token (check against the current
# pricing page). Cache writes depend on the cache's TTL.
READ = 0.1
WRITE_5M = 1.25
WRITE_1H = 2.0
OUTPUT = 5.0

# Calibration: the newest entries of the usage log it looks at, and how many it needs.
CALIBRATION_ENTRIES = 50
CALIBRATION_MIN = 10


def cache_ttl(cfg) -> int:
    """Seconds the conversation's prompt cache lives, as the bot assumes it.

    CACHE_TTL=auto: the CLI's own default, 1 hour on a subscription login and 5 minutes with
    an API key (a subscription in overage also drops to 5 minutes, which can't be told from
    here). 5m or 1h are passed to the CLI too, so both agree. COT_DEBUG_TTL_SECONDS only
    changes what the bot believes, to see the warnings in minutes.
    """
    if cfg.debug_ttl:
        return cfg.debug_ttl
    if cfg.cache_ttl == "5m":
        return 300
    if cfg.cache_ttl == "1h":
        return 3600
    return 300 if cfg.api_key else 3600


def write_price(ttl: int) -> float:
    return WRITE_1H if ttl > 300 else WRITE_5M


def is_cold(now: float, last_api_at: float, ttl: int, margin: int = 60) -> bool:
    """True once the cache has expired, or is about to (margin seconds early: the cache's
    clock started when the last request was sent, a little before its answer came)."""
    return now - last_api_at >= ttl - margin


def cache_left(now: float, last_api_at: float, ttl: int, margin: int = 60) -> float:
    """Seconds until is_cold turns true."""
    return last_api_at + ttl - margin - now


def alarm_step(left: float, ttl: int, steps: tuple[int, ...], last: int | None) -> int | None:
    """The cache alarm (minutes left) due now, or None.

    Only the closest of the steps already reached rings, so an alarm missed while the bot was
    down doesn't bring the earlier ones with it; last is the one that rang for this cache. Steps
    as long as the cache itself would ring right after every answer, so they are left out.
    """
    if left <= 0:
        return None
    reached = [s for s in steps if left <= s * 60 < ttl]
    if not reached:
        return None
    step = min(reached)
    return step if last is None or step < last else None


def weighted(tokens: dict, ttl: int = 3600) -> float:
    """Equivalent tokens of a turn's usage ({input_tokens, cache_creation_input_tokens, …})."""
    t = {k: tokens.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                          "cache_read_input_tokens", "output_tokens")}
    return (
        t["input_tokens"]
        + t["cache_creation_input_tokens"] * write_price(ttl)
        + t["cache_read_input_tokens"] * READ
        + t["output_tokens"] * OUTPUT
    )


@dataclass(frozen=True)
class Calibration:
    """Share of the 5-hour window (in %) that one equivalent token takes."""

    percent_per_token: float
    samples: int

    @classmethod
    def from_log(cls, path: Path) -> "Calibration | None":
        """From the newest turns of usage.jsonl that ran alone and moved the window.

        None without enough of them: the warnings then leave the % out. The window's percent
        is a whole number, so a single small turn says little; the median of many does.
        """
        ratios: list[float] = []
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                e = json.loads(line)
                before, after = (e.get("before") or {}).get("session"), (e.get("after") or {}).get("session")
                if e.get("parallel") or before is None or after is None or after <= before:
                    continue
                equiv = weighted(e.get("tokens") or {}, e.get("ttl_s") or 3600)
            except (ValueError, AttributeError, TypeError):
                continue
            if equiv > 0:
                ratios.append((after - before) / equiv)
            if len(ratios) >= CALIBRATION_ENTRIES:
                break
        if len(ratios) < CALIBRATION_MIN:
            return None
        return cls(statistics.median(ratios), len(ratios))


@dataclass(frozen=True)
class Estimate:
    context: int  # tokens of context the message re-sends
    cold: bool
    equivalent: float  # its cost in equivalent tokens
    factor: float  # times a message with a warm cache
    percent: float | None  # of the 5-hour window, when calibrated


def estimate(context: int, cold: bool, ttl: int, calibration: Calibration | None = None) -> Estimate:
    """Cost of the next message's context; the message itself and the answer are small next to it."""
    warm = context * READ
    equivalent = context * write_price(ttl) if cold else warm
    factor = equivalent / warm if warm else 1.0
    percent = equivalent * calibration.percent_per_token if calibration else None
    return Estimate(context, cold, equivalent, factor, percent)


def kilo(tokens: float) -> str:
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else f"{tokens:.0f}"


def format_estimate(e: Estimate) -> str:
    """E.g. "180k tokens · ~20x a warm message · ~3% of your 5-hour window"."""
    parts = [f"{kilo(e.context)} tokens"]
    if e.cold:
        parts.append(f"~{e.factor:.0f}x a warm message")
    if e.percent is not None:
        pct = f"{e.percent:.0f}" if e.percent >= 1 else f"{e.percent:.1f}"
        parts.append(f"~{pct}% of your 5-hour window")
    return " · ".join(parts)
