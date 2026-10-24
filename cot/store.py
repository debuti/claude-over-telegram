"""Persists per-conversation state so sessions survive bot restarts.

A conversation is keyed by (chat_id, thread_id); thread_id is None outside
forum topics. Keying by thread from v1 keeps parallel topics (v2) cheap.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

Key = tuple[int, int | None]


@dataclass
class ConvState:
    cwd: str
    session_id: str | None = None
    mode: str | None = None
    model: str | None = None
    tools: str = "hidden"  # hidden | calls | full (calls plus their results)
    cost_usd: float = 0.0  # estimated, the CLI's running total for session_id
    context_tokens: int = 0  # context size at Claude's last answer
    after_compact: int = 0  # context size right after the last compaction
    last_active: float = 0.0  # epoch seconds of Claude's last turn or output
    # Epoch seconds of the main thread's last API call: the prompt cache lives on from it.
    # 0 in state from before 2.13 (then the transcript's last write stands in).
    last_api_at: float = 0.0
    title: str | None = None  # topic name, known when the topic is created or renamed
    # Messages not yet handed to Claude ({"text", "ids"}), so a restart doesn't lose them.
    pending: list[dict] = field(default_factory=list)
    # Messages held back by a warning ({"text", "ids"}) until the user picks what happens to them.
    held: list[dict] = field(default_factory=list)
    # The warning whose buttons are live: {"nonce", "msg_id", "kind"}; at most one per conversation.
    prompt: dict | None = None


def _migrate(v: dict) -> dict:
    if "verbose" in v:  # <= 0.4: a boolean for showing tool results
        v["tools"] = "full" if v.pop("verbose") else "calls"
    if "parked" in v:  # <= 2.12: messages to a sleeping conversation
        v["held"] = v.pop("parked")
    return v


class Store:
    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, ConvState] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            self._data = {k: ConvState(**_migrate(v)) for k, v in raw.items()}

    @staticmethod
    def _k(key: Key) -> str:
        chat_id, thread_id = key
        return f"{chat_id}:{thread_id or 0}"

    def get(self, key: Key) -> ConvState | None:
        return self._data.get(self._k(key))

    def put(self, key: Key, state: ConvState) -> None:
        self._data[self._k(key)] = state
        self.save()

    def items(self) -> list[tuple[Key, ConvState]]:
        out = []
        for k, state in self._data.items():
            chat_id, thread_id = k.split(":")
            out.append(((int(chat_id), int(thread_id) or None), state))
        return out

    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._data.items()}, indent=2))
        tmp.replace(self._path)
