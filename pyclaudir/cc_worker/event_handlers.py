"""Stream-json event dispatch for the CC subprocess.

:class:`CcEventHandlerMixin` holds the per-event parsing half of
:class:`pyclaudir.cc_worker.worker.CcWorker` — relocated verbatim in the
file-size split. It is a mixin (not a standalone object) because the
handlers read and write the worker's turn state: ``_current_turn``,
``_session_id``, ``_result_queue``, ``_stderr_tail``, ``_capture``, and
the tool-error breaker hooks (``_record_tool_error``,
``_cancel_tool_error_watchdog``). Those attributes are defined in
``CcWorker.__init__``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..models import ControlAction
from ..transcript import (
    log_cc_result,
    log_cc_text,
    log_cc_tool_result,
    log_cc_tool_use,
)
from .events import TurnResult

# Pinned to the parent package name so log captures keyed on
# ``"pyclaudir.cc_worker"`` keep matching after the module split.
log = logging.getLogger("pyclaudir.cc_worker")

#: MCP tools that put content in front of the user, namespaced the way
#: Claude Code reports them in tool_use events. A turn that produced text
#: without calling any of these "dropped" its text — the user never saw
#: it — regardless of whether StructuredOutput ended the turn cleanly.
#: Add a tool here when it delivers content to a chat (test_tool_discovery
#: pins every entry to a real tool so a rename can't silently break this).
CHAT_DELIVERY_TOOLS: frozenset[str] = frozenset({
    "mcp__pyclaudir__send_message",
    "mcp__pyclaudir__reply_to_message",
    "mcp__pyclaudir__send_photo",
    "mcp__pyclaudir__send_memory_document",
    "mcp__pyclaudir__create_poll",
})


class CcEventHandlerMixin:
    """Event-dispatch methods mixed into ``CcWorker``."""

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Parse one stream-json event from the CC subprocess.

        Stream-json events come in several shapes; we only care about a few:

        - ``{"type": "system", "subtype": "init", "session_id": "..."}``
          — captured so we can persist + resume.
        - ``{"type": "assistant", "message": {"content": [...]}}`` — text and
          tool-use blocks.
        - ``{"type": "result", ...}`` — turn finished. The structured-output
          payload is parsed into a :class:`ControlAction`.

        Anything else (tool_use, tool_result, ping at this layer) is ignored —
        side effects already happened via the MCP server.
        """
        etype = event.get("type")
        if etype != "user":
            self._relay_top_level_error(event, etype)
        if etype == "system" and event.get("subtype") == "init":
            self._on_system_init(event)
            return
        if self._current_turn is None:
            self._current_turn = TurnResult()
        if etype == "assistant":
            self._on_assistant_event(event)
        elif etype == "user":
            self._on_user_event(event)
        elif etype == "result":
            self._on_result_event(event)

    def _relay_top_level_error(
        self, event: dict[str, Any], etype: str | None,
    ) -> None:
        """Generic relay of any error-shaped top-level field.

        Per-tool ``is_error`` lives inside ``user``-typed events and is
        handled by the tool-error breaker; the caller skips those to
        avoid double-logging.
        """
        err_bits: list[str] = []
        if event.get("is_error"):
            err_bits.append("is_error=true")
        api_err = event.get("api_error_status")
        if api_err:
            err_bits.append(f"api_error_status={api_err}")
        err = event.get("error")
        if err:
            err_bits.append(f"error={err}")
        if err_bits:
            log.error(
                "cc reported error in %s/%s event: %s",
                etype, event.get("subtype") or "-", ", ".join(err_bits),
            )

    def _on_system_init(self, event: dict[str, Any]) -> None:
        """Capture the CC session id and surface MCP-server init failures."""
        sid = event.get("session_id")
        if isinstance(sid, str):
            self._session_id = sid
            log.info("cc session id %s", sid)
            self._capture.maybe_rename(self._session_id)
        for server in event.get("mcp_servers") or ():
            if not isinstance(server, dict):
                continue
            status = server.get("status")
            if status != "connected":
                log.error(
                    "mcp server %s did not connect (status=%s) — its "
                    "tools won't be available this session",
                    server.get("name", "?"), status,
                )

    def _on_assistant_event(self, event: dict[str, Any]) -> None:
        """Process one assistant event's content blocks (text / tool_use /
        thinking). Side effects: append to ``text_blocks``, set
        ``control`` when StructuredOutput lands, log everything."""
        assert self._current_turn is not None
        message = event.get("message") or {}
        for block in message.get("content") or []:
            self._handle_assistant_block(block)

    def _handle_assistant_block(self, block: dict[str, Any]) -> None:
        assert self._current_turn is not None
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if txt:
                self._current_turn.text_blocks.append(txt)
                log_cc_text(txt)
        elif btype == "tool_use":
            self._handle_assistant_tool_use(block)
        elif btype == "thinking":
            # Extended-thinking blocks (visible only with the right
            # model + flag). Treat like text but with its own tag.
            log_cc_text("(thinking) " + block.get("thinking", ""))

    def _handle_assistant_tool_use(self, block: dict[str, Any]) -> None:
        assert self._current_turn is not None
        tool_name = block.get("name", "?")
        tool_input = block.get("input")
        log_cc_tool_use(
            tool_name=tool_name,
            tool_use_id=str(block.get("id", "")),
            args=tool_input,
        )
        if tool_name in CHAT_DELIVERY_TOOLS:
            self._current_turn.sent_to_chat = True
        # StructuredOutput is the definitive turn-end signal. Claudir
        # confirmed: the action lives in the tool_use event's input
        # field, NOT in the result event payload.
        if tool_name == "StructuredOutput" and isinstance(tool_input, dict):
            try:
                self._current_turn.control = ControlAction.model_validate(tool_input)
            except Exception:
                log.warning(
                    "could not parse StructuredOutput input: %r",
                    tool_input,
                )

    def _on_user_event(self, event: dict[str, Any]) -> None:
        """The other half of the channel: tool_result blocks the runtime
        injects back into the conversation as a synthetic user message."""
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if block.get("type") == "tool_result":
                self._handle_tool_result_block(block)

    def _handle_tool_result_block(self, block: dict[str, Any]) -> None:
        raw = block.get("content")
        if isinstance(raw, list):
            # Sometimes a list of {"type":"text","text":...}
            text = " ".join(
                (b.get("text", "") if isinstance(b, dict) else str(b))
                for b in raw
            )
        else:
            text = "" if raw is None else str(raw)
        is_error = bool(block.get("is_error", False))
        log_cc_tool_result(
            tool_use_id=str(block.get("tool_use_id", "")),
            content=text,
            is_error=is_error,
        )
        if is_error:
            self._record_tool_error()

    def _on_result_event(self, event: dict[str, Any]) -> None:
        """Turn complete. Parse the structured-output payload, finalise the
        :class:`TurnResult`, and hand it to the engine via the result queue."""
        assert self._current_turn is not None
        payload = self._extract_result_payload(event)
        if isinstance(payload, dict):
            try:
                self._current_turn.control = ControlAction.model_validate(payload)
            except Exception:
                log.warning("could not parse control action from %r", payload)
        self._current_turn.stderr_tail = list(self._stderr_tail)
        self._current_turn.dropped_text = (
            bool(self._current_turn.text_blocks)
            and not self._current_turn.sent_to_chat
        )
        ctrl = self._current_turn.control
        log_cc_result(
            action=ctrl.action if ctrl else None,
            reason=ctrl.reason if ctrl else None,
        )
        # Turn finished cleanly; defuse the watchdog so a stale deadline
        # from this turn can't trip the breaker after the fact.
        self._cancel_tool_error_watchdog()
        self._result_queue.put_nowait(self._current_turn)
        self._current_turn = None

    def _extract_result_payload(self, event: dict[str, Any]) -> Any:
        """Pull the structured-output payload out of a result event.

        Structured output is delivered in ``event["result"]`` when the JSON
        schema is enforced. Older CC versions stream it via ``event["output"]``
        or stuff it into the last text block. JSON-encoded strings are
        decoded; everything else is returned as-is.
        """
        assert self._current_turn is not None
        payload = (
            event.get("result")
            or event.get("output")
            or (self._current_turn.text_blocks[-1] if self._current_turn.text_blocks else None)
        )
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
        return payload
