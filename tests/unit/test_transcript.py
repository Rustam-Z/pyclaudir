"""Transcript logger formatting and chat-title cache."""

from __future__ import annotations

import logging

import pytest

from hamroh.transcript import (
    ChatRef,
    MsgRef,
    UserRef,
    log_delete,
    log_edit,
    log_inbound,
    log_inbound_edit,
    log_outbound,
    log_reaction,
)


@pytest.fixture()
def caplog_tx(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="hamroh.tx")
    return caplog


def test_inbound_dm_format(caplog_tx) -> None:
    log_inbound(
        ChatRef(12345, {12345: "Alice"}, "private"),
        UserRef(12345, "Alice"),
        MsgRef(42, "hi"),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[RX]" in line
    assert "DM" in line
    assert "Alice" in line
    assert "12345" in line
    assert "m42" in line
    assert "| hi" in line


def test_inbound_group_format(caplog_tx) -> None:
    log_inbound(
        ChatRef(-1001234567890, {-1001234567890: "Team Chat"}, "supergroup"),
        UserRef(42, "Alice"),
        MsgRef(10, "hello team", reply_to_id=5),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[RX]" in line
    assert 'G "Team Chat"' in line
    assert "Alice[42]" in line
    assert "m10" in line
    assert "→m5" in line
    assert "| hello team" in line


def test_inbound_dropped_format(caplog_tx) -> None:
    log_inbound(
        ChatRef(999, {}, "private"),
        UserRef(999, "Stranger"),
        MsgRef(1, "leaked spam"),
        allowed=False,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[DROP]" in line
    assert "(chat not allowed)" in line
    assert "| leaked spam" in line


def test_outbound_uses_cached_title(caplog_tx) -> None:
    titles = {-1001234567890: "Team Chat"}
    log_outbound(
        ChatRef(-1001234567890, titles),
        MsgRef(99, "hello!", reply_to_id=10),
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[TX]" in line
    assert 'G "Team Chat"' in line
    assert "m99" in line
    assert "→m10" in line
    assert "| hello!" in line


def test_outbound_falls_back_to_chat_id_only(caplog_tx) -> None:
    log_outbound(
        ChatRef(-1009999999999, {}),
        MsgRef(1, "hi"),
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[TX]" in line
    assert "-1009999999999" in line
    assert "| hi" in line


def test_inbound_truncates_long_body(caplog_tx) -> None:
    body = "x" * 500
    log_inbound(
        ChatRef(1, {}, "private"),
        UserRef(1, None),
        MsgRef(1, body),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "…" in line
    assert len(line.split("|", 1)[1]) < 250


def test_inbound_flattens_newlines(caplog_tx) -> None:
    log_inbound(
        ChatRef(1, {}, "private"),
        UserRef(1, None),
        MsgRef(1, "line one\nline two\rline three"),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "\n" not in line.split("|", 1)[1]
    assert "line one line two line three" in line


def test_edit_and_delete_and_reaction(caplog_tx) -> None:
    titles = {-1: "G"}
    log_edit(chat_id=-1, chat_titles=titles, message_id=5, text="new body")
    log_delete(chat_id=-1, chat_titles=titles, message_id=5)
    log_reaction(chat_id=-1, chat_titles=titles, message_id=5, emoji="👍")
    log_inbound_edit(
        ChatRef(-1, titles),
        UserRef(42, "Alice"),
        MsgRef(5, "user fixed typo"),
    )
    msgs = [r.getMessage() for r in caplog_tx.records[-4:]]
    assert any(m.startswith("[EDIT]") for m in msgs)
    assert any(m.startswith("[DEL]") for m in msgs)
    assert any(m.startswith("[REACT]") and "👍" in m for m in msgs)
    assert any(m.startswith("[RX↺]") for m in msgs)


def test_tool_context_chat_titles_default_is_independent_dict() -> None:
    """Two ToolContext instances must not share the same dict."""
    from hamroh.tools.base import ToolContext

    a = ToolContext()
    b = ToolContext()
    a.chat_titles[1] = "x"
    assert 1 not in b.chat_titles
