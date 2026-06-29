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
    #: True when any user-visible tool was called this turn — a delivered
    #: message (``telegram_send_message`` & co.) OR a reaction/edit/delete/poll-close.
    #: I.e. the user perceived a response from the bot.
    user_visible_action: bool = False
    #: True iff CC produced text but took no user-visible action — the
    #: text never reached the user.
    dropped_text: bool = False
    #: Non-None when hamroh short-circuited this turn (e.g.
    #: ``"tool-error-limit"``). Engine branches on this before treating
    #: the result as a normal turn completion.
    aborted_reason: str | None = None
    #: Error text when the result event reported ``is_error`` — the turn
    #: failed at the API level (e.g. usage-policy refusal).
    api_error: str | None = None


class CrashLoop(RuntimeError):
    """Raised when the CC subprocess crashes too often to recover."""
