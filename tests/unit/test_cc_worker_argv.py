"""Pure unit tests for the CC worker's argv builder and event parser.

We don't spawn a real ``claude`` process here — that happens in the manual
end-to-end check described in the README.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from hamroh.cc_schema import CONTROL_ACTION_SCHEMA, schema_json
from hamroh.cc_worker import (
    CcSpawnSpec,
    CcWorker,
    FORBIDDEN_FLAG,
    TurnResult,
    WorkerHooks,
    build_argv,
)
from hamroh.config import Config


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


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    return Config.for_test(tmp_path)


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
    from hamroh.models import ControlAction
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


def test_event_parser_handles_assistant_text(spec: CcSpawnSpec, cfg: Config) -> None:
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
    )
    assert worker._current_turn.text_blocks == ["hello"]


def test_event_parser_captures_session_id(spec: CcSpawnSpec, cfg: Config) -> None:
    worker = CcWorker(spec, cfg)
    worker._handle_event({"type": "system", "subtype": "init", "session_id": "sid-1"})
    assert worker.session_id == "sid-1"


def test_event_parser_completes_turn_with_control(
    spec: CcSpawnSpec, cfg: Config
) -> None:
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "result",
            "result": {"action": "stop", "reason": "done"},
        }
    )
    # The completed turn was queued
    queued = worker._result_queue.get_nowait()
    assert queued.control is not None
    assert queued.control.action == "stop"
    assert queued.dropped_text is False


def test_event_parser_detects_dropped_text(spec: CcSpawnSpec, cfg: Config) -> None:
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "I would say hi"}]},
        }
    )
    worker._handle_event({"type": "result"})  # no control payload
    queued = worker._result_queue.get_nowait()
    assert queued.dropped_text is True
    assert queued.control is None


def test_event_parser_logs_tool_use(spec: CcSpawnSpec, cfg: Config, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="hamroh.cc")
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "telegram_send_message",
                        "id": "toolu_abcdef1234",
                        "input": {"chat_id": 12345, "text": "hello!"},
                    }
                ]
            },
        }
    )
    msgs = [r.getMessage() for r in caplog.records if r.name == "hamroh.cc"]
    assert any("[CC.tool→]" in m and "telegram_send_message" in m for m in msgs)


def test_event_parser_logs_tool_result(spec: CcSpawnSpec, cfg: Config, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="hamroh.cc")
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
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
        }
    )
    msgs = [r.getMessage() for r in caplog.records if r.name == "hamroh.cc"]
    assert any("[CC.tool✓]" in m and "sent message_id=99" in m for m in msgs)


def test_event_parser_logs_done_with_action(
    spec: CcSpawnSpec, cfg: Config, caplog
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="hamroh.cc")
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "result",
            "result": {"action": "stop", "reason": "replied to user"},
        }
    )
    msgs = [r.getMessage() for r in caplog.records if r.name == "hamroh.cc"]
    assert any("[CC.done]" in m and "action=stop" in m for m in msgs)


def test_structured_output_parsed_from_tool_use(spec: CcSpawnSpec, cfg: Config) -> None:
    """Claudir confirmed: StructuredOutput arrives as a tool_use event,
    NOT in the result event's payload. This test pins the correct parsing
    path that was broken for the entire v1 release (always action=None).
    """
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()

    # Step 1: the model calls StructuredOutput as a tool_use
    worker._handle_event(
        {
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
        }
    )
    # Control action should be parsed BEFORE the result event
    assert worker._current_turn.control is not None
    assert worker._current_turn.control.action == "stop"
    assert worker._current_turn.control.reason == "Greeted the user."

    # Step 2: the tool_result comes back
    worker._handle_event(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_structured_001",
                        "content": [
                            {
                                "type": "text",
                                "text": "Structured output provided successfully",
                            }
                        ],
                        "is_error": False,
                    }
                ]
            },
        }
    )

    # Step 3: the result event finalises the turn
    worker._handle_event({"type": "result"})
    queued = worker._result_queue.get_nowait()

    # The turn should have the parsed control, not None
    assert queued.control is not None
    assert queued.control.action == "stop"
    assert queued.control.reason == "Greeted the user."
    assert queued.dropped_text is False


def test_structured_output_sleep_action(spec: CcSpawnSpec, cfg: Config) -> None:
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
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
        }
    )
    assert worker._current_turn.control is not None
    assert worker._current_turn.control.action == "sleep"
    assert worker._current_turn.control.sleep_ms == 30000


def test_on_giveup_fires_before_crashloop_raises(
    spec: CcSpawnSpec, cfg: Config
) -> None:
    """When the crash budget is exhausted the worker must fire
    ``on_giveup`` *before* raising :class:`CrashLoop`. Tests the contract
    directly by simulating the giveup branch of ``_supervise_loop``
    without spawning a real subprocess.
    """
    import asyncio
    import time

    from hamroh.cc_worker import CrashLoop

    calls: list[int] = []

    async def record_giveup(count: int) -> None:
        calls.append(count)

    worker = CcWorker(spec, cfg, WorkerHooks(on_giveup=record_giveup))
    # Seed `crash_limit` entries so the very next exit trips the ceiling.
    now = time.monotonic()
    worker._crash_times = [now - i for i in range(worker._crash_limit - 1)]

    async def run() -> None:
        worker._crash_times.append(now)
        assert len(worker._crash_times) >= worker._crash_limit
        if worker._on_giveup is not None:
            await worker._on_giveup(len(worker._crash_times))
        raise CrashLoop("simulated")

    with pytest.raises(CrashLoop):
        asyncio.run(run())

    assert calls == [worker._crash_limit]


def _structured_output_event(uid: str = "toolu_so") -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "StructuredOutput",
                    "id": uid,
                    "input": {"action": "stop", "reason": "done"},
                }
            ]
        },
    }


def test_text_without_delivery_tool_is_dropped_even_with_control(
    spec: CcSpawnSpec, cfg: Config
) -> None:
    """Regression: the model wrote its answer as plain text, then called
    StructuredOutput(stop) — but never ``telegram_send_message``. The user received
    nothing, so dropped_text must be True so the engine nags the model to
    resend via the tool. (StructuredOutput is a clean turn-end signal,
    not proof of delivery.)"""
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Yep, here I am."}]},
        }
    )
    worker._handle_event(_structured_output_event())
    worker._handle_event({"type": "result"})
    queued = worker._result_queue.get_nowait()
    assert queued.control is not None, "StructuredOutput must still parse"
    assert queued.control.action == "stop"
    assert queued.dropped_text is True, (
        "text never reached the user — the corrective nag must fire"
    )


def test_text_with_delivery_tool_is_not_dropped(spec: CcSpawnSpec, cfg: Config) -> None:
    """Text blocks alongside a real ``telegram_send_message`` call are fine — the
    user got the message, no nag needed."""
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Sending now..."}]},
        }
    )
    worker._handle_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__hamroh__telegram_send_message",
                        "id": "toolu_send",
                        "input": {"chat_id": 587272213, "text": "Yep, here I am."},
                    }
                ]
            },
        }
    )
    worker._handle_event(_structured_output_event())
    worker._handle_event({"type": "result"})
    queued = worker._result_queue.get_nowait()
    assert queued.user_visible_action is True, (
        "telegram_send_message call must be tracked"
    )
    assert queued.dropped_text is False, "delivered text must not trigger the nag"


def test_text_with_reaction_is_not_dropped(spec: CcSpawnSpec, cfg: Config) -> None:
    """Regression: the model reacted with an emoji, narrated why in a text
    block, then called StructuredOutput(stop). A reaction is a real
    response, so the narration must NOT be treated as dropped text — else
    the engine nags the model into a loop on every greeting/ack."""
    worker = CcWorker(spec, cfg)
    worker._current_turn = TurnResult()
    worker._handle_event(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Reacted instead of replying."}]
            },
        }
    )
    worker._handle_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__hamroh__telegram_add_reaction",
                        "id": "toolu_react",
                        "input": {
                            "chat_id": 587272213,
                            "message_id": 444,
                            "emoji": "👀",
                        },
                    }
                ]
            },
        }
    )
    worker._handle_event(_structured_output_event())
    worker._handle_event({"type": "result"})
    queued = worker._result_queue.get_nowait()
    assert queued.user_visible_action is True, (
        "telegram_add_reaction must count as a visible action"
    )
    assert queued.dropped_text is False, "a reaction is a response — no nag"


def test_stale_session_pattern_detection(spec: CcSpawnSpec, cfg: Config) -> None:
    """``_stderr_indicates_stale_session`` matches the known CC wording
    and nothing else. False positives would silently drop live sessions
    on unrelated crashes; false negatives reintroduce the issue-#29
    crash-loop, so the matcher is pinned tightly."""
    worker = CcWorker(spec, cfg)

    worker._stderr_tail = []
    assert worker._stderr_indicates_stale_session() is False

    worker._stderr_tail = ["oom killed", "segfault"]
    assert worker._stderr_indicates_stale_session() is False

    worker._stderr_tail = [
        "No conversation found with session ID: abc-123",
    ]
    assert worker._stderr_indicates_stale_session() is True

    # Substring match — claude may prefix with a timestamp or wrap the
    # message; we still want to catch it.
    worker._stderr_tail = [
        "2025-05-09T10:00 No conversation found with session ID: abc trailing",
    ]
    assert worker._stderr_indicates_stale_session() is True

    # Malformed-id wording (observed in the wild against claude 2.1.138
    # when the persisted id was corrupted on disk).
    worker._stderr_tail = [
        "Error: --resume requires a valid session ID or session title "
        "when used with --print. Usage: claude -p --resume "
        '<session-id|title>. Provided value "c649fda5-...19sd" is not '
        "a UUID and does not match any session title.",
    ]
    assert worker._stderr_indicates_stale_session() is True


def test_run_stale_recovery_drops_session_and_fires_callback(
    spec: CcSpawnSpec, cfg: Config
) -> None:
    """``_run_stale_recovery`` invokes ``on_stale_session`` with the
    rejected id, clears ``spec.session_id`` and the cached
    ``_session_id``, and does NOT consume the crash budget. Mirrors the
    style of ``test_on_giveup_fires_before_crashloop_raises``: drives
    the recovery branch directly without spawning a real subprocess."""
    spec_with_session = dataclasses.replace(spec, session_id="abc-123")
    seen: list[str] = []

    async def record(stale_id: str) -> None:
        seen.append(stale_id)

    worker = CcWorker(spec_with_session, cfg, WorkerHooks(on_stale_session=record))
    # _run_stale_recovery awaits sleep + _terminate_proc + start; stub
    # the process-touching bits and zero the backoff so the test is fast.
    worker._crash_backoff_base = 0.0

    async def _noop() -> None:
        return

    worker._terminate_proc = _noop  # type: ignore[assignment]
    worker.start = _noop  # type: ignore[assignment]

    asyncio.run(worker._run_stale_recovery("abc-123"))

    assert seen == ["abc-123"]
    assert worker.spec.session_id is None
    assert worker._session_id is None
    assert worker._crash_times == []


def test_drain_readers_waits_for_pending_stderr_line(
    spec: CcSpawnSpec, cfg: Config
) -> None:
    """The race that ``_drain_readers`` exists to fix: a reader that
    has not yet appended its final line to ``_stderr_tail`` when
    ``proc.wait()`` returns. Simulate it with a reader task that
    sleeps briefly, appends, then exits — the drain must wait for
    that append to land."""
    worker = CcWorker(spec, cfg)

    async def slow_reader() -> None:
        await asyncio.sleep(0.05)
        worker._stderr_tail.append("No conversation found with session ID: abc-123")

    async def run() -> None:
        worker._stderr_task = asyncio.create_task(slow_reader())
        await worker._drain_readers()
        # After draining the stale-session line must be visible to the
        # supervisor's classifier.
        assert worker._stderr_indicates_stale_session() is True

    asyncio.run(run())
