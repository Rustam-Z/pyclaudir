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
import time
from datetime import datetime, timezone
from typing import Protocol

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from ..access import gate, load_access
from ..config import Config
from ..db.database import Database
from ..db.messages import (
    apply_user_reaction,
    insert_message,
    mark_edited,
    upsert_user,
)
from ..db.unauthorized import chat_has_refusal, insert_unauthorized_message
from ..input_normalizer import normalize_inbound
from ..models import ChatMessage
from ..rate_limiter import RateLimitExceeded, RateLimiter
from ..secrets_scrubber import contains_secret, scrub
from ..transcript import log_inbound, log_inbound_edit
from .attachments import _process_attachments
from .commands import OwnerCommandsMixin

log = logging.getLogger("pyclaudir.telegram_io")


class EnginePort(Protocol):
    """Minimal surface the engine must expose to the dispatcher."""

    async def submit(self, msg: ChatMessage) -> None: ...

    async def reset_session(self) -> None: ...

    def prime_typing(self, chat_id: int) -> None: ...

    @property
    def pending_count(self) -> int: ...

    @property
    def turn_elapsed_s(self) -> float | None: ...


def _clean_inbound(raw: str | None) -> tuple[str | None, frozenset[str]]:
    """Scrub credentials, then defang Unicode obfuscation (zero-width /
    bidi / NFKC). Order matters: scrub first so the credential regexes
    see original bytes; normalize after so DB and model see clean text.

    Returns ``(cleaned_or_None, flags)``. Empty / None input passes
    through with an empty flag set.
    """
    if not raw:
        return raw, frozenset()
    return normalize_inbound(scrub(raw))


def _reply_context(msg) -> tuple[int | None, str | None, frozenset[str]]:
    """Extract ``(reply_to_id, scrubbed reply text, flags)`` from a message."""
    reply = msg.reply_to_message
    if reply is None:
        return None, None, frozenset()
    reply_to_text, flags = _clean_inbound(reply.text or reply.caption or None)
    return reply.message_id, reply_to_text, flags


def _scrubbed_raw_update(update: Update) -> str:
    """JSON-serialize the raw update, redacting any credential-shaped text."""
    raw = json.dumps(update.to_dict(), default=str)
    return scrub(raw) if contains_secret(raw) else raw


def _to_chat_message(update: Update, direction: str = "in") -> ChatMessage | None:
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return None
    text, text_flags = _clean_inbound(msg.text or msg.caption or "")
    reply_to_id, reply_to_text, reply_flags = _reply_context(msg)
    return ChatMessage(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        user_id=msg.from_user.id,
        username=msg.from_user.username,
        first_name=msg.from_user.first_name,
        direction=direction,
        timestamp=msg.date or datetime.now(timezone.utc),
        text=text or "",
        reply_to_id=reply_to_id,
        reply_to_text=reply_to_text,
        raw_update_json=_scrubbed_raw_update(update),
        input_flags=text_flags | reply_flags,
    )


