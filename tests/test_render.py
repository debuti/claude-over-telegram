"""Markdown tables are laid out for a phone."""

from cot.render import markdown_to_html


def test_a_table_glued_to_text_is_still_a_table_and_aligned():
    md = "Resumen:\n| A | Bb |\n|---|---|\n| **x** | `yyy` |\nfin"
    assert markdown_to_html(md) == ["Resumen:\n\n<pre>A  Bb\n─  ───\nx  yyy</pre>\n\nfin"]


def test_wide_characters_count_double():
    out = markdown_to_html("| a | b |\n|-|-|\n| 🔥 | z |")[0]
    assert "🔥  z" in out and "a   b" in out


def test_a_table_too_wide_for_a_phone_becomes_one_card_per_row():
    md = "| File | What |\n|---|---|\n| bot.py | " + "w " * 30 + "|\n| a\\|b | x |"
    out = markdown_to_html(md)[0]
    assert "<pre>" not in out
    assert out.startswith("<b>bot.py</b>\nWhat: w w")
    assert out.endswith("<b>a|b</b>\nWhat: x")
