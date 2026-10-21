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


def _until(iso: str | None) -> str:
    if not iso:
        return ""
    secs = int((datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "resets now"
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    mins = rest // 60
    left = f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"
    return f"resets in {left}"


def _pace(percent: float, iso: str | None) -> str:
    """How far usage is ahead of (over) or behind (under) an even spread across the week."""
    if not iso:
        return ""
    left = datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)
    elapsed = 1 - min(max(left / timedelta(days=7), 0), 1)
    diff = percent - elapsed * 100
    if abs(diff) < 1:
        return "\n⚖️ on pace"
    return f"\n{'🔥' if diff > 0 else '🧊'} {abs(diff):.0f}% {'over' if diff > 0 else 'under'} pace ({elapsed * 100:.0f}% of the week gone)"


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
