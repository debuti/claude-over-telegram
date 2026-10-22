"""Persists per-conversation state so sessions survive bot restarts.

A conversation is keyed by (chat_id, thread_id); thread_id is None outside
forum topics. Keying by thread from v1 keeps parallel topics (v2) cheap.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

Key = tuple[int, int | None]


@dataclass
class ConvState:
    cwd: str
    session_id: str | None = None
    mode: str | None = None
    model: str | None = None
    tools: str = "hidden"  # hidden | calls | full (calls plus their results)
    cost_usd: float = 0.0  # estimated, accumulated over the whole conversation
    title: str | None = None  # topic name, known when the topic is created or renamed


def _migrate(v: dict) -> dict:
    if "verbose" in v:  # <= 0.4: a boolean for showing tool results
        v["tools"] = "full" if v.pop("verbose") else "calls"
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

    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._data.items()}, indent=2))
        tmp.replace(self._path)
