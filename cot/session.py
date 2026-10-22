"""One Claude Code conversation, driven through the Agent SDK.

Incoming user messages go into a queue; a single consumer task feeds them to
Claude one turn at a time and forwards the output through callbacks.
"""

import asyncio
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import container, permissions, tg_tools
from .jail import make_hook
from .output import TelegramOutput
from .store import ConvState

log = logging.getLogger(__name__)

APPEND_PROMPT = (
    "The user is talking to you through a Telegram chat, not a terminal. "
    "Never use the AskUserQuestion tool: when you need input, ask in plain text and end your turn. "
    "To give the user a file, send it with the send_file tool. "
    "This session's process stops after a while without messages and when the bot restarts, "
    "and the commands it is running stop with it. Start anything that may take more than a few "
    "minutes (test suites, builds, servers) detached, e.g. `setsid nohup cmd > job.log 2>&1 &`, "
    "and follow its log: detached jobs keep running."
)
IDLE_DISCONNECT = 30 * 60  # stop the idle Claude process; the next message resumes it
# Telegram splits long texts into several messages: wait this long after the last one
# so they reach Claude as a single turn. Messages queued during a turn merge too.
GATHER_SECONDS = 3.0

OnState = Callable[[ConvState], None]


def _transcript_exists(session_id: str) -> bool:
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return any((config_dir / "projects").glob(f"*/{session_id}.jsonl"))


