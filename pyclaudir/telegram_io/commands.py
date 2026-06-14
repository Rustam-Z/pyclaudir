"""Owner-only Telegram commands for the dispatcher.

Relocated verbatim from ``dispatcher.py`` in the file-size split.
:class:`OwnerCommandsMixin` is a mixin (not a standalone object) because
the handlers read the dispatcher's ``config``, ``db``, ``engine``, and
``application`` attributes, all defined in ``TelegramDispatcher.__init__``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import ContextTypes

from ..access import load_access, save_access

# Pinned to the parent package name so log captures keyed on
# ``"pyclaudir.telegram_io"`` keep matching after the module split.
log = logging.getLogger("pyclaudir.telegram_io")


def _parse_allow_args(
    args: list[str] | None, *, verb: str
) -> tuple[str, int, None] | tuple[None, None, str]:
    """Parse ``/allow|/deny <user|group> <id>`` argv. Returns either
    ``(kind, target_id, None)`` on success or ``(None, None, error_msg)``."""
    usage = f"Usage: /{verb} <user|group> <id>"
    if not args or len(args) < 2:
        return None, None, usage
    kind = args[0].lower()
    if kind not in ("user", "group"):
        return None, None, usage
    try:
        target_id = int(args[1])
    except ValueError:
        return None, None, "ID must be a number."
    return kind, target_id, None


class OwnerCommandsMixin:
    """Owner-only command handlers mixed into ``TelegramDispatcher``."""

    def _is_owner(self, update: Update) -> bool:
        return (
            update.effective_user is not None
            and update.effective_user.id == self.config.owner_id
        )

    async def _cmd_kill(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        log.warning("/kill received from owner; shutting down")
        try:
            await update.effective_message.reply_text("Shutting down…")
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    async def _cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Owner-only: drop all inbound messages until /resume. In-memory only."""
        if not self._is_owner(update):
            return
        self._paused = True
        log.warning("/pause received from owner; dropping inbound messages")
        await update.effective_message.reply_text(
            "⏸ paused — messages dropped until /resume"
        )

    async def _cmd_resume(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Owner-only: re-enable message forwarding. Paused messages stay dropped."""
        if not self._is_owner(update):
            return
        self._paused = False
        log.warning("/resume received from owner; forwarding inbound messages")
        await update.effective_message.reply_text("▶ resumed")

    async def _cmd_reset_session(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Owner-only: drop the CC session and respawn with a fresh context.

        The escape hatch for unbounded context growth — the worker
        respawns Claude Code without ``--resume``, i.e. a fresh, empty
        context. The bot itself stays up; chat history (SQLite) and
        memories (markdown) survive.
        """
        if not self._is_owner(update):
            return
        log.warning("/reset_session received from owner; respawning cc with a fresh session")
        await self.engine.stash_restore_context("owner-reset")
        await self.engine.reset_session()
        try:
            await update.effective_message.reply_text(
                "Session cleared — Claude restarted with a fresh context. "
                "Chat history and memories are preserved; a short recap of "
                "recent messages will be carried into the next turn."
            )
        except Exception:
            pass

    async def _cmd_health(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Quick operational health readout — owner-only, DM or group.

        Surfaces things that matter day-to-day: when the CC subprocess
        last produced output, whether the self-reflection auto-seed
        reminder is active, recent rate-limit hits.
        """
        if not self._is_owner(update):
            return
        lines: list[str] = ["*pyclaudir health*"]
        status = "⏸ PAUSED (dropping messages)" if self._paused else "active"
        lines.append(f"- status: {status}")
        try:
            row = await self.db.fetch_one(
                "SELECT MAX(timestamp) AS last FROM messages WHERE direction='out'"
            )
            last_tx = row["last"] if row and row["last"] else "(none yet)"
            lines.append(f"- last bot send: `{last_tx}` UTC")
        except Exception as exc:
            lines.append(f"- last bot send: query error ({exc})")
        lines.extend(await self._health_reminder_lines())
        try:
            row = await self.db.fetch_one(
                "SELECT COUNT(*) AS c FROM rate_limits WHERE notice_sent = 1"
            )
            notices = int(row["c"]) if row else 0
            lines.append(f"- rate-limit notices fired (lifetime): {notices}")
        except Exception as exc:
            lines.append(f"- rate-limit notices: query error ({exc})")
        lines.extend(self._health_engine_lines())
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="Markdown"
        )

    async def _health_reminder_lines(self) -> list[str]:
        """Health section: state of the self-reflection auto-seed reminder."""
        try:
            row = await self.db.fetch_one(
                "SELECT status, cron_expr, trigger_at FROM reminders "
                "WHERE auto_seed_key = 'self-reflection-default' "
                "ORDER BY id DESC LIMIT 1"
            )
        except Exception as exc:
            return [f"- self-reflection reminder: query error ({exc})"]
        if row is None:
            return ["- self-reflection reminder: MISSING (will re-seed on restart)"]
        return [
            f"- self-reflection reminder: {row['status']} "
            f"(cron `{row['cron_expr']}`, next `{row['trigger_at']}` UTC)"
        ]

    def _health_engine_lines(self) -> list[str]:
        """Health section: current turn duration and queued-message count."""
        if self.engine is None:
            return []
        elapsed = self.engine.turn_elapsed_s
        turn = (
            f"- current turn: running for {elapsed:.0f}s"
            if elapsed is not None
            else "- current turn: idle"
        )
        return [turn, f"- queued messages: {self.engine.pending_count}"]

    async def _cmd_audit(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Recent changes / failures / backups — owner-only.

        Richer than /health; intended for occasional "what's been
        happening" review rather than live monitoring.
        """
        if not self._is_owner(update):
            return
        lines: list[str] = ["*pyclaudir audit*"]
        lines += await self._audit_tool_failures()
        lines += self._audit_prompt_backups()
        lines += self._audit_memory_footprint()
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="Markdown"
        )

    async def _audit_tool_failures(self) -> list[str]:
        """Audit section: the last 5 failed tool calls, newest first."""
        try:
            rows = await self.db.fetch_all(
                "SELECT tool_name, error, created_at FROM tool_calls "
                "WHERE error IS NOT NULL AND error != '' "
                "ORDER BY id DESC LIMIT 5"
            )
        except Exception as exc:
            return [f"*recent tool failures:* query error ({exc})"]
        if not rows:
            return ["*recent tool failures:* none"]
        lines = ["*recent tool failures:*"]
        for r in rows:
            err = (r["error"] or "")[:80]
            lines.append(f"  • `{r['created_at']}` {r['tool_name']} — {err}")
        return lines

    def _audit_prompt_backups(self) -> list[str]:
        """Audit section: how many prompt backup files exist."""
        try:
            backups_dir = self.config.data_dir / "prompt_backups"
            if not backups_dir.exists():
                return ["*prompt backups:* (none yet)"]
            files = [
                p for p in backups_dir.iterdir() if p.is_file() and p.suffix == ".md"
            ]
            return [f"*prompt backups:* {len(files)} file(s) in `{backups_dir}`"]
        except Exception as exc:
            return [f"*prompt backups:* error ({exc})"]

    def _audit_memory_footprint(self) -> list[str]:
        """Audit section: total bytes stored under the memories root."""
        try:
            mem_dir = self.config.memories_dir
            total_bytes = (
                sum(p.stat().st_size for p in mem_dir.rglob("*") if p.is_file())
                if mem_dir.exists()
                else 0
            )
            return [f"*memory footprint:* {total_bytes:,} bytes under `data/memories/`"]
        except Exception as exc:
            return [f"*memory footprint:* error ({exc})"]

    async def _cmd_usage(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Relay Claude Code's own ``/usage`` output verbatim — owner-only.

        Runs a short-lived headless ``claude --print /usage`` and forwards its
        text, so the bot shows exactly what Claude Code reports (subscription
        session and weekly rate limits with reset times).
        """
        if not self._is_owner(update):
            return
        output = await self._fetch_claude_usage()
        await update.effective_message.reply_text(output)

    async def _fetch_claude_usage(self) -> str:
        """Run ``claude --print /usage`` and return its text, or an error line."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.claude_code_bin,
                "--print",
                "/usage",
                "--output-format",
                "text",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (asyncio.TimeoutError, OSError) as exc:
            log.warning("/usage subprocess failed: %s", exc)
            return f"Could not read Claude Code usage: {exc}"
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()[:200]
            log.warning("/usage exited %s: %s", proc.returncode, err)
            return f"Could not read Claude Code usage (exit {proc.returncode})."
        return stdout.decode(errors="replace").strip() or "(no usage output)"

    # ------------------------------------------------------------------
    # Access management commands (owner-only)
    # ------------------------------------------------------------------

    async def _cmd_allow(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        kind, target_id, error = _parse_allow_args(ctx.args, verb="allow")
        if error is not None:
            await update.effective_message.reply_text(error)
            return
        access = load_access(self.config.access_path)
        bucket = access.allowed_users if kind == "user" else access.allowed_chats
        if target_id not in bucket:
            bucket.append(target_id)
            save_access(self.config.access_path, access)
        label = "User" if kind == "user" else "Group"
        await update.effective_message.reply_text(
            f"{label} {target_id} added to allowlist."
        )

    async def _cmd_deny(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        kind, target_id, error = _parse_allow_args(ctx.args, verb="deny")
        if error is not None:
            await update.effective_message.reply_text(error)
            return
        access = load_access(self.config.access_path)
        bucket = access.allowed_users if kind == "user" else access.allowed_chats
        label = "User" if kind == "user" else "Group"
        if target_id in bucket:
            bucket.remove(target_id)
            save_access(self.config.access_path, access)
            await update.effective_message.reply_text(
                f"{label} {target_id} removed from allowlist."
            )
        else:
            await update.effective_message.reply_text(
                f"{label} {target_id} was not in the allowlist."
            )

    async def _cmd_policy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        args = ctx.args
        valid = ("owner_only", "allowlist", "open")
        if not args or args[0] not in valid:
            await update.effective_message.reply_text(
                f"Usage: /policy <{'|'.join(valid)}>"
            )
            return
        access = load_access(self.config.access_path)
        access.policy = args[0]  # type: ignore[assignment]
        save_access(self.config.access_path, access)
        await update.effective_message.reply_text(f"Policy set to: {args[0]}")

    async def _cmd_access(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_owner(update):
            return
        access = load_access(self.config.access_path)
        users = ", ".join(str(u) for u in access.allowed_users) or "(none)"
        chats = ", ".join(str(c) for c in access.allowed_chats) or "(none)"
        await update.effective_message.reply_text(
            f"Policy: {access.policy}\n"
            f"Allowed users: {users}\n"
            f"Allowed chats: {chats}\n"
            f"Owner: {self.config.owner_id} (always allowed)"
        )


    async def _register_owner_commands(self) -> None:
        commands = [
            BotCommand("health", "quick health readout"),
            BotCommand("audit", "recent failures, backups, memory footprint"),
            BotCommand("usage", "Claude Code usage and rate limits"),
            BotCommand("access", "show access policy"),
            BotCommand("allow", "add to allowlist: /allow <user|group> <id>"),
            BotCommand("deny", "remove from allowlist: /deny <user|group> <id>"),
            BotCommand("policy", "set policy: /policy <owner_only|allowlist|open>"),
            BotCommand("pause", "drop inbound messages until /resume"),
            BotCommand("resume", "re-enable message forwarding"),
            BotCommand("kill", "stop the bot"),
            BotCommand("reset_session", "fresh Claude session (history kept)"),
        ]
        try:
            await self.application.bot.set_my_commands(
                commands,
                scope=BotCommandScopeChat(chat_id=self.config.owner_id),
            )
        except Exception:
            log.exception("failed to register owner-scoped bot commands")

