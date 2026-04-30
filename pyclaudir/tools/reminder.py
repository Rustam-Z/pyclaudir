"""Reminder tools — set, list, and cancel scheduled reminders."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..db.reminders import (
    cancel_reminder,
    fetch_reminder_by_id,
    insert_reminder,
    list_pending_reminders,
)
from .base import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# set_reminder
# ---------------------------------------------------------------------------

class SetReminderArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id where the reminder should fire.")
    user_id: int = Field(description="Telegram user id who requested the reminder.")
    text: str = Field(description="The reminder message text.")
    trigger_at: str = Field(
        description=(
            "When to fire, as a UTC ISO-8601 datetime string "
            "(e.g. '2026-04-15T14:30:00Z'). You MUST convert the user's "
            "local time to UTC before passing this value."
        ),
    )
    cron_expr: str | None = Field(
        default=None,
        description=(
            "Optional cron expression for recurring reminders "
            "(e.g. '0 9 * * 1-5' for weekdays at 09:00 UTC). "
            "Leave null for one-shot reminders."
        ),
    )


class SetReminderTool(BaseTool):
    name = "set_reminder"
    description = (
        "Schedule a reminder. Provide trigger_at in UTC. "
        "For recurring reminders, also provide a cron expression. "
        "Ask the user for their timezone if not already known, and convert "
        "to UTC before calling this tool."
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
        try:
            trigger_dt = datetime.fromisoformat(args.trigger_at.replace("Z", "+00:00"))
        except ValueError:
            return ToolResult(
                content=f"invalid trigger_at format: {args.trigger_at!r}",
                is_error=True,
            )

        if trigger_dt.tzinfo is None:
            return ToolResult(
                content="trigger_at must include a timezone offset (use UTC, e.g. '...Z')",
                is_error=True,
            )
        trigger_dt = trigger_dt.astimezone(timezone.utc)

        now = datetime.now(timezone.utc)
        if trigger_dt <= now:
            return ToolResult(
                content="trigger_at must be in the future",
                is_error=True,
            )

        trigger_at_canonical = trigger_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Validate cron expression if provided.
        if args.cron_expr is not None:
            try:
                from croniter import croniter

                if not croniter.is_valid(args.cron_expr):
                    return ToolResult(
                        content=f"invalid cron expression: {args.cron_expr!r}",
                        is_error=True,
                    )
            except ImportError:
                return ToolResult(
                    content="croniter is not installed — recurring reminders unavailable",
                    is_error=True,
                )

        reminder_id = await insert_reminder(
            self.ctx.database,
            chat_id=args.chat_id,
            user_id=args.user_id,
            text=args.text,
            trigger_at=trigger_at_canonical,
            cron_expr=args.cron_expr,
        )

        kind = "recurring" if args.cron_expr else "one-shot"
        return ToolResult(
            content=f"reminder #{reminder_id} ({kind}) set for {args.trigger_at}",
            data={"reminder_id": reminder_id},
        )


# ---------------------------------------------------------------------------
# list_reminders
# ---------------------------------------------------------------------------

class ListRemindersArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id to list reminders for.")


class ListRemindersTool(BaseTool):
    name = "list_reminders"
    description = "List all pending reminders for a chat, ordered by trigger time."
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
            lines.append(f'{r["id"]}\t{r["trigger_at"]}\t{cron}\t{r["text"]}')
        return ToolResult(
            content="\n".join(lines),
            data={"count": len(rows)},
        )


# ---------------------------------------------------------------------------
# cancel_reminder
# ---------------------------------------------------------------------------

class CancelReminderArgs(BaseModel):
    reminder_id: int = Field(description="The id of the reminder to cancel.")


class CancelReminderTool(BaseTool):
    name = "cancel_reminder"
    description = (
        "Cancel a pending reminder by id. Auto-seeded mandatory "
        "reminders (e.g. the self-reflection loop) cannot be cancelled "
        "through this tool — attempts are refused and the reminder "
        "continues to fire on schedule."
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
