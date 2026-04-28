"""Telegram dispatcher.

The handlers do the absolute minimum: persist the incoming update to SQLite
and enqueue it on the engine. They never call any LLM directly. Owner-only
slash commands (``/kill``, ``/health``, ``/audit``, ``/access``, ``/allow``,
``/deny``, ``/policy``) are intercepted before the engine sees the
message and silently no-op for non-owners.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Protocol

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from ..access import gate, load_access, save_access
from ..config import Config
from ..db.database import Database
from ..db.messages import (
    apply_user_reaction,
    insert_message,
    mark_edited,
    upsert_user,
)
from ..models import ChatMessage
from ..rate_limiter import RateLimitExceeded, RateLimiter
from ..secrets_scrubber import contains_secret, scrub
from ..transcript import log_inbound, log_inbound_edit
from .attachments import _process_attachments

log = logging.getLogger("pyclaudir.telegram_io")


class EnginePort(Protocol):
    """Minimal surface the engine must expose to the dispatcher."""

    async def submit(self, msg: ChatMessage) -> None: ...

    def prime_typing(self, chat_id: int) -> None: ...


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


def _to_chat_message(update: Update, direction: str = "in") -> ChatMessage | None:
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return None
    raw_text = msg.text or msg.caption or ""
    # Redact credential-shaped strings BEFORE persistence (OWASP LLM02,
    # data-handling rule #2). If the user pastes an API key, we never
    # want it landing in SQLite where query_db can later surface it.
    text = scrub(raw_text)
    reply_to_text_raw = (
        (msg.reply_to_message.text or msg.reply_to_message.caption or None)
        if msg.reply_to_message
        else None
    )
    reply_to_text = scrub(reply_to_text_raw) if reply_to_text_raw else None
    raw_update_json = json.dumps(update.to_dict(), default=str)
    if contains_secret(raw_update_json):
        raw_update_json = scrub(raw_update_json)
    return ChatMessage(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        user_id=msg.from_user.id,
        username=msg.from_user.username,
        first_name=msg.from_user.first_name,
        direction=direction,
        timestamp=msg.date or datetime.now(timezone.utc),
        text=text,
        reply_to_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
        reply_to_text=reply_to_text,
        raw_update_json=raw_update_json,
    )


class TelegramDispatcher:
    def __init__(
        self,
        config: Config,
        db: Database,
        engine: EnginePort | None = None,
        *,
        chat_titles: dict[int, str] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.rate_limiter = rate_limiter
        #: May be ``None`` at construction time so callers can break the
        #: circular dep between dispatcher (owns the bot) and engine
        #: (needs the bot for the typing indicator). Must be set before
        #: :meth:`start` is called, otherwise inbound messages will crash
        #: when the handler tries to forward them.
        self.engine: EnginePort | None = engine
        #: Shared with ToolContext.chat_titles so outbound logs can render
        #: the chat's display name. We populate it from every inbound message.
        self.chat_titles: dict[int, str] = chat_titles if chat_titles is not None else {}
        self.application: Application = (
            Application.builder().token(config.telegram_bot_token).build()
        )
        self._wire_handlers()

    @property
    def bot(self):
        return self.application.bot

    def _wire_handlers(self) -> None:
        # Owner-only control commands first so they short-circuit the engine.
        self.application.add_handler(CommandHandler("kill", self._cmd_kill))
        self.application.add_handler(CommandHandler("health", self._cmd_health))
        self.application.add_handler(CommandHandler("audit", self._cmd_audit))
        # Owner-only access management commands.
        self.application.add_handler(CommandHandler("allow", self._cmd_allow))
        self.application.add_handler(CommandHandler("deny", self._cmd_deny))
        self.application.add_handler(CommandHandler("policy", self._cmd_policy))
        self.application.add_handler(CommandHandler("access", self._cmd_access))

        # All other text/caption messages plus photos and documents.
        self.application.add_handler(
            MessageHandler(
                filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.ALL,
                self._on_message,
            )
        )
        self.application.add_handler(
            MessageHandler(filters.UpdateType.EDITED_MESSAGE, self._on_edited)
        )
        # Inbound reaction updates. Bots only receive these in DMs or when
        # they are an admin in a group/supergroup (Telegram API limitation).
        self.application.add_handler(MessageReactionHandler(self._on_reaction))

    # ------------------------------------------------------------------
    # Owner-only commands
    # ------------------------------------------------------------------

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

    async def _cmd_health(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Quick operational health readout — owner-only, DM or group.

        Surfaces things that matter day-to-day: when the CC subprocess
        last produced output, whether the self-reflection auto-seed
        reminder is active, recent rate-limit hits.
        """
        if not self._is_owner(update):
            return
        lines: list[str] = ["*pyclaudir health*"]
        try:
            row = await self.db.fetch_one(
                "SELECT MAX(timestamp) AS last FROM messages WHERE direction='out'"
            )
            last_tx = row["last"] if row and row["last"] else "(none yet)"
            lines.append(f"- last bot send: `{last_tx}` UTC")
        except Exception as exc:
            lines.append(f"- last bot send: query error ({exc})")
        try:
            row = await self.db.fetch_one(
                "SELECT status, cron_expr, trigger_at FROM reminders "
                "WHERE auto_seed_key = 'self-reflection-default' "
                "ORDER BY id DESC LIMIT 1"
            )
            if row is None:
                lines.append("- self-reflection reminder: MISSING (will re-seed on restart)")
            else:
                lines.append(
                    f"- self-reflection reminder: {row['status']} "
                    f"(cron `{row['cron_expr']}`, next `{row['trigger_at']}` UTC)"
                )
        except Exception as exc:
            lines.append(f"- self-reflection reminder: query error ({exc})")
        try:
            row = await self.db.fetch_one(
                "SELECT COUNT(*) AS c FROM rate_limits WHERE notice_sent = 1"
            )
            notices = int(row["c"]) if row else 0
            lines.append(f"- rate-limit notices fired (lifetime): {notices}")
        except Exception as exc:
            lines.append(f"- rate-limit notices: query error ({exc})")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_audit(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Recent changes / failures / backups — owner-only.

        Richer than /health; intended for occasional "what's been
        happening" review rather than live monitoring.
        """
        if not self._is_owner(update):
            return
        lines: list[str] = ["*pyclaudir audit*"]
        # Recent failed tool calls.
        try:
            rows = await self.db.fetch_all(
                "SELECT tool_name, error, created_at FROM tool_calls "
                "WHERE error IS NOT NULL AND error != '' "
                "ORDER BY id DESC LIMIT 5"
            )
            if rows:
                lines.append("*recent tool failures:*")
                for r in rows:
                    err = (r["error"] or "")[:80]
                    lines.append(f"  • `{r['created_at']}` {r['tool_name']} — {err}")
            else:
                lines.append("*recent tool failures:* none")
        except Exception as exc:
            lines.append(f"*recent tool failures:* query error ({exc})")
        # Prompt backup count.
        try:
            backups_dir = self.config.data_dir / "prompt_backups"
            if backups_dir.exists():
                files = [
                    p for p in backups_dir.iterdir()
                    if p.is_file() and p.suffix == ".md"
                ]
                lines.append(f"*prompt backups:* {len(files)} file(s) in `{backups_dir}`")
            else:
                lines.append("*prompt backups:* (none yet)")
        except Exception as exc:
            lines.append(f"*prompt backups:* error ({exc})")
        # Memory footprint.
        try:
            mem_dir = self.config.memories_dir
            total_bytes = sum(
                p.stat().st_size for p in mem_dir.rglob("*") if p.is_file()
            ) if mem_dir.exists() else 0
            lines.append(f"*memory footprint:* {total_bytes:,} bytes under `data/memories/`")
        except Exception as exc:
            lines.append(f"*memory footprint:* error ({exc})")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

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
        await update.effective_message.reply_text(f"{label} {target_id} added to allowlist.")

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
            await update.effective_message.reply_text(f"{label} {target_id} removed from allowlist.")
        else:
            await update.effective_message.reply_text(f"{label} {target_id} was not in the allowlist.")

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

    async def _cmd_access(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
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

    # ------------------------------------------------------------------
    # Message ingest
    # ------------------------------------------------------------------

    def _remember_chat_title(self, update: Update) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        title = chat.title or chat.full_name or chat.username
        if title:
            self.chat_titles[chat.id] = title

    async def _on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        received_at = time.monotonic()
        cm = _to_chat_message(update, direction="in")
        if cm is None:
            return
        cm.received_at_monotonic = received_at
        log.info(
            "hot-path stage=receipt chat=%s msg=%s t_ms=0",
            cm.chat_id, cm.message_id,
        )

        await self._attach_attachment_markers(update, cm)
        self._remember_chat_title(update)
        await self._persist_inbound(cm)

        chat_type = update.effective_chat.type if update.effective_chat else None
        if not self._check_access(cm, chat_type):
            return
        if not await self._check_rate_limit(cm, chat_type):
            return

        if self.engine is None:
            log.error("dispatcher received message before engine was attached")
            return

        # Fire typing indicator NOW — before debounce + XML format + worker.send.
        # Without this, the user waits silently for the whole hot path before
        # Telegram renders "typing...". Fire-and-forget inside prime_typing.
        self.engine.prime_typing(cm.chat_id)
        log.info(
            "hot-path stage=submit chat=%s msg=%s t_ms=%d",
            cm.chat_id, cm.message_id,
            int((time.monotonic() - received_at) * 1000),
        )
        await self.engine.submit(cm)

    async def _attach_attachment_markers(
        self, update: Update, cm: ChatMessage,
    ) -> None:
        """Download photos/documents BEFORE persistence so the marker line
        lands in the same row as the user's caption (or stands alone when
        the user sent only a file). Rejected attachments still produce a
        marker — the model decides how to apologise."""
        msg = update.effective_message
        if msg is None or not (msg.photo or msg.document is not None):
            return
        markers = await _process_attachments(self.bot, msg, self.config)
        if not markers:
            return
        marker_block = "\n".join(markers)
        cm.text = f"{cm.text}\n{marker_block}" if cm.text else marker_block

    async def _persist_inbound(self, cm: ChatMessage) -> None:
        """Persist *every* message we receive — even from disallowed chats —
        so we have an audit trail."""
        await insert_message(self.db, cm)
        await upsert_user(
            self.db,
            chat_id=cm.chat_id,
            user_id=cm.user_id,
            username=cm.username,
            first_name=cm.first_name,
            timestamp=cm.timestamp,
        )

    def _check_access(self, cm: ChatMessage, chat_type: str | None) -> bool:
        """Hot-reload the access config and gate the message. Disallowed
        chats are a silent drop — no refusal reply, no owner alert.
        Strangers learn nothing about the bot, and they can't burn
        Telegram API quota by flooding us. Returns ``True`` to forward."""
        access = load_access(self.config.access_path)
        allowed = gate(
            access=access,
            owner_id=self.config.owner_id,
            chat_id=cm.chat_id,
            user_id=cm.user_id,
            chat_type=chat_type,
        )
        log_inbound(
            chat_id=cm.chat_id,
            chat_type=chat_type,
            chat_titles=self.chat_titles,
            user_id=cm.user_id,
            user_name=cm.first_name or cm.username,
            message_id=cm.message_id,
            reply_to_id=cm.reply_to_id,
            text=cm.text,
            allowed=allowed,
        )
        return allowed

    async def _check_rate_limit(
        self, cm: ChatMessage, chat_type: str | None,
    ) -> bool:
        """Per-user DM rate limit. Owner is exempt (enforced inside the
        limiter). Group messages skip the check — noisy group users are
        the group's problem, not ours. Returns ``True`` to forward."""
        if self.rate_limiter is None or chat_type != "private":
            return True
        try:
            await self.rate_limiter.check_and_record(cm.user_id)
        except RateLimitExceeded as exc:
            if exc.notify:
                try:
                    await self.bot.send_message(
                        chat_id=cm.chat_id,
                        text=(
                            f"⏳ You're sending messages too fast ({exc.limit}/min). "
                            f"Try again in ~{exc.retry_after_s}s."
                        ),
                    )
                except Exception:
                    log.warning(
                        "rate-limit notice send failed for user %s", cm.user_id,
                    )
            return False
        return True

    async def _on_reaction(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``MessageReactionUpdated``.

        Extract the before/after emoji sets and update the message row's
        ``reactions`` JSON column. Silently no-ops if the reacting user
        isn't identifiable (anonymous group admin reactions arrive without
        a ``user`` field).
        """
        evt = update.message_reaction
        if evt is None or evt.user is None:
            return
        self._remember_chat_title(update)

        def _emojis(reactions) -> list[str]:
            out: list[str] = []
            for r in reactions or ():
                emoji = getattr(r, "emoji", None)
                if emoji:
                    out.append(emoji)
            return out

        await apply_user_reaction(
            self.db,
            chat_id=evt.chat.id,
            message_id=evt.message_id,
            user_id=evt.user.id,
            old_emoji=_emojis(evt.old_reaction),
            new_emoji=_emojis(evt.new_reaction),
        )

    async def _on_edited(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.edited_message
        if msg is None:
            return
        self._remember_chat_title(update)
        await mark_edited(self.db, msg.chat_id, msg.message_id, msg.text or "")
        log_inbound_edit(
            chat_id=msg.chat_id,
            chat_titles=self.chat_titles,
            user_id=msg.from_user.id if msg.from_user else None,
            user_name=(msg.from_user.first_name or msg.from_user.username) if msg.from_user else None,
            message_id=msg.message_id,
            text=msg.text or "",
        )

    # ------------------------------------------------------------------
    # Lifecycle (manual — we co-run with other asyncio tasks).
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.engine is None:
            raise RuntimeError(
                "TelegramDispatcher.start() called with no engine attached. "
                "Set dispatcher.engine before starting."
            )
        await self.application.initialize()
        await self._register_owner_commands()
        await self.application.start()
        await self.application.updater.start_polling(
            allowed_updates=[
                "message",
                "edited_message",
                "callback_query",
                "message_reaction",
            ],
        )
        log.info("telegram dispatcher polling")

    async def _register_owner_commands(self) -> None:
        commands = [
            BotCommand("health", "quick health readout"),
            BotCommand("audit", "recent failures, backups, memory footprint"),
            BotCommand("access", "show access policy"),
            BotCommand("allow", "add to allowlist: /allow <user|group> <id>"),
            BotCommand("deny", "remove from allowlist: /deny <user|group> <id>"),
            BotCommand("policy", "set policy: /policy <owner_only|allowlist|open>"),
            BotCommand("kill", "stop the bot"),
        ]
        try:
            await self.application.bot.set_my_commands(
                commands,
                scope=BotCommandScopeChat(chat_id=self.config.owner_id),
            )
        except Exception:
            log.exception("failed to register owner-scoped bot commands")

    async def stop(self) -> None:
        try:
            await self.application.updater.stop()
        except Exception:  # pragma: no cover
            log.exception("updater stop failed")
        await self.application.stop()
        await self.application.shutdown()
