"""Tools Claude gets for talking back through Telegram (an in-process MCP server)."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .output import TelegramOutput

log = logging.getLogger(__name__)

SERVER = "telegram"
SEND_FILE = f"mcp__{SERVER}__send_file"
LIST_AGENTS = f"mcp__{SERVER}__list_agents"
SEND_TO_AGENTS = f"mcp__{SERVER}__send_to_agents"
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


SEND_TO_AGENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Projects to write to, as list_agents names them.",
        },
        "text": {"type": "string", "description": "The message, self-contained: its readers don't know your context."},
    },
    "required": ["to", "text"],
}


@dataclass
class Agents:
    """What the bot offers a conversation for reaching the agents of other projects."""

    overview: Callable[[], str]
    # (projects, text, on_wait) -> how it went. Raises ValueError if the request makes no sense.
    send: Callable[[list[str], str, Callable[[bool], None]], Awaitable[str]]


def _result(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def make_server(
    out: TelegramOutput,
    cwd: Path,
    agents: Agents | None = None,
    on_wait: Callable[[bool], None] = lambda waiting: None,
):
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

    @tool(
        "list_agents",
        "List the other projects, each with its own agent, and how each one stands.",
        {"type": "object", "properties": {}},
    )
    async def list_agents(args: dict[str, Any]) -> dict[str, Any]:
        return _result(agents.overview())

    @tool(
        "send_to_agents",
        "Send a message to the agents of other projects, in their Telegram topics. The user approves "
        "each send with a button, and this returns once they have. Agents answer later, if at all, as "
        "a new message to you: don't wait for them.",
        SEND_TO_AGENTS_SCHEMA,
    )
    async def send_to_agents(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _result(await agents.send(args["to"], args["text"], on_wait))
        except ValueError as e:
            return _result(str(e), error=True)

    tools = [send_file, list_agents, send_to_agents] if agents else [send_file]
    return create_sdk_mcp_server(SERVER, tools=tools)
