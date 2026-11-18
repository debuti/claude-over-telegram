"""The plan usage over time: every reading (at most one each few minutes) in state/usage_history.jsonl,
and a chart of the last days of it as a PNG."""

import io
import json
import time
from pathlib import Path

EVERY = 240.0  # seconds: readings closer than this to the last one kept aren't kept
KEEP_DAYS = 31
GAP = 1800.0  # seconds without readings after which the lines break instead of joining the dots

# Categorical slots in fixed order (dataviz reference palette, light surface), by limit: a limit keeps its colour
# whichever others there are.
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
FIXED = ["Week, all models", "Week, Opus", "Week, Sonnet"]
SESSION = "5-hour session"

_last_at = 0.0


def record(path: Path, data: dict, now: float | None = None) -> bool:
    """Keep this reading, unless one was kept less than EVERY seconds ago. Once a day, drops the old ones."""
    global _last_at
    from .usage import _label  # here: usage imports this module

    now = time.time() if now is None else now
    if not _last_at and path.exists():
        _last_at = _tail_time(path)
    if now - _last_at < EVERY:
        return False
    limits = {_label(lim): round(lim.get("percent") or 0, 1) for lim in data.get("limits") or []}
    if not limits:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"t": round(now), "l": limits}, ensure_ascii=False) + "\n")
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


def load(path: Path, since: float) -> list[tuple[float, dict[str, float]]]:
    rows = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            if row["t"] >= since:
                rows.append((float(row["t"]), row["l"]))
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def _prune(path: Path, now: float) -> None:
    rows = load(path, now - KEEP_DAYS * 86400)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps({"t": round(t), "l": lim}, ensure_ascii=False) + "\n" for t, lim in rows))
    tmp.replace(path)


def colours(labels: list[str]) -> dict[str, str]:
    """The fixed limits keep their slot; the others (a model's own weekly limit) take the next ones, by name."""
    others = sorted(x for x in labels if x not in FIXED and x != SESSION)
    order = FIXED + others
    return {label: SLOTS[order.index(label) % len(SLOTS)] for label in labels if label != SESSION} | {
        SESSION: SLOTS[0]
    }


def _series(rows, label):
    """(times, percents) for one limit, with a None where readings stop for longer than GAP."""
    xs, ys, prev = [], [], None
    for t, lim in rows:
        if label not in lim:
            continue
        if prev is not None and t - prev > GAP:
            xs.append(prev + 1)
            ys.append(None)
        xs.append(t)
        ys.append(lim[label])
        prev = t
    return xs, ys


def chart(path: Path, days: int = 30, now: float | None = None) -> bytes | None:
    """PNG with the weekly limits above and the 5-hour session below; None with fewer than two readings."""
    import matplotlib

    matplotlib.use("Agg")
    from datetime import datetime, timezone

    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    now = time.time() if now is None else now
    rows = load(path, now - days * 86400)
    if len(rows) < 2:
        return None
    labels = sorted({label for _, lim in rows for label in lim})
    weekly = [x for x in FIXED if x in labels] + sorted(x for x in labels if x not in FIXED and x != SESSION)
    colour = colours(labels)

    def dt(ts):
        return [datetime.fromtimestamp(t, timezone.utc) for t in ts]

    plt.rcParams.update({"font.size": 10, "text.color": INK, "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": MUTED})
    panels = [("Weekly limits", weekly)] + ([(SESSION, [SESSION])] if SESSION in labels else [])
    fig, axes = plt.subplots(len(panels), 1, figsize=(9, 3.4 * len(panels)), sharex=True, squeeze=False,
                             facecolor=SURFACE, dpi=150)
    start = datetime.fromtimestamp(max(rows[0][0], now - days * 86400), timezone.utc)
    end = datetime.fromtimestamp(now, timezone.utc)
    for ax, (title, series) in zip(axes[:, 0], panels, strict=True):
        ax.set_facecolor(SURFACE)
        for label in series:
            xs, ys = _series(rows, label)
            ys = [float("nan") if y is None else y for y in ys]
            ax.plot(dt(xs), ys, color=colour[label], linewidth=1.6, solid_capstyle="round",
                    label=f"{label.removeprefix('Week, ')}  {ys[-1]:.0f}%")
            # Direct label at the line's end: the colours alone don't carry identity.
            ax.annotate(f"{ys[-1]:.0f}%", (dt(xs[-1:])[0], ys[-1]), xytext=(4, 0), textcoords="offset points",
                        va="center", fontsize=9, color=INK)
        ax.set_ylim(0, 105)
        ax.set_xlim(start, end)
        ax.set_yticks([0, 25, 50, 75, 100])
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(length=0)
        ax.set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold")
        if len(series) > 1:
            ax.legend(loc="lower right", bbox_to_anchor=(1, 1), frameon=False, ncol=min(len(series), 4), fontsize=9,
                      borderaxespad=0.2)
    span = (end - start).total_seconds() / 86400
    ax = axes[-1, 0]
    if span <= 3:
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M"))
    else:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, round(span / 8))))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.text(0.99, 0.005, "UTC", ha="right", va="bottom", fontsize=8, color=MUTED)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
