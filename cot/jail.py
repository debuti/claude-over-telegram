"""Keeps Claude's file writes inside the directory the bot was launched for.

Claude may read anywhere but write only inside the workdir. A PreToolUse hook
sees every tool call, whatever the permission mode, and denies any write whose
path resolves (symlinks included) outside the allowed roots. Bash is not
covered here: it is either sandboxed (SANDBOX_BASH) or left to the permission
mode.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

# Writing tool name -> input field holding the path it writes.
PATH_FIELDS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def allowed_roots(jail: Path) -> list[Path]:
    """The jail plus Claude Code's own temp dir and per-project memory and plan files."""
    claude_dir = Path.home() / ".claude"
    project = re.sub(r"[^A-Za-z0-9]", "-", str(jail))  # Claude Code's project dir naming
    return [
        jail,
        claude_dir / "projects" / project,
        claude_dir / "plans",
        Path(tempfile.gettempdir()) / f"claude-{os.getuid()}",
    ]


def make_hook(jail: Path) -> HookMatcher:
    roots = [Path(os.path.realpath(r)) for r in allowed_roots(jail)]

    async def check(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool = input_data.get("tool_name", "")
        inp = input_data.get("tool_input") or {}
        field = PATH_FIELDS.get(tool)
        raw = inp.get(field) if field else None
        if not raw:
            return {}
        path = Path(os.path.realpath(jail / Path(raw).expanduser()))
        if any(path.is_relative_to(r) for r in roots):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{raw} is outside the working directory {jail}; files can only be written inside it."
                ),
            }
        }

    return HookMatcher(matcher="|".join(PATH_FIELDS), hooks=[check])
