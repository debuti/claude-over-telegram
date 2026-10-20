"""One Claude Code conversation, driven through the Agent SDK.

Incoming user messages go into a queue; a single consumer task feeds them to
Claude one turn at a time and forwards the output through callbacks.
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .jail import make_hook
from .output import TelegramOutput
from . import permissions
from .store import ConvState

log = logging.getLogger(__name__)

APPEND_PROMPT = (
    "The user is talking to you through a Telegram chat, not a terminal. "
    "Never use the AskUserQuestion tool: when you need input, ask in plain text and end your turn."
)

OnState = Callable[[ConvState], None]


class Session:
    def __init__(
        self,
        state: ConvState,
        *,
        out: TelegramOutput,
        on_state: OnState,
        default_mode: str,
        cli_path: str | None,
        sandbox_bash: bool,
    ):
        self.state = state
        self.out = out
        self._sandbox_bash = sandbox_bash
        self._on_state = on_state
        self._default_mode = default_mode
        self._cli_path = cli_path
        self._queue: asyncio.Queue[str] = asyncio.Queue()
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
            system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND_PROMPT},
            cli_path=self._cli_path,
            can_use_tool=permissions.make_can_use_tool(self.out, Path(self.state.cwd), self._set_waiting),
            hooks={"PreToolUse": [make_hook(Path(self.state.cwd))]},
            # Bash writes confined to cwd; needs working unprivileged bubblewrap on Linux.
            sandbox={"enabled": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False}
            if self._sandbox_bash
            else None,
        )

    async def _connect(self) -> None:
        client = ClaudeSDKClient(self._options())
        try:
            await client.connect()
        except Exception:
            if not self.state.session_id:
                raise
            # The stored session may be gone (e.g. deleted transcripts): start fresh.
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
        return self.busy or not self._queue.empty()

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    async def stop(self) -> tuple[bool, int]:
        """Interrupt the running turn and drop queued messages.

        Returns (a turn was running, number of queued messages dropped).
        """
        dropped = 0
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
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                log.exception("disconnect failed")
            self._client = None

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            self.busy = True
            try:
                if self._client is None:
                    await self._connect()
                await self._turn(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("turn failed")
                await self.out.notice(f"⚠️ Error: {e}")
                # Drop the client so the next message reconnects (resuming the session).
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
            finally:
                await self.out.end_turn()
                self.busy = False
                self._stopping = False

    async def _turn(self, text: str) -> None:
        assert self._client
        await self._client.query(text)
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
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
