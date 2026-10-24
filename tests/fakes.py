"""A scripted stand-in for ClaudeSDKClient and TelegramOutput, to drive Session without the CLI."""

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

INTERRUPT = object()  # in a script: wait here until the turn is interrupted


def answer(read=0, write=0, inp=5, text="ok", parent=None):
    usage = {"input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
             "output_tokens": 10}
    return AssistantMessage([TextBlock(text)], "claude-test", parent_tool_use_id=parent, usage=usage)


def result(is_error=False, terminal_reason=None, cost=0.01):
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=cost,
        usage={},
        terminal_reason=terminal_reason,
    )


class FakeClient:
    """Each query() plays the next script: a list of messages, maybe with INTERRUPT in it."""

    scripts: list[list] = []
    queries: list[str] = []

    def __init__(self, options=None):
        self._out: asyncio.Queue = asyncio.Queue()
        self._interrupted = asyncio.Event()
        self._play: asyncio.Task | None = None

    async def connect(self):
        pass

    async def disconnect(self):
        if self._play:
            self._play.cancel()

    async def query(self, text):
        FakeClient.queries.append(text)
        self._interrupted.clear()
        script = FakeClient.scripts.pop(0) if FakeClient.scripts else [answer(), result()]
        self._play = asyncio.create_task(self._feed(script))

    async def _feed(self, script):
        for msg in script:
            if msg is INTERRUPT:
                await self._interrupted.wait()
            else:
                await self._out.put(msg)

    async def interrupt(self):
        self._interrupted.set()

    async def receive_messages(self):
        while True:
            yield await self._out.get()


class FakeOutput:
    chat_id, thread_id = 1, None

    def __init__(self):
        self.notices: list[str] = []
        self.texts: list[str] = []

    async def notice(self, text):
        self.notices.append(text)

    async def text(self, md):
        self.texts.append(md)

    async def tool_use(self, *args):
        pass

    async def tool_result(self, *args):
        pass

    async def end_turn(self):
        pass
