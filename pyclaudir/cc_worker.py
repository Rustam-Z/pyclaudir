"""Claude Code subprocess worker.

A long-lived asyncio task that wraps a single ``claude`` child process. The
worker is the *only* place in pyclaudir allowed to call
``asyncio.create_subprocess_exec`` (security invariant 6).

Lifecycle:

- ``start()`` spawns ``claude`` with the locked-down argv built by
  :func:`build_argv` and starts background reader tasks.
- ``send(text)`` writes a stream-json user message to stdin and triggers a
  new turn.
- ``inject(text)`` queues additional user content to be flushed mid-turn.
- ``wait_for_result()`` returns the next :class:`TurnResult` produced by the
  subprocess.
- ``stop()`` terminates the subprocess and reaps it.

Step 7 implements basic spawn/read/send. Inject and crash-recovery come in
Steps 9 and 10.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO

from .models import ControlAction
from .tools.base import Heartbeat
from .transcript import (
    log_cc_result,
    log_cc_text,
    log_cc_tool_result,
    log_cc_tool_use,
    log_cc_user,
)

log = logging.getLogger(__name__)


#: The set of built-in tools we explicitly deny. Belt-and-braces with
#: ``--allowedTools`` so even if Claude Code's allowlist behaviour ever
#: changes, every dangerous tool is still off.
#:
#: ``WebFetch`` and ``WebSearch`` are *not* on this list — they were
#: re-enabled by operator decision so the agent can answer questions that
#: need fresh information. The trade is: it can now exfiltrate data via
#: URL + read SSRF-able internal addresses if a user asks her nicely.
#: We rely on her system prompt + Telegram-only output channel to keep
#: that surface bounded.
DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Read",
    "NotebookEdit",
)

#: Allowed tools — pyclaudir MCP, community mcp-atlassian Jira tools,
#: and web tools. Only Jira tools are allowed; Confluence, JSM, and
#: ProForma tools are deliberately excluded.
ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__pyclaudir",
    # Jira — community mcp-atlassian (sooperset/mcp-atlassian)
    # The server prefixes all Jira tools with "jira_".
    "mcp__mcp-atlassian__jira_search",
    "mcp__mcp-atlassian__jira_get_issue",
    "mcp__mcp-atlassian__jira_create_issue",
    "mcp__mcp-atlassian__jira_batch_create_issues",
    "mcp__mcp-atlassian__jira_update_issue",
    "mcp__mcp-atlassian__jira_delete_issue",
    "mcp__mcp-atlassian__jira_transition_issue",
    "mcp__mcp-atlassian__jira_add_comment",
    "mcp__mcp-atlassian__jira_edit_comment",
    "mcp__mcp-atlassian__jira_add_worklog",
    "mcp__mcp-atlassian__jira_get_worklog",
    "mcp__mcp-atlassian__jira_get_transitions",
    "mcp__mcp-atlassian__jira_get_all_projects",
    "mcp__mcp-atlassian__jira_get_project_issues",
    "mcp__mcp-atlassian__jira_get_project_versions",
    "mcp__mcp-atlassian__jira_get_project_components",
    "mcp__mcp-atlassian__jira_search_fields",
    "mcp__mcp-atlassian__jira_get_field_options",
    "mcp__mcp-atlassian__jira_get_user_profile",
    "mcp__mcp-atlassian__jira_get_issue_watchers",
    "mcp__mcp-atlassian__jira_add_watcher",
    "mcp__mcp-atlassian__jira_remove_watcher",
    "mcp__mcp-atlassian__jira_get_link_types",
    "mcp__mcp-atlassian__jira_create_issue_link",
    "mcp__mcp-atlassian__jira_create_remote_issue_link",
    "mcp__mcp-atlassian__jira_link_to_epic",
    "mcp__mcp-atlassian__jira_remove_issue_link",
    "mcp__mcp-atlassian__jira_get_issue_dates",
    "mcp__mcp-atlassian__jira_batch_get_changelogs",
    "mcp__mcp-atlassian__jira_download_attachments",
    "mcp__mcp-atlassian__jira_get_issue_images",
    # Agile / sprints
    "mcp__mcp-atlassian__jira_get_agile_boards",
    "mcp__mcp-atlassian__jira_get_board_issues",
    "mcp__mcp-atlassian__jira_get_sprints_from_board",
    "mcp__mcp-atlassian__jira_get_sprint_issues",
    "mcp__mcp-atlassian__jira_create_sprint",
    "mcp__mcp-atlassian__jira_update_sprint",
    "mcp__mcp-atlassian__jira_add_issues_to_sprint",
    # Versions
    "mcp__mcp-atlassian__jira_create_version",
    "mcp__mcp-atlassian__jira_batch_create_versions",
    # GitLab — @zereight/mcp-gitlab (all tools, prefix match like pyclaudir).
    # Unlike mcp-atlassian (which bundles Jira+Confluence+Compass), mcp-gitlab
    # is GitLab-only so a blanket prefix is safe.
    "mcp__mcp-gitlab",
    # Web
    "WebFetch",
    "WebSearch",
)

#: Forbidden flag — never pass this. ``build_argv`` enforces it at build
#: time and the worker re-asserts it at spawn time.
FORBIDDEN_FLAG = "--dangerously-skip-permissions"


@dataclass(frozen=True)
class CcSpawnSpec:
    binary: str
    model: str
    system_prompt_path: Path
    mcp_config_path: Path
    json_schema_path: Path
    project_prompt_path: Path | None = None
    effort: str = "high"
    session_id: str | None = None
    #: If set, raw stdout/stderr from the CC subprocess is appended to
    #: ``<cc_logs_dir>/<session_id>.stream.jsonl`` and ``<session_id>.stderr.log``
    #: as the data arrives. Set to ``None`` to disable raw capture.
    cc_logs_dir: Path | None = None


@dataclass
class TurnResult:
    """One full conversational turn from the CC subprocess."""

    text_blocks: list[str] = field(default_factory=list)
    control: ControlAction | None = None
    #: Stderr lines captured during the turn (most recent last).
    stderr_tail: list[str] = field(default_factory=list)
    #: True iff CC produced text without ever calling ``send_message``.
    dropped_text: bool = False


def build_argv(spec: CcSpawnSpec) -> list[str]:
    """Construct the exact argv we hand to ``asyncio.create_subprocess_exec``.

    Pinned by ``tests/test_security_invariants.py``.
    """
    if not spec.system_prompt_path.exists():
        raise FileNotFoundError(spec.system_prompt_path)
    if not spec.mcp_config_path.exists():
        raise FileNotFoundError(spec.mcp_config_path)
    if not spec.json_schema_path.exists():
        raise FileNotFoundError(spec.json_schema_path)

    runtime_block = (
        "# Runtime\n\n"
        "You are running with:\n"
        f"- model: `{spec.model}`\n"
        f"- effort: `{spec.effort}`\n\n"
        "If a user asks which model or effort level you are running on, "
        "answer honestly with these exact values. This is public info — "
        "the hard boundary against revealing internal config does not apply "
        "to these two fields.\n"
    )
    system_prompt = spec.system_prompt_path.read_text(encoding="utf-8")
    if spec.project_prompt_path and spec.project_prompt_path.exists():
        system_prompt += "\n\n" + spec.project_prompt_path.read_text(encoding="utf-8")
    system_prompt += "\n\n" + runtime_block
    json_schema = spec.json_schema_path.read_text(encoding="utf-8")
    json.loads(json_schema)  # sanity check

    argv: list[str] = [
        spec.binary,
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--model", spec.model,
        "--effort", spec.effort,
        "--system-prompt", system_prompt,
        "--mcp-config", str(spec.mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
        "--json-schema", json_schema,
    ]
    if spec.session_id:
        argv += ["--resume", spec.session_id]

    if FORBIDDEN_FLAG in argv:
        raise RuntimeError(
            f"refusing to build argv containing {FORBIDDEN_FLAG!r}; this flag "
            "is forbidden in pyclaudir under all circumstances"
        )
    return argv


class CrashLoop(RuntimeError):
    """Raised when the CC subprocess crashes too often to recover."""


class CcWorker:
    """Manage one ``claude`` subprocess and pump messages through it.

    Crash recovery: if the subprocess exits unexpectedly we record the time,
    sleep with exponential backoff (2s → 64s cap), and respawn with the
    same ``session_id`` so conversation context is preserved. If 10 crashes
    happen within a 10-minute window we raise :class:`CrashLoop` so the
    OS-level supervisor (systemd) can restart the entire process.
    """

    CRASH_BACKOFF_BASE = 2.0
    CRASH_BACKOFF_CAP = 64.0
    CRASH_LIMIT = 10
    CRASH_WINDOW_SECONDS = 600.0

    #: Optional callback the supervisor calls when CC crashes.
    #: Signature: ``async on_crash(stderr_tail: list[str], attempt: int, backoff: float)``
    OnCrash = Any  # Callable[[list[str], int, float], Awaitable[None]] | None

    def __init__(
        self, spec: CcSpawnSpec, *, heartbeat: Heartbeat | None = None,
        on_crash: OnCrash = None,
    ) -> None:
        self.spec = spec
        self.heartbeat = heartbeat or Heartbeat()
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._result_queue: asyncio.Queue[TurnResult] = asyncio.Queue()
        self._inject_queue: asyncio.Queue[str] = asyncio.Queue()
        self._stderr_tail: list[str] = []
        self._current_turn: TurnResult | None = None
        self._session_id: str | None = spec.session_id
        self._crash_times: list[float] = []
        self._supervisor_task: asyncio.Task | None = None
        self._stop_supervisor = asyncio.Event()
        self._on_crash = on_crash
        # Raw-capture state. We open with "pending-<ts>" names if we don't
        # know the session id at start time, then rename to "<sid>.*" once
        # the system/init event tells us.
        self._stream_log: IO[str] | None = None
        self._stream_log_path: Path | None = None
        self._stderr_log: IO[str] | None = None
        self._stderr_log_path: Path | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        argv = build_argv(self.spec)
        # Re-assert at spawn time, even though build_argv already checked.
        assert FORBIDDEN_FLAG not in argv, (
            f"{FORBIDDEN_FLAG} found in argv at spawn time — refusing to start"
        )
        log.info(
            "spawning claude (model=%s, allowed=%s, disallowed=%s)",
            self.spec.model, ALLOWED_TOOLS, DISALLOWED_TOOLS,
        )
        self._open_raw_logs()
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
            limit=4 * 1024 * 1024,  # 4 MiB – large MCP responses (e.g. GitLab)
        )
        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name="cc-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="cc-stderr"
        )

    # ------------------------------------------------------------------
    # Raw stdout/stderr capture
    # ------------------------------------------------------------------

    def _open_raw_logs(self) -> None:
        """Open the per-session raw-capture files in append mode.

        If we know the session id (resume case) we open with the final name.
        Otherwise we open with a ``pending-<ts>`` name and rename later when
        :meth:`_handle_event` sees the system/init payload.
        """
        if self.spec.cc_logs_dir is None:
            return
        self.spec.cc_logs_dir.mkdir(parents=True, exist_ok=True)
        sid = self._session_id
        prefix = sid if sid else f"pending-{int(time.time() * 1000)}"
        self._stream_log_path = self.spec.cc_logs_dir / f"{prefix}.stream.jsonl"
        self._stderr_log_path = self.spec.cc_logs_dir / f"{prefix}.stderr.log"
        try:
            self._stream_log = self._stream_log_path.open("a", encoding="utf-8")
            self._stderr_log = self._stderr_log_path.open("a", encoding="utf-8")
            log.info(
                "raw cc capture: stream=%s stderr=%s",
                self._stream_log_path.name, self._stderr_log_path.name,
            )
        except OSError:
            log.exception("failed to open cc raw-capture files; capture disabled")
            self._stream_log = None
            self._stderr_log = None

    def _close_raw_logs(self) -> None:
        for handle in (self._stream_log, self._stderr_log):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # pragma: no cover
                    log.exception("error closing cc raw log")
        self._stream_log = None
        self._stderr_log = None

    def _maybe_rename_raw_logs(self) -> None:
        """Rename ``pending-*`` files to ``<session_id>.*`` once we learn it."""
        if self._session_id is None:
            return
        if self._stream_log_path is None or self._stderr_log_path is None:
            return
        if not self._stream_log_path.name.startswith("pending-"):
            return
        if self.spec.cc_logs_dir is None:
            return
        new_stream = self.spec.cc_logs_dir / f"{self._session_id}.stream.jsonl"
        new_stderr = self.spec.cc_logs_dir / f"{self._session_id}.stderr.log"
        try:
            # Close, rename, reopen in append mode so the file handle
            # continues to point at the renamed file. macOS would let us
            # rename without closing, but we close to keep the code portable
            # to platforms that lock open files.
            for handle in (self._stream_log, self._stderr_log):
                if handle is not None:
                    handle.flush()
                    handle.close()
            # If a previous run already created files for this session id
            # (resume case after a crash), we append to them by deleting
            # the empty pending file and reopening the existing one.
            if new_stream.exists():
                self._stream_log_path.unlink(missing_ok=True)
            else:
                self._stream_log_path.rename(new_stream)
            if new_stderr.exists():
                self._stderr_log_path.unlink(missing_ok=True)
            else:
                self._stderr_log_path.rename(new_stderr)
            self._stream_log_path = new_stream
            self._stderr_log_path = new_stderr
            self._stream_log = new_stream.open("a", encoding="utf-8")
            self._stderr_log = new_stderr.open("a", encoding="utf-8")
            log.info("raw cc capture renamed to %s", new_stream.name)
        except OSError:
            log.exception("failed to rename raw cc capture files")
            self._stream_log = None
            self._stderr_log = None

    def _write_stream_line(self, raw: bytes) -> None:
        if self._stream_log is None:
            return
        try:
            self._stream_log.write(raw.decode("utf-8", errors="replace"))
            if not raw.endswith(b"\n"):
                self._stream_log.write("\n")
            self._stream_log.flush()
        except Exception:  # pragma: no cover
            log.exception("failed to write to cc stream log")

    def _write_stderr_line(self, decoded: str) -> None:
        if self._stderr_log is None:
            return
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._stderr_log.write(f"{ts} {decoded}\n")
            self._stderr_log.flush()
        except Exception:  # pragma: no cover
            log.exception("failed to write to cc stderr log")

    async def stop(self) -> None:
        self._stop_supervisor.set()
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor_task = None
        await self._terminate_proc()

    async def _terminate_proc(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        finally:
            for t in (self._stdout_task, self._stderr_task):
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            self._proc = None
            self._stdout_task = None
            self._stderr_task = None
            self._close_raw_logs()

    # ------------------------------------------------------------------
    # Crash supervisor
    # ------------------------------------------------------------------

    async def supervise(self) -> None:
        """Background loop that watches the subprocess and respawns on crash.

        Call this once after :meth:`start`. Returns when the subprocess exits
        cleanly or when ``stop()`` is called.
        """
        self._supervisor_task = asyncio.create_task(
            self._supervise_loop(), name="cc-supervisor"
        )

    async def _supervise_loop(self) -> None:
        import time

        while not self._stop_supervisor.is_set():
            if self._proc is None:
                await asyncio.sleep(0.05)
                continue
            rc = await self._proc.wait()
            if self._stop_supervisor.is_set():
                return

            log.warning(
                "cc subprocess exited rc=%s; recent stderr=%s",
                rc, self._stderr_tail[-5:],
            )

            now = time.monotonic()
            self._crash_times = [t for t in self._crash_times if now - t < self.CRASH_WINDOW_SECONDS]
            self._crash_times.append(now)
            if len(self._crash_times) >= self.CRASH_LIMIT:
                raise CrashLoop(
                    f"cc subprocess crashed {self.CRASH_LIMIT} times in "
                    f"{self.CRASH_WINDOW_SECONDS:.0f}s; bailing out"
                )

            attempt = len(self._crash_times)
            backoff = min(
                self.CRASH_BACKOFF_CAP,
                self.CRASH_BACKOFF_BASE * (2 ** (attempt - 1)),
            )
            log.warning("respawning cc in %.1fs (attempt %d)", backoff, attempt)
            if self._on_crash is not None:
                try:
                    await self._on_crash(list(self._stderr_tail), attempt, backoff)
                except Exception:
                    log.debug("on_crash callback failed", exc_info=True)
            await asyncio.sleep(backoff)
            await self._terminate_proc()
            await self.start()

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, text: str) -> None:
        """Write one stream-json user message to stdin."""
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("cc worker not started")
        self._current_turn = TurnResult()
        log_cc_user(text)
        envelope = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }
        line = json.dumps(envelope) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def wait_for_result(self) -> TurnResult:
        return await self._result_queue.get()

    async def inject(self, text: str) -> None:
        """Send additional user content to a running turn.

        We can't *literally* push text into a turn that's already mid-stream
        in CC's protocol — what we do instead is write a fresh user envelope
        to stdin so the next reasoning step in the current turn sees it.
        Claude Code reads stdin at message boundaries, so the inject lands as
        soon as the model finishes its current step.
        """
        if self._proc is None or self._proc.stdin is None:
            await self._inject_queue.put(text)
            return
        envelope = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }
        line = json.dumps(envelope) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.warning("inject failed: stdin closed; queueing for next turn")
            await self._inject_queue.put(text)

    async def drain_inject_queue(self) -> list[str]:
        """Pop everything queued during a stalled inject window."""
        out: list[str] = []
        while not self._inject_queue.empty():
            out.append(self._inject_queue.get_nowait())
        return out

    # ------------------------------------------------------------------
    # Background readers
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                # Capture the raw bytes *before* parsing so even malformed
                # or partial events are preserved on disk.
                self._write_stream_line(line)
                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    log.debug("cc stdout non-json line: %r", line[:200])
                    continue
                self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("cc stdout reader crashed")

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self._stderr_tail.append(decoded)
                self._stderr_tail = self._stderr_tail[-10:]
                self._write_stderr_line(decoded)
                if "rate limit" in decoded.lower() or "quota" in decoded.lower():
                    log.warning("cc stderr: %s", decoded)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("cc stderr reader crashed")

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Parse one stream-json event from the CC subprocess.

        Stream-json events come in several shapes; we only care about a few:

        - ``{"type": "system", "subtype": "init", "session_id": "..."}``
          — captured so we can persist + resume.
        - ``{"type": "assistant", "message": {"content": [...]}}`` — text and
          tool-use blocks.
        - ``{"type": "result", ...}`` — turn finished. The structured-output
          payload is parsed into a :class:`ControlAction`.
        """
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if isinstance(sid, str):
                self._session_id = sid
                log.info("cc session id %s", sid)
                self._maybe_rename_raw_logs()
            return

        if self._current_turn is None:
            self._current_turn = TurnResult()

        if etype == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    txt = block.get("text", "")
                    if txt:
                        self._current_turn.text_blocks.append(txt)
                        log_cc_text(txt)
                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input")
                    log_cc_tool_use(
                        tool_name=tool_name,
                        tool_use_id=str(block.get("id", "")),
                        args=tool_input,
                    )
                    # StructuredOutput is the definitive turn-end signal.
                    # Claudir confirmed: the action lives in the tool_use
                    # event's input field, NOT in the result event payload.
                    if tool_name == "StructuredOutput" and isinstance(tool_input, dict):
                        try:
                            self._current_turn.control = ControlAction.model_validate(tool_input)
                        except Exception:
                            log.warning(
                                "could not parse StructuredOutput input: %r",
                                tool_input,
                            )
                elif btype == "thinking":
                    # Extended-thinking blocks (visible only with the right
                    # model + flag). Treat like text but with its own tag.
                    log_cc_text("(thinking) " + block.get("thinking", ""))
            return

        if etype == "user":
            # The other half of the channel: tool_result blocks the runtime
            # injects back into the conversation as a synthetic user message.
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "tool_result":
                    raw = block.get("content")
                    if isinstance(raw, list):
                        # Sometimes a list of {"type":"text","text":...}
                        text = " ".join(
                            (b.get("text", "") if isinstance(b, dict) else str(b))
                            for b in raw
                        )
                    else:
                        text = "" if raw is None else str(raw)
                    log_cc_tool_result(
                        tool_use_id=str(block.get("tool_use_id", "")),
                        content=text,
                        is_error=bool(block.get("is_error", False)),
                    )
            return

        if etype == "result":
            # Structured output is delivered in event["result"] when the
            # JSON schema is enforced. Older CC versions stream it via
            # event["output"] or stuff it into the last text block.
            payload = (
                event.get("result")
                or event.get("output")
                or (self._current_turn.text_blocks[-1] if self._current_turn.text_blocks else None)
            )
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, dict):
                try:
                    self._current_turn.control = ControlAction.model_validate(payload)
                except Exception:
                    log.warning("could not parse control action from %r", payload)
            self._current_turn.stderr_tail = list(self._stderr_tail)
            self._current_turn.dropped_text = (
                bool(self._current_turn.text_blocks) and self._current_turn.control is None
            )
            ctrl = self._current_turn.control
            log_cc_result(
                action=ctrl.action if ctrl else None,
                reason=ctrl.reason if ctrl else None,
            )
            self._result_queue.put_nowait(self._current_turn)
            self._current_turn = None
            return

        # Anything else (tool_use, tool_result, ping) is ignored at this
        # layer — it's already produced its side effects via the MCP server.


def make_session_id() -> str:
    return str(uuid.uuid4())
