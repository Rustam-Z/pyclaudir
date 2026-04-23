"""Unit tests for the send_message chunker."""

from __future__ import annotations

from pyclaudir.tools.send_message import _chunk_text


def test_short_text_returns_single_chunk():
    assert _chunk_text("hello", limit=4096) == ["hello"]


def test_empty_text_returns_single_empty_chunk():
    assert _chunk_text("", limit=4096) == [""]


def test_text_exactly_at_limit_is_not_split():
    text = "a" * 100
    assert _chunk_text(text, limit=100) == [text]


def test_paragraph_split_preferred_over_line():
    # \n\n must fully fit inside the window for rfind("\n\n") to find it.
    text = "A\n\nB\nCCCCCCCCCCCCCCC"  # 20 chars, \n\n at indices 1-2
    chunks = _chunk_text(text, limit=15)
    # Window first 15 chars is "A\n\nB\nCCCCCCCCCC" — contains both \n\n and \n.
    # Algorithm must prefer \n\n.
    assert chunks[0] == "A"
    assert chunks[1].startswith("B")


def test_line_split_when_no_paragraph():
    text = "line one\nline two\nline three"  # 28 chars, no \n\n
    chunks = _chunk_text(text, limit=15)
    assert all(len(c) <= 15 for c in chunks)
    assert chunks == ["line one", "line two", "line three"]


def test_space_split_when_no_newlines():
    text = ("word " * 6).strip()  # "word word word word word word" — 29 chars
    chunks = _chunk_text(text, limit=10)
    assert all(len(c) <= 10 for c in chunks)
    # Each chunk should contain whole words only.
    for c in chunks:
        for w in c.split():
            assert w == "word"


def test_hard_cut_when_no_whitespace():
    text = "a" * 250
    chunks = _chunk_text(text, limit=100)
    assert len(chunks) == 3
    assert chunks[0] == "a" * 100
    assert chunks[1] == "a" * 100
    assert chunks[2] == "a" * 50


def test_separator_consumed_not_duplicated():
    text = "aaa\n\nbbb"
    chunks = _chunk_text(text, limit=5)
    # "aaa" then "bbb" — separator is consumed, not leading the next chunk.
    assert chunks[0] == "aaa"
    assert chunks[1] == "bbb"


def test_non_space_content_preserved():
    # Boundary spaces/newlines are consumed, but non-whitespace content
    # must be preserved verbatim.
    text = ("paragraph " * 500).strip()
    chunks = _chunk_text(text, limit=4096)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_realistic_long_reply():
    text = (
        "Here's the answer you asked for.\n\n"
        + ("Paragraph content. " * 200)
        + "\n\nAnd a final paragraph."
    )
    chunks = _chunk_text(text, limit=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert all(c for c in chunks)  # no empty chunks


def test_chunk_does_not_lead_with_separator():
    # If the split point is a separator, the separator goes with the previous
    # chunk or is consumed — it must never lead the next chunk.
    # Construct text just over the limit with a \n\n near the boundary so the
    # second \n straddles the window edge.
    text = "a" * 4095 + "\n\nmore content here"
    chunks = _chunk_text(text, limit=4096)
    assert len(chunks) == 2
    assert not chunks[1].startswith("\n")
    assert chunks[1] == "more content here"


def test_default_limit_is_telegram_max():
    # A 4096-char text should not be split at the default limit.
    text = "a" * 4096
    assert _chunk_text(text) == [text]
    # One more character, and it should split.
    text_longer = "a" * 4097
    chunks = _chunk_text(text_longer)
    assert len(chunks) == 2
