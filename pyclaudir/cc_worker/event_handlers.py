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
from typing import TYPE_CHECKING, Any

from ..models import ControlAction
from ..transcript import (
    log_cc_result,
    log_cc_text,
    log_cc_tool_result,
    log_cc_tool_use,
)

if TYPE_CHECKING:
    from .events import TurnResult
# Pinned to the parent package name so log captures keyed on
# ``"pyclaudir.cc_worker"`` keep matching after the module split.
log = logging.getLogger("pyclaudir.cc_worker")

#: MCP tools that produce a user-visible effect — a delivered message or
#: a reaction/edit/delete/poll-close — namespaced the way Claude Code
#: reports them in tool_use events. A turn that produced text but called
#: none of these "dropped" its text: the user perceived nothing, so a
#: trailing narration block went nowhere. A reaction (or edit/delete) is
#: a real response, so its narration must NOT be treated as dropped text.
#: Add a tool here when the user can perceive its effect (test_tool_discovery
#: pins every entry to a real tool so a rename can't silently break this).
USER_VISIBLE_TOOLS: frozenset[str] = frozenset({
    "mcp__pyclaudir__telegram_send_message",
    "mcp__pyclaudir__telegram_reply_to_message",
    "mcp__pyclaudir__telegram_send_photo",
    "mcp__pyclaudir__telegram_send_memory_document",
    "mcp__pyclaudir__telegram_create_poll",
    "mcp__pyclaudir__telegram_add_reaction",
    "mcp__pyclaudir__telegram_edit_message",
    "mcp__pyclaudir__telegram_delete_message",
    "mcp__pyclaudir__telegram_stop_poll",
})


class CcEventHandlerMixin:
    """Event-dispatch methods mixed into ``CcWorker``."""

    #: Defined in ``CcWorker.__init__``; annotated here (no assignment) so
    #: the mixin's reads type-check now that ``_handle_event`` no longer
    #: lazily creates the turn.
    _current_turn: TurnResult | None
    #: Init-gate read by ``_handle_event`` / set by ``_on_system_init``;
    #: defined in ``CcWorker.__init__`` (see worker.py).
    _awaiting_turn_init: bool

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
        if self._current_turn is None or self._awaiting_turn_init:
            # Drop: no turn is collecting events here. Either _current_turn is
            # None (a stray event from a dying/terminated session — enqueuing
            # it would orphan the next session's reply), or _awaiting_turn_init
            # is set (a turn was just send()-ed but its own system/init hasn't
            # arrived; cc runs one turn per stdin message, so these events
            # belong to a PRIOR turn still draining — folding them in would
            # misattribute them and could close the wrong turn early).
            return
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
        # The real cc-turn for the most recent send() has begun: events from
        # here on belong to it, not to a prior draining turn. Clear before the
        # session_id read so an init without one still disarms the gate.
        self._awaiting_turn_init = False
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
        if tool_name in USER_VISIBLE_TOOLS:
            self._current_turn.user_visible_action = True
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
        # The result event is the definitive turn outcome — mid-turn
        # assistant-event errors can still be retried by CC and recover,
        # so only an error here marks the turn as failed.
        if event.get("is_error"):
            raw = event.get("result")
            self._current_turn.api_error = (
                raw if isinstance(raw, str) and raw else "unknown API error"
            )
        payload = self._extract_result_payload(event)
        if isinstance(payload, dict):
            try:
                self._current_turn.control = ControlAction.model_validate(payload)
            except Exception:
                log.warning("could not parse control action from %r", payload)
        self._current_turn.stderr_tail = list(self._stderr_tail)
        self._current_turn.dropped_text = (
            bool(self._current_turn.text_blocks)
            and not self._current_turn.user_visible_action
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
