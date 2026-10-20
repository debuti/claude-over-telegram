"""Tools Claude gets for talking back through Telegram (an in-process MCP server)."""

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .output import TelegramOutput

log = logging.getLogger(__name__)

SERVER = "telegram"
SEND_FILE = f"mcp__{SERVER}__send_file"
MAX_UPLOAD = 50 * 1024 * 1024  # Bot API limit for files sent by bots

SEND_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to send; relative paths are resolved against the working directory.",
        },
        "caption": {"type": "string", "description": "Optional short text shown under the file."},
    },
    "required": ["path"],
}


def _result(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def make_server(out: TelegramOutput, cwd: Path):
    @tool(
        "send_file",
        "Send a file from this machine to the user's Telegram chat (images are shown inline). "
        "Use it whenever the user asks for a file, or when a file is the clearest way to deliver a result.",
        SEND_FILE_SCHEMA,
    )
    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        path = (cwd / Path(args["path"]).expanduser()).resolve()
        if not path.is_file():
            return _result(f"No such file: {path}", error=True)
        size = path.stat().st_size
        if size > MAX_UPLOAD:
            return _result(f"{path} is {size // (1024 * 1024)} MB; Telegram bots can send at most 50 MB.", error=True)
        try:
            await out.send_path(path, args.get("caption") or "")
        except Exception as e:
            log.exception("send_file failed")
            return _result(f"Telegram rejected the file: {e}", error=True)
        shown = f"{size} bytes" if size < 1024 else f"{size // 1024} KB"
        return _result(f"Sent {path.name} ({shown}) to the user.")

    return create_sdk_mcp_server(SERVER, tools=[send_file])
