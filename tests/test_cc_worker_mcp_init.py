"""``_handle_event`` surfaces every error CC reports — failed MCP
servers from ``system/init``, plus generic top-level error indicators
(``is_error``, ``api_error_status``, ``error``) on any other event.
Lets us relay CC's own signals instead of reinventing failure
detection per-shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyclaudir.cc_worker import CcSpawnSpec, CcWorker, TurnResult
from pyclaudir.config import Config


def _spec(tmp_path: Path) -> CcSpawnSpec:
    sp = tmp_path / "system.md"
    sp.write_text("system")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    return CcSpawnSpec(
        binary="/bin/true",
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )


@pytest.fixture
def worker(tmp_path: Path) -> CcWorker:
    w = CcWorker(_spec(tmp_path), Config.for_test(tmp_path))
    w._proc = MagicMock()
    w._proc.returncode = None
    w._current_turn = TurnResult()
    return w


def _init_event(servers: list[dict]) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "sid-1",
        "mcp_servers": servers,
    }


def test_failed_mcp_server_logs_error(worker: CcWorker, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="pyclaudir.cc_worker"):
        worker._handle_event(_init_event([
            {"name": "mcp-atlassian", "status": "failed"},
            {"name": "pyclaudir", "status": "connected"},
        ]))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "mcp-atlassian" in msg
    assert "failed" in msg
    assert "pyclaudir" not in msg


def test_all_connected_emits_no_log(worker: CcWorker, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="pyclaudir.cc_worker"):
        worker._handle_event(_init_event([
            {"name": "pyclaudir", "status": "connected"},
            {"name": "deepwiki", "status": "connected"},
        ]))

    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_missing_mcp_servers_field_is_fine(worker: CcWorker, caplog) -> None:
    """Older Claude Code versions (or odd payloads) may omit the field."""
    with caplog.at_level(logging.WARNING, logger="pyclaudir.cc_worker"):
        worker._handle_event({
            "type": "system", "subtype": "init", "session_id": "sid-1",
        })
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_result_with_api_error_status_errors(worker: CcWorker, caplog) -> None:
    """Turn-level API error on a ``result`` event should surface as an
    ERROR — currently silent without the generic relay."""
    with caplog.at_level(logging.WARNING, logger="pyclaudir.cc_worker"):
        worker._handle_event({
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 529,
        })
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("api_error_status=529" in m and "is_error=true" in m for m in msgs)


def test_event_with_error_field_errors(worker: CcWorker, caplog) -> None:
    """Any event carrying a top-level ``error`` field is relayed."""
    with caplog.at_level(logging.WARNING, logger="pyclaudir.cc_worker"):
        worker._handle_event({"type": "system", "subtype": "boom", "error": "kaboom"})
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("error=kaboom" in m for m in msgs)


def test_user_event_is_skipped_by_generic_relay(worker: CcWorker, caplog) -> None:
    """Per-tool ``is_error`` on user/tool_result blocks is the breaker's
    job — generic relay must not double-log them."""
    with caplog.at_level(logging.WARNING, logger="pyclaudir.cc_worker"):
        worker._handle_event({
            "type": "user",
            "is_error": True,  # would never appear at this level in practice
            "message": {"content": []},
        })
    assert not any(
        "cc reported error" in r.getMessage()
        for r in caplog.records
    )
