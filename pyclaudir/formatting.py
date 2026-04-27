"""Convert Markdown-flavoured text to Telegram-safe HTML.

Telegram's Bot API supports a limited subset of HTML (see
https://core.telegram.org/bots/api#html-style). This module converts
common Markdown constructs the LLM likes to produce into that subset
so messages render correctly in Telegram clients.
"""

from __future__ import annotations

import re
from html import escape


def markdown_to_telegram_html(text: str) -> str:
    """Best-effort Markdown → Telegram HTML conversion.

    Handles: bold, italic, strikethrough, inline code, fenced code blocks,
    inline links, bare URLs, and blockquotes.  Unsupported constructs
    (headings, lists, images) are simplified to plain text equivalents.
    """

    # Step 1: extract fenced code blocks so inner content isn't processed
    code_blocks: list[str] = []

    def _stash_code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = escape(m.group(2).rstrip("\n"))
        idx = len(code_blocks)
        if lang:
            code_blocks.append(
                f'<pre><code class="language-{escape(lang)}">{code}</code></pre>'
            )
        else:
            code_blocks.append(f"<pre>{code}</pre>")
        return f"\x00CODEBLOCK{idx}\x00"

    text = re.sub(
        r"```(\w+)?\n?(.*?)```", _stash_code_block, text, flags=re.DOTALL
    )

    # Step 2: extract inline code spans
    inline_codes: list[str] = []

    def _stash_inline_code(m: re.Match) -> str:
        idx = len(inline_codes)
        inline_codes.append(f"<code>{escape(m.group(1))}</code>")
        return f"\x00INLINECODE{idx}\x00"

    text = re.sub(r"`([^`]+)`", _stash_inline_code, text)

    # Step 3: HTML-escape the remaining text
    text = escape(text)

    # Step 4: inline formatting (order matters — bold+italic before each)
    # Bold+italic ***text*** or ___text___
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    # Bold **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic *text* or _text_ (but not inside words for underscore)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    # Strikethrough ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Step 5: links [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )

    # Step 6: strip markdown headings (### Title → Title)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Step 6.5: blockquotes — group consecutive `&gt; `-prefixed lines.
    # Done post-escape so the wrapper emits real HTML (not escaped) and so
    # `>` inside fenced code (stashed in Step 1) is left alone.
    def _wrap_blockquote(m: re.Match) -> str:
        block = m.group(0).rstrip("\n")
        inner = re.sub(r"^&gt;[ \t]?", "", block, flags=re.MULTILINE)
        return f"<blockquote>{inner}</blockquote>\n"

    text = re.sub(
        r"(?:^&gt;[ \t]?[^\n]*(?:\n|$))+",
        _wrap_blockquote,
        text,
        flags=re.MULTILINE,
    )

    # Step 7: restore stashed code blocks and inline codes
    for idx, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{idx}\x00", code)

    return text
