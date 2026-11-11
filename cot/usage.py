"""Claude subscription usage (the 5-hour and weekly limits that /usage shows in the terminal)."""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

URL = "https://api.anthropic.com/api/oauth/usage"

LABELS = {
    "session": "5-hour session",
    "weekly_all": "Week, all models",
    "weekly_opus": "Week, Opus",
    "weekly_sonnet": "Week, Sonnet",
}


def _label(lim: dict) -> str:
    # A weekly limit of its own for one model (Fable) or surface, named by the API.
    scope = lim.get("scope") or {}
    named = next((part.get("display_name") for part in (scope.get("model"), scope.get("surface"))
                  if isinstance(part, dict) and part.get("display_name")), None)
    if lim["kind"] == "weekly_scoped" and named:
        return f"Week, {named}"
    return LABELS.get(lim["kind"], lim["kind"].replace("_", " ").capitalize())


class Unavailable(Exception):
    pass


class RateLimited(Exception):
    """The endpoint said 429: no use asking again before `until` (epoch)."""

    def __init__(self, until: float):
        super().__init__(f"the usage endpoint is rate-limited, {retry_in(until)}")
        self.until = until


# Every reader (/usage, the usage log, the limit checks) shares one reading and one back-off,
# so that together they don't ask the endpoint more than it allows.
_last: tuple[float, dict] | None = None  # (epoch, data) of the latest good reading
_retry_at = 0.0  # no request before this, after a 429
_backoff = 0.0
_lock: asyncio.Lock | None = None
BACKOFF_MIN, BACKOFF_MAX = 60.0, 900.0


def last() -> tuple[float, dict] | None:
    """The latest good reading (epoch, data), however old."""
    return _last


def _credentials() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"


def _login() -> dict:
    """OAuth login of Claude Code on this machine (the CLI refreshes it)."""
    try:
        return json.loads(_credentials().read_text())["claudeAiOauth"]
    except (OSError, ValueError, KeyError):
        raise Unavailable("no Claude subscription login found (API keys have no plan limits)") from None


def renewed_since(when: float) -> bool:
    """Whether there is a good login written after that moment (a failed refresh can write one without a token)."""
    try:
        login = _login()
        return bool(_credentials().stat().st_mtime > when and login.get("accessToken")
                    and login.get("expiresAt", 0) / 1000 > time.time())
    except (OSError, Unavailable):
        return False


