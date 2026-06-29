"""Claude Code subprocess worker.

A long-lived asyncio task that wraps a single ``claude`` child process. The
worker is the *only* place in hamroh allowed to call
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
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from typing import Awaitable, Callable

from ..config import Config
from ..tools.base import Heartbeat
from ..transcript import log_cc_user
from .event_handlers import CcEventHandlerMixin
from .events import CrashLoop, TurnResult
from .raw_capture import RawCapture
from .spec import CcSpawnSpec, FORBIDDEN_FLAG, build_argv

#: Supervisor lifecycle callbacks. Each is optional (``None`` disables it).
#:
#: ``OnCrash`` — called when CC crashes (one per crash, before the
#: backoff/respawn). Only fires for *unexpected* exits; intentional
#: terminations (tool-error breaker, liveness watchdog) are recognised via
#: ``_supervisor_abort_reason`` and do not reach it.
#: Signature: ``async on_crash(attempt: int, backoff: float)``.
OnCrash = Callable[[int, float], Awaitable[None]] | None
#: ``OnGiveup`` — called *once* when the crash loop has exhausted its budget,
#: before :class:`CrashLoop` is re-raised, so it can notify the owner/users.
#: Signature: ``async on_giveup(crash_count: int)``.
OnGiveup = Callable[[int], Awaitable[None]] | None
#: ``OnStaleSession`` — called when the supervisor sees CC reject the resumed
#: ``session_id`` as stale (see :data:`STALE_SESSION_PATTERNS`), *before* the
#: fresh respawn, so it can drop the persisted id and notify the owner. Stale
#: recoveries do not consume the crash budget.
#: Signature: ``async on_stale_session(stale_id: str)``.
OnStaleSession = Callable[[str], Awaitable[None]] | None
#: ``OnStatus`` — fired by the per-turn status heartbeat every
#: ``status_interval_seconds`` while a turn is still running, so a long task
#: can report progress instead of going silent; the turn keeps running.
#: Signature: ``async on_status(elapsed_s: float, last_action: str | None)``.
OnStatus = Callable[[float, "str | None"], Awaitable[None]] | None


@dataclasses.dataclass(frozen=True)
class WorkerHooks:
    """Optional liveness heartbeat + supervisor lifecycle callbacks for
    :class:`CcWorker`. All default to ``None`` so a test can build a worker
    from just a spec and config."""

    heartbeat: Heartbeat | None = None
    on_crash: OnCrash = None
    on_giveup: OnGiveup = None
    on_stale_session: OnStaleSession = None
    on_status: OnStatus = None


# Pinned to the parent package name so log captures keyed on
# ``"hamroh.cc_worker"`` (e.g. tests/test_cc_worker_mcp_init.py) keep
# matching after the module split.
log = logging.getLogger("hamroh.cc_worker")

#: Substrings that, when seen in CC's stderr, indicate the resumed
#: ``session_id`` is unusable — either pruned/expired (first pattern)
#: or malformed / not a known session title (second pattern). Both
#: cases are recoverable by dropping the persisted id and starting a
#: fresh session. Match is case-sensitive substring; expand only when
#: a different wording is observed in the wild. Verified against
#: claude 2.1.138.
STALE_SESSION_PATTERNS: tuple[str, ...] = (
    "No conversation found with session ID",
    "--resume requires a valid session ID",
)


class CcWorker(CcEventHandlerMixin):
    """Manage one ``claude`` subprocess and pump messages through it.

    Crash recovery: if the subprocess exits unexpectedly we record the time,
    sleep with exponential backoff (``crash_backoff_base`` → ``crash_backoff_cap``,
    defaults 2s → 64s), and respawn with the same ``session_id`` so the
    conversation context is preserved. If ``crash_limit`` crashes happen
    within ``crash_window_seconds`` (defaults 10 / 600s) we raise
    :class:`CrashLoop` so the OS-level supervisor (systemd, docker
    restart-policy) can restart the entire process. All four thresholds
    flow through :class:`hamroh.config.Config`.
    """

    def __init__(
        self, spec: CcSpawnSpec, config: Config, hooks: WorkerHooks = WorkerHooks()
    ) -> None:
        self.spec = spec
        self.heartbeat = hooks.heartbeat or Heartbeat()
        self._session_id_path = config.session_id_path
        self._session_id: str | None = spec.session_id
        self._on_crash = hooks.on_crash
        self._on_giveup = hooks.on_giveup
        self._on_stale_session = hooks.on_stale_session
        self._on_status = hooks.on_status
        #: Raw stdout/stderr capture — no-ops when ``spec.cc_logs_dir`` is None.
        self._capture = RawCapture(spec.cc_logs_dir)
        self._cache_config(config)
        self._init_io_state()
        self._init_turn_state()

    def _cache_config(self, config: Config) -> None:
        """Snapshot the runtime knobs once so the hot paths (liveness, breaker,
        supervisor) never re-read the Config dataclass per event. Tests can
        override these attributes directly without rebuilding a Config."""
        self._liveness_timeout: float = config.liveness_timeout_seconds
        self._liveness_poll: float = config.liveness_poll_seconds
        self._tool_error_max_count: int = config.tool_error_max_count
        self._tool_error_window: float = config.tool_error_window_seconds
        self._status_interval: float = config.status_interval_seconds
        self._crash_backoff_base: float = config.crash_backoff_base
        self._crash_backoff_cap: float = config.crash_backoff_cap
        self._crash_limit: int = config.crash_limit
        self._crash_window_seconds: float = config.crash_window_seconds

    def _init_io_state(self) -> None:
        """Subprocess handles, stdout/stderr pump tasks, the result/inject
        queues, and the supervisor/liveness coordination primitives."""
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._result_queue: asyncio.Queue[TurnResult] = asyncio.Queue()
        self._inject_queue: asyncio.Queue[str] = asyncio.Queue()
        self._stderr_tail: list[str] = []
        self._supervisor_task: asyncio.Task | None = None
        self._liveness_task: asyncio.Task | None = None
        self._stop_supervisor = asyncio.Event()
        self._crash_times: list[float] = []
        #: ``time.monotonic()`` of the last parsed stdout event; with
        #: ``heartbeat.last_activity`` it tells the liveness monitor whether the
        #: subprocess is working or wedged.
        self._last_event_at: float = time.monotonic()

    def _init_turn_state(self) -> None:
        """Per-turn state: the in-flight TurnResult, the init-gate, the
        tool-error circuit breaker, and the status-heartbeat bookkeeping.

        ``_awaiting_turn_init`` is armed by ``send()`` and cleared by the next
        ``system/init``; while armed, assistant/result events from a prior
        draining turn are dropped so they aren't folded into the new turn. The
        breaker (``_turn_tool_error_*``) trips on whichever comes first: the
        count reaching ``_tool_error_max_count`` or the wall-clock watchdog at
        ``_turn_first_tool_error_at + _tool_error_window``.
        ``_supervisor_abort_reason`` marks a self-inflicted exit so the
        supervisor skips the crash callback.
        """
        self._current_turn: TurnResult | None = None
        self._awaiting_turn_init: bool = False
        self._turn_tool_error_count: int = 0
        self._turn_first_tool_error_at: float | None = None
        self._tool_error_watchdog_task: asyncio.Task | None = None
        self._supervisor_abort_reason: str | None = None
        self._tool_error_abort_task: asyncio.Task | None = None
        self._turn_started_at: float | None = None
        self._last_tool_action: str | None = None
        self._status_task: asyncio.Task | None = None

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
        enabled_features = [
            f
            for f, on in (
                ("bash", self.spec.enable_bash),
                ("code", self.spec.enable_code),
                ("subagents", self.spec.enable_subagents),
            )
            if on
        ]
        log.info(
            "spawning claude (model=%s, enabled=%s, mcp_tools=%d)",
            self.spec.model,
            enabled_features or "[base only]",
            len(self.spec.mcp_allowed_tools),
        )
        self._capture.open(self._session_id)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
            limit=4 * 1024 * 1024,  # 4 MiB – large MCP responses (e.g. GitLab)
        )
        self._stdout_task = asyncio.create_task(self._read_stdout(), name="cc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="cc-stderr")

    async def stop(self) -> None:
        self._stop_supervisor.set()
        self._cancel_tool_error_watchdog()
        self._cancel_status_heartbeat()
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
            self._capture.close()

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

    def _wedge_silence(self) -> float | None:
        """Seconds of silence if a turn is wedged past the timeout, else ``None``.

        Wedged means the subprocess is running, a turn is in progress, and the
        most recent activity signal — the max of ``_last_event_at`` (stdout
        parse time) and ``heartbeat.last_activity`` (bumped on every MCP tool
        call) — is older than ``_liveness_timeout``. Idle silence between turns
        is fine, so it returns ``None``.
        """
        if not self.is_running or self._current_turn is None:
            return None
        last_activity = max(self._last_event_at, self.heartbeat.last_activity)
        silence = time.monotonic() - last_activity
        return silence if silence > self._liveness_timeout else None

    async def _liveness_loop(self) -> None:
        """Poll for a wedged subprocess and terminate it on detection.

        On wedge we call ``_terminate_proc()`` — the supervisor's existing
        crash-recovery path respawns with the same session id. See
        :meth:`_wedge_silence` for the wedge criteria.
        """
        while not self._stop_supervisor.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_supervisor.wait(),
                    timeout=self._liveness_poll,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass

            silence = self._wedge_silence()
            if silence is None:
                continue
            log.error(
                "cc subprocess wedged mid-turn: no activity for %.0fs "
                "(timeout=%.0fs). Terminating to trigger respawn.",
                silence,
                self._liveness_timeout,
            )
            self._supervisor_abort_reason = "liveness-wedge"
            await self._terminate_proc()

    def _stderr_indicates_stale_session(self) -> bool:
        """Scan the recent stderr tail for any stale-session marker."""
        return any(
            pat in line for line in self._stderr_tail for pat in STALE_SESSION_PATTERNS
        )

    async def _drain_readers(self) -> None:
        """Wait briefly for stdout/stderr readers to hit EOF after the
        subprocess exits, so any final stderr bytes (e.g. the stale-id
        marker) land in ``_stderr_tail`` before we classify the exit.
        """
        for task in (self._stdout_task, self._stderr_task):
            if task is None or task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _supervise_loop(self) -> None:
        while not self._stop_supervisor.is_set():
            if self._proc is None:
                await asyncio.sleep(0.05)
                continue
            rc = await self._proc.wait()
            if self._stop_supervisor.is_set():
                return

            await self._drain_readers()

            intentional = self._supervisor_abort_reason
            self._supervisor_abort_reason = None
            if intentional is not None:
                log.info(
                    "cc subprocess exited rc=%s on intentional %s — respawning",
                    rc,
                    intentional,
                )
                await asyncio.sleep(self._crash_backoff_base)
                await self._terminate_proc()
                await self.start()
                continue

            if (
                self.spec.session_id is not None
                and self._stderr_indicates_stale_session()
            ):
                await self._run_stale_recovery(self.spec.session_id)
                continue

            await self._run_crash_recovery(rc)

    async def _run_stale_recovery(self, stale_id: str) -> None:
        """Drop the rejected ``session_id`` and respawn with a fresh session.

        Called from :meth:`_supervise_loop` when CC's stderr matches
        :data:`STALE_SESSION_PATTERNS` after an unexpected exit. Does
        *not* consume the crash budget — a stale id is a recoverable
        configuration drift, not a real crash.
        """
        log.warning(
            "cc subprocess rejected stale session_id=%s; "
            "dropping it and respawning with a fresh session",
            stale_id,
        )
        if self._on_stale_session is not None:
            try:
                await self._on_stale_session(stale_id)
            except Exception:
                log.debug("on_stale_session callback failed", exc_info=True)
        self.spec = dataclasses.replace(self.spec, session_id=None)
        self._session_id = None
        await asyncio.sleep(self._crash_backoff_base)
        await self._terminate_proc()
        await self.start()

    async def _run_crash_recovery(self, rc: int | None) -> None:
        """Record one crash, fire ``on_crash`` / ``on_giveup``, respawn or
        raise :class:`CrashLoop` if the budget is exhausted."""
        log.error(
            "cc subprocess exited rc=%s; recent stderr=%s",
            rc,
            self._stderr_tail[-5:],
        )
        now = time.monotonic()
        self._crash_times = [
            t for t in self._crash_times if now - t < self._crash_window_seconds
        ]
        self._crash_times.append(now)
        if len(self._crash_times) >= self._crash_limit:
            if self._on_giveup is not None:
                try:
                    await self._on_giveup(len(self._crash_times))
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
        log.error("respawning cc in %.1fs (attempt %d)", backoff, attempt)
        if self._on_crash is not None:
            try:
                await self._on_crash(attempt, backoff)
            except Exception:
                log.debug("on_crash callback failed", exc_info=True)
        await asyncio.sleep(backoff)
        await self._terminate_proc()
        await self.start()

    async def reset_session(self) -> None:
        """Drop the session id and respawn CC with a fresh, empty context.

        In-process counterpart of stale-session recovery: the supervisor's
        intentional-abort path respawns the subprocess, and ``build_argv``
        omits ``--resume`` because the spec no longer carries a session id.
        Does not consume the crash budget. If a turn is in flight, a
        sentinel result unblocks the engine immediately (same trick as the
        tool-error breaker).
        """
        log.warning(
            "session reset: dropping session_id=%s, respawning fresh",
            self._session_id,
        )
        self.spec = dataclasses.replace(self.spec, session_id=None)
        self._session_id = None
        # Drop the persisted id too — an unclean exit before the next
        # clean shutdown would otherwise resume the dropped session.
        self._session_id_path.unlink(missing_ok=True)
        self._supervisor_abort_reason = "session-reset"
        if self._current_turn is not None:
            sentinel = TurnResult(aborted_reason="session-reset")
            self._result_queue.put_nowait(sentinel)
            self._current_turn = None
        self._awaiting_turn_init = False
        await self._terminate_proc()

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, text: str) -> None:
        """Write one stream-json user message to stdin."""
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("cc worker not started")
        self._current_turn = TurnResult()
        self._awaiting_turn_init = True
        self._turn_tool_error_count = 0
        self._turn_first_tool_error_at = None
        self._cancel_tool_error_watchdog()
        self._last_tool_action = None
        self._arm_status_heartbeat()  # report progress on long turns
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
                self._capture.write_stream(line)
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
                self._capture.write_stderr(decoded)
                if decoded:
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
        log.error(
            "cc tool-error circuit breaker tripped (reason=%s): "
            "%d errors in %.1fs (max=%d, window=%.0fs). "
            "Terminating to trigger respawn.",
            reason,
            self._turn_tool_error_count,
            elapsed,
            self._tool_error_max_count,
            self._tool_error_window,
        )
        self._supervisor_abort_reason = "tool-error-limit"
        # Unblock the engine's ``wait_for_result`` immediately with a
        # sentinel TurnResult — the supervisor's respawn path can
        # take seconds, we don't want the user waiting on it.
        sentinel = TurnResult(aborted_reason="tool-error-limit")
        sentinel.stderr_tail = list(self._stderr_tail)
        self._result_queue.put_nowait(sentinel)
        self._current_turn = None
        self._awaiting_turn_init = False
        self._tool_error_abort_task = asyncio.create_task(
            self._terminate_proc(),
            name="cc-tool-error-abort",
        )

    def _arm_status_heartbeat(self) -> None:
        """(Re)start the per-turn status heartbeat at the start of a turn."""
        self._cancel_status_heartbeat()
        self._turn_started_at = time.monotonic()
        self._status_task = asyncio.create_task(
            self._status_heartbeat(),
            name="cc-status-heartbeat",
        )

    async def _status_heartbeat(self) -> None:
        """Report progress every ``status_interval`` while a turn is running.

        Worker-side (not the agent), so it fires even if the agent is wedged.
        The turn is NOT aborted — a long task keeps going; the owner can reply
        "stop" to halt it. Recurs until the turn ends (this task is cancelled,
        or ``_current_turn`` goes None). Other guards still apply: the liveness
        monitor catches silence, the tool-error breaker catches error loops.
        """
        while True:
            await asyncio.sleep(self._status_interval)
            if self._current_turn is None:
                return
            started = self._turn_started_at or time.monotonic()
            await self._fire_status(time.monotonic() - started)

    async def _fire_status(self, elapsed: float) -> None:
        """Invoke the status callback; never let a notify failure kill the loop."""
        if self._on_status is None:
            return
        try:
            await self._on_status(elapsed, self._last_tool_action)
        except Exception:
            log.warning("status callback failed", exc_info=True)

    def _cancel_status_heartbeat(self) -> None:
        """Stop the status heartbeat if running. Idempotent — safe from
        ``send()`` (new turn), ``stop()`` (shutdown), or the result-event handler
        (turn finished cleanly)."""
        task = self._status_task
        if task is not None and not task.done():
            task.cancel()
        self._status_task = None
