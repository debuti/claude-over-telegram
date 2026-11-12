"""Turns Claude's output into Telegram messages.

Claude writes Markdown; Telegram understands a small HTML subset, which is far
more forgiving to produce than MarkdownV2. Messages are split below Telegram's
4096-char limit without cutting a tag or code block in half.
"""

import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

TG_LIMIT = 4000  # a little under 4096: the limit counts entities, not our tags

FENCE = re.compile(r"^```[ \t]*([\w+-]*)[^\n]*\n(.*?)(?:^```[ \t]*$|\Z)", re.M | re.S)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
STRIKE = re.compile(r"~~(.+?)~~")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s+")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(line: str) -> str:
    """Markdown inline syntax of one line -> HTML. Code spans are left untouched."""
    parts = INLINE_CODE.split(line)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{esc(part)}</code>")
            continue
        s = esc(part)
        s = LINK.sub(lambda m: '<a href="{}">{}</a>'.format(m[2].replace('"', "%22"), m[1]), s)
        s = BOLD.sub(lambda m: f"<b>{m[1] or m[2]}</b>", s)
        s = ITALIC.sub(r"<i>\1</i>", s)
        s = STRIKE.sub(r"<s>\1</s>", s)
        out.append(s)
    return "".join(out)


TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP = re.compile(r"^:?-+:?$")
TABLE_WIDTH = 40  # monospace columns a phone shows without wrapping


def _cells(line: str) -> list[str]:
    line = line.strip()[1:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _plain(cell: str) -> str:
    """A cell's Markdown inline syntax stripped, for monospace alignment."""
    cell = LINK.sub(r"\1", cell)
    cell = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m[1] or m[2], cell)
    cell = re.sub(r"`([^`]+)`", r"\1", cell)
    cell = STRIKE.sub(r"\1", cell)
    return ITALIC.sub(r"\1", cell)


def _width(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch) or ch in "‍︎️":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return w


def _table(lines: list[str]) -> str:
    """A Markdown table -> aligned <pre> if it fits a phone, else one card per row."""
    rows = [_cells(line) for line in lines]
    rows = [r for r in rows if not (any(r) and all(TABLE_SEP.match(c) for c in r if c))] or [[""]]
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    plain = [[_plain(c) for c in r] for r in rows]
    widths = [max(_width(r[i]) for r in plain) for i in range(n)]
    if sum(widths) + 2 * (n - 1) <= TABLE_WIDTH or len(rows) < 2:
        pad = lambda c, i: c + " " * (widths[i] - _width(c))  # noqa: E731
        out = ["  ".join(pad(c, i) for i, c in enumerate(r)).rstrip() for r in plain]
        out.insert(1, "  ".join("─" * w for w in widths))
        return f"<pre>{esc(chr(10).join(out))}</pre>"
    head = rows[0]  # too wide: header cells become each row's labels
    cards = []
    for r in rows[1:]:
        card = [f"<b>{_inline(r[0])}</b>"] if r[0] else []
        card += [f"{_inline(h)}: {_inline(c)}" if h else _inline(c) for h, c in zip(head[1:], r[1:], strict=True) if c]
        cards.append("\n".join(card))
    return "\n\n".join(cards)


def _text_units(text: str) -> list[str]:
    """Prose -> one HTML unit per paragraph; Markdown tables are laid out for a phone."""
    units = []
    for para in re.split(r"\n\s*\n", text.strip("\n")):
        rendered, lines = [], para.split("\n")
        i = 0
        while i < len(lines):
            j = i
            while j < len(lines) and TABLE_ROW.match(lines[j]):
                j += 1
            if j - i >= 2:  # a table, even with no blank line around it
                if rendered:
                    units.append("\n".join(rendered))
                    rendered = []
                units.append(_table(lines[i:j]))
                i = j
                continue
            line = lines[i]
            if m := HEADING.match(line):
                rendered.append(f"<b>{_inline(m[1])}</b>")
            else:
                rendered.append(_inline(BULLET.sub(r"\1• ", line)))
            i += 1
        if rendered:
            units.append("\n".join(rendered))
    return [u for u in units if u.strip()]


def _pre(code: str, lang: str) -> str:
    cls = f' class="language-{esc(lang)}"' if lang else ""
    return f"<pre><code{cls}>{esc(code)}</code></pre>"


