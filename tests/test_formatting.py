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
    assert markdown_to_telegram_html("`<b>tag</b>`") == "<code>&lt;b&gt;tag&lt;/b&gt;</code>"


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
    assert '<a href="https://venturebeat.com/article">VentureBeat — Article</a>' in result
    assert '<a href="https://aws.amazon.com/blog/post">AWS Blog — Post</a>' in result


def test_plain_text_unchanged():
    assert markdown_to_telegram_html("hello world") == "hello world"


def test_underscore_in_words_not_italicized():
    result = markdown_to_telegram_html("some_variable_name")
    assert "<i>" not in result
