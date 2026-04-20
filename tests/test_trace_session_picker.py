"""``find_bot_session`` must distinguish the bot's JSONL from any other
Claude Code session sitting in the same project directory (notably the
session of the operator's *own* CC instance running in the same cwd).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import pyclaudir.scripts.trace as trace_mod


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _bot_event(text: str = "<msg id=\"1\" chat=\"-100\" user=\"42\" name=\"A\" time=\"10:00\">hi</msg>") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "sessionId": "bot-sid",
    }


def _operator_event(text: str = "fix the bug in main.py") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "sessionId": "operator-sid",
    }


@pytest.fixture()
def patched_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Redirect both PROJECT_DIR and _data_dir() at temp paths."""
    project_dir = tmp_path / "claude_projects"
    project_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(trace_mod, "PROJECT_DIR", project_dir)
    monkeypatch.setenv("PYCLAUDIR_DATA_DIR", str(data_dir))
    return project_dir, data_dir


def test_session_id_file_wins(patched_dirs) -> None:
    """If data/session_id points at a real file, prefer it unconditionally."""
    project_dir, data_dir = patched_dirs
    bot = project_dir / "abc-bot.jsonl"
    _write_jsonl(bot, [_bot_event()])

    operator = project_dir / "xyz-operator.jsonl"
    _write_jsonl(operator, [_operator_event()])
    # Make operator newer
    os.utime(operator, (operator.stat().st_atime, bot.stat().st_mtime + 100))

    (data_dir / "session_id").write_text("abc-bot\n")

    found = trace_mod.find_bot_session()
    assert found is not None
    assert found.name == "abc-bot.jsonl"


def test_falls_back_to_xml_fingerprint(patched_dirs) -> None:
    """No data/session_id → scan for the <msg ...> fingerprint and ignore
    sessions that look like a regular Claude Code prompt."""
    project_dir, _ = patched_dirs
    bot = project_dir / "bot.jsonl"
    _write_jsonl(bot, [_bot_event()])

    operator = project_dir / "operator.jsonl"
    _write_jsonl(operator, [_operator_event()])
    # Operator is newer; without fingerprinting we'd pick it.
    os.utime(operator, (operator.stat().st_atime, bot.stat().st_mtime + 100))

    found = trace_mod.find_bot_session()
    assert found is not None
    assert found.name == "bot.jsonl"


def test_returns_none_when_no_bot_session(patched_dirs) -> None:
    project_dir, _ = patched_dirs
    (project_dir / "operator.jsonl").write_text(
        json.dumps(_operator_event()) + "\n"
    )
    assert trace_mod.find_bot_session() is None


def test_returns_none_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace_mod, "PROJECT_DIR", tmp_path / "does_not_exist")
    monkeypatch.setenv("PYCLAUDIR_DATA_DIR", str(tmp_path / "data"))
    assert trace_mod.find_bot_session() is None


def test_session_id_file_with_missing_jsonl_falls_back(patched_dirs) -> None:
    """If data/session_id points at a file that no longer exists, fall back
    to fingerprinting instead of returning None."""
    project_dir, data_dir = patched_dirs
    (data_dir / "session_id").write_text("vanished-sid\n")

    bot = project_dir / "real-bot.jsonl"
    _write_jsonl(bot, [_bot_event()])

    found = trace_mod.find_bot_session()
    assert found is not None
    assert found.name == "real-bot.jsonl"


def test_looks_like_bot_session_skips_non_text_first_event(patched_dirs) -> None:
    """The first user event might be a tool_result; we should keep scanning."""
    project_dir, _ = patched_dirs
    path = project_dir / "weird.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
                },
            },
            _bot_event(),
        ],
    )
    assert trace_mod._looks_like_bot_session(path) is True


def test_looks_like_bot_session_rejects_plain_text(patched_dirs) -> None:
    project_dir, _ = patched_dirs
    path = project_dir / "human.jsonl"
    _write_jsonl(path, [_operator_event("debug the test")])
    assert trace_mod._looks_like_bot_session(path) is False
