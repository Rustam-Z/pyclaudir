"""Base interfaces for pyclaudir MCP tools.

A tool is a subclass of :class:`BaseTool` that:

- declares ``name``, ``description``, ``args_model`` (a Pydantic model);
- implements ``async def run(self, args)`` returning a :class:`ToolResult`.

Tools receive a :class:`ToolContext` in their constructor that exposes the
shared Telegram bot, database, memory store, rate limiter, and heartbeat.
None of those services need to exist for tools that don't use them — the
context is a passive container.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..db.database import Database
    from ..instructions_store import InstructionsStore
    from ..memory_store import MemoryStore


class Heartbeat:
    """Liveness atomic the MCP server bumps on every tool invocation.

    The CC worker reads ``last_activity`` to decide whether the subprocess is
    actually wedged or just busy inside a long MCP call (see Claudir Part 3).
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        import time

        self._last = time.monotonic()

    def beat(self) -> None:
        import time

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
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    #: Telegram user id of the bot owner. Used by instruction-edit tools
    #: to verify the triggering inbound came from the operator.
    owner_id: int | None = None
    #: Updated by the dispatcher on every allowed inbound message. The
    #: instruction-edit tools gate on these two fields. ``None`` at
    #: startup until the first message arrives.
    last_inbound_user_id: int | None = None
    last_inbound_chat_type: str | None = None
    #: chat_id → display name. Populated by the dispatcher on every inbound
    #: message so outbound transcript lines can show the chat's title.
    chat_titles: dict[int, str] = field(default_factory=dict)
    #: Sync callback the ``send_message`` tool fires the moment Telegram
    #: confirms delivery. The engine wires it to drop the chat from the
    #: typing-indicator set so "typing..." vanishes as soon as the user has
    #: the message in their hand — not when the entire CC turn officially
    #: ends, which can be 5-10 seconds later.
    on_chat_replied: Any = None  # Callable[[int], None] | None — kept untyped to avoid an import


@dataclass
class ToolResult:
    """Uniform return type for ``BaseTool.run``.

    ``content`` is the human/model-readable string the LLM sees. ``data`` is
    optional structured payload for tools whose callers might want it (we
    don't use it yet, but it lets future tools return rich data without
    breaking the interface).
    """

    content: str
    data: dict[str, Any] | None = None
    is_error: bool = False


class BaseTool(ABC):
    """Subclass me, drop the file in ``pyclaudir/tools/``, and you're done."""

    #: MCP tool name. The MCP server prefixes this with ``mcp__pyclaudir__``
    #: when Claude Code sees it, but inside our codebase we use the bare name.
    name: ClassVar[str]

    #: Short human-facing description, surfaced in the MCP tool list.
    description: ClassVar[str]

    #: Pydantic model describing the call arguments.
    args_model: ClassVar[type[BaseModel]]

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    @abstractmethod
    async def run(self, args: BaseModel) -> ToolResult:  # pragma: no cover - abstract
        ...
