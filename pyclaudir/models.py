"""Pydantic models shared across modules."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ChatMessage(BaseModel):
    """A Telegram message normalized for the engine.

    Both inbound and outbound messages flow through this type so the engine,
    debouncer, and persistence layer all speak the same shape.
    """

    chat_id: int
    message_id: int
    user_id: int
    username: str | None = None
    first_name: str | None = None
    direction: Literal["in", "out"]
    timestamp: datetime
    text: str
    reply_to_id: int | None = None
    reply_to_text: str | None = None
    raw_update_json: str | None = None
    #: ``time.monotonic()`` at the moment the dispatcher first saw this
    #: message. Used only for hot-path latency logging (``hot-path stage=...``);
    #: not persisted. ``None`` for synthetic messages (reminders, etc.) that
    #: don't originate from a Telegram update.
    received_at_monotonic: float | None = Field(default=None, exclude=True)
    #: Names of input-normalization transforms that fired on this message
    #: (e.g. ``"zero_width_stripped"``, ``"bidi_stripped"``,
    #: ``"nfkc_changed"`` from :mod:`pyclaudir.input_normalizer`). Surfaced
    #: to the model via the ``flags=`` attribute on the rendered ``<msg>``
    #: envelope so it can refuse obfuscated requests on-character. Not
    #: persisted — lives only in-memory between dispatcher and engine.
    input_flags: frozenset[str] = Field(default_factory=frozenset, exclude=True)


class ControlAction(BaseModel):
    """Structured output the CC subprocess returns at the end of every turn.

    ``reason`` is required only when ``action == "stop"`` — a forcing
    function so the model doesn't drop conversations reflexively. For
    ``sleep`` / ``heartbeat`` (provisional, non-terminal) it's optional.
    """

    action: Literal["stop", "sleep", "heartbeat"]
    reason: str | None = Field(
        default=None,
        description="Terse justification (≤10 words). Required on stop.",
    )
    sleep_ms: int | None = None

    @model_validator(mode="after")
    def _reason_required_on_stop(self) -> "ControlAction":
        if self.action == "stop" and not (self.reason and self.reason.strip()):
            raise ValueError("reason is required (non-empty) when action == 'stop'")
        return self
