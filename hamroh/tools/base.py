"""Base interfaces for hamroh MCP tools.

A tool is a subclass of :class:`BaseTool` that:

- declares ``name``, ``description``, ``args_model`` (a Pydantic model);
- implements ``async def run(self, args)`` returning a :class:`ToolResult`.

Tools receive a :class:`ToolContext` in their constructor that exposes the
shared Telegram bot, database, memory store, rate limiter, and heartbeat.
None of those services need to exist for tools that don't use them — the
context is a passive container.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from ..db.messages import insert_message
from ..models import ChatMessage
from ..transcript import ChatRef, MsgRef, log_outbound

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..storage.attachments import AttachmentStore
    from ..db.database import Database
    from ..instructions_store import InstructionsStore
    from ..storage.memory import MemoryStore
    from ..storage.render import RenderStore
    from ..skills_store import SkillsStore
    from .browser import BrowserManager, BrowserSession


class Heartbeat:
    """Liveness atomic the MCP server bumps on every tool invocation.

    The CC worker reads ``last_activity`` to decide whether the subprocess is
    actually wedged or just busy inside a long MCP call (see Claudir Part 3).
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    @property
    def last_activity(self) -> float:
        return self._last


@dataclass
class ToolContext:
    """Container of shared services available to every tool."""

    bot: Any = None  # telegram.Bot — left untyped to keep this module import-light
    database: "Database | None" = None
    memory_store: "MemoryStore | None" = None
    instructions_store: "InstructionsStore | None" = None
    skills_store: "SkillsStore | None" = None
    attachment_store: "AttachmentStore | None" = None
    render_store: "RenderStore | None" = None
    #: Process-wide warm Chromium shared by render_html/render_latex so the
    #: browser isn't relaunched per call. None in tests (falls back to a
    #: throwaway browser inside the renderer).
    browser_manager: "BrowserManager | None" = None
    #: Long-lived browser context+page shared by the ``browser_*`` tools so a
    #: navigated page survives across separate tool calls. None in tests.
    browser_session: "BrowserSession | None" = None
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    #: chat_id → display name. Populated by the dispatcher on every inbound
    #: message so outbound transcript lines can show the chat's title.
    chat_titles: dict[int, str] = field(default_factory=dict)
    #: Sync callback the ``telegram_send_message`` tool fires the moment Telegram
    #: confirms delivery. The engine wires it to drop the chat from the
    #: typing-indicator set so "typing..." vanishes as soon as the user has
    #: the message in their hand — not when the entire CC turn officially
    #: ends, which can be 5-10 seconds later.
    on_chat_replied: Any = (
        None  # Callable[[int], None] | None — kept untyped to avoid an import
    )


@dataclass
class ToolResult:
    """Uniform return type for ``BaseTool.run``.

    ``content`` is the human/model-readable string the LLM sees. ``data`` is
    optional structured payload for tools whose callers might want it (we
    don't use it yet, but it lets future tools return rich data without
    breaking the interface).

    ``image_path``, when set, signals the MCP wrapper to deliver the file
    at that absolute path as an MCP image content block (so Claude actually
    *sees* it) instead of returning ``content`` as text. Used by
    ``telegram_read_attachment`` to surface inbound photos.
    """

    content: str
    data: dict[str, Any] | None = None
    is_error: bool = False
    image_path: Any = (
        None  # pathlib.Path | None — left untyped to keep this module import-light
    )


#: The Pydantic args model a concrete tool's ``run`` accepts. Parametrising
#: ``BaseTool`` on it lets subclasses narrow ``run``'s argument type without
#: tripping Liskov — ``BaseTool[FooArgs].run`` accepts exactly ``FooArgs``.
ArgsT = TypeVar("ArgsT", bound=BaseModel)


class BaseTool(ABC, Generic[ArgsT]):
    """Subclass me, drop the file in ``hamroh/tools/``, and you're done."""

    #: MCP tool name. The MCP server prefixes this with ``mcp__hamroh__``
    #: when Claude Code sees it, but inside our codebase we use the bare name.
    name: ClassVar[str]

    #: Short human-facing description, surfaced in the MCP tool list.
    description: ClassVar[str]

    #: Pydantic model describing the call arguments.
    args_model: ClassVar[type[BaseModel]]

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    @abstractmethod
    async def run(self, args: ArgsT) -> ToolResult:  # pragma: no cover - abstract
        ...


def notify_chat_replied(ctx: ToolContext, chat_id: int) -> None:
    """Fire the typing-indicator stop hook; never let it break delivery."""
    if ctx.on_chat_replied is None:
        return
    try:
        ctx.on_chat_replied(chat_id)
    except Exception:  # pragma: no cover
        pass


@dataclass
class OutboundDelivery:
    """One delivered Telegram message, ready for post-send bookkeeping."""

    chat_id: int
    message_id: int
    reply_to_id: int | None
    transcript_text: str
    #: Text persisted to the DB when it differs from the transcript line
    #: (``telegram_create_poll`` stores the options too). Defaults to transcript.
    db_text: str | None = None


@dataclass(frozen=True)
class OutboundRecord:
    """One delivered message to persist; the bot's identity is filled in by
    :func:`record_outbound`."""

    chat_id: int
    message_id: int
    text: str
    reply_to_id: int | None = None


async def deliver_bookkeeping(ctx: ToolContext, sent: OutboundDelivery) -> None:
    """Post-send tail shared by the outbound tools: stop the typing
    indicator, write the transcript line, persist the message row."""
    notify_chat_replied(ctx, sent.chat_id)
    log_outbound(
        ChatRef(sent.chat_id, ctx.chat_titles),
        MsgRef(sent.message_id, sent.transcript_text, sent.reply_to_id),
    )
    db_text = sent.db_text if sent.db_text is not None else sent.transcript_text
    await record_outbound(
        ctx, OutboundRecord(sent.chat_id, sent.message_id, db_text, sent.reply_to_id)
    )


async def bot_identity(bot: Any) -> tuple[int, str | None, str]:
    """The bot's ``(user_id, username, first_name)`` via ``get_me``.

    Falls back to ``(0, None, "bot")`` on failure so a transient
    ``get_me`` glitch never tanks the delivery being recorded.
    """
    try:
        me = await bot.get_me()
        return me.id, me.username, me.first_name
    except Exception:
        return 0, None, "bot"


async def record_outbound(ctx: ToolContext, record: OutboundRecord) -> None:
    """Persist one outbound message row with the bot's identity.

    Used by ``telegram_send_message``, ``telegram_send_photo``, ``telegram_send_memory_document``,
    and ``telegram_create_poll`` after the Telegram API confirms delivery. No-ops
    when the database or bot is unavailable (tests).
    """
    if ctx.database is None or ctx.bot is None:
        return
    bot_user_id, bot_username, bot_first_name = await bot_identity(ctx.bot)
    await insert_message(
        ctx.database,
        ChatMessage(
            chat_id=record.chat_id,
            message_id=record.message_id,
            user_id=bot_user_id,
            username=bot_username,
            first_name=bot_first_name,
            direction="out",
            timestamp=datetime.now(timezone.utc),
            text=record.text,
            reply_to_id=record.reply_to_id,
        ),
    )
