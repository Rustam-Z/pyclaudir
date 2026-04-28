"""Telegram dispatcher package.

Re-exports :class:`TelegramDispatcher` so callers can keep writing
``from pyclaudir.telegram_io import TelegramDispatcher`` after the
module was split into ``attachments`` + ``dispatcher`` submodules.
"""

from __future__ import annotations

from .dispatcher import EnginePort, TelegramDispatcher

__all__ = ["EnginePort", "TelegramDispatcher"]
