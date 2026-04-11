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
    assert "action" in CONTROL_ACTION_SCHEMA["required"]
    assert "reason" in CONTROL_ACTION_SCHEMA["required"]


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
                    "input": {"chat_id": 587272213, "text": "hello!"},
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
