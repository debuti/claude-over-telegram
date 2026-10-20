"""Keeps Claude's file tools inside the directory the bot was launched for.

A PreToolUse hook sees every tool call, whatever the permission mode, and
denies any file path that resolves (symlinks included) outside the allowed
roots. Bash is not covered here: it is either sandboxed (SANDBOX_BASH) or
left to the permission prompt.
"""

import os
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

# Tool name -> input field holding the path it touches.
PATH_FIELDS = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
}


def allowed_roots(jail: Path) -> list[Path]:
    """The jail plus Claude Code's own per-project memory and plan files."""
    claude_dir = Path.home() / ".claude"
    project = re.sub(r"[^A-Za-z0-9]", "-", str(jail))  # Claude Code's project dir naming
    return [jail, claude_dir / "projects" / project, claude_dir / "plans"]


def make_hook(jail: Path) -> HookMatcher:
    roots = [Path(os.path.realpath(r)) for r in allowed_roots(jail)]

    async def check(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool = input_data.get("tool_name", "")
        inp = input_data.get("tool_input") or {}
        field = PATH_FIELDS.get(tool)
        raw = inp.get(field) if field else None
        if tool == "Glob" and str(inp.get("pattern", "")).startswith(("/", "~")):
            raw = inp["pattern"].split("*", 1)[0] or "/"  # absolute pattern: check its fixed prefix
        if not raw:
            return {}
        path = Path(os.path.realpath(jail / Path(raw).expanduser()))
        if any(path.is_relative_to(r) for r in roots):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{raw} is outside the working directory {jail}; only paths inside it are allowed.",
            }
        }

    return HookMatcher(matcher="|".join(PATH_FIELDS), hooks=[check])
