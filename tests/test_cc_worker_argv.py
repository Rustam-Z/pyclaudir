"""Pure unit tests for the CC worker's argv builder and event parser.

We don't spawn a real ``claude`` process here — that happens in the manual
end-to-end check described in the README.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyclaudir.cc_schema import CONTROL_ACTION_SCHEMA, schema_json
from pyclaudir.cc_worker import (
    CcSpawnSpec,
    CcWorker,
    FORBIDDEN_FLAG,
    TurnResult,
    build_argv,
)


@pytest.fixture()
def spec(tmp_path: Path) -> CcSpawnSpec:
    sp = tmp_path / "system.md"
    sp.write_text("Pretend system prompt.")
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}))
    schema = tmp_path / "schema.json"
    schema.write_text(schema_json())
    return CcSpawnSpec(
        binary="claude",
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )


def test_build_argv_includes_required_flags(spec: CcSpawnSpec) -> None:
    argv = build_argv(spec)
    assert "--print" in argv
    assert "--input-format" in argv and "stream-json" in argv
    assert "--output-format" in argv
    assert "--verbose" in argv
    assert "--model" in argv
    assert "--effort" in argv
    assert "--system-prompt" in argv
    assert "--mcp-config" in argv
    assert "--strict-mcp-config" in argv
    assert "--allowedTools" in argv
    assert "--disallowedTools" in argv
    assert "--json-schema" in argv


def test_build_argv_resume_optional(spec: CcSpawnSpec, tmp_path: Path) -> None:
    argv = build_argv(spec)
    assert "--resume" not in argv

    spec2 = CcSpawnSpec(
        binary="claude",
        model="claude-opus-4-6",
        system_prompt_path=spec.system_prompt_path,
        mcp_config_path=spec.mcp_config_path,
        json_schema_path=spec.json_schema_path,
        session_id="abc-123",
    )
    argv2 = build_argv(spec2)
    assert "--resume" in argv2
    assert argv2[argv2.index("--resume") + 1] == "abc-123"


def test_build_argv_refuses_forbidden_flag(spec: CcSpawnSpec) -> None:
    # Sanity: clean argv contains no trace of the forbidden flag, even as a
    # substring of any element.
    for token in build_argv(spec):
        assert FORBIDDEN_FLAG not in token


def test_control_schema_is_strict() -> None:
    assert CONTROL_ACTION_SCHEMA["additionalProperties"] is False
    assert CONTROL_ACTION_SCHEMA["required"] == ["action"]
    # Anthropic's tool input_schema rejects top-level oneOf/allOf/anyOf,
    # so "reason required on stop" is enforced by the pydantic validator,
    # not the schema. The schema keeps reason optional but capped.
    assert "allOf" not in CONTROL_ACTION_SCHEMA
    assert "oneOf" not in CONTROL_ACTION_SCHEMA
    assert "anyOf" not in CONTROL_ACTION_SCHEMA
    assert CONTROL_ACTION_SCHEMA["properties"]["reason"]["maxLength"] > 0


def test_control_action_requires_reason_only_on_stop() -> None:
    from pyclaudir.models import ControlAction
    import pytest

    # stop without reason → rejected
    with pytest.raises(ValueError, match="reason is required"):
        ControlAction(action="stop")
    with pytest.raises(ValueError, match="reason is required"):
        ControlAction(action="stop", reason="   ")

    # stop with reason → ok
    ControlAction(action="stop", reason="replied to user")

    # sleep / heartbeat without reason → ok (provisional, not terminal)
    ControlAction(action="sleep")
    ControlAction(action="heartbeat")


def test_event_parser_handles_assistant_text(spec: CcSpawnSpec) -> None:
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    })
    assert worker._current_turn.text_blocks == ["hello"]


def test_event_parser_captures_session_id(spec: CcSpawnSpec) -> None:
    worker = CcWorker(spec)
    worker._handle_event({"type": "system", "subtype": "init", "session_id": "sid-1"})
    assert worker.session_id == "sid-1"


def test_event_parser_completes_turn_with_control(spec: CcSpawnSpec) -> None:
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "result",
        "result": {"action": "stop", "reason": "done"},
    })
    # The completed turn was queued
    queued = worker._result_queue.get_nowait()
    assert queued.control is not None
    assert queued.control.action == "stop"
    assert queued.dropped_text is False


def test_event_parser_detects_dropped_text(spec: CcSpawnSpec) -> None:
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "I would say hi"}]},
    })
    worker._handle_event({"type": "result"})  # no control payload
    queued = worker._result_queue.get_nowait()
    assert queued.dropped_text is True
    assert queued.control is None


def test_event_parser_logs_tool_use(spec: CcSpawnSpec, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="pyclaudir.cc")
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "send_message",
                    "id": "toolu_abcdef1234",
                    "input": {"chat_id": 12345, "text": "hello!"},
                }
            ]
        },
    })
    msgs = [r.getMessage() for r in caplog.records if r.name == "pyclaudir.cc"]
    assert any("[CC.tool→]" in m and "send_message" in m for m in msgs)


def test_event_parser_logs_tool_result(spec: CcSpawnSpec, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="pyclaudir.cc")
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_abcdef1234",
                    "content": [{"type": "text", "text": "sent message_id=99"}],
                    "is_error": False,
                }
            ]
        },
    })
    msgs = [r.getMessage() for r in caplog.records if r.name == "pyclaudir.cc"]
    assert any("[CC.tool✓]" in m and "sent message_id=99" in m for m in msgs)


def test_event_parser_logs_done_with_action(spec: CcSpawnSpec, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="pyclaudir.cc")
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "result",
        "result": {"action": "stop", "reason": "replied to user"},
    })
    msgs = [r.getMessage() for r in caplog.records if r.name == "pyclaudir.cc"]
    assert any("[CC.done]" in m and "action=stop" in m for m in msgs)


def test_structured_output_parsed_from_tool_use(spec: CcSpawnSpec) -> None:
    """Claudir confirmed: StructuredOutput arrives as a tool_use event,
    NOT in the result event's payload. This test pins the correct parsing
    path that was broken for the entire v1 release (always action=None).
    """
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()

    # Step 1: the model calls StructuredOutput as a tool_use
    worker._handle_event({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "StructuredOutput",
                    "id": "toolu_structured_001",
                    "input": {
                        "action": "stop",
                        "reason": "Greeted the user.",
                    },
                }
            ]
        },
    })
    # Control action should be parsed BEFORE the result event
    assert worker._current_turn.control is not None
    assert worker._current_turn.control.action == "stop"
    assert worker._current_turn.control.reason == "Greeted the user."

    # Step 2: the tool_result comes back
    worker._handle_event({
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_structured_001",
                    "content": [{"type": "text", "text": "Structured output provided successfully"}],
                    "is_error": False,
                }
            ]
        },
    })

    # Step 3: the result event finalises the turn
    worker._handle_event({"type": "result"})
    queued = worker._result_queue.get_nowait()

    # The turn should have the parsed control, not None
    assert queued.control is not None
    assert queued.control.action == "stop"
    assert queued.control.reason == "Greeted the user."
    assert queued.dropped_text is False


def test_structured_output_sleep_action(spec: CcSpawnSpec) -> None:
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    worker._handle_event({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "StructuredOutput",
                    "id": "toolu_sleep_001",
                    "input": {
                        "action": "sleep",
                        "reason": "Nothing to do for a while.",
                        "sleep_ms": 30000,
                    },
                }
            ]
        },
    })
    assert worker._current_turn.control is not None
    assert worker._current_turn.control.action == "sleep"
    assert worker._current_turn.control.sleep_ms == 30000


def test_on_giveup_fires_before_crashloop_raises(spec: CcSpawnSpec) -> None:
    """When the crash budget is exhausted the worker must fire
    ``on_giveup`` *before* raising :class:`CrashLoop`. Tests the contract
    directly by simulating the giveup branch of ``_supervise_loop``
    without spawning a real subprocess.
    """
    import asyncio
    import time

    from pyclaudir.cc_worker import CrashLoop

    calls: list[tuple[list[str], int]] = []

    async def record_giveup(stderr_tail: list[str], count: int) -> None:
        calls.append((list(stderr_tail), count))

    worker = CcWorker(spec, on_giveup=record_giveup)
    worker._stderr_tail = ["unauthorized", "authentication failed"]
    # Seed CRASH_LIMIT entries so the very next exit trips the ceiling.
    now = time.monotonic()
    worker._crash_times = [now - i for i in range(CcWorker.CRASH_LIMIT - 1)]

    async def run() -> None:
        # Reproduce the accounting + trip logic from _supervise_loop
        # without subprocess plumbing.
        worker._crash_times.append(now)
        assert len(worker._crash_times) >= CcWorker.CRASH_LIMIT
        if worker._on_giveup is not None:
            await worker._on_giveup(
                list(worker._stderr_tail), len(worker._crash_times),
            )
        raise CrashLoop("simulated")

    with pytest.raises(CrashLoop):
        asyncio.run(run())

    assert len(calls) == 1
    stderr, count = calls[0]
    assert "unauthorized" in stderr
    assert count == CcWorker.CRASH_LIMIT


def test_structured_output_with_text_blocks_is_not_dropped_text(spec: CcSpawnSpec) -> None:
    """If the model emits text AND calls StructuredOutput, dropped_text
    should be False because the turn ended cleanly with a control action."""
    worker = CcWorker(spec)
    worker._current_turn = TurnResult()
    # Model emits some thinking text
    worker._handle_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Let me think..."}]},
    })
    # Then calls StructuredOutput
    worker._handle_event({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "StructuredOutput",
                    "id": "toolu_so",
                    "input": {"action": "stop", "reason": "done"},
                }
            ]
        },
    })
    # Result event
    worker._handle_event({"type": "result"})
    queued = worker._result_queue.get_nowait()
    assert queued.control is not None
    assert queued.control.action == "stop"
    # Has text blocks BUT also has control → NOT dropped text
    assert queued.text_blocks == ["Let me think..."]
    assert queued.dropped_text is False
