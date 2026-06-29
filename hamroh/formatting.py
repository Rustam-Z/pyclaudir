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
    (headings, lists, images, tables, horizontal rules) are simplified to
    plain text equivalents.
    """
    # Step 1+2: stash code blocks/spans so their inner content isn't processed
    text, code_blocks = _stash_code_blocks(text)
    text, inline_codes = _stash_inline_codes(text)

    # Step 2.5: strip/convert markdown constructs Telegram can't render
    text = _sanitize_unsupported_markdown(text)

    # Step 3: HTML-escape the remaining text
    text = escape(text)

    # Steps 4-6.5: inline formatting, links, headings, blockquotes
    text = _apply_inline_formatting(text)
    text = _apply_links_and_headings(text)
    text = _wrap_blockquotes(text)

    # Step 7: restore stashed code blocks and inline codes
    return _restore_stashed(text, code_blocks, inline_codes)


def _stash_code_blocks(text: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks with placeholders, returning the HTML.

    The returned list holds the rendered ``<pre>``/``<code>`` HTML, indexed
    by the placeholder embedded in the text.
    """
    code_blocks: list[str] = []

    def _stash(m: re.Match) -> str:
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

    text = re.sub(r"```(\w+)?\n?(.*?)```", _stash, text, flags=re.DOTALL)
    return text, code_blocks


def _stash_inline_codes(text: str) -> tuple[str, list[str]]:
    """Replace inline code spans with placeholders, returning the HTML."""
    inline_codes: list[str] = []

    def _stash(m: re.Match) -> str:
        idx = len(inline_codes)
        inline_codes.append(f"<code>{escape(m.group(1))}</code>")
        return f"\x00INLINECODE{idx}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    return text, inline_codes


def _apply_inline_formatting(text: str) -> str:
    """Convert bold, italic, and strikethrough markdown to HTML tags.

    Order matters: bold+italic is handled before bold, which is handled
    before italic, so the greedier markers win.
    """
    # Bold+italic ***text*** or ___text___
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    # Bold **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic *text* or _text_ (but not inside words for underscore)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    # Strikethrough ~~text~~
    return re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)


def _apply_links_and_headings(text: str) -> str:
    """Convert ``[text](url)`` links to anchors and strip heading markers."""
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )
    return re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)


def _wrap_blockquotes(text: str) -> str:
    """Wrap consecutive ``&gt; ``-prefixed lines in ``<blockquote>`` tags.

    Done post-escape so the wrapper emits real HTML (not escaped) and so
    ``>`` inside fenced code (already stashed) is left alone.
    """

    def _wrap(m: re.Match) -> str:
        block = m.group(0).rstrip("\n")
        inner = re.sub(r"^&gt;[ \t]?", "", block, flags=re.MULTILINE)
        return f"<blockquote>{inner}</blockquote>\n"

    return re.sub(
        r"(?:^&gt;[ \t]?[^\n]*(?:\n|$))+",
        _wrap,
        text,
        flags=re.MULTILINE,
    )


def _restore_stashed(text: str, code_blocks: list[str], inline_codes: list[str]) -> str:
    """Replace code-block/inline-code placeholders with their stashed HTML."""
    for idx, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{idx}\x00", code)
    return text


_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|={3,}|\*{3,})[ \t]*$", re.MULTILINE)
_LIST_RE = re.compile(r"^([ \t]*)[-*+] ", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_SEP_CELL_RE = re.compile(r"[ \t]*:?-+:?[ \t]*")


def _sanitize_unsupported_markdown(text: str) -> str:
    """Convert constructs Telegram's HTML parser cannot render.

    Tables become `•` bullet rows, horizontal rules become blank lines,
    `-`/`*`/`+` list markers become `•`, images become plain links.
    Runs on raw text after code spans are stashed.
    """
    text = _convert_tables_to_bullets(text)
    text = _HR_RE.sub("", text)
    text = _LIST_RE.sub(r"\1• ", text)
    text = _IMAGE_RE.sub(r"[\1](\2)", text)
    return text


def _convert_tables_to_bullets(text: str) -> str:
    """Replace pipe-tables (`| a | b |` + separator row) with `•` rows."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (
            i + 1 < len(lines)
            and _is_table_row(lines[i])
            and _is_separator_row(lines[i + 1])
        ):
            j = i
            while j < len(lines) and _is_table_row(lines[j]):
                if not _is_separator_row(lines[j]):
                    out.append(_row_to_bullet(lines[j]))
                j += 1
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return len(s) >= 2 and s.startswith("|") and s.endswith("|")


def _is_separator_row(line: str) -> bool:
    s = line.strip()
    if not (len(s) >= 2 and s.startswith("|") and s.endswith("|")):
        return False
    return all(_SEP_CELL_RE.fullmatch(c) for c in s[1:-1].split("|"))


def _row_to_bullet(line: str) -> str:
    cells = [c.strip() for c in line.strip()[1:-1].split("|")]
    cells = [c for c in cells if c]
    return "• " + " — ".join(cells)


#: Telegram's hard limit on a single text message.
TELEGRAM_TEXT_LIMIT = 4096


def chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Prefers ``\\n\\n`` (paragraph) over ``\\n`` (line) over space over hard-cut.
    Separators at a chosen boundary are consumed, never duplicated onto the
    next chunk. Empty input returns ``[""]`` so callers can treat the result
    as a non-empty list.

    Runs on raw (pre-markdown) text so each chunk can be converted to
    Telegram HTML independently without splitting an inline tag in half.
    Markdown constructs rarely span paragraph boundaries, so paragraph-
    preferring splits keep the rendered output intact.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > 0:
                chunks.append(remaining[:idx])
                next_start = idx + len(sep)
                # A ``\n\n`` straddling the window edge is only partially
                # visible to ``rfind("\n\n")`` — we split on the single
                # ``\n`` we saw, then consume any directly-adjacent ``\n``
                # so the next chunk doesn't lead with a separator.
                while next_start < len(remaining) and remaining[next_start] == "\n":
                    next_start += 1
                remaining = remaining[next_start:]
                break
        else:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]

    if remaining:
        chunks.append(remaining)
    return chunks