class Session:
    def __init__(
        self,
        state: ConvState,
        *,
        out: TelegramOutput,
        on_state: OnState,
        default_mode: str,
        cli_path: str | None,
        cli_env: dict[str, str],
        sandbox_bash: bool,
    ):
        self.state = state
        self.out = out
        self._sandbox_bash = sandbox_bash
        self._on_state = on_state
        self._default_mode = default_mode
        self._cli_path = cli_path
        self._cli_env = cli_env
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._gathered: list[str] = []  # taken from the queue, waiting for the rest
        self._client: ClaudeSDKClient | None = None
        self._task: asyncio.Task | None = None
        self.busy = False
        self.waiting = False  # blocked on a permission prompt
        self.model: str | None = None  # model actually answering, as reported by Claude
        self.last_turn_cost: float | None = None
        self._proc_cost = 0.0  # total_cost_usd is cumulative per CLI process
        self._stopping = False

    def _set_waiting(self, value: bool) -> None:
        self.waiting = value

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.state.cwd,
            resume=self.state.session_id,
            permission_mode=self.state.mode or self._default_mode,
            model=self.state.model,
            disallowed_tools=["AskUserQuestion"],
            # Stream events tell when thinking starts, so the chat can show it live.
            include_partial_messages=True,
            # The SDK's 1 MB default per message is too small for image/PDF reads (base64).
            max_buffer_size=64 * 1024 * 1024,
            mcp_servers={tg_tools.SERVER: tg_tools.make_server(self.out, Path(self.state.cwd))},
            allowed_tools=[tg_tools.SEND_FILE],
            system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND_PROMPT},
            cli_path=self._cli_path,
            env={**self._cli_env, "COT_NAME": self._container_name()},
            can_use_tool=permissions.make_can_use_tool(self.out, Path(self.state.cwd), self._set_waiting),
            hooks={"PreToolUse": [make_hook(Path(self.state.cwd))]},
            # Bash writes confined to cwd; needs working unprivileged bubblewrap on Linux.
            sandbox={"enabled": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False}
            if self._sandbox_bash
            else None,
        )

    def _container_name(self) -> str:
        """cot-<topic or folder>-<topic id>: tells conversations apart in docker ps."""
        label = self.state.title or Path(self.state.cwd).name
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "chat"
        return f"cot-{slug}-{self.out.thread_id or abs(self.out.chat_id)}"

    async def _connect(self) -> None:
        client = ClaudeSDKClient(self._options())
        try:
            await client.connect()
        except Exception:
            # Only a session whose transcript is gone starts afresh: any other failure (the
            # CLI or its container not starting) must not cost the conversation its history.
            if not self.state.session_id or _transcript_exists(self.state.session_id):
                raise
            log.warning("resume of %s failed, starting a new session", self.state.session_id)
            self.state.session_id = None
            self._on_state(self.state)
            client = ClaudeSDKClient(self._options())
            await client.connect()
        self._client = client
        self._proc_cost = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def send(self, text: str) -> None:
        await self._queue.put(text)

    @property
    def pending(self) -> bool:
        """True while a turn runs or messages wait in the queue."""
        return self.busy or bool(self._gathered) or not self._queue.empty()

    @property
    def queued(self) -> int:
        return self._queue.qsize() + len(self._gathered)

    async def stop(self) -> tuple[bool, int]:
        """Interrupt the running turn and drop queued messages.

        Returns (a turn was running, number of queued messages dropped).
        """
        dropped = len(self._gathered)
        self._gathered.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            dropped += 1
        running = self.busy
        if running:
            self._stopping = True
            await permissions.cancel_for(self.out)
            if self._client:
                await self._client.interrupt()
        return running, dropped

    async def set_model(self, model: str | None) -> None:
        self.state.model = model
        self._on_state(self.state)
        if self._client:
            await self._client.set_model(model)

    async def context_usage(self) -> dict | None:
        """Context window usage, or None while no Claude process is running."""
        if not self._client or self.busy:
            return None
        return await self._client.get_context_usage()

    def save(self) -> None:
        self._on_state(self.state)

    async def set_mode(self, mode: str) -> None:
        self.state.mode = mode
        self._on_state(self.state)
        if self._client:
            await self._client.set_permission_mode(mode)

    async def close(self) -> None:
        await permissions.cancel_for(self.out)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._disconnect()
        if self._in_docker:
            await asyncio.to_thread(container.stop_claude, self._container_name())

    @property
    def _in_docker(self) -> bool:
        return "COT_IMAGE" in self._cli_env

    def _alive(self) -> bool:
        """False once the CLI process is gone (killed, crashed): the client can't be reused."""
        process = getattr(getattr(self._client, "_transport", None), "_process", None)
        return process is None or process.returncode is None

    async def _disconnect(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                log.exception("disconnect failed")
            self._client = None

    async def _run(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(self._queue.get(), IDLE_DISCONNECT)
            except asyncio.TimeoutError:
                await self._disconnect()
                if self._in_docker:
                    await asyncio.to_thread(container.reap, self._container_name())
                continue
            text = await self._gather(text)
            if not text:  # dropped by /stop while gathering
                continue
            self.busy = True
            try:
                if self._client is not None and not self._alive():
                    log.warning("Claude exited between turns; reconnecting")
                    await self._disconnect()
                if self._client is None:
                    await self._connect()
                await self._turn(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("turn failed")
                await self.out.notice(f"⚠️ Error: {e}")
                # Drop the client so the next message reconnects (resuming the session).
                await self._disconnect()
            finally:
                await self.out.end_turn()
                self.busy = False
                self._stopping = False

    async def _gather(self, first: str) -> str:
        """The first message plus everything that follows it in quick succession."""
        self._gathered = [first]
        while True:
            try:
                text = await asyncio.wait_for(self._queue.get(), GATHER_SECONDS)
            except asyncio.TimeoutError:
                break
            self._gathered.append(text)  # after a /stop this starts afresh
        text, self._gathered = "\n\n".join(self._gathered), []
        return text

    async def _turn(self, text: str) -> None:
        assert self._client
        await self._client.query(text)
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                if msg.parent_tool_use_id is None:
                    await self._stream_event(msg.event)
            elif isinstance(msg, AssistantMessage):
                nested = msg.parent_tool_use_id is not None
                if not nested and msg.model and not msg.model.startswith("<"):
                    self.model = msg.model
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip() and not nested:
                        await self.out.text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        await self.out.tool_use(block.name, block.input, nested)
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock) and not self._stopping:
                        await self.out.tool_result(block.content, bool(block.is_error))
            elif isinstance(msg, ResultMessage):
                if msg.session_id and msg.session_id != self.state.session_id:
                    self.state.session_id = msg.session_id
                    self._on_state(self.state)
                if msg.total_cost_usd is not None:
                    self.last_turn_cost = max(msg.total_cost_usd - self._proc_cost, 0.0)
                    self._proc_cost = msg.total_cost_usd
                    self.state.cost_usd += self.last_turn_cost
                    self._on_state(self.state)
                if self._stopping or (msg.terminal_reason or "").startswith("aborted"):
                    await self.out.notice("🛑 Stopped")
                elif msg.is_error:
                    detail = "; ".join(msg.errors or []) or msg.subtype
                    await self.out.notice(f"⚠️ Claude ended with an error: {detail}")

    async def _stream_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "content_block_start" and event.get("content_block", {}).get("type") == "thinking":
            await self.out.thinking_start()
        elif kind == "content_block_stop":
            await self.out.thinking_end()
