"""One line per turn in state/usage.jsonl: what it cost and how it moved the plan limits.

The limits are read before and after each turn, so a turn's share of the 5-hour and weekly
windows shows up next to its cost. Turns of other conversations running at the same time
move the same windows; `parallel` says how many there were.

`python -m cot.usagelog [state/usage.jsonl]` sums it up per conversation.
"""

import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import usage

log = logging.getLogger(__name__)

# Two turns ending together read the limits once.
FRESH = 15.0


class UsageLog:
    def __init__(self, path: Path, cli: str):
        self._path = path
        self._cli = cli
        self._before: dict[object, asyncio.Task] = {}
        self._last: tuple[float, dict | None] = (0.0, None)
        self._lock = asyncio.Lock()

    async def _limits(self) -> dict | None:
        """{kind: percent}, or None without a subscription login or when the endpoint fails."""
        async with self._lock:
            at, limits = self._last
            if time.monotonic() - at < FRESH:
                return limits
            try:
                data = await usage.fetch(self._cli)
                limits = {lim["kind"]: lim.get("percent") for lim in data.get("limits") or []}
            except usage.Unavailable:
                limits = None
            except Exception as e:
                log.warning("usage for the log failed: %s", e)
                limits = None
            self._last = (time.monotonic(), limits)
            return limits

    def turn_started(self, key: object) -> None:
        if key not in self._before:
            self._before[key] = asyncio.create_task(self._limits())

    async def turn_ended(self, key: object, entry: dict) -> None:
        before = self._before.pop(key, None)
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **entry,
            "before": await before if before else None,
            "after": await self._limits(),
        }
        try:
            with self._path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            log.warning("could not write %s: %s", self._path, e)


def summary(path: Path) -> str:
    convs: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        c = convs[e.get("conversation") or "?"]
        c["turns"] += 1
        c["cost"] += e.get("cost") or 0
        c["api_calls"] += e.get("api_calls") or 0
        before, after = e.get("before") or {}, e.get("after") or {}
        for kind in after:
            if before.get(kind) is not None and after[kind] is not None and after[kind] >= before[kind]:
                c[kind] += after[kind] - before[kind]  # a window that reset mid-turn is skipped
    rows = [f"{'conversation':30} {'turns':>5} {'calls':>6} {'cost':>8} {'5h %':>6} {'week %':>6}"]
    for name, c in sorted(convs.items(), key=lambda kv: -kv[1]["cost"]):
        rows.append(
            f"{name[:30]:30} {int(c['turns']):5} {int(c['api_calls']):6} {c['cost']:8.2f} "
            f"{c['session']:6.0f} {c['weekly_all']:6.0f}"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    print(summary(Path(sys.argv[1] if len(sys.argv) > 1 else "state/usage.jsonl")))