class TelegramDispatcher(OwnerCommandsMixin):
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
        #: Owner /pause toggle. While True, inbound user messages from ALL
        #: chats are dropped (not persisted, not forwarded). In-memory only:
        #: a restart starts un-paused. Flipped by /pause and /resume
        #: (see OwnerCommandsMixin).
        self._paused: bool = False
        #: Shared with ToolContext.chat_titles so outbound logs can render
        #: the chat's display name. We populate it from every inbound message.
        self.chat_titles: dict[int, str] = (
            chat_titles if chat_titles is not None else {}
        )
        # AIORateLimiter queues outbound calls under Telegram's flood
        # limits and honours 429 retry_after, so a long multi-chunk reply
        # can't trip flood control and abort the turn via the tool-error
        # breaker.
        self.application: Application = (
            Application.builder()
            .token(config.telegram_bot_token)
            .rate_limiter(AIORateLimiter())
            .build()
        )
        self._wire_handlers()

    @property
    def bot(self):
        return self.application.bot

    def _wire_handlers(self) -> None:
        # Owner-only control commands first so they short-circuit the engine.
        self.application.add_handler(CommandHandler("kill", self._cmd_kill))
        self.application.add_handler(
            CommandHandler("reset_session", self._cmd_reset_session)
        )
        self.application.add_handler(CommandHandler("pause", self._cmd_pause))
        self.application.add_handler(CommandHandler("resume", self._cmd_resume))
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
    # Message ingest
    # ------------------------------------------------------------------

    def _remember_chat_title(self, update: Update) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        title = chat.title or chat.full_name or chat.username
        if title:
            self.chat_titles[chat.id] = title

    async def _on_message(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        received_at = time.monotonic()
        cm = _to_chat_message(update, direction="in")
        if cm is None:
            return
        cm.received_at_monotonic = received_at
        if self._paused:
            log.info("paused — dropping chat=%s msg=%s", cm.chat_id, cm.message_id)
            return
        log.info("hot-path stage=receipt chat=%s msg=%s t_ms=0", cm.chat_id, cm.message_id)

        self._remember_chat_title(update)
        chat_type = update.effective_chat.type if update.effective_chat else None
        if not self._check_access(cm, chat_type):
            await self._handle_unauthorized(cm, chat_type)
            return
        if not await self._check_rate_limit(cm, chat_type):
            return

        await self._attach_attachment_markers(update, cm)
        await self._persist_inbound(cm)

        if self.engine is None:
            log.error("dispatcher received message before engine was attached")
            return

        # Fire typing indicator NOW — before debounce + XML format + worker.send.
        # Without this, the user waits silently for the whole hot path before
        # Telegram renders "typing...". Fire-and-forget inside prime_typing.
        self.engine.prime_typing(cm.chat_id)
        log.info(
            "hot-path stage=submit chat=%s msg=%s t_ms=%d",
            cm.chat_id,
            cm.message_id,
            int((time.monotonic() - received_at) * 1000),
        )
        await self.engine.submit(cm)

    async def _attach_attachment_markers(
        self,
        update: Update,
        cm: ChatMessage,
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
        """Persist an inbound message. Callers must gate on
        ``_check_access`` first — disallowed chats are dropped upstream
        and never reach this method."""
        await insert_message(self.db, cm)
        await upsert_user(self.db, cm)

    def _is_allowed(self, chat_id: int, user_id: int, chat_type: str | None) -> bool:
        """Hot-reload ``access.json`` and return the gate decision.

        No side effects — callers that want a transcript log line should
        use ``_check_access`` instead.
        """
        access = load_access(self.config.access_path)
        return gate(
            access=access,
            owner_id=self.config.owner_id,
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
        )

    def _check_access(self, cm: ChatMessage, chat_type: str | None) -> bool:
        """Gate an inbound message and log the attempt. Disallowed chats
        are a silent drop — no refusal reply, no owner alert, no DB or
        memory write. Strangers learn nothing about the bot, and they
        can't burn Telegram API quota by flooding us. Returns ``True``
        to forward."""
        allowed = self._is_allowed(cm.chat_id, cm.user_id, chat_type)
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

    async def _handle_unauthorized(
        self, cm: ChatMessage, chat_type: str | None
    ) -> None:
        """Log a denied message to ``unauthorized_messages`` and, in DMs
        only, send the one-time refusal reply. Groups stay silent. The
        row is written first so a failed send still records the attempt
        and won't trigger a retry on the next message."""
        should_reply = chat_type == "private" and not await chat_has_refusal(
            self.db, cm.chat_id
        )
        await insert_unauthorized_message(
            self.db,
            cm=cm,
            chat_type=chat_type,
            refusal_sent=should_reply,
        )
        if not should_reply:
            return
        try:
            await self.bot.send_message(
                chat_id=cm.chat_id,
                text=(
                    "This is a private assistant. "
                    "Please contact the owner if you want an access."
                ),
            )
        except Exception:
            log.warning("unauthorized refusal send failed for chat %s", cm.chat_id)

    async def _check_rate_limit(
        self,
        cm: ChatMessage,
        chat_type: str | None,
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
                        "rate-limit notice send failed for user %s",
                        cm.user_id,
                    )
            return False
        return True

    async def _on_reaction(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
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
        chat_type = evt.chat.type if evt.chat else None
        if not self._is_allowed(evt.chat.id, evt.user.id, chat_type):
            return

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
        chat_type = msg.chat.type if msg.chat else None
        user_id = msg.from_user.id if msg.from_user else 0
        if not self._is_allowed(msg.chat_id, user_id, chat_type):
            return
        await mark_edited(self.db, msg.chat_id, msg.message_id, msg.text or "")
        log_inbound_edit(
            chat_id=msg.chat_id,
            chat_titles=self.chat_titles,
            user_id=msg.from_user.id if msg.from_user else None,
            user_name=(msg.from_user.first_name or msg.from_user.username)
            if msg.from_user
            else None,
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

    async def stop(self) -> None:
        try:
            await self.application.updater.stop()
        except Exception:  # pragma: no cover
            log.exception("updater stop failed")
        await self.application.stop()
        await self.application.shutdown()
