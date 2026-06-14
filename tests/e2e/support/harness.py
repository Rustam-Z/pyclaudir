"""Framework-agnostic plumbing shared by the e2e suite and the eval script.

One definition of: which env vars configure a run, how the bot subprocess
is launched and detected as ready, how to authorize the tester, and how to
read the bot's SQLite state read-only while it runs.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from pyclaudir.access import AccessConfig, save_access

#: Repo root (…/pyclaudir). The SUT runs with this as its cwd so it picks
#: up the operator's ``plugins.json`` and ``prompts/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
#: Gitignored file holding the ``E2E_*`` vars (written by ``make_session.py``).
#: Lives at the e2e root (one level up from this support package).
ENV_FILE = Path(__file__).resolve().parents[1] / ".env.e2e"
#: ``__main__.py`` logs this exact line once the dispatcher starts polling.
READY_LINE = "pyclaudir is live"
_READY_TIMEOUT_S = 90.0
_LOG_RING = 400  # keep the last N output lines for failure dumps
#: The bot runs as a subprocess; forward its output through this logger so
#: pytest's live log (``log_cli``) streams the RX/TX/timing lines as they
#: happen, not just on failure.
_SUT_LOG = logging.getLogger("pyclaudir.sut")

_REQUIRED_ENV = (
    "E2E_TG_API_ID",
    "E2E_TG_API_HASH",
    "E2E_TG_SESSION",
    "E2E_BOT_TOKEN",
    "E2E_BOT_USERNAME",
    "E2E_OWNER_ID",
    "E2E_GROUP_ID",
)


def load_e2e_env() -> None:
    """Load ``tests/e2e/.env.e2e`` so a run needs no manual ``export``.

    Real environment variables win over the file (``override=False``); a
    missing file is a no-op.
    """
    load_dotenv(ENV_FILE)


def missing_env() -> list[str]:
    """Names of required ``E2E_*`` vars that are unset (drives skip-gating)."""
    return [name for name in _REQUIRED_ENV if not os.environ.get(name)]


@dataclass(frozen=True)
class E2EConfig:
    """Everything a real run needs, read once from the environment."""

    api_id: int
    api_hash: str
    session: str
    bot_token: str
    bot_username: str
    owner_id: int
    group_id: int
    model: str

    @classmethod
    def from_env(cls) -> "E2EConfig":
        return cls(
            api_id=int(os.environ["E2E_TG_API_ID"]),
            api_hash=os.environ["E2E_TG_API_HASH"],
            session=os.environ["E2E_TG_SESSION"],
            bot_token=os.environ["E2E_BOT_TOKEN"],
            bot_username=os.environ["E2E_BOT_USERNAME"].lstrip("@"),
            owner_id=int(os.environ["E2E_OWNER_ID"]),
            group_id=int(os.environ["E2E_GROUP_ID"]),
            model=os.environ.get("E2E_MODEL", "claude-sonnet-4-6"),
        )


@dataclass
class Sut:
    """A running pyclaudir subprocess plus the paths a test inspects."""

    proc: subprocess.Popen[str]
    data_dir: Path
    _log: deque[str]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pyclaudir.db"

    @property
    def memories_dir(self) -> Path:
        return self.data_dir / "memories"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def access_path(self) -> Path:
        return self.data_dir / "access.json"

    def log_tail(self) -> str:
        return "".join(self._log)


def _child_env(cfg: E2EConfig, data_dir: Path, access_path: Path) -> dict[str, str]:
    """Env for the SUT: a cheap/fast model, zero debounce (clean latency),
    and a high rate limit so a burst of test messages is never throttled."""
    env = dict(os.environ)
    env.update(
        TELEGRAM_BOT_TOKEN=cfg.bot_token,
        PYCLAUDIR_OWNER_ID=str(cfg.owner_id),
        PYCLAUDIR_MODEL=cfg.model,
        PYCLAUDIR_EFFORT="low",
        PYCLAUDIR_DATA_DIR=str(data_dir),
        PYCLAUDIR_ACCESS_PATH=str(access_path),
        PYCLAUDIR_DEBOUNCE_MS="0",
        PYCLAUDIR_RATE_LIMIT_PER_MIN="120",
    )
    return env


def _write_access(path: Path, cfg: E2EConfig) -> None:
    """Authorize the tester (owner) and the test group so both DM and group
    messages pass the gate — without touching the repo's access.json."""
    save_access(
        path,
        AccessConfig(
            policy="allowlist",
            allowed_users=[cfg.owner_id],
            allowed_chats=[cfg.group_id],
        ),
    )


