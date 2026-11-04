"""The topic name's status line."""

import pytest

from cot import titles


@pytest.mark.parametrize(
    "context, shown",
    [(0, "🔟"), (14_000, "9️⃣"), (15_000, "9️⃣"), (45_000, "7️⃣"), (149_000, "0️⃣"), (150_000, "0️⃣"), (400_000, "0️⃣")],
)
def test_room(context, shown):
    assert titles.room(context, 150_000) == shown


@pytest.mark.parametrize(
    "left, shown",
    [(3540, ">45m"), (2700, ">45m"), (2699, "30m"), (130, "2m"), (100, "1m"), (1, "1m"), (0, "🧊"), (-500, "🧊")],
)
def test_cache_never_overstates_whats_left(left, shown):
    assert titles.cache(left) == shown


def test_compose_leaves_out_what_has_nothing_to_say():
    assert titles.compose("web") == "web"
    assert titles.compose("web", "⚙️", "7️⃣", "30m", 2) == "⚙️·7️⃣·30m·web·📨2"


def test_a_long_name_gives_way():
    title = titles.compose("x" * 200, "⚙️", "7️⃣", "30m", 2)
    assert len(title) == titles.MAX_LENGTH and title.endswith("·📨2")


@pytest.mark.parametrize(
    "edited, name",
    [
        ("⚙️·7️⃣·30m·web·📨2", "web"),
        ("💤·🔟·🧊·Mi web", "Mi web"),
        ("❓·7️⃣·web", "web"),
        ("7️⃣·>45m·web", "web"),
        ("web", "web"),
        ("a·b", "a·b"),
        ("🧊", "🧊"),
    ],
)
def test_plain_takes_the_status_out_of_an_edited_title(edited, name):
    assert titles.plain(edited) == name


def test_legend_explains_every_field():
    states = (titles.WORKING, titles.ASKING, titles.LIMITED, titles.ASLEEP)
    for field in (*states, titles.COLD, titles.QUEUED, *titles.KEYCAPS[::10]):
        assert field in titles.LEGEND
