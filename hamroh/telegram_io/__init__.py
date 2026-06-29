"""Telegram dispatcher package.

Re-exports :class:`TelegramDispatcher` so callers can keep writing
``from hamroh.telegram_io import TelegramDispatcher`` after the
module was split into ``attachments`` + ``dispatcher`` submodules.
"""

from __future__ import annotations

from .dispatcher import DispatcherDeps, EnginePort, TelegramDispatcher

__all__ = ["DispatcherDeps", "EnginePort", "TelegramDispatcher"]
