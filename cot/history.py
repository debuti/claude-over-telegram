"""Past Claude Code conversations of a folder (the ones `claude -r` offers)."""

import asyncio
import time

from claude_agent_sdk import SDKSessionInfo, list_sessions

from .render import esc

LIMIT = 10


async def recent(cwd: str) -> list[SDKSessionInfo]:
    # Only this exact folder: a session is resumable only from the cwd it was started in.
    return await asyncio.to_thread(list_sessions, cwd, LIMIT, 0, False)


def find(sessions: list[SDKSessionInfo], ref: str) -> SDKSessionInfo | None:
    """A session by its number in the list (1-based) or by a prefix of its id."""
    if ref.isdigit() and 1 <= int(ref) <= len(sessions):
        return sessions[int(ref) - 1]
    matches = [s for s in sessions if len(ref) >= 4 and s.session_id.startswith(ref)]
    return matches[0] if len(matches) == 1 else None


def ago(ts: float) -> str:
    secs = time.time() - (ts / 1000 if ts > 1e11 else ts)  # epoch ms or s
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def title(s: SDKSessionInfo, width: int = 70) -> str:
    text = " ".join((s.custom_title or s.summary or s.first_prompt or s.session_id).split())
    return text if len(text) <= width else text[: width - 1] + "…"


FOOTER = "Tap a number, or /resume &lt;number or id&gt;. /new starts a fresh one."


def render(sessions: list[SDKSessionInfo], current: str | None, where: str, footer: str = FOOTER) -> str:
    lines = [f"🗂 Recent conversations in <b>{esc(where)}</b>"]
    for i, s in enumerate(sessions, 1):
        mark = " ● <i>(this one)</i>" if s.session_id == current else ""
        meta = " · ".join(x for x in (ago(s.last_modified), s.git_branch, s.session_id[:8]) if x)
        lines.append(f"<b>{i}.</b> {esc(title(s))}{mark}\n      <i>{esc(meta)}</i>")
    lines.append(footer)
    return "\n\n".join(lines)
