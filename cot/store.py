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
    verbose: bool = False


class Store:
    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, ConvState] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            self._data = {k: ConvState(**v) for k, v in raw.items()}

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
