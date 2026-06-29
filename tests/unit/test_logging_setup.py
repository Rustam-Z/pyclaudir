"""Unit tests for structured JSON logging wiring (``logging_setup``)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hamroh.config import Config
from hamroh.logging_setup import (
    JsonLogFormatter,
    _component,
    format_log_line,
    setup_logging,
    tail_log,
)


def _record(
    name: str, msg: str = "hello", level: int = logging.INFO
) -> logging.LogRecord:
    """Build a bare LogRecord for formatter tests."""
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


# ----------------------------------------------------------------------
# _component
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logger_name", "expected"),
    [
        ("hamroh", "core"),
        ("hamroh.telegram_io", "dispatcher"),
        ("hamroh.telegram_io.commands", "dispatcher"),
        ("hamroh.mcp_server", "mcp"),
        ("hamroh.reminder", "reminder"),
        ("hamroh.cc_worker", "cc_worker"),
        ("hamroh.tx", "tx"),
        ("httpx", "httpx"),
    ],
)
def test_component_mapping(logger_name: str, expected: str) -> None:
    assert _component(logger_name) == expected, f"{logger_name} → {expected}"


# ----------------------------------------------------------------------
# JsonLogFormatter
# ----------------------------------------------------------------------


def test_formatter_emits_core_fields() -> None:
    # Given a plain INFO record from the dispatcher
    record = _record("hamroh.telegram_io", "RX message")

    # When formatted as JSON
    payload = json.loads(JsonLogFormatter().format(record))

    # Then the structured fields are present and correct
    assert payload["level"] == "INFO", "level must be carried"
    assert payload["component"] == "dispatcher", "component derives from logger name"
    assert payload["logger"] == "hamroh.telegram_io"
    assert payload["msg"] == "RX message"
    assert payload["ts"].endswith("+00:00"), "timestamp must be UTC ISO-8601"


def test_formatter_includes_optional_event_and_data() -> None:
    # Given a record carrying event/data via extra
    record = _record("hamroh.mcp_server")
    record.event = "tool_call"
    record.data = {"tool": "database_query", "ms": 4}

    # When formatted
    payload = json.loads(JsonLogFormatter().format(record))

    # Then the optional fields appear verbatim
    assert payload["event"] == "tool_call", "event must pass through"
    assert payload["data"] == {"tool": "database_query", "ms": 4}


def test_formatter_omits_event_and_data_when_absent() -> None:
    payload = json.loads(JsonLogFormatter().format(_record("hamroh.engine")))
    assert "event" not in payload, "event must be omitted when not provided"
    assert "data" not in payload, "data must be omitted when not provided"


def test_formatter_captures_exception() -> None:
    # Given a record built while handling an exception
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "hamroh.cc_worker",
            logging.ERROR,
            __file__,
            1,
            "failed",
            None,
            sys.exc_info(),
        )

    # When formatted
    payload = json.loads(JsonLogFormatter().format(record))

    # Then the traceback text is captured
    assert "ValueError: boom" in payload["exc"], "exception text must be captured"


# ----------------------------------------------------------------------
# setup_logging
# ----------------------------------------------------------------------


def test_setup_logging_writes_json_file_and_is_idempotent(tmp_path: Path) -> None:
    # Given a configured logger (save pytest's own handlers/level first)
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        setup_logging(cfg)
        setup_logging(cfg)  # second call must not stack handlers

        # Then exactly two handlers exist (console + JSON file)
        assert len(root.handlers) == 2, "console + file handler, no duplicates"

        # When something is logged
        logging.getLogger("hamroh.engine").info("a structured line")
        for handler in root.handlers:
            handler.flush()

        # Then the JSON file holds a parseable record
        last = tail_log(cfg.log_dir / "hamroh.log", 1)
        assert last, "the log file must contain the line"
        assert json.loads(last[0])["msg"] == "a structured line"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# ----------------------------------------------------------------------
# tail_log / format_log_line
# ----------------------------------------------------------------------


def test_tail_log_returns_last_n_oldest_first(tmp_path: Path) -> None:
    path = tmp_path / "hamroh.log"
    path.write_text("".join(f"line-{i}\n" for i in range(10)), encoding="utf-8")
    assert tail_log(path, 3) == ["line-7", "line-8", "line-9"], "last 3, oldest-first"


def test_tail_log_missing_file_is_empty(tmp_path: Path) -> None:
    assert tail_log(tmp_path / "nope.log", 5) == [], "absent file → no lines"


def test_format_log_line_renders_compact_line() -> None:
    raw = json.dumps(
        {
            "ts": "2026-06-25T10:31:00+00:00",
            "level": "INFO",
            "component": "dispatcher",
            "msg": "RX message",
        }
    )
    assert format_log_line(raw) == "10:31:00 INFO dispatcher | RX message"


def test_format_log_line_tolerates_non_json() -> None:
    assert format_log_line("not json at all") == "not json at all", "raw fallback"
