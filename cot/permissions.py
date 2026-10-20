"""Permission prompts as Telegram inline buttons.

Claude Code calls can_use_tool whenever it would ask in the terminal. The
request is shown with ✅ / ❌ / ♾️ buttons and the call waits until a button
is pressed or the request expires (then it is denied).
"""

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, PermissionUpdate, ToolPermissionContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .output import TelegramOutput
from .render import TG_LIMIT, esc, tool_line

log = logging.getLogger(__name__)

TIMEOUT = 15 * 60
DETAIL_LIMIT = 1500

# Suggestions that would widen the jail (addDirectories) are never offered.
SAFE_UPDATES = ("addRules", "setMode")

ALLOW, DENY, ALWAYS = "allow", "deny", "always"
OUTCOME = {ALLOW: "✅ Allowed", DENY: "❌ Denied", ALWAYS: "♾️ Always allowed", "expired": "⌛ Expired (denied)"}


@dataclass
class Pending:
    future: asyncio.Future
    message_id: int | None
    text: str


# Open requests of every session, by request id (the id travels in callback_data).
PENDING: dict[str, Pending] = {}


def _clip(text: str, n: int = DETAIL_LIMIT) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _details(tool: str, inp: dict[str, Any]) -> str:
    match tool:
        case "Bash":
            desc = f"\n<i>{esc(inp['description'])}</i>" if inp.get("description") else ""
            return f"<pre>{esc(_clip(inp.get('command', '')))}</pre>{desc}"
        case "Edit":
            diff = "\n".join(
                [f"- {l}" for l in inp.get("old_string", "").splitlines()]
                + [f"+ {l}" for l in inp.get("new_string", "").splitlines()]
            )
            return f"<pre><code class=\"language-diff\">{esc(_clip(diff))}</code></pre>"
        case "Write":
            return f"<pre>{esc(_clip(inp.get('content', '')))}</pre>"
    return ""


def _rule_text(updates: list[PermissionUpdate]) -> str:
    rules = []
    for u in updates:
        if u.type == "setMode":
            rules.append(f"mode {u.mode}")
        for r in u.rules or []:
            rules.append(f"{r.tool_name}({r.rule_content})" if r.rule_content else r.tool_name)
    return ", ".join(rules)


def _keyboard(req_id: str, always: bool, plan: bool) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("✅ Approve plan" if plan else "✅ Allow", callback_data=f"perm:{req_id}:{ALLOW}"),
        InlineKeyboardButton("❌ Keep planning" if plan else "❌ Deny", callback_data=f"perm:{req_id}:{DENY}"),
    ]
    if always:
        buttons.append(InlineKeyboardButton("♾️ Always", callback_data=f"perm:{req_id}:{ALWAYS}"))
    return InlineKeyboardMarkup([buttons])


def make_can_use_tool(out: TelegramOutput, cwd, on_wait: Callable[[bool], None]):
    async def can_use_tool(tool: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        if tool == "AskUserQuestion":
            return PermissionResultDeny(message="AskUserQuestion is unavailable: ask the user in plain text and end your turn.")

        await out.flush()
        plan = tool == "ExitPlanMode"
        if plan:
            await out.text(inp.get("plan", "") or "(empty plan)")
            text = "📋 <b>Claude has a plan.</b> Approve it to start working?"
            updates = []
        else:
            title = esc(ctx.title) if ctx.title else "Claude wants to run:"
            # Bash shows its full command below, so the one-liner would repeat it.
            header = "💻 Bash" if tool == "Bash" else tool_line(tool, inp, cwd)
            text = f"🔐 <b>{title}</b>\n{header}"
            if details := _details(tool, inp):
                text += f"\n{details}"
            updates = [u for u in ctx.suggestions if u.type in SAFE_UPDATES]
            if rule := _rule_text(updates):
                text += f"\n♾️ = always allow <code>{esc(rule)}</code>"
        text = text[:TG_LIMIT]

        req_id = secrets.token_hex(6)
        future = asyncio.get_running_loop().create_future()
        message_id = await out.send_html(text, reply_markup=_keyboard(req_id, bool(updates), plan))
        PENDING[req_id] = Pending(future, message_id, text)
        on_wait(True)
        try:
            choice = await asyncio.wait_for(future, TIMEOUT)
        except asyncio.TimeoutError:
            choice = "expired"
            await _mark(out, PENDING.get(req_id), choice)
        finally:
            PENDING.pop(req_id, None)
            on_wait(False)

        log.info("permission %s for %s: %s", req_id, tool, choice)
        if choice == ALLOW:
            return PermissionResultAllow()
        if choice == ALWAYS:
            return PermissionResultAllow(updated_permissions=updates)
        reason = "The user rejected the plan; keep planning and ask what to change." if plan else "The user denied this action from Telegram."
        if choice == "expired":
            reason = "Nobody answered the permission request in time, so it was denied."
        return PermissionResultDeny(message=reason)

    return can_use_tool


async def _mark(out: TelegramOutput, pending: Pending | None, choice: str) -> None:
    """Replace the buttons with the outcome."""
    if not pending or pending.message_id is None:
        return
    try:
        await out.bot.edit_message_text(
            f"{pending.text}\n\n<b>{OUTCOME[choice]}</b>"[:4096],
            chat_id=out.chat_id,
            message_id=pending.message_id,
            parse_mode="HTML",
        )
    except Exception as e:  # message too old, unchanged, ...
        log.warning("could not update permission message: %s", e)


async def resolve(out: TelegramOutput, req_id: str, choice: str) -> bool:
    """Called from a button press. False if the request is gone (expired, restarted)."""
    pending = PENDING.get(req_id)
    if pending is None or pending.future.done() or choice not in (ALLOW, DENY, ALWAYS):
        return False
    pending.future.set_result(choice)
    await _mark(out, pending, choice)
    return True
