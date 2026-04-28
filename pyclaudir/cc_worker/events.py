"""Per-turn event types produced by the CC worker.

:class:`TurnResult` is the structured handoff between worker and engine:
the worker assembles one as it parses stdout events from Claude Code,
then enqueues it on ``_result_queue`` for the engine's control loop.
:class:`CrashLoop` signals that the supervisor has exhausted its
crash-recovery budget — the OS-level supervisor (systemd, docker
restart-policy) is expected to restart the whole process.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ControlAction


@dataclass
class TurnResult:
    """One full conversational turn from the CC subprocess."""

    text_blocks: list[str] = field(default_factory=list)
    control: ControlAction | None = None
    #: Stderr lines captured during the turn (most recent last).
    stderr_tail: list[str] = field(default_factory=list)
    #: True iff CC produced text without ever calling ``send_message``.
    dropped_text: bool = False
    #: Non-None when pyclaudir short-circuited this turn (e.g.
    #: ``"tool-error-limit"``). Engine branches on this before treating
    #: the result as a normal turn completion.
    aborted_reason: str | None = None


class CrashLoop(RuntimeError):
    """Raised when the CC subprocess crashes too often to recover."""
