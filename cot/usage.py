"""Claude subscription usage (the 5-hour and weekly limits that /usage shows in the terminal)."""

import json
import os
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


class Unavailable(Exception):
    pass


def _token() -> str:
    """OAuth token of the Claude Code login on this machine (refreshed by the CLI itself)."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        creds = json.loads((config_dir / ".credentials.json").read_text())
        return creds["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError):
        raise Unavailable("no Claude subscription login found (API keys have no plan limits)") from None


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
    when = "before" if diff > 0 else "after"
    return f"\n{'🔥' if diff > 0 else '🧊'} {pace}, at the current rate, runs out ~{_duration(abs(diff))} {when} reset"


async def fetch() -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            URL,
            headers={"Authorization": f"Bearer {_token()}", "anthropic-beta": "oauth-2025-04-20"},
        )
    if r.status_code == 401:
        raise Unavailable("login expired; it is refreshed the next time Claude runs")
    r.raise_for_status()
    return r.json()


def render(data: dict) -> str:
    lines = ["📈 Plan usage"]
    for lim in data.get("limits") or []:
        label = LABELS.get(lim["kind"], lim["kind"].replace("_", " ").capitalize())
        pct = lim.get("percent") or 0
        pace = _pace(pct, lim.get("resets_at")) if lim.get("group") == "weekly" else ""
        lines.append(f"{label}\n{_bar(pct)} {pct:.0f}%  {_until(lim.get('resets_at'))}{pace}")
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("utilization") is not None:
        lines.append(f"Extra usage: {extra['utilization']:.0f}% of the monthly limit")
    if len(lines) == 1:
        lines.append("no limits reported")
    return "\n\n".join(lines)
