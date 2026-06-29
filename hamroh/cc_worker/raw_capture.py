"""Raw stdout/stderr capture for the CC subprocess.

Appends every raw line to ``<cc_logs_dir>/<session_id>.stream.jsonl`` and
``<session_id>.stderr.log`` as the data arrives. Files open under a
``pending-<ts>`` name when the session id is unknown at spawn time and are
renamed once the system/init event reveals it. Disabled entirely when the
worker is constructed without a logs dir.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import IO

# Pinned to the parent package name so log captures keyed on
# ``"hamroh.cc_worker"`` keep matching after the module split.
log = logging.getLogger("hamroh.cc_worker")


class RawCapture:
    """File lifecycle for one worker's raw capture.

    Every method no-ops when capture is disabled (``logs_dir=None``) or
    the files failed to open — a logging problem must never take down
    the worker's hot path.
    """

    def __init__(self, logs_dir: Path | None) -> None:
        self._dir = logs_dir
        self.stream_log: IO[str] | None = None
        self.stream_path: Path | None = None
        self.stderr_log: IO[str] | None = None
        self.stderr_path: Path | None = None

    def open(self, session_id: str | None) -> None:
        """Open the per-session raw-capture files in append mode.

        If we know the session id (resume case) we open with the final name.
        Otherwise we open with a ``pending-<ts>`` name and rename later when
        :meth:`maybe_rename` is called with the discovered id.
        """
        if self._dir is None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        prefix = session_id if session_id else f"pending-{int(time.time() * 1000)}"
        self.stream_path = self._dir / f"{prefix}.stream.jsonl"
        self.stderr_path = self._dir / f"{prefix}.stderr.log"
        try:
            self.stream_log = self.stream_path.open("a", encoding="utf-8")
            self.stderr_log = self.stderr_path.open("a", encoding="utf-8")
            log.info(
                "raw cc capture: stream=%s stderr=%s",
                self.stream_path.name,
                self.stderr_path.name,
            )
        except OSError:
            log.exception("failed to open cc raw-capture files; capture disabled")
            self.stream_log = None
            self.stderr_log = None

    def close(self) -> None:
        for handle in (self.stream_log, self.stderr_log):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # pragma: no cover
                    log.exception("error closing cc raw log")
        self.stream_log = None
        self.stderr_log = None

    def maybe_rename(self, session_id: str | None) -> None:
        """Rename ``pending-*`` files to ``<session_id>.*`` once we learn it."""
        if not self._should_rename(session_id):
            return
        assert self._dir is not None  # narrowed by _should_rename
        new_stream = self._dir / f"{session_id}.stream.jsonl"
        new_stderr = self._dir / f"{session_id}.stderr.log"
        try:
            self._rename_to(new_stream, new_stderr)
            log.info("raw cc capture renamed to %s", new_stream.name)
        except OSError:
            log.exception(
                "failed to rename raw cc capture files; keeping capture alive"
            )
            self._recover_after_failed_rename(new_stream, new_stderr)

    def _should_rename(self, session_id: str | None) -> bool:
        """Return whether a pending capture exists that should be renamed."""
        if session_id is None:
            return False
        if self.stream_path is None or self.stderr_path is None:
            return False
        if not self.stream_path.name.startswith("pending-"):
            return False
        return self._dir is not None

    def _rename_to(self, new_stream: Path, new_stderr: Path) -> None:
        """Close, rename pending files to the final names, and reopen them.

        macOS would let us rename without closing, but we close to keep the
        code portable to platforms that lock open files.
        """
        assert self.stream_path is not None and self.stderr_path is not None
        self._close_handles()
        # If a previous run already created files for this session id
        # (resume case after a crash), we append to them by deleting
        # the empty pending file and reopening the existing one.
        self._move_or_drop(self.stream_path, new_stream)
        self._move_or_drop(self.stderr_path, new_stderr)
        self.stream_path = new_stream
        self.stderr_path = new_stderr
        self.stream_log = new_stream.open("a", encoding="utf-8")
        self.stderr_log = new_stderr.open("a", encoding="utf-8")

    def _close_handles(self) -> None:
        """Flush and close both open capture handles before a rename."""
        for handle in (self.stream_log, self.stderr_log):
            if handle is not None:
                handle.flush()
                handle.close()

    @staticmethod
    def _move_or_drop(pending: Path, target: Path) -> None:
        """Rename ``pending`` to ``target``, or drop ``pending`` if target exists."""
        if target.exists():
            pending.unlink(missing_ok=True)
        else:
            pending.rename(target)

    def _recover_after_failed_rename(self, new_stream: Path, new_stderr: Path) -> None:
        """Reopen capture under whichever name each file actually lives under.

        A rename failure must not silently disable capture for the rest of the
        worker's lifetime. A partial failure may have renamed one of the two
        files; a later init event retries the rename via the ``pending-`` guard.
        """
        assert self.stream_path is not None and self.stderr_path is not None
        if not self.stream_path.exists() and new_stream.exists():
            self.stream_path = new_stream
        if not self.stderr_path.exists() and new_stderr.exists():
            self.stderr_path = new_stderr
        self.stream_log = self.stream_path.open("a", encoding="utf-8")
        self.stderr_log = self.stderr_path.open("a", encoding="utf-8")

    def write_stream(self, raw: bytes) -> None:
        if self.stream_log is None:
            return
        try:
            self.stream_log.write(raw.decode("utf-8", errors="replace"))
            if not raw.endswith(b"\n"):
                self.stream_log.write("\n")
            self.stream_log.flush()
        except Exception:  # pragma: no cover
            log.exception("failed to write to cc stream log")

    def write_stderr(self, decoded: str) -> None:
        if self.stderr_log is None:
            return
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self.stderr_log.write(f"{ts} {decoded}\n")
            self.stderr_log.flush()
        except Exception:  # pragma: no cover
            log.exception("failed to write to cc stderr log")
