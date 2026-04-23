"""Pydantic models shared across modules."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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


class ControlAction(BaseModel):
    """Structured output the CC subprocess returns at the end of every turn."""

    action: Literal["stop", "sleep", "heartbeat"]
    reason: str = Field(description="Required justification.")
    sleep_ms: int | None = None
