"""The plan usage over time: every reading (at most one each few minutes) in state/usage_history.jsonl,
and charts of it as PNGs: the weeks one over the other from their reset, and the 5-hour session over the last days."""

import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

EVERY = 240.0  # seconds: readings closer than this to the last one kept aren't kept
KEEP_DAYS = 31
GAP = 1800.0  # seconds without readings after which the lines break instead of joining the dots
WEEK = 7 * 86400

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
SESSION_COLOUR = "#2a78d6"
# One hue, from the current week (intense) to the oldest ones (towards the surface): sequential, not categorical.
WEEK_REDS = ["#c8102e", "#e0525f", "#eb8a91", "#f2b6b9", "#f6d4d5"]
SESSION = "5-hour session"
# The weekly reset when a reading doesn't carry its own: Friday 20:00 UTC.
FALLBACK_RESET = datetime(2026, 1, 2, 20, tzinfo=timezone.utc).timestamp()

_last_at = 0.0


def record(path: Path, data: dict, now: float | None = None) -> bool:
    """Keep this reading, unless one was kept less than EVERY seconds ago. Once a day, drops the old ones."""
    global _last_at
    from .usage import _label, epoch  # here: usage imports this module

    now = time.time() if now is None else now
    if not _last_at and path.exists():
        _last_at = _tail_time(path)
    if now - _last_at < EVERY:
        return False
    limits = {_label(lim): round(lim.get("percent") or 0, 1) for lim in data.get("limits") or []}
    if not limits:
        return False
    resets = {}
    for lim in data.get("limits") or []:
        try:
            resets[_label(lim)] = round(epoch(lim["resets_at"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"t": round(now), "l": limits, "r": resets}, ensure_ascii=False) + "\n")
    if int(now // 86400) != int(_last_at // 86400):
        _prune(path, now)
    _last_at = now
    return True


def _tail_time(path: Path) -> float:
    try:
        with path.open("rb") as f:
            f.seek(max(f.seek(0, 2) - 4096, 0))
            line = f.read().decode(errors="ignore").strip().rsplit("\n", 1)[-1]
        return float(json.loads(line)["t"])
    except (OSError, ValueError, KeyError, IndexError):
        return 0.0


def load(path: Path, since: float) -> list[dict]:
    """The readings since then, as {"t", "l": {label: percent}, "r": {label: reset epoch}}."""
    rows = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            if row["t"] >= since:
                rows.append({"t": float(row["t"]), "l": row["l"], "r": row.get("r") or {}})
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def _prune(path: Path, now: float) -> None:
    rows = load(path, now - KEEP_DAYS * 86400)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    tmp.replace(path)


def week_start(row: dict, label: str) -> float:
    """When the week this reading belongs to began: a week before its reset (to the hour, so the
    reset's jitter doesn't split a week), or the last Friday 20:00 UTC without one."""
    reset = row["r"].get(label)
    if reset:
        return round((reset - WEEK) / 3600) * 3600
    return row["t"] - (row["t"] - FALLBACK_RESET) % WEEK


def _broken(points):
    """(xs, ys) with a NaN where readings stop for longer than GAP, so the line breaks there."""
    xs, ys, prev = [], [], None
    for t, x, y in points:
        if prev is not None and t - prev > GAP:
            xs.append(x)
            ys.append(float("nan"))
        xs.append(x)
        ys.append(y)
        prev = t
    return xs, ys


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 10, "text.color": INK, "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": MUTED})
    return plt


def _style(ax, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    ax.set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold")


def _png(fig, plt) -> bytes:
    fig.text(0.99, 0.005, "UTC", ha="right", va="bottom", fontsize=8, color=MUTED)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def weekly_chart(path: Path, now: float | None = None) -> bytes | None:
    """PNG with one panel per weekly limit: x the week from its reset, one line per week, the current one
    intense red and the older ones fading. None without readings."""
    now = time.time() if now is None else now
    rows = load(path, now - KEEP_DAYS * 86400)
    labels = sorted({x for row in rows for x in row["l"] if x != SESSION},
                    key=lambda x: (x != "Week, all models", x))
    if not rows or not labels:
        return None
    plt = _plt()
    fig, axes = plt.subplots(len(labels), 1, figsize=(9, 3.6 * len(labels)), squeeze=False,
                             facecolor=SURFACE, dpi=150)
    for ax, label in zip(axes[:, 0], labels, strict=True):
        weeks: dict[float, list] = {}
        for row in rows:
            if label in row["l"]:
                start = week_start(row, label)
                weeks.setdefault(start, []).append((row["t"], (row["t"] - start) / 86400, row["l"][label]))
        starts = sorted(weeks, reverse=True)[: len(WEEK_REDS)]
        for i, start in reversed(list(enumerate(starts))):  # the current one drawn last, on top
            xs, ys = _broken(weeks[start])
            name = "This week" if i == 0 else f"From {datetime.fromtimestamp(start, timezone.utc):%b %d}"
            ax.plot(xs, ys, color=WEEK_REDS[i], linewidth=2.2 if i == 0 else 1.6, solid_capstyle="round",
                    label=f"{name}  {weeks[start][-1][2]:.0f}%", zorder=10 - i)
            if i == 0:  # its value at its end: colour alone doesn't say which line is which
                ax.annotate(f"{ys[-1]:.0f}%", (xs[-1], ys[-1]), xytext=(5, 0), textcoords="offset points",
                            va="center", fontsize=9, color=INK, fontweight="bold", zorder=20,
                            bbox={"boxstyle": "round,pad=0.2", "facecolor": SURFACE, "edgecolor": "none"})
        _style(ax, label.replace("Week, ", "Weekly limit, "))
        ax.set_xlim(0, 7)
        first = datetime.fromtimestamp(starts[0], timezone.utc)
        ax.set_xticks(range(8))
        ax.set_xticklabels([f"{first + timedelta(days=d):%a %H:%M}" if d < 7 else "reset" for d in range(8)])
        ax.axvline((now - starts[0]) / 86400, color=GRID, linewidth=1, zorder=0)  # now
        handles, names = ax.get_legend_handles_labels()
        ax.legend(handles[::-1], names[::-1], loc="lower right", bbox_to_anchor=(1, 1), frameon=False,
                  ncol=min(len(names), 3), fontsize=9, borderaxespad=0.2)
    return _png(fig, plt)


def session_chart(path: Path, days: int = 7, now: float | None = None) -> bytes | None:
    """PNG of the 5-hour session over the last days; None with fewer than two readings."""
    now = time.time() if now is None else now
    rows = [row for row in load(path, now - days * 86400) if SESSION in row["l"]]
    if len(rows) < 2:
        return None
    plt = _plt()
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(9, 3.8), facecolor=SURFACE, dpi=150)
    xs, ys = _broken([(r["t"], datetime.fromtimestamp(r["t"], timezone.utc), r["l"][SESSION]) for r in rows])
    ax.plot(xs, ys, color=SESSION_COLOUR, linewidth=1.6, solid_capstyle="round")
    ax.annotate(f"{ys[-1]:.0f}%", (xs[-1], ys[-1]), xytext=(5, 0), textcoords="offset points",
                va="center", fontsize=9, color=INK, fontweight="bold")
    _style(ax, SESSION)
    start = datetime.fromtimestamp(max(rows[0]["t"], now - days * 86400), timezone.utc)
    end = datetime.fromtimestamp(now, timezone.utc)
    ax.set_xlim(start, end)
    span = (end - start).total_seconds() / 86400
    if span <= 3:
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6 if span <= 1.5 else 12)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %Hh"))
    else:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, round(span / 8))))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d"))
    return _png(fig, plt)
