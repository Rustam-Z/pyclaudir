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

from .config import Config
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
    # NOTE: "Agent" (subagent spawning) is *not* in this static tuple.
    # It is conditionally injected by ``build_argv`` when the spawn spec
    # has ``enable_subagents=True`` (driven by PYCLAUDIR_ENABLE_SUBAGENTS).
    # When disabled — the default — it is appended to ``--disallowedTools``
    # instead, because Claude Code treats tools absent from both lists as
    # implicitly reachable (the agent can find them via ToolSearch).
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
    #: When True, the ``Agent`` tool is added to ``--allowedTools`` and the
    #: subagent documentation (``subagents_prompt_path``) is appended to the
    #: system prompt. When False (default), ``Agent`` is added to
    #: ``--disallowedTools`` and the docs file is not read — Nodira cannot
    #: spawn subagents and doesn't even see the capability. Subagent turns
    #: are token-heavy; keep off unless you need them. Driven by
    #: ``PYCLAUDIR_ENABLE_SUBAGENTS``.
    enable_subagents: bool = False
    #: Path to the subagent docs markdown. Read and appended to the system
    #: prompt iff ``enable_subagents`` is True. Ignored otherwise.
    subagents_prompt_path: Path | None = None


@dataclass
class TurnResult:
    """One full conversational turn from the CC subprocess."""

    text_blocks: list[str] = field(default_factory=list)
    control: ControlAction | None = None
    #: Stderr lines captured during the turn (most recent last).
    stderr_tail: list[str] = field(default_factory=list)
    #: True iff CC produced text without ever calling ``send_message``.
    dropped_text: bool = False
    #: Non-None when pyclaudir short-circuited this turn (e.g.
    #: ``"tool-error-limit"``). Engine branches on this before treating
    #: the result as a normal turn completion.
    aborted_reason: str | None = None


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
    if spec.enable_subagents:
        if spec.subagents_prompt_path is None or not spec.subagents_prompt_path.exists():
            raise FileNotFoundError(
                "enable_subagents=True but subagents_prompt_path is missing: "
                f"{spec.subagents_prompt_path!r}"
            )
        system_prompt += "\n\n" + spec.subagents_prompt_path.read_text(encoding="utf-8")
    json_schema = spec.json_schema_path.read_text(encoding="utf-8")
    json.loads(json_schema)  # sanity check

    # Subagent gating: when enabled, Agent goes in allow; when disabled,
    # Agent goes in deny (belt-and-braces, because unlisted tools are
    # still implicitly reachable via ToolSearch).
    if spec.enable_subagents:
        allowed_tools = ALLOWED_TOOLS + ("Agent",)
        disallowed_tools = DISALLOWED_TOOLS
    else:
        allowed_tools = ALLOWED_TOOLS
        disallowed_tools = DISALLOWED_TOOLS + ("Agent",)

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
        "--allowedTools", ",".join(allowed_tools),
        "--disallowedTools", ",".join(disallowed_tools),
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
    sleep with exponential backoff (``crash_backoff_base`` → ``crash_backoff_cap``,
    defaults 2s → 64s), and respawn with the same ``session_id`` so the
    conversation context is preserved. If ``crash_limit`` crashes happen
    within ``crash_window_seconds`` (defaults 10 / 600s) we raise
    :class:`CrashLoop` so the OS-level supervisor (systemd, docker
    restart-policy) can restart the entire process. All four thresholds
    flow through :class:`pyclaudir.config.Config`.
    """

    #: Optional callback the supervisor calls when CC crashes (one per
    #: crash, before the backoff/respawn).
    #: Signature: ``async on_crash(stderr_tail: list[str], attempt: int, backoff: float)``
    OnCrash = Any  # Callable[[list[str], int, float], Awaitable[None]] | None
    #: Optional callback the supervisor calls *once* when the crash loop
    #: has exhausted its budget (``Config.crash_limit`` crashes in
    #: ``Config.crash_window_seconds``) — a terminal condition: no further
    #: respawn will be attempted by this worker. The operator needs to
    #: intervene. Fires before the underlying :class:`CrashLoop` is
    #: re-raised so the callback can notify the owner/users even though
    #: the worker is about to die.
    #: Signature: ``async on_giveup(stderr_tail: list[str], crash_count: int)``
    OnGiveup = Any  # Callable[[list[str], int], Awaitable[None]] | None

    def __init__(
        self,
        spec: CcSpawnSpec,
        config: Config,
        *,
        heartbeat: Heartbeat | None = None,
        on_crash: OnCrash = None,
        on_giveup: OnGiveup = None,
    ) -> None:
        self.spec = spec
        self.heartbeat = heartbeat or Heartbeat()
        # Cache the runtime knobs once at construction so the hot paths
        # (``_liveness_loop``, ``_record_tool_error``, ``_supervise_loop``)
        # don't re-read the config dataclass on every event. Tests can
        # override the cached attributes directly
        # (``worker._tool_error_max_count = 99``) for fine-grained
        # control without rebuilding the Config.
        self._liveness_timeout: float = config.liveness_timeout_seconds
        self._liveness_poll: float = config.liveness_poll_seconds
        self._tool_error_max_count: int = config.tool_error_max_count
        self._tool_error_window: float = config.tool_error_window_seconds
        self._crash_backoff_base: float = config.crash_backoff_base
        self._crash_backoff_cap: float = config.crash_backoff_cap
        self._crash_limit: int = config.crash_limit
        self._crash_window_seconds: float = config.crash_window_seconds
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
        self._liveness_task: asyncio.Task | None = None
        self._stop_supervisor = asyncio.Event()
        self._on_crash = on_crash
        self._on_giveup = on_giveup
        #: ``time.monotonic()`` of the last successfully parsed stdout
        #: event. Together with ``heartbeat.last_activity`` (bumped on
        #: every MCP tool call) this tells the liveness monitor whether
        #: the subprocess is alive-and-working or actually wedged.
        self._last_event_at: float = time.monotonic()
        #: Tool-error circuit-breaker state. Reset in ``send()`` at the
        #: start of every turn; incremented in ``_handle_event`` each
        #: time a ``tool_result`` block arrives with ``is_error=true``.
        #: The breaker trips on whichever fires first: the count
        #: reaches ``_tool_error_max_count``, OR the wall-clock
        #: watchdog fires at ``_turn_first_tool_error_at +
        #: _tool_error_window``. The watchdog handles the
        #: "single error, then silence" case where no further errors
        #: arrive to drive an event-based check.
        self._turn_tool_error_count: int = 0
        self._turn_first_tool_error_at: float | None = None
        self._tool_error_watchdog_task: asyncio.Task | None = None
        #: Set just before ``_terminate_proc`` when the worker aborts a
        #: turn for a known reason (e.g. ``"tool-error-limit"``). The
        #: engine reads this after ``wait_for_result`` raises so it can
        #: tailor the user-facing message. One-shot: cleared on read.
        self._last_abort_reason: str | None = None
        self._tool_error_abort_task: asyncio.Task | None = None
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

    def consume_abort_reason(self) -> str | None:
        """Return the reason for the most recent aborted turn, if any.

        One-shot: the value is cleared after this read so the next turn
        starts with a clean slate. The engine calls this after
        ``wait_for_result`` raises, to pick the user-facing message.
        """
        reason = self._last_abort_reason
        self._last_abort_reason = None
        return reason

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
        self._cancel_tool_error_watchdog()
        if self._liveness_task and not self._liveness_task.done():
            self._liveness_task.cancel()
            try:
                await self._liveness_task
            except (asyncio.CancelledError, Exception):
                pass
            self._liveness_task = None
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

        Also starts a liveness monitor that detects a wedged-mid-turn
        subprocess (no stdout events, no MCP heartbeat activity, for
        ``Config.liveness_timeout_seconds``) and kills it so the
        supervisor respawns it.

        Call this once after :meth:`start`. Returns when the subprocess
        exits cleanly or when ``stop()`` is called.
        """
        self._supervisor_task = asyncio.create_task(
            self._supervise_loop(), name="cc-supervisor"
        )
        self._liveness_task = asyncio.create_task(
            self._liveness_loop(), name="cc-liveness"
        )

    async def _liveness_loop(self) -> None:
        """Detect wedged subprocesses and terminate them.

        Only fires when all three conditions hold:
        1. The subprocess is running (``is_running``).
        2. A turn is currently in progress (``_current_turn is not None``).
           Idle silence is fine — we only care about stuck turns.
        3. Time since the most recent activity signal exceeds
           ``self._liveness_timeout``. Activity signal is the max of
           ``_last_event_at`` (stdout parse time) and
           ``heartbeat.last_activity`` (bumped on every MCP tool call).

        On wedge, we call ``_terminate_proc()`` — the supervisor's
        existing crash-recovery path respawns with the same session id.
        """
        timeout = self._liveness_timeout

        while not self._stop_supervisor.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_supervisor.wait(), timeout=self._liveness_poll,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass

            if not self.is_running:
                continue
            if self._current_turn is None:
                continue  # idle — silence is expected
            now = time.monotonic()
            last_activity = max(self._last_event_at, self.heartbeat.last_activity)
            silence = now - last_activity
            if silence <= timeout:
                continue

            log.warning(
                "cc subprocess wedged mid-turn: no activity for %.0fs "
                "(timeout=%.0fs). Terminating to trigger respawn.",
                silence, timeout,
            )
            # Terminate. The supervisor's wait() will wake up and the
            # crash-recovery path respawns automatically.
            await self._terminate_proc()

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
            self._crash_times = [
                t for t in self._crash_times if now - t < self._crash_window_seconds
            ]
            self._crash_times.append(now)
            if len(self._crash_times) >= self._crash_limit:
                # Terminal — fire the giveup callback so the operator
                # and active users learn the bot is actually down, then
                # re-raise so the supervisor task exits. Callback errors
                # are swallowed so a buggy callback can't mask CrashLoop.
                if self._on_giveup is not None:
                    try:
                        await self._on_giveup(
                            list(self._stderr_tail), len(self._crash_times),
                        )
                    except Exception:
                        log.debug("on_giveup callback failed", exc_info=True)
                raise CrashLoop(
                    f"cc subprocess crashed {self._crash_limit} times in "
                    f"{self._crash_window_seconds:.0f}s; bailing out"
                )

            attempt = len(self._crash_times)
            backoff = min(
                self._crash_backoff_cap,
                self._crash_backoff_base * (2 ** (attempt - 1)),
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
        self._turn_tool_error_count = 0
        self._turn_first_tool_error_at = None
        self._cancel_tool_error_watchdog()
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

        Event-driven: writes a fresh user envelope directly to CC's stdin
        and returns as soon as the OS accepts the bytes (typically
        microseconds). No polling, no queue in the hot path — claudir's
        1s inject poll doesn't apply here. CC reads stdin at message
        boundaries, so the inject lands at the next reasoning step.

        The ``_inject_queue`` is a fallback for the narrow windows when
        stdin is unavailable (proc not started yet, or ``BrokenPipeError``
        during a crash-restart). Callers must not assume queued items
        will be replayed automatically.
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
                self._last_event_at = time.monotonic()
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

    def _record_tool_error(self) -> None:
        """Record one ``tool_result`` with ``is_error=true``.

        Trips the breaker on whichever fires first within a turn:
        ``_tool_error_max_count`` errors (count branch), or
        ``_tool_error_window`` seconds elapsed since the first error
        (watchdog branch — see :meth:`_tool_error_watchdog`). The
        watchdog covers the "single stuck error, then silence" case
        where no further errors arrive to drive an event-based check.

        On trip: schedule ``_terminate_proc`` so the crash-recovery
        path respawns and the user sees ``_on_cc_crash``'s notice.
        """
        now = time.monotonic()
        self._turn_tool_error_count += 1

        if self._turn_first_tool_error_at is None:
            self._turn_first_tool_error_at = now
            deadline = now + self._tool_error_window
            self._tool_error_watchdog_task = asyncio.create_task(
                self._tool_error_watchdog(deadline),
                name="cc-tool-error-watchdog",
            )

        if self._turn_tool_error_count >= self._tool_error_max_count:
            self._trip_tool_error_breaker(reason="count")

    async def _tool_error_watchdog(self, deadline: float) -> None:
        """Wall-clock companion to the count branch.

        Sleeps until ``deadline``. If the breaker hasn't already
        tripped via the count branch (or been cancelled because the
        turn ended successfully), trips now.
        """
        delay = max(0.0, deadline - time.monotonic())
        await asyncio.sleep(delay)
        self._trip_tool_error_breaker(reason="window")

    def _cancel_tool_error_watchdog(self) -> None:
        """Cancel the per-turn tool-error watchdog if it's still
        armed. Idempotent — safe to call from ``send()`` (new turn
        reset), ``stop()`` (shutdown), or the result-event handler
        (turn finished cleanly)."""
        task = self._tool_error_watchdog_task
        if task is not None and not task.done():
            task.cancel()
        self._tool_error_watchdog_task = None

    def _trip_tool_error_breaker(self, *, reason: str) -> None:
        """Idempotent breaker trip. ``reason`` is ``"count"`` or
        ``"window"`` — used for logging only; the abort reason
        surfaced to the engine remains ``"tool-error-limit"``.
        """
        if (
            self._tool_error_abort_task is not None
            and not self._tool_error_abort_task.done()
        ):
            return  # already aborting
        self._cancel_tool_error_watchdog()

        elapsed = (
            time.monotonic() - self._turn_first_tool_error_at
            if self._turn_first_tool_error_at is not None
            else 0.0
        )
        log.warning(
            "cc tool-error circuit breaker tripped (reason=%s): "
            "%d errors in %.1fs (max=%d, window=%.0fs). "
            "Terminating to trigger respawn.",
            reason, self._turn_tool_error_count, elapsed,
            self._tool_error_max_count, self._tool_error_window,
        )
        self._last_abort_reason = "tool-error-limit"
        # Unblock the engine's ``wait_for_result`` immediately with a
        # sentinel TurnResult — the supervisor's respawn path can
        # take seconds, we don't want the user waiting on it.
        sentinel = TurnResult(aborted_reason="tool-error-limit")
        sentinel.stderr_tail = list(self._stderr_tail)
        self._result_queue.put_nowait(sentinel)
        self._current_turn = None
        self._tool_error_abort_task = asyncio.create_task(
            self._terminate_proc(), name="cc-tool-error-abort",
        )

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
                    is_error = bool(block.get("is_error", False))
                    log_cc_tool_result(
                        tool_use_id=str(block.get("tool_use_id", "")),
                        content=text,
                        is_error=is_error,
                    )
                    if is_error:
                        self._record_tool_error()
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
            # Turn finished cleanly; defuse the watchdog so a stale
            # deadline from this turn can't trip the breaker after
            # the fact.
            self._cancel_tool_error_watchdog()
            self._result_queue.put_nowait(self._current_turn)
            self._current_turn = None
            return

        # Anything else (tool_use, tool_result, ping) is ignored at this
        # layer — it's already produced its side effects via the MCP server.


def make_session_id() -> str:
    return str(uuid.uuid4())