def _split_unit(unit: str, code: tuple[str, str] | None) -> list[str]:
    """Break one oversized unit into pieces under the limit."""
    if code:
        body, lang = code
        pieces, cur = [], ""
        for line in body.splitlines(keepends=True):
            while len(esc(line)) > TG_LIMIT - 100:  # one absurdly long line
                pieces.append(_pre(line[: TG_LIMIT // 2], lang))
                line = line[TG_LIMIT // 2 :]
            if len(esc(cur + line)) > TG_LIMIT - 100:
                pieces.append(_pre(cur, lang))
                cur = ""
            cur += line
        if cur:
            pieces.append(_pre(cur, lang))
        return pieces
    # Prose: tags never span lines, so cutting at newlines keeps HTML valid.
    pieces, cur = [], ""
    for line in unit.split("\n"):
        if len(line) > TG_LIMIT:
            line = line[: TG_LIMIT - 1] + "…"
        if cur and len(cur) + len(line) + 1 > TG_LIMIT:
            pieces.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        pieces.append(cur)
    return pieces


def markdown_to_html(md: str) -> list[str]:
    """Markdown -> list of HTML messages, each under the Telegram limit."""
    units: list[tuple[str, tuple[str, str] | None]] = []
    pos = 0
    for m in FENCE.finditer(md):
        units += [(u, None) for u in _text_units(md[pos : m.start()])]
        units.append((_pre(m[2].rstrip("\n"), m[1]), (m[2].rstrip("\n"), m[1])))
        pos = m.end()
    units += [(u, None) for u in _text_units(md[pos:])]

    messages, cur = [], ""
    for unit, code in units:
        for piece in _split_unit(unit, code) if len(unit) > TG_LIMIT else [unit]:
            if cur and len(cur) + len(piece) + 2 > TG_LIMIT:
                messages.append(cur)
                cur = ""
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        messages.append(cur)
    return messages


def html_to_plain(text: str) -> str:
    """Fallback when Telegram rejects our HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# --- tool calls ---------------------------------------------------------------


def _short(text: str, n: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _rel(path: str, cwd: Path) -> str:
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path


TODO_MARK = {"completed": "✅", "in_progress": "⏳", "pending": "▫️"}


def tool_line(name: str, inp: dict[str, Any], cwd: Path) -> str:
    """One compact HTML line describing a tool call."""
    c = lambda s, n=120: f"<code>{esc(_short(s, n))}</code>"  # noqa: E731
    match name:
        case "Bash":
            return f"💻 {c(inp.get('command', ''), 200)}"
        case "Read":
            return f"📖 {c(_rel(inp.get('file_path', ''), cwd))}"
        case "Write":
            return f"📝 {c(_rel(inp.get('file_path', ''), cwd))}"
        case "Edit" | "MultiEdit":
            return f"✏️ {c(_rel(inp.get('file_path', ''), cwd))}"
        case "NotebookEdit":
            return f"✏️ {c(_rel(inp.get('notebook_path', ''), cwd))}"
        case "Glob":
            return f"🔎 {c(inp.get('pattern', ''))}"
        case "Grep":
            where = f" in {c(_rel(inp['path'], cwd))}" if inp.get("path") else ""
            return f"🔎 {c(inp.get('pattern', ''))}{where}"
        case "WebFetch":
            return f"🌐 {c(inp.get('url', ''))}"
        case "WebSearch":
            return f"🌐 {esc(_short(inp.get('query', '')))}"
        case "Task" | "Agent":
            return f"🤖 {esc(_short(inp.get('description', '') or inp.get('prompt', '')))}"
        case "TodoWrite":
            todos = inp.get("todos") or []
            return "📋 Todo\n" + "\n".join(
                f"{TODO_MARK.get(t.get('status'), '▫️')} {esc(_short(t.get('content', ''), 80))}" for t in todos
            )
    if name == "mcp__telegram__send_file":
        return f"📤 {c(_rel(inp.get('path', ''), cwd))}"
    if name.startswith("mcp__"):
        return f"🔌 {esc(name.split('__', 2)[-1])} {c(json.dumps(inp, ensure_ascii=False), 100)}"
    return f"🔧 {esc(name)} {c(json.dumps(inp, ensure_ascii=False), 100)}"


def tool_result_text(content: Any) -> str:
    """Flatten a ToolResultBlock's content to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif isinstance(item, dict):
            parts.append(f"[{item.get('type', 'data')}]")
        else:
            parts.append(str(item))
    return "\n".join(parts)