def _drain(
    proc: subprocess.Popen[str], log: deque[str], ready: threading.Event
) -> None:
    """Pump child output into the ring buffer and the live log; flag readiness
    on READY_LINE."""
    assert proc.stdout is not None
    for line in proc.stdout:
        log.append(line)
        if line.strip():
            _SUT_LOG.info(line.rstrip())
        if READY_LINE in line:
            ready.set()


def launch_sut(cfg: E2EConfig, data_dir: Path) -> Sut:
    """Start ``python -m pyclaudir`` and block until it logs READY_LINE."""
    data_dir.mkdir(parents=True, exist_ok=True)
    access_path = data_dir / "access.json"
    _write_access(access_path, cfg)

    proc = subprocess.Popen(
        [sys.executable, "-m", "pyclaudir"],
        cwd=REPO_ROOT,
        env=_child_env(cfg, data_dir, access_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log: deque[str] = deque(maxlen=_LOG_RING)
    ready = threading.Event()
    threading.Thread(target=_drain, args=(proc, log, ready), daemon=True).start()

    sut = Sut(proc, data_dir, log)
    if not ready.wait(_READY_TIMEOUT_S):
        stop_sut(sut)
        raise RuntimeError(
            f"pyclaudir did not become ready in {_READY_TIMEOUT_S:.0f}s\n"
            f"--- last output ---\n{sut.log_tail()}"
        )
    return sut


def stop_sut(sut: Sut, timeout: float = 15.0) -> None:
    """Graceful SIGTERM (the SUT shuts down cleanly), SIGKILL on hang."""
    proc = sut.proc
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5.0)


def new_png_files(renders_dir: Path, after: float) -> list[Path]:
    """PNG files in ``renders_dir`` modified at or after ``after`` (epoch secs)."""
    if not renders_dir.exists():
        return []
    return [p for p in renders_dir.glob("*.png") if p.stat().st_mtime >= after]


def memory_files_containing(memories_dir: Path, token: str) -> list[Path]:
    """Memory files whose text contains ``token`` — proves disk persistence."""
    return [
        path
        for path in memories_dir.rglob("*")
        if path.is_file() and token in path.read_text(encoding="utf-8", errors="ignore")
    ]


def set_access(sut: Sut, access: AccessConfig) -> None:
    """Rewrite the SUT's access.json — the bot hot-reloads it per message."""
    save_access(sut.access_path, access)


def read_only_query(db_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run a SELECT against the live DB without locking out the bot.

    WAL mode (``db/database.py``) lets this second connection read a
    consistent snapshot while the bot keeps writing; opened read-only so a
    test can never mutate bot state.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def unauthorized_rows(db_path: Path, token: str) -> list[sqlite3.Row]:
    """`unauthorized_messages` rows whose text contains ``token``."""
    return read_only_query(
        db_path,
        "SELECT * FROM unauthorized_messages WHERE text LIKE ?",
        (f"%{token}%",),
    )


def reminder_rows(db_path: Path, token: str) -> list[sqlite3.Row]:
    """`reminders` rows whose text contains ``token``."""
    return read_only_query(
        db_path, "SELECT * FROM reminders WHERE text LIKE ?", (f"%{token}%",)
    )


def message_rows(db_path: Path, token: str) -> list[sqlite3.Row]:
    """`messages` rows whose text contains ``token`` (either direction).

    Proves a dropped (paused) message was never persisted."""
    return read_only_query(
        db_path, "SELECT * FROM messages WHERE text LIKE ?", (f"%{token}%",)
    )


def tool_calls_since(db_path: Path, since: str) -> list[sqlite3.Row]:
    """`tool_calls` rows recorded at or after ``since`` (a "%Y-%m-%d %H:%M:%S"
    UTC string) — for correlating a test's action to the tools it triggered."""
    return read_only_query(
        db_path,
        "SELECT tool_name, duration_ms, created_at FROM tool_calls "
        "WHERE created_at >= ?",
        (since,),
    )


def reply_info(db_path: Path, token: str) -> sqlite3.Row | None:
    """The inbound message containing ``token`` (its ``reply_to_id`` and
    ``reply_to_text``), matched by text — the bot's Bot-API message_ids
    differ from the Telethon client's, so they can't be cross-queried by id.
    """
    rows = read_only_query(
        db_path,
        "SELECT reply_to_id, reply_to_text FROM messages "
        "WHERE direction = 'in' AND text LIKE ?",
        (f"%{token}%",),
    )
    return rows[0] if rows else None