async def _refresh(cli: str) -> None:
    """`claude auth status` renews an expired token, without a model call."""
    proc = await asyncio.create_subprocess_exec(
        cli, "auth", "status", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        await asyncio.wait_for(proc.wait(), 30)
    except asyncio.TimeoutError:
        proc.kill()


def _bar(percent: float, width: int = 10) -> str:
    filled = round(min(max(percent, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _duration(secs: float) -> str:
    secs = int(secs)
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    mins = rest // 60
    return f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"


def _when(at: datetime) -> str:
    """Like "Fri 23rd at 8 PM UTC" (minutes only when not on the hour)."""
    at = (at + timedelta(seconds=30)).replace(second=0, microsecond=0).astimezone(timezone.utc)
    day = at.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    hour = at.hour % 12 or 12
    clock = f"{hour}:{at.minute:02d}" if at.minute else f"{hour}"
    return f"{at:%a} {day}{suffix} at {clock} {'AM' if at.hour < 12 else 'PM'} UTC"


def _until(iso: str | None) -> str:
    if not iso:
        return ""
    at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    secs = (at - datetime.now(timezone.utc)).total_seconds()
    return f"resets in {_duration(secs)} ({_when(at)})" if secs > 0 else "resets now"


def until(epoch: float) -> str:
    return _until(datetime.fromtimestamp(epoch, timezone.utc).isoformat())


def retry_at() -> float:
    """No request to the endpoint before this (epoch); in the past when it can be asked."""
    return _retry_at


def fresh(max_age: float) -> bool:
    return bool(_last) and time.time() - _last[0] <= max_age


def ago(at: float) -> str:
    secs = time.time() - at
    return f"{_duration(secs)} ago" if secs >= 60 else "under a minute ago"


def retry_in(at: float) -> str:
    secs = at - time.time()
    return f"retry in {_duration(secs)}" if secs >= 60 else "retry in under a minute"


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _pace(percent: float, iso: str | None) -> str:
    """How far usage is from an even spread across the week, and whether it lasts until the reset."""
    if not iso:
        return ""
    week = timedelta(days=7).total_seconds()
    left = (datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    left = min(max(left, 0), week)
    elapsed = week - left
    if percent >= 100 or not percent or not elapsed:
        return ""
    off = percent - elapsed / week * 100
    pace = "on pace" if abs(off) < 1 else f"{abs(off):.0f}% {'over' if off > 0 else 'under'} pace"
    lasts = (100 - percent) / (percent / elapsed)  # seconds until 100% at the current rate
    diff = left - lasts
    if abs(diff) < 3600:
        return f"\n⚖️ {pace}, at the current rate, runs out right at reset"
    icon, when = ("🔥", "before") if diff > 0 else ("🧊", "after")
    return f"\n{icon} {pace}, at the current rate, runs out ~{_duration(abs(diff))} {when} reset"


def _rate_limited(r: httpx.Response) -> float:
    """Note a 429 and return when to try again: as the server says, or doubling from a minute."""
    global _retry_at, _backoff
    _backoff = min(max(_backoff * 2, BACKOFF_MIN), BACKOFF_MAX)
    wait = _backoff
    try:
        wait = max(float(r.headers.get("retry-after", "")), 1.0)
    except ValueError:
        pass
    _retry_at = time.time() + wait
    return _retry_at


async def fetch(cli: str, max_age: float = 0.0) -> dict:
    """The plan usage; a reading up to `max_age` seconds old will do. Raises RateLimited while
    the endpoint asks to wait."""
    global _last, _lock, _backoff
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _last and time.time() - _last[0] <= max_age:
            return _last[1]
        if time.time() < _retry_at:
            raise RateLimited(_retry_at)
        data = await _get(cli)
        _last, _backoff = (time.time(), data), 0.0
        return data


async def _get(cli: str) -> dict:
    # After a while with no Claude running (a reboot, a quiet night) the token has expired.
    if _login().get("expiresAt", 0) / 1000 < time.time() + 60:
        await _refresh(cli)
    for attempt in range(2):
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                URL,
                headers={"Authorization": f"Bearer {_login()['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
            )
        if r.status_code != 401:
            break
        if attempt == 0:
            await _refresh(cli)
    if r.status_code == 401:
        raise Unavailable("the Claude login was rejected; log in again with `claude /login`")
    if r.status_code == 429:
        raise RateLimited(_rate_limited(r))
    r.raise_for_status()
    return r.json()


def render(data: dict) -> str:
    lines = ["📈 Plan usage"]
    for lim in data.get("limits") or []:
        label = _label(lim)
        pct = lim.get("percent") or 0
        pace = _pace(pct, lim.get("resets_at")) if lim.get("group") == "weekly" else ""
        lines.append(f"{label}\n{_bar(pct)} {pct:.0f}%  {_until(lim.get('resets_at'))}{pace}")
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("utilization") is not None:
        lines.append(f"Extra usage: {extra['utilization']:.0f}% of the monthly limit")
    if len(lines) == 1:
        lines.append("no limits reported")
    return "\n\n".join(lines)


def blocked(data: dict) -> str | None:
    """When the plan stops Claude (a 5-hour or weekly limit used up, with no extra usage left):
    the latest of those limits' resets (ISO, or "" if unknown); None if Claude can run."""
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and (extra.get("utilization") or 0) < 100:
        return None
    full = [lim for lim in data.get("limits") or []
            if lim.get("kind") in ("session", "weekly_all") and (lim.get("percent") or 0) >= 100]
    if not full:
        return None
    return max(lim.get("resets_at") or "" for lim in full)
