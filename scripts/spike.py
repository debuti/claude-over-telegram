"""F0 spike: check the Agent SDK runs with the local Claude login (no API key)."""

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)


async def main() -> None:
    options = ClaudeAgentOptions(max_turns=1, setting_sources=[])
    async with ClaudeSDKClient(options) as client:
        await client.query("Reply with just: ok")
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                print("init:", {k: msg.data.get(k) for k in ("apiKeySource", "model", "claude_code_version")})
            elif isinstance(msg, AssistantMessage):
                print("claude:", "".join(b.text for b in msg.content if isinstance(b, TextBlock)))
            elif isinstance(msg, ResultMessage):
                print("result:", msg.subtype, "session:", msg.session_id, "cost_usd:", msg.total_cost_usd)


asyncio.run(main())
