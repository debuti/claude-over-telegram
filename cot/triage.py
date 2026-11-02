"""Where a message to a conversation whose cache went cold should go, decided by a small model.

Continuing re-sends the whole context at full price, so asking Claude itself would cost what
the decision is meant to save. Haiku reads the conversation's title, its last exchanges and
the new message, and says whether the message goes on with it (compact it, then continue)
or starts something else (a new conversation). When in doubt it says it goes on: starting
anew by mistake loses the context, compacting by mistake costs little.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from .session import _transcripts

log = logging.getLogger(__name__)

MODEL = "haiku"
TIMEOUT = 60
# How much of the conversation it reads: the last exchanges, each cut short.
EXCHANGES = 6
CHARS = 600

SYSTEM = (
    "You route a chat message. The user comes back, after a while, to a conversation with an "
    "assistant (about code or anything else). Decide whether their new message goes on with "
    "that conversation (SAME: same task or subject, a follow-up, a reference to it) or turns to "
    "an unrelated task or subject (NEW). Judge by subject, not by the chat it was sent in: a "
    "general chat still starts a new topic when the subject changes. Answer SAME without a clear "
    "link only when the message is too short or vague to tell. First write the reason in at "
    "most 15 words, in the language of the new message; then, on a last line of its own, only "
    "SAME or NEW."
)

ANSWER = re.compile(r"^\W*(SAME|NEW)\W*[:\-–—]?\s*(.*)$", re.IGNORECASE | re.DOTALL)
VERDICT = re.compile(r"^\W*(SAME|NEW)\W*$", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    same: bool
    reason: str
    cost_usd: float


def _text(content) -> str:
    """The text of a message's content, leaving tool calls and results out."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def excerpt(session_id: str) -> str:
    """The conversation's last exchanges (main thread only), oldest first; "" if unreadable."""
    lines: list[str] = []
    try:
        for path in _transcripts(session_id):
            with path.open(errors="ignore") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("type") not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
                        continue
                    text = " ".join(_text((e.get("message") or {}).get("content")).split())
                    if text:
                        who = "User" if e["type"] == "user" else "Assistant"
                        lines.append(f"{who}: {text[:CHARS]}")
    except Exception:
        log.warning("could not read the transcript of %s", session_id, exc_info=True)
    return "\n".join(lines[-EXCHANGES:])


def parse(answer: str) -> tuple[bool, str] | None:
    """The verdict on the last line after the reason, or (older form) "SAME: reason"."""
    lines = [line.strip() for line in answer.strip().splitlines() if line.strip()]
    if lines and (m := VERDICT.match(lines[-1])):
        return m.group(1).upper() == "SAME", " ".join(" ".join(lines[:-1]).split())
    m = ANSWER.match(answer.strip())
    if not m:
        return None
    return m.group(1).upper() == "SAME", " ".join(m.group(2).split())


async def decide(cli: str | None, cwd: Path, title: str, history: str, message: str) -> Verdict | None:
    """Haiku's verdict, or None if it could not be had (the bot then asks instead)."""
    prompt = (
        f"Conversation title: {title or '(none)'}\n\n"
        f"Its last exchanges:\n{history or '(unavailable)'}\n\n"
        f"New message:\n{message[:2000]}"
    )
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM,
        tools=[],
        max_turns=1,
        setting_sources=[],
        cwd=str(cwd),
        cli_path=cli,
        extra_args={"no-session-persistence": None},
    )
    answer, cost = "", 0.0

    async def run() -> None:
        nonlocal answer, cost
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                answer += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0

    try:
        await asyncio.wait_for(run(), TIMEOUT)
    except Exception:
        log.warning("triage failed", exc_info=True)
        return None
    parsed = parse(answer)
    if not parsed:
        log.warning("triage gave no verdict: %r", answer[:200])
        return None
    return Verdict(*parsed, cost)
