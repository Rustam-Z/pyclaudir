"""All settings for pyclaudir, read from environment variables.

Every setting the bot uses is in this file. The rest of the code should
get values by calling ``Config.from_env()`` — that way tests can build
their own ``Config`` without touching environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# python-dotenv loads variables from a .env file. It's optional so tests
# don't have to install it.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - best effort
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _required(name: str) -> str:
    value = _env(name)
    if value is None:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be a boolean (true/false), got {raw!r}")


def _csv_ints(name: str) -> list[int]:
    raw = _env(name)
    if raw is None:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise RuntimeError(
                f"{name} must be a comma-separated list of integers, got {raw!r}"
            ) from exc
    return out


@dataclass(frozen=True)
class Config:
    """All settings the bot uses at runtime."""

    #: The bot's API token from @BotFather. Used to log in to Telegram.
    #: Env var: ``TELEGRAM_BOT_TOKEN`` (required).
    telegram_bot_token: str
    #: Telegram user ID of the bot's owner (you). Owner-only commands
    #: like ``/kill`` and ``/access`` check this. Direct-message-only
    #: mode also uses it to decide who can talk to the bot.
    #: Env var: ``PYCLAUDIR_OWNER_ID`` (required).
    owner_id: int
    #: Which Claude model to use. Passed to ``claude --model``.
    #: Env var: ``PYCLAUDIR_MODEL`` (required).
    model: str
    #: How hard Claude thinks before answering. Passed to ``claude --effort``.
    #: Env var: ``PYCLAUDIR_EFFORT`` (required, e.g. ``"high"``).
    effort: str
    #: Name or full path of the ``claude`` program to run.
    #: Env var: ``CLAUDE_CODE_BIN`` (default ``"claude"``).
    claude_code_bin: str
    #: Folder where the bot stores its data: the database, memory files,
    #: claude logs, the access list, and the session ID. The folder is
    #: created automatically by ``ensure_dirs``.
    #: Env var: ``PYCLAUDIR_DATA_DIR`` (default ``"./data"``).
    data_dir: Path
    #: When the daily self-reflection task runs. Standard cron format,
    #: in UTC time.
    #: Env var: ``PYCLAUDIR_SELF_REFLECTION_CRON`` (default ``"0 0 * * *"``,
    #: which means midnight UTC every day).
    self_reflection_cron: str
    #: How long to wait (in milliseconds) after a message before sending
    #: it to Claude. If more messages come in during this wait, they are
    #: bundled together into one turn. Set to ``0`` to send each message
    #: right away.
    #: Env var: ``PYCLAUDIR_DEBOUNCE_MS`` (default ``0``).
    debounce_ms: int
    #: Max messages per minute the bot will accept from one user in
    #: direct messages. The owner is not limited. Group chats are not
    #: limited either.
    #: Env var: ``PYCLAUDIR_RATE_LIMIT_PER_MIN`` (default ``20``).
    rate_limit_per_min: int
    #: Whether Claude can spawn sub-agents (the ``Agent`` tool). Off by
    #: default because sub-agents use a lot of tokens. When off, the
    #: tool is blocked and Claude isn't even told it exists.
    #: Env var: ``PYCLAUDIR_ENABLE_SUBAGENTS`` (default ``False``).
    enable_subagents: bool
    #: Whether Claude can run shell commands (Bash, PowerShell, Monitor).
    #: Off by default. Off means those tools are added to
    #: ``--disallowedTools`` so Claude refuses to invoke them. See
    #: ``docs/tools.md``.
    #: Env var: ``PYCLAUDIR_ENABLE_BASH`` (default ``False``).
    enable_bash: bool
    #: Whether Claude can read or write files outside ``data/memories/``
    #: (Edit, Write, Read, NotebookEdit, Glob, Grep, LSP). Off by default.
    #: Useful for forks that want the bot to do code work; the regular
    #: Telegram-assistant deployment leaves this off.
    #: Env var: ``PYCLAUDIR_ENABLE_CODE`` (default ``False``).
    enable_code: bool
    #: Per-file size cap (bytes) for inbound Telegram attachments. Files
    #: larger than this are rejected without download. Photos and documents
    #: both use this cap. 20 MB by default.
    #: Env var: ``PYCLAUDIR_ATTACHMENT_MAX_BYTES`` (default 20_000_000).
    attachment_max_bytes: int

    # ----- Settings for handling tool errors -----
    # These control what happens when Claude is still running fine, but
    # one of its tool calls keeps failing or the turn goes quiet.

    #: How many tool errors are allowed before the bot gives up. Used
    #: in two places: (1) inside one turn — too many failed tool calls
    #: stops the turn; (2) across turns — too many empty replies in a
    #: row stops retrying.
    #: Env var: ``PYCLAUDIR_TOOL_ERROR_MAX_COUNT`` (default 3).
    tool_error_max_count: int
    #: Time-based version of the rule above. If errors keep coming in
    #: for this many seconds after the first one, the bot stops the
    #: turn — even if the count is still under the limit.
    #: Env var: ``PYCLAUDIR_TOOL_ERROR_WINDOW_SECONDS`` (default 30).
    tool_error_window_seconds: float
    #: If Claude hasn't sent a message to a chat after this many seconds,
    #: the bot posts "Still on it — one moment." as a reply to the
    #: user's original message, so they know it's still working.
    #: Env var: ``PYCLAUDIR_PROGRESS_NOTIFY_SECONDS`` (default 60).
    progress_notify_seconds: float

    # ----- Settings for spotting a stuck Claude process -----
    # A separate watcher checks if Claude has gone silent in the middle
    # of a turn (no output, no tool activity). If yes, it kills Claude
    # so the supervisor can start it again.

    #: Max seconds of silence allowed during a turn. If Claude produces
    #: no output and no tool activity for longer than this, the watcher
    #: kills it. Silence between turns (when the bot is idle) is fine
    #: and ignored.
    #: Env var: ``PYCLAUDIR_LIVENESS_TIMEOUT_SECONDS`` (default 300).
    liveness_timeout_seconds: float
    #: How often the watcher wakes up to check. Smaller numbers catch a
    #: stuck process sooner but use a bit more CPU.
    #: Env var: ``PYCLAUDIR_LIVENESS_POLL_SECONDS`` (default 30).
    liveness_poll_seconds: float

    # ----- Settings for restarting Claude after a crash -----
    # The supervisor watches the Claude process. When it exits, the
    # supervisor waits a bit and starts it again. The wait gets longer
    # after each crash. If too many crashes happen in a short time, the
    # supervisor gives up and exits — and something outside (systemd,
    # docker, etc.) is expected to restart the whole bot.

    #: How long to wait before the first restart, in seconds. Each
    #: extra crash doubles the wait (``base * 2^(n-1)``), up to
    #: ``crash_backoff_cap``. Smaller = recovers faster from a one-off
    #: glitch but spins more on real problems.
    #: Env var: ``PYCLAUDIR_CRASH_BACKOFF_BASE`` (default 2.0).
    crash_backoff_base: float
    #: Maximum wait between restarts. Once the wait reaches this value,
    #: it stops growing. Stops the bot from waiting minutes between
    #: retries when something is really wrong.
    #: Env var: ``PYCLAUDIR_CRASH_BACKOFF_CAP`` (default 64.0).
    crash_backoff_cap: float
    #: How many crashes within ``crash_window_seconds`` count as "too
    #: many". When this is reached, the bot tells the owner and active
    #: chats, then exits.
    #: Env var: ``PYCLAUDIR_CRASH_LIMIT`` (default 10).
    crash_limit: int
    #: Time window used together with ``crash_limit``. Only crashes
    #: from the last ``crash_window_seconds`` are counted.
    #: Env var: ``PYCLAUDIR_CRASH_WINDOW_SECONDS`` (default 600.0,
    #: which is 10 minutes).
    crash_window_seconds: float

    # ----- Credentials for outside services -----
    # Read once when the bot starts. Kept here so every env var the bot
    # uses is in one place.

    #: Login info for Jira (used by the ``mcp-atlassian`` server). All
    #: three must be set or Jira is turned off.
    #: Env vars: ``JIRA_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN``.
    jira_url: str
    jira_username: str
    jira_api_token: str
    #: Login info for GitLab (used by the ``mcp-gitlab`` server). Both
    #: must be set or GitLab is turned off.
    #: Env vars: ``GITLAB_URL`` / ``GITLAB_TOKEN``.
    gitlab_url: str
    gitlab_token: str
    #: GitHub personal access token (used by the GitHub MCP server).
    #: Empty turns GitHub off. GitHub.com is assumed.
    #: Env var: ``GITHUB_PERSONAL_ACCESS_TOKEN``.
    github_token: str
    #: Optional GitHub Enterprise host (e.g. ``github.example.com``).
    #: Empty for github.com. Passed through to the MCP server's spawn
    #: environment. Env var: ``GITHUB_HOST``.
    github_host: str

    # Derived paths
    db_path: Path = field(init=False)
    memories_dir: Path = field(init=False)
    session_id_path: Path = field(init=False)
    cc_logs_dir: Path = field(init=False)
    access_path: Path = field(init=False)
    attachments_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", self.data_dir / "pyclaudir.db")
        object.__setattr__(self, "memories_dir", self.data_dir / "memories")
        object.__setattr__(self, "session_id_path", self.data_dir / "session_id")
        object.__setattr__(self, "cc_logs_dir", self.data_dir / "cc_logs")
        object.__setattr__(self, "access_path", self.data_dir / "access.json")
        object.__setattr__(self, "attachments_dir", self.data_dir / "attachments")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            owner_id=int(_required("PYCLAUDIR_OWNER_ID")),
            model=_required("PYCLAUDIR_MODEL"),
            effort=_required("PYCLAUDIR_EFFORT"),
            claude_code_bin=_env("CLAUDE_CODE_BIN", "claude") or "claude",
            data_dir=Path(_env("PYCLAUDIR_DATA_DIR", "./data") or "./data").resolve(),
            self_reflection_cron=(
                _env("PYCLAUDIR_SELF_REFLECTION_CRON", "0 0 * * *") or "0 0 * * *"
            ),  
            debounce_ms=_int("PYCLAUDIR_DEBOUNCE_MS", 0),
            rate_limit_per_min=_int("PYCLAUDIR_RATE_LIMIT_PER_MIN", 20),
            enable_subagents=_bool("PYCLAUDIR_ENABLE_SUBAGENTS", False),
            enable_bash=_bool("PYCLAUDIR_ENABLE_BASH", False),
            enable_code=_bool("PYCLAUDIR_ENABLE_CODE", False),
            attachment_max_bytes=_int("PYCLAUDIR_ATTACHMENT_MAX_BYTES", 20_000_000),
            tool_error_max_count=_int("PYCLAUDIR_TOOL_ERROR_MAX_COUNT", 3),
            tool_error_window_seconds=_float("PYCLAUDIR_TOOL_ERROR_WINDOW_SECONDS", 30.0),
            progress_notify_seconds=_float("PYCLAUDIR_PROGRESS_NOTIFY_SECONDS", 60.0),
            liveness_timeout_seconds=_float("PYCLAUDIR_LIVENESS_TIMEOUT_SECONDS", 300.0),
            liveness_poll_seconds=_float("PYCLAUDIR_LIVENESS_POLL_SECONDS", 30.0),
            crash_backoff_base=_float("PYCLAUDIR_CRASH_BACKOFF_BASE", 2.0),
            crash_backoff_cap=_float("PYCLAUDIR_CRASH_BACKOFF_CAP", 64.0),
            crash_limit=_int("PYCLAUDIR_CRASH_LIMIT", 10),
            crash_window_seconds=_float("PYCLAUDIR_CRASH_WINDOW_SECONDS", 600.0),
            jira_url=_env("JIRA_URL", "") or "",
            jira_username=_env("JIRA_USERNAME", "") or "",
            jira_api_token=_env("JIRA_API_TOKEN", "") or "",
            gitlab_url=_env("GITLAB_URL", "") or "",
            gitlab_token=_env("GITLAB_TOKEN", "") or "",
            github_token=_env("GITHUB_PERSONAL_ACCESS_TOKEN", "") or "",
            github_host=_env("GITHUB_HOST", "") or "",
        )

    @classmethod
    def for_test(cls, data_dir: Path) -> "Config":
        """Build a Config with fixed values, ignoring environment variables.

        Used by tests so they don't depend on whatever is set on the
        machine running them.
        """
        return cls(
            telegram_bot_token="test-token",
            owner_id=0,
            model="claude-opus-4-7",
            effort="high",
            claude_code_bin="claude",
            data_dir=data_dir.resolve(),
            self_reflection_cron="0 0 * * *",
            debounce_ms=1000,
            rate_limit_per_min=20,
            enable_subagents=False,
            enable_bash=False,
            enable_code=False,
            attachment_max_bytes=20_000_000,
            tool_error_max_count=3,
            tool_error_window_seconds=30.0,
            progress_notify_seconds=60.0,
            liveness_timeout_seconds=300.0,
            liveness_poll_seconds=30.0,
            crash_backoff_base=2.0,
            crash_backoff_cap=64.0,
            crash_limit=10,
            crash_window_seconds=600.0,
            jira_url="",
            jira_username="",
            jira_api_token="",
            gitlab_url="",
            gitlab_token="",
            github_token="",
            github_host="",
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memories_dir.mkdir(parents=True, exist_ok=True)
        self.cc_logs_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
