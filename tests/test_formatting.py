"""Tests for pyclaudir.formatting — Markdown → Telegram HTML conversion."""

from pyclaudir.formatting import markdown_to_telegram_html


def test_bold():
    assert markdown_to_telegram_html("**hello**") == "<b>hello</b>"


def test_italic():
    assert markdown_to_telegram_html("*hello*") == "<i>hello</i>"


def test_bold_italic():
    assert markdown_to_telegram_html("***hello***") == "<b><i>hello</i></b>"


def test_strikethrough():
    assert markdown_to_telegram_html("~~deleted~~") == "<s>deleted</s>"


def test_inline_code():
    assert markdown_to_telegram_html("`print(1)`") == "<code>print(1)</code>"


def test_inline_code_html_escaped():
    assert (
        markdown_to_telegram_html("`<b>tag</b>`")
        == "<code>&lt;b&gt;tag&lt;/b&gt;</code>"
    )


def test_fenced_code_block():
    md = "```python\nprint('hi')\n```"
    expected = '<pre><code class="language-python">print(&#x27;hi&#x27;)</code></pre>'
    assert markdown_to_telegram_html(md) == expected


def test_fenced_code_block_no_lang():
    md = "```\nsome code\n```"
    assert markdown_to_telegram_html(md) == "<pre>some code</pre>"


def test_link():
    md = "[Google](https://google.com)"
    assert markdown_to_telegram_html(md) == '<a href="https://google.com">Google</a>'


def test_link_with_special_chars():
    md = "[A & B](https://example.com?a=1&b=2)"
    result = markdown_to_telegram_html(md)
    assert "&amp;" in result
    assert 'href="https://example.com?a=1&amp;b=2"' in result


def test_heading_stripped():
    assert markdown_to_telegram_html("### Title") == "Title"
    assert markdown_to_telegram_html("# H1") == "H1"


def test_html_entities_escaped():
    assert markdown_to_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_mixed_formatting():
    md = "**Bold** and *italic* and `code`"
    result = markdown_to_telegram_html(md)
    assert "<b>Bold</b>" in result
    assert "<i>italic</i>" in result
    assert "<code>code</code>" in result


def test_sources_with_links():
    """Regression — markdown links in a list rendered broken at one point."""
    md = (
        "Sources:\n"
        "- [VentureBeat — Article](https://venturebeat.com/article)\n"
        "- [AWS Blog — Post](https://aws.amazon.com/blog/post)"
    )
    result = markdown_to_telegram_html(md)
    assert (
        '<a href="https://venturebeat.com/article">VentureBeat — Article</a>' in result
    )
    assert '<a href="https://aws.amazon.com/blog/post">AWS Blog — Post</a>' in result


def test_plain_text_unchanged():
    assert markdown_to_telegram_html("hello world") == "hello world"


def test_underscore_in_words_not_italicized():
    result = markdown_to_telegram_html("some_variable_name")
    assert "<i>" not in result


def test_blockquote_single_line():
    assert markdown_to_telegram_html("> hi") == "<blockquote>hi</blockquote>\n"


def test_blockquote_multi_line():
    md = "> first\n> second"
    assert markdown_to_telegram_html(md) == "<blockquote>first\nsecond</blockquote>\n"


def test_blockquote_with_inline_formatting():
    md = "> **bold** and [link](https://x)"
    result = markdown_to_telegram_html(md)
    assert result.startswith("<blockquote>")
    assert "<b>bold</b>" in result
    assert '<a href="https://x">link</a>' in result
    assert result.rstrip("\n").endswith("</blockquote>")


def test_blockquote_followed_by_text():
    md = "> quoted\nplain tail"
    result = markdown_to_telegram_html(md)
    assert "<blockquote>quoted</blockquote>" in result
    assert result.endswith("plain tail")


def test_gt_inside_code_block_not_blockquoted():
    md = "```\n> not a quote\n```"
    result = markdown_to_telegram_html(md)
    assert "<blockquote>" not in result
    assert "&gt; not a quote" in result


def test_plain_gt_not_at_line_start():
    result = markdown_to_telegram_html("a > b")
    assert result == "a &gt; b"
    assert "<blockquote>" not in result


def test_markdown_table_converted_to_bullets():
    md = "| col1 | col2 |\n|------|------|\n| a    | b    |"
    result = markdown_to_telegram_html(md)
    assert "|" not in result
    assert "• col1 — col2" in result
    assert "• a — b" in result


def test_markdown_table_three_columns():
    md = "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |"
    result = markdown_to_telegram_html(md)
    assert "• a — b — c" in result
    assert "• 1 — 2 — 3" in result


def test_horizontal_rule_dash_stripped():
    result = markdown_to_telegram_html("before\n---\nafter")
    assert "---" not in result
    assert "before" in result
    assert "after" in result


def test_horizontal_rule_equals_stripped():
    result = markdown_to_telegram_html("before\n===\nafter")
    assert "===" not in result


def test_horizontal_rule_stars_stripped():
    result = markdown_to_telegram_html("before\n***\nafter")
    assert "***" not in result


def test_dash_list_marker_converted_to_bullet():
    result = markdown_to_telegram_html("- foo\n- bar")
    assert "• foo" in result
    assert "• bar" in result
    assert "- foo" not in result


def test_asterisk_list_marker_converted_to_bullet():
    result = markdown_to_telegram_html("* foo\n* bar")
    assert "• foo" in result
    assert "• bar" in result


def test_plus_list_marker_converted_to_bullet():
    result = markdown_to_telegram_html("+ foo")
    assert "• foo" in result


def test_nested_list_indent_preserved():
    result = markdown_to_telegram_html("  - foo")
    assert "  • foo" in result


def test_image_converted_to_link():
    md = "![alt text](https://example.com/img.png)"
    result = markdown_to_telegram_html(md)
    assert result == '<a href="https://example.com/img.png">alt text</a>'


def test_table_inside_code_block_preserved():
    md = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    result = markdown_to_telegram_html(md)
    assert "| a | b |" in result
    assert "|---|---|" in result


def test_dash_inside_code_block_preserved():
    md = "```\n---\n```"
    result = markdown_to_telegram_html(md)
    assert "---" in result


def test_dash_list_inside_code_block_preserved():
    md = "```\n- foo\n```"
    result = markdown_to_telegram_html(md)
    assert "- foo" in result
    assert "• foo" not in result


def test_single_pipe_line_not_treated_as_table():
    """A lone `| ... |` line without a separator row should pass through."""
    result = markdown_to_telegram_html("| just text |")
    assert "• just text" not in result
    assert "| just text |" in result
