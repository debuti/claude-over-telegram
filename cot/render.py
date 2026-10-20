"""Turns Claude's output into Telegram messages.

Claude writes Markdown; Telegram understands a small HTML subset, which is far
more forgiving to produce than MarkdownV2. Messages are split below Telegram's
4096-char limit without cutting a tag or code block in half.
"""

import html
import json
import re
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
        s = LINK.sub(lambda m: f'<a href="{m[2].replace('"', "%22")}">{m[1]}</a>', s)
        s = BOLD.sub(lambda m: f"<b>{m[1] or m[2]}</b>", s)
        s = ITALIC.sub(r"<i>\1</i>", s)
        s = STRIKE.sub(r"<s>\1</s>", s)
        out.append(s)
    return "".join(out)


def _text_units(text: str) -> list[str]:
    """Prose -> one HTML unit per paragraph; Markdown tables become <pre>."""
    units = []
    for para in re.split(r"\n\s*\n", text.strip("\n")):
        lines = para.split("\n")
        if all(l.lstrip().startswith("|") for l in lines):
            units.append(f"<pre>{esc(para)}</pre>")
            continue
        rendered = []
        for line in lines:
            if m := HEADING.match(line):
                rendered.append(f"<b>{_inline(m[1])}</b>")
            else:
                rendered.append(_inline(BULLET.sub(r"\1• ", line)))
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
