"""Engine package — debounce, batch, format, control loop.

Re-exports the public API so existing imports
(``from hamroh.engine import Engine`` etc.) keep working after the
module was split into ``format`` + ``engine`` submodules.
"""

from __future__ import annotations

from .engine import Engine, EngineOptions, ErrorNotify
from .typing_indicator import (
    MIN_TYPING_VISIBLE_SECONDS,
    TYPING_REFRESH_SECONDS,
    TypingAction,
)
from .format import (
    DEFAULT_REPLY_DEPTH,
    format_messages_as_xml,
    format_messages_with_context,
)

__all__ = [
    "DEFAULT_REPLY_DEPTH",
    "Engine",
    "EngineOptions",
    "ErrorNotify",
    "MIN_TYPING_VISIBLE_SECONDS",
    "TYPING_REFRESH_SECONDS",
    "TypingAction",
    "format_messages_as_xml",
    "format_messages_with_context",
]
