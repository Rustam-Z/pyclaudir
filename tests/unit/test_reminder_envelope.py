"""Synthetic reminders render as a top-level ``<reminder>`` envelope.

A reminder fires by being injected as a synthetic ``ChatMessage`` whose
``message_id`` is 0 and whose ``text`` is already a complete
``<reminder>…</reminder>`` block. The formatter must emit that block
verbatim. If it wrapped the reminder in a ``<msg>`` envelope and
HTML-escaped it, the model would receive ``&lt;reminder…&gt;`` inside a
``<msg>`` body — the exact encoded-tag shape ``system.md`` treats as a
prompt-injection attempt (#44).
"""

from __future__ import annotations

from datetime import datetime, timezone

from hamroh.engine import format_messages_as_xml
from hamroh.models import ChatMessage

_REMINDER_XML = (
    '<reminder id="7" chat_id="-100" user_id="-1">'
    '<skill name="self-reflection">run</skill>'
    "</reminder>"
)


def _reminder() -> ChatMessage:
    """A reminder exactly as ``_fire_one_reminder`` injects it."""
    return ChatMessage(
        chat_id=-100,
        message_id=0,
        user_id=-1,
        direction="in",
        timestamp=datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc),
        text=_REMINDER_XML,
    )


def _human(text: str) -> ChatMessage:
    return ChatMessage(
        chat_id=-100,
        message_id=12,
        user_id=42,
        first_name="Alice",
        direction="in",
        timestamp=datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc),
        text=text,
    )


def test_reminder_renders_as_raw_top_level_block() -> None:
    """given  a synthetic reminder (message_id == 0)
    when    it is formatted for the model
    then    its <reminder> XML appears verbatim, never wrapped or escaped.
    """
    xml = format_messages_as_xml([_reminder()])

    assert xml == _REMINDER_XML, "reminder must be emitted verbatim"
    assert "<msg" not in xml, "reminder must not be wrapped in a <msg> envelope"
    assert "&lt;reminder" not in xml, "reminder must not be HTML-escaped"


def test_human_message_still_wrapped_and_escaped() -> None:
    """given  an ordinary human message with angle brackets in its body
    when    it is formatted
    then    it is wrapped in <msg> and its body is HTML-escaped as before.
    """
    xml = format_messages_as_xml([_human("look at <b>this</b>")])

    assert xml.startswith('<msg id="12"'), "human message must keep its <msg> envelope"
    assert "&lt;b&gt;this&lt;/b&gt;" in xml, "human body must still be escaped"


def test_reminder_batched_with_human_message() -> None:
    """given  a reminder and a human message in one batch
    when    they are formatted together
    then    the reminder is a top-level block alongside the human's <msg>.
    """
    xml = format_messages_as_xml([_reminder(), _human("hi")])

    assert _REMINDER_XML in xml, "reminder block must survive batching verbatim"
    assert '<msg id="12"' in xml, "human message must still be wrapped"
    assert "&lt;reminder" not in xml, "reminder must never be escaped in a batch"
