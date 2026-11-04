"""The topic's name as a status line: state · room until compacting · cache left · name · queued."""

import re

KEYCAPS = ("0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
WORKING, LIMITED, ASLEEP = "⚙️", "⏸", "💤"
COLD, QUEUED = "🧊", "📨"
# Telegram posts a service message per rename, so the cache's time moves in steps (minutes), never
# overstating what's left.
CACHE_STEPS = (45, 30, 15, 10, 5, 2, 1)
MAX_LENGTH = 128  # Telegram's limit for a topic's name

_STATUS_FIELD = re.compile(
    "|".join(map(re.escape, (WORKING, LIMITED, ASLEEP, COLD, *KEYCAPS))) + rf"|\d+m|{QUEUED}\d+"
)


def room(context: int, compact_at: int) -> str:
    """The share left until compacting, in tenths."""
    return KEYCAPS[int(max(1 - context / compact_at, 0) * 10)]


def cache(left: float) -> str:
    if left <= 0:
        return COLD
    return f"{next((step for step in CACHE_STEPS if left >= step * 60), 1)}m"


def compose(name: str, status: str = "", room: str = "", cache: str = "", queued: int = 0) -> str:
    tail = [f"{QUEUED}{queued}"] if queued else []
    head = [field for field in (status, room, cache) if field]
    # The name gives way: what's around it is what changes.
    fits = MAX_LENGTH - len("·".join([*head, *tail])) - 1
    return "·".join([*head, name[:fits], *tail])


def plain(title: str) -> str:
    """The name in a title the user edited with the status still in it."""
    fields = [field for field in title.split("·") if not _STATUS_FIELD.fullmatch(field.strip())]
    return "·".join(fields).strip() or title
