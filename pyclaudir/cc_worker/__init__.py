"""Claude Code subprocess worker — package entry point.

Re-exports the public API so callers can keep writing
``from pyclaudir.cc_worker import CcWorker`` (etc.) after the module was
split into ``spec`` / ``events`` / ``worker`` submodules.
"""

from __future__ import annotations

from .events import CrashLoop, TurnResult
from .spec import (
    BASE_ALLOWED_TOOLS,
    BASH_TOOLS,
    CODE_TOOLS,
    DEFAULT_DISALLOWED_TOOLS,
    FORBIDDEN_FLAG,
    CcSpawnSpec,
    build_argv,
)
from .worker import CcWorker

__all__ = [
    "BASE_ALLOWED_TOOLS",
    "BASH_TOOLS",
    "CODE_TOOLS",
    "DEFAULT_DISALLOWED_TOOLS",
    "FORBIDDEN_FLAG",
    "CcSpawnSpec",
    "CcWorker",
    "CrashLoop",
    "TurnResult",
    "build_argv",
]
