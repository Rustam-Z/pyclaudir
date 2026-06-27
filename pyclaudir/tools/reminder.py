"""Reminder tools — set, list, and cancel scheduled reminders."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..db.reminders import (
    NewReminder,
    cancel_reminder,
    fetch_reminder_by_id,
    insert_reminder,
    list_pending_reminders,
)
from .base import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# reminder_set
# ---------------------------------------------------------------------------


def _parse_trigger_at(trigger_at: str) -> datetime | ToolResult:
    """Parse ``trigger_at`` to a future UTC datetime, or return an error result.

    Validates that the value is ISO-8601 parseable, carries a timezone offset,
    and lies in the future. On success returns the normalized UTC datetime.
    """
    try:
        trigger_dt = datetime.fromisoformat(trigger_at.replace("Z", "+00:00"))
    except ValueError:
        return ToolResult(
            content=f"invalid trigger_at format: {trigger_at!r}",
            is_error=True,
        )

    if trigger_dt.tzinfo is None:
        return ToolResult(
            content="trigger_at must include a timezone offset (use UTC, e.g. '...Z')",
            is_error=True,
        )

    trigger_dt = trigger_dt.astimezone(timezone.utc)
    if trigger_dt <= datetime.now(timezone.utc):
        return ToolResult(content="trigger_at must be in the future", is_error=True)
    return trigger_dt


def _validate_cron_expr(cron_expr: str | None) -> ToolResult | None:
    """Validate an optional cron expression; return an error result or ``None``.

    ``None`` means the expression is absent or valid. A ``ToolResult`` is
    returned when the expression is malformed or croniter is unavailable.
    """
    if cron_expr is None:
        return None
    try:
        from croniter import croniter
    except ImportError:
        return ToolResult(
            content="croniter is not installed — recurring reminders unavailable",
            is_error=True,
        )
    if not croniter.is_valid(cron_expr):
        return ToolResult(
            content=f"invalid cron expression: {cron_expr!r}",
            is_error=True,
        )
    return None


class SetReminderArgs(BaseModel):
    chat_id: int = Field(
        description="Numeric Telegram chat id where the reminder should fire."
    )
    user_id: int = Field(
        description="Numeric Telegram user id who requested the reminder."
    )
    text: str = Field(description="The reminder message text.")
    trigger_at: str = Field(
        description=(
            "When to fire, as a UTC ISO-8601 datetime string with offset "
            "(e.g. '2026-04-15T14:30:00Z'). Must be in the FUTURE. Convert the "
            "user's local time to UTC before passing this value."
        ),
    )
    cron_expr: str | None = Field(
        default=None,
        description=(
            "Optional 5-field cron expression (evaluated in UTC) for recurring "
            "reminders, e.g. '0 9 * * 1-5' for weekdays at 09:00 UTC. Leave "
            "null for one-shot reminders."
        ),
    )


class SetReminderTool(BaseTool[SetReminderArgs]):
    name = "reminder_set"
    description = (
        "Schedule a reminder to fire later — one-shot (trigger_at only) or "
        "recurring (also set cron_expr). trigger_at is UTC ISO-8601 and must "
        "be in the future; ask the user's timezone if unknown and convert to "
        "UTC first. Do NOT use for something to do right now — just do it. "
        "Manage existing reminders with reminder_list and reminder_cancel."
    )
    args_model = SetReminderArgs

    async def run(self, args: SetReminderArgs) -> ToolResult:
        if self.ctx.database is None:
            return ToolResult(content="database unavailable", is_error=True)

        # Validate trigger_at is parseable and in the future. Normalize
        # to UTC; the rest of the system stores trigger_at as the naive
        # ``"%Y-%m-%d %H:%M:%S"`` UTC string used by the auto-seed and
        # cron-advance paths, so the SQL string-comparison in
        # ``fetch_due_reminders`` works correctly across all sources.
        trigger_dt = _parse_trigger_at(args.trigger_at)
        if isinstance(trigger_dt, ToolResult):
            return trigger_dt
        trigger_at_canonical = trigger_dt.strftime("%Y-%m-%d %H:%M:%S")

        cron_error = _validate_cron_expr(args.cron_expr)
        if cron_error is not None:
            return cron_error

        reminder_id = await insert_reminder(
            self.ctx.database,
            NewReminder(
                chat_id=args.chat_id,
                user_id=args.user_id,
                text=args.text,
                trigger_at=trigger_at_canonical,
                cron_expr=args.cron_expr,
            ),
        )

        kind = "recurring" if args.cron_expr else "one-shot"
        return ToolResult(
            content=f"reminder #{reminder_id} ({kind}) set for {args.trigger_at}",
            data={"reminder_id": reminder_id},
        )


# ---------------------------------------------------------------------------
# reminder_list
# ---------------------------------------------------------------------------


class ListRemindersArgs(BaseModel):
    chat_id: int = Field(description="Numeric Telegram chat id to list reminders for.")


class ListRemindersTool(BaseTool[ListRemindersArgs]):
    name = "reminder_list"
    description = (
        "List all pending reminders for a chat, ordered by trigger time. Use "
        "to find a reminder's id before calling reminder_cancel."
    )
    args_model = ListRemindersArgs

    async def run(self, args: ListRemindersArgs) -> ToolResult:
        if self.ctx.database is None:
            return ToolResult(content="database unavailable", is_error=True)

        rows = await list_pending_reminders(self.ctx.database, args.chat_id)
        if not rows:
            return ToolResult(content="no pending reminders")

        lines = ["id\ttrigger_at\tcron\ttext"]
        for r in rows:
            cron = r["cron_expr"] or "-"
            lines.append(f"{r['id']}\t{r['trigger_at']}\t{cron}\t{r['text']}")
        return ToolResult(
            content="\n".join(lines),
            data={"count": len(rows)},
        )


# ---------------------------------------------------------------------------
# reminder_cancel
# ---------------------------------------------------------------------------


class CancelReminderArgs(BaseModel):
    reminder_id: int = Field(
        description=("Numeric id of the reminder to cancel, as shown by reminder_list.")
    )


class CancelReminderTool(BaseTool[CancelReminderArgs]):
    name = "reminder_cancel"
    description = (
        "Cancel a pending reminder by id (get ids from reminder_list). "
        "Auto-seeded mandatory reminders (e.g. the self-reflection loop) "
        "cannot be cancelled through this tool — attempts are refused and the "
        "reminder continues to fire on schedule."
    )
    args_model = CancelReminderArgs

    async def run(self, args: CancelReminderArgs) -> ToolResult:
        if self.ctx.database is None:
            return ToolResult(content="database unavailable", is_error=True)

        # Hard-gate: auto-seeded reminders represent mandatory, operator-
        # installed loops (currently: self-reflection). They are not
        # cancellable via the agent tool surface. Even if the bot is
        # prompt-injected into trying, the tool refuses.
        reminder = await fetch_reminder_by_id(self.ctx.database, args.reminder_id)
        if reminder is not None and reminder.get("auto_seed_key"):
            return ToolResult(
                content=(
                    f"reminder #{args.reminder_id} is an auto-seeded mandatory "
                    f"loop ({reminder['auto_seed_key']}) and cannot be cancelled"
                ),
                is_error=True,
            )

        ok = await cancel_reminder(self.ctx.database, args.reminder_id)
        if ok:
            return ToolResult(content=f"reminder #{args.reminder_id} cancelled")
        return ToolResult(
            content=f"reminder #{args.reminder_id} not found or already sent/cancelled",
            is_error=True,
        )
