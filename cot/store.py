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
    # The model that answered last (what "default" turned out to be), for the topic's name.
    last_model: str | None = None
    title: str | None = None  # topic name, known when the topic is created or renamed
    shown_title: str | None = None  # what the bot last set the topic's name to, status included
    # Messages not yet handed to Claude ({"text", "ids"}), so a restart doesn't lose them.
    pending: list[dict] = field(default_factory=list)
    # Messages held back by a warning ({"text", "ids"}) until the user picks what happens to them.
    held: list[dict] = field(default_factory=list)
    # While a plan limit stops Claude: epoch seconds of when to look again. Held messages wait for it.
    limit_until: float = 0.0
    # A plan limit cut Claude's work short: when it lifts, whether Claude carries on is asked.
    limit_cut: bool = False
    # Asked in advance to go on when a plan limit lifts, without asking again: "send" or "compact".
    limit_auto: str = ""
    # /catchup: what every plan limit gets without asking, as limit_auto; "" asks each time.
    catchup: str = ""
    # When Claude's login was found gone, while held messages wait for it to come back; else 0.
    login_lost_at: float = 0.0
    # /sleep: no cache warnings, keep-alives or alarms until its next message.
    asleep: bool = False
    # An "authentication failed" with a login that looks fine (a 403 from Cloudflare in front of the API):
    # when to try again (0 if it's the login itself that's gone), how many tries so far, and when the last one went.
    login_retry_at: float = 0.0
    login_retries: int = 0
    login_retried_at: float = 0.0
    # The warning whose buttons are live: {"nonce", "msg_id", "kind"}; at most one per conversation.
    prompt: dict | None = None
    # Messages to other agents waiting for the user's button ({"id", "sender", "to", "text", "html"}).
    agent_sends: list[dict] = field(default_factory=list)
    # Alarms Claude asked for with set_alarm ({"id", "reason", "minutes", "cwd", "asked_at", "at", "html"}):
    # "at" is 0 until the user approves it, then the epoch seconds it rings at.
    wakeups: list[dict] = field(default_factory=list)
    # Messages sent while Claude sleeps until an approved alarm ({"text", "ids"}): they go with it
    # when it rings, or with /wake.
    for_alarm: list[dict] = field(default_factory=list)


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
