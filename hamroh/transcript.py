"""Conversation transcript logging.

A dedicated logger named ``hamroh.tx`` that emits one line per inbound,
outbound, edited, deleted, dropped, or reacted message. Lines are prefixed
``[RX]`` / ``[TX]`` / ``[DROP]`` / ``[EDIT]`` / ``[DEL]`` / ``[RX↺]`` so they
are easy to grep and stand out from the boring polling/HTTP chatter.

The chat-title cache (a plain ``dict[int, str]``) is populated by the
dispatcher on every inbound message, so outbound logs from tools can show
the chat's display name instead of just its numeric id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("hamroh.tx")

#: Maximum body length we render inline before truncating.
MAX_BODY = 200


@dataclass(frozen=True)
class ChatRef:
    """The chat a transcript line belongs to, for label rendering."""

    chat_id: int
    chat_titles: dict[int, str] | None = None
    chat_type: str | None = None


@dataclass(frozen=True)
class UserRef:
    """The author of a message, for label rendering."""

    user_id: int | None
    name: str | None = None


@dataclass(frozen=True)
class MsgRef:
    """A message's identity + body for a transcript line."""

    message_id: int | None
    text: str | None
    reply_to_id: int | None = None


def _truncate(text: str | None) -> str:
    if not text:
        return ""
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(flat) > MAX_BODY:
        flat = flat[:MAX_BODY] + "…"
    return flat


def _chat_label(
    chat_id: int,
    chat_titles: dict[int, str] | None = None,
    chat_type: str | None = None,
) -> str:
    """Render a chat as ``DM`` or ``G "title"[-100..]``."""
    title = (chat_titles or {}).get(chat_id)
    if chat_type == "private" or chat_id > 0:
        if title:
            return f"DM {title}[{chat_id}]"
        return f"DM [{chat_id}]"
    if title:
        return f'G "{title}"[{chat_id}]'
    return f"G [{chat_id}]"


def _user_label(user_id: int | None, name: str | None) -> str:
    if user_id is None:
        return ""
    if name:
        return f"{name}[{user_id}]"
    return f"[{user_id}]"


def log_inbound(chat: ChatRef, user: UserRef, msg: MsgRef, *, allowed: bool) -> None:
    chat_label = _chat_label(chat.chat_id, chat.chat_titles, chat.chat_type)
    user_label = _user_label(user.user_id, user.name)
    reply = f" →m{msg.reply_to_id}" if msg.reply_to_id else ""
    body = _truncate(msg.text)
    prefix = "[RX]" if allowed else "[DROP]"
    suffix = "" if allowed else " (chat not allowed)"
    log.info(
        "%s %s %s m%d%s%s | %s",
        prefix,
        chat_label,
        user_label,
        msg.message_id,
        reply,
        suffix,
        body,
    )


def log_inbound_edit(chat: ChatRef, user: UserRef, msg: MsgRef) -> None:
    chat_label = _chat_label(chat.chat_id, chat.chat_titles)
    user_label = _user_label(user.user_id, user.name)
    log.info(
        "[RX↺] %s %s m%d (edited) | %s",
        chat_label,
        user_label,
        msg.message_id,
        _truncate(msg.text),
    )


def log_outbound(chat: ChatRef, msg: MsgRef) -> None:
    chat_label = _chat_label(chat.chat_id, chat.chat_titles)
    reply = f" →m{msg.reply_to_id}" if msg.reply_to_id else ""
    mid = f" m{msg.message_id}" if msg.message_id else ""
    log.info("[TX] %s%s%s | %s", chat_label, mid, reply, _truncate(msg.text))


def log_edit(
    *,
    chat_id: int,
    chat_titles: dict[int, str] | None,
    message_id: int,
    text: str | None,
) -> None:
    chat = _chat_label(chat_id, chat_titles)
    log.info("[EDIT] %s m%d | %s", chat, message_id, _truncate(text))


def log_delete(
    *,
    chat_id: int,
    chat_titles: dict[int, str] | None,
    message_id: int,
) -> None:
    chat = _chat_label(chat_id, chat_titles)
    log.info("[DEL] %s m%d", chat, message_id)


def log_reaction(
    *,
    chat_id: int,
    chat_titles: dict[int, str] | None,
    message_id: int,
    emoji: str,
) -> None:
    chat = _chat_label(chat_id, chat_titles)
    log.info("[REACT] %s m%d %s", chat, message_id, emoji)


# ---------------------------------------------------------------------------
# Claude Code subprocess introspection
# ---------------------------------------------------------------------------

#: Separate logger for "what the model is doing right now" lines, distinct
#: from the inbound/outbound conversation transcript.
cc_log = logging.getLogger("hamroh.cc")


def log_cc_user(text: str) -> None:
    """One inbound user envelope sent into the CC subprocess (the XML batch)."""
    cc_log.info("[CC.user] %s", _truncate(text))


def log_cc_text(text: str) -> None:
    """A text content block from the assistant — visible 'thinking out loud'.

    Note: in normal hamroh operation the agent should NOT produce these,
    because text blocks are invisible to the user. Seeing one here usually
    means dropped-text detection is about to fire.
    """
    cc_log.info("[CC.text] %s", _truncate(text))


def log_cc_tool_use(tool_name: str, tool_use_id: str, args: dict | None) -> None:
    """The assistant is calling a tool."""
    args_str = ""
    if args:
        try:
            import json as _json

            args_str = _truncate(_json.dumps(args, default=str, ensure_ascii=False))
        except Exception:
            args_str = _truncate(str(args))
    cc_log.info("[CC.tool→] %s(%s) id=%s", tool_name, args_str, tool_use_id[:8])


def log_cc_tool_result(tool_use_id: str, content: str | None, is_error: bool) -> None:
    """A tool returned a result back to the assistant."""
    tag = "[CC.tool✗]" if is_error else "[CC.tool✓]"
    cc_log.info("%s id=%s | %s", tag, tool_use_id[:8], _truncate(content))


def log_cc_result(action: str | None, reason: str | None) -> None:
    """End of one assistant turn — the structured ControlAction."""
    cc_log.info("[CC.done] action=%s reason=%s", action, _truncate(reason))
