"""Raw stdout/stderr capture from the CC subprocess.

We don't spawn an actual ``claude`` here — instead we drive the worker's
private file handles directly to verify the file lifecycle:

1. Files open with a ``pending-<ts>`` name when session id is unknown.
2. They open with the final name when session id is known up front (resume).
3. They get renamed atomically once the system/init event tells us the id.
4. Subsequent writes append.
5. Disabling capture (``cc_logs_dir=None``) is a no-op and never crashes.
"""

from __future__ import annotations

from pathlib import Path


from pyclaudir.cc_schema import schema_json
from pyclaudir.cc_worker import CcSpawnSpec, CcWorker
from pyclaudir.config import Config


def _spec(tmp_path: Path, *, with_logs: bool, session_id: str | None = None) -> CcSpawnSpec:
    sp = tmp_path / "system.md"
    sp.write_text("system")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers":{}}')
    schema = tmp_path / "schema.json"
    schema.write_text(schema_json())
    return CcSpawnSpec(
        binary="claude",
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
        session_id=session_id,
        cc_logs_dir=(tmp_path / "cc_logs") if with_logs else None,
    )


def test_capture_disabled_is_noop(tmp_path: Path) -> None:
    worker = CcWorker(_spec(tmp_path, with_logs=False), Config.for_test(tmp_path))
    worker._open_raw_logs()
    assert worker._stream_log is None
    assert worker._stderr_log is None
    # These should be no-ops
    worker._write_stream_line(b'{"type":"system"}\n')
    worker._write_stderr_line("nothing")
    worker._close_raw_logs()


def test_capture_pending_then_renamed_on_init(tmp_path: Path) -> None:
    worker = CcWorker(_spec(tmp_path, with_logs=True), Config.for_test(tmp_path))
    worker._open_raw_logs()
    assert worker._stream_log is not None
    assert worker._stream_log_path is not None
    assert worker._stream_log_path.name.startswith("pending-")
    assert worker._stream_log_path.suffix == ".jsonl"

    # Simulate one stdout line arriving before the init event
    worker._write_stream_line(b'{"type":"ping"}\n')
    worker._write_stderr_line("warming up")

    # System init event arrives → triggers rename
    worker._handle_event({
        "type": "system",
        "subtype": "init",
        "session_id": "abc-123-xyz",
    })

    assert worker._stream_log_path is not None
    assert worker._stream_log_path.name == "abc-123-xyz.stream.jsonl"
    assert worker._stderr_log_path is not None
    assert worker._stderr_log_path.name == "abc-123-xyz.stderr.log"

    # File exists, contains the line we wrote pre-rename
    contents = worker._stream_log_path.read_text()
    assert '"ping"' in contents

    stderr_contents = worker._stderr_log_path.read_text()
    assert "warming up" in stderr_contents

    # Post-rename writes still go to the same file (handle was reopened)
    worker._write_stream_line(b'{"type":"assistant"}\n')
    worker._close_raw_logs()
    contents_after = (tmp_path / "cc_logs" / "abc-123-xyz.stream.jsonl").read_text()
    assert '"ping"' in contents_after
    assert '"assistant"' in contents_after


def test_capture_with_known_session_id_uses_final_name(tmp_path: Path) -> None:
    worker = CcWorker(
        _spec(tmp_path, with_logs=True, session_id="resumed-sid"),
        Config.for_test(tmp_path),
    )
    worker._open_raw_logs()
    assert worker._stream_log_path is not None
    assert worker._stream_log_path.name == "resumed-sid.stream.jsonl"
    worker._write_stream_line(b'{"type":"ping","resumed":true}\n')
    worker._close_raw_logs()
    text = (tmp_path / "cc_logs" / "resumed-sid.stream.jsonl").read_text()
    assert '"resumed":true' in text


def test_capture_preserves_malformed_lines(tmp_path: Path) -> None:
    """Even non-JSON garbage gets written — we capture before parsing."""
    worker = CcWorker(
        _spec(tmp_path, with_logs=True, session_id="sid"),
        Config.for_test(tmp_path),
    )
    worker._open_raw_logs()
    worker._write_stream_line(b"this is not json\n")
    worker._write_stream_line(b'{"valid":true}\n')
    worker._close_raw_logs()
    text = (tmp_path / "cc_logs" / "sid.stream.jsonl").read_text()
    assert "this is not json" in text
    assert '"valid":true' in text


def test_capture_appends_across_reopen(tmp_path: Path) -> None:
    """Two starts on the same session id append, not overwrite."""
    spec = _spec(tmp_path, with_logs=True, session_id="sticky")
    cfg = Config.for_test(tmp_path)
    w1 = CcWorker(spec, cfg)
    w1._open_raw_logs()
    w1._write_stream_line(b'{"first":true}\n')
    w1._close_raw_logs()

    w2 = CcWorker(spec, cfg)
    w2._open_raw_logs()
    w2._write_stream_line(b'{"second":true}\n')
    w2._close_raw_logs()

    text = (tmp_path / "cc_logs" / "sticky.stream.jsonl").read_text()
    assert '"first":true' in text
    assert '"second":true' in text


def test_capture_appends_when_pending_renames_to_existing(tmp_path: Path) -> None:
    """Crash + respawn case: a previous run already created files for this
    session id. The pending file from the new run should drop and we should
    keep appending to the existing one.
    """
    cfg = Config.for_test(tmp_path)
    spec_a = _spec(tmp_path, with_logs=True, session_id="prev")
    a = CcWorker(spec_a, cfg)
    a._open_raw_logs()
    a._write_stream_line(b'{"old":true}\n')
    a._close_raw_logs()

    # New worker doesn't know session id at start time
    spec_b = _spec(tmp_path, with_logs=True, session_id=None)
    b = CcWorker(spec_b, cfg)
    b._open_raw_logs()
    pending_name = b._stream_log_path.name if b._stream_log_path else ""
    assert pending_name.startswith("pending-")
    b._write_stream_line(b'{"new_pending":true}\n')

    # System init arrives with the same session id as the prior run
    b._handle_event({"type": "system", "subtype": "init", "session_id": "prev"})

    # The existing file is preserved; the new pending is dropped
    final_path = tmp_path / "cc_logs" / "prev.stream.jsonl"
    assert final_path.exists()
    text = final_path.read_text()
    assert '"old":true' in text
    # The pending file should be gone
    pendings = list((tmp_path / "cc_logs").glob("pending-*.stream.jsonl"))
    assert pendings == []
    b._close_raw_logs()
