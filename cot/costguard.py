"""What the next message will cost, for the cost warnings: pure functions, no Telegram, no SDK.

Every message re-sends the whole conversation. While the prompt cache holds it, the context
is read at a tenth of the input price; once the cache expires it is written again in full,
at 1.25x the input price with the 5-minute cache or 2x with the 1-hour one. Costs here are
"equivalent tokens": tokens weighted by those multipliers, in units of an input token. No
dollar prices: on a subscription what counts is the share of the plan, which comes from
calibrating against the usage log.
"""

import json
import math
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


def keepalive_lead(ttl: int) -> int:
    """Seconds before the cache expires at which a keep-alive goes: early enough to beat the
    idle stop of the Claude process (a few minutes before a 1-hour cache ends)."""
    return min(360, ttl // 5)


def keeps_alive(now: float, left: float, last_used: float, ttl: int, window: float) -> bool:
    """Whether a keep-alive will refresh this cache before it expires.

    The cache is kept warm until about window seconds after the conversation was last used:
    keep-alives go out during its first window - ttl seconds (the last one lasts a ttl more).
    A window no longer than the cache's own life keeps nothing.
    """
    ping_at = now + left - keepalive_lead(ttl)
    return window > ttl and left > 0 and ping_at - last_used < window - ttl


def keepalive_due(now: float, left: float, last_used: float, ttl: int, window: float) -> bool:
    return left <= keepalive_lead(ttl) and keeps_alive(now, left, last_used, ttl, window)


def warm_left(now: float, left: float, last_used: float, ttl: int, window: float) -> float:
    """Seconds until the cache is really lost: left, plus what the keep-alives still to come add."""
    at = now
    while keeps_alive(at, left, last_used, ttl, window):
        at += max(left - keepalive_lead(ttl), 1)
        left = cache_left(at, at, ttl)
    return at + left - now


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


def usd_per_token(path: Path) -> float | None:
    """Dollars one equivalent token cost in the newest turns of usage.jsonl (median), or None.

    What the CLI reports per turn: on a subscription it is what the turn would have cost on
    the API, a yardstick rather than a bill (the % of the window is what counts there).
    """
    ratios: list[float] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
            equiv = weighted(e.get("tokens") or {}, e.get("ttl_s") or 3600)
            cost = e.get("cost") or 0
        except (ValueError, AttributeError, TypeError):
            continue
        if equiv > 0 and cost > 0:
            ratios.append(cost / equiv)
        if len(ratios) >= CALIBRATION_ENTRIES:
            break
    return statistics.median(ratios) if len(ratios) >= CALIBRATION_MIN else None


# A new conversation's first request (Claude Code's system prompt, tools and skills; measured
# on 2026-10-29) and the part of it all conversations share, usually in the cache. What a compaction
# leaves when the conversation's own isn't known yet, and what it writes (the rest is files it adds back).
STARTUP = 19_000
SHARED = 8_600
SUMMARY = 20_000
COMPACT_OUTPUT = 2_000


@dataclass(frozen=True)
class WakeCosts:
    """Equivalent tokens of each way to go on with a conversation whose cache went cold."""

    context: int
    summary: int  # what compacting leaves, re-read by every later step instead of the context
    keep: float  # continue as it is: the whole context written to the cache again
    compact: float  # summarise it (reading it all once, uncached), then write the result to the cache
    new: float  # a new conversation's first request


def wake_costs(context: int, after_compact: int, ttl: int) -> WakeCosts:
    w = write_price(ttl)
    summary = after_compact or SUMMARY
    return WakeCosts(
        context=context,
        summary=summary,
        keep=context * w,
        compact=context + COMPACT_OUTPUT * OUTPUT + max(summary - SHARED, 0) * w,
        new=STARTUP * w,
    )


def format_cost(
    equivalent: float, usd: float | None, eur_per_usd: float, calibration: Calibration | None
) -> str:
    """E.g. "~0.95 € · ~3% of your 5-hour window"; "" when neither is known."""
    parts = []
    if usd:
        parts.append(f"~{equivalent * usd * eur_per_usd:.2f} €")
    if calibration:
        pct = equivalent * calibration.percent_per_token
        parts.append(f"~{pct:.0f}% of your 5-hour window" if pct >= 1 else f"~{pct:.1f}% of your 5-hour window")
    return " · ".join(parts)


# The reserve: near a plan limit the last bit of it is kept for keep-alives, so the big
# conversations come back warm once it resets instead of writing their whole context again.
# What is left of a window when it resets is lost anyway; the context written again after it
# is paid from the new one.
RESERVE_KINDS = ("session", "weekly_all")
# Share of each window one equivalent token takes, measured on 2026-11-11 over ~500 turns
# (usage.jsonl, ratio of sums): used until the log has enough of its own.
PLAN_RATES = {"session": 2.2e-5, "weekly_all": 1.5e-6}
PLAN_RATE_ENTRIES = 500
PLAN_RATE_MIN = 50
KEEPALIVE_EXTRA = 2_000  # equivalent tokens of a keep-alive besides re-reading the context
RESERVE_MARGIN = 1.0  # %: the percent comes in whole numbers and a while late; a turn may be running
RESERVE_MAX = 5.0  # %: a bigger reserve would stop real work too early


def plan_rates(path: Path) -> dict[str, float]:
    """% of each window per equivalent token, from the newest turns that ran alone.

    Turns that left the whole-number percent unchanged count too (a ratio of sums, not a
    median of the ones that moved it): small turns would look far costlier otherwise.
    """
    rates = dict(PLAN_RATES)
    try:
        lines = path.read_text().splitlines()[-PLAN_RATE_ENTRIES:]
    except OSError:
        return rates
    sums = {kind: [0.0, 0.0, 0] for kind in RESERVE_KINDS}
    for line in lines:
        try:
            e = json.loads(line)
            if e.get("parallel"):
                continue
            equiv = weighted(e.get("tokens") or {}, e.get("ttl_s") or 3600)
            for kind, acc in sums.items():
                before, after = (e.get("before") or {}).get(kind), (e.get("after") or {}).get(kind)
                if before is None or after is None or not 0 <= after - before < 50:  # < 0: it reset
                    continue
                acc[0] += after - before
                acc[1] += equiv
                acc[2] += 1
        except (ValueError, AttributeError, TypeError):
            continue
    for kind, (percent, equiv, n) in sums.items():
        if n >= PLAN_RATE_MIN and equiv > 0 and percent > 0:
            rates[kind] = percent / equiv
    return rates


@dataclass(frozen=True)
class Reserve:
    kind: str  # the limit it is for
    resets: float  # epoch
    percent: float  # of that limit held back
    contexts: tuple[int, ...]  # of the conversations it keeps warm


def keepalives_until(now: float, resets: float, ttl: int) -> int:
    """Keep-alives one conversation needs for its cache to last until resets."""
    return math.ceil(max(resets - now, 0) / (ttl - keepalive_lead(ttl)))


def worth_keeping(now: float, until: float, context: int, ttl: int) -> bool:
    """Whether keep-alives until then cost less than writing the context again then."""
    pings = keepalives_until(now, until, ttl)
    return pings * (context * READ + KEEPALIVE_EXTRA) < context * write_price(ttl)


def reserve(
    now: float, limits: list[tuple[str, float, float]], contexts: list[int], ttl: int, rates: dict[str, float]
) -> Reserve | None:
    """The reserve due now, or None. limits: (kind, percent, resets epoch); contexts: of the
    conversations whose cache is warm and big enough to keep.

    Only the conversations whose keep-alives until the reset cost less than writing their
    context again count, the biggest first while the reserve stays within RESERVE_MAX.
    """
    best = None
    for kind, percent, resets in limits:
        if kind not in rates or percent >= 100 or resets <= now:
            continue
        pings = keepalives_until(now, resets, ttl)
        kept, need = [], RESERVE_MARGIN
        for c in sorted(contexts, reverse=True):  # the biggest save the most
            cost = pings * (c * READ + KEEPALIVE_EXTRA)
            if cost < c * write_price(ttl) and need + cost * rates[kind] <= RESERVE_MAX:
                kept.append(c)
                need += cost * rates[kind]
        if not kept or percent < 100 - need:
            continue
        if best is None or resets > best.resets:
            best = Reserve(kind, resets, need, tuple(kept))
    return best
