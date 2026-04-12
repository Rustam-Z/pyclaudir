"""Environment-driven configuration for pyclaudir.

All runtime knobs live here. ``Config.from_env()`` is the only way the rest of
the codebase should pick up environment values, so tests can construct a
``Config`` directly without touching ``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is optional at import time so unit tests don't need it.
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
    """Resolved runtime configuration."""

    telegram_bot_token: str
    owner_id: int
    data_dir: Path
    model: str
    effort: str
    debounce_ms: int
    rate_limit_per_min: int
    claude_code_bin: str

    # Derived paths
    db_path: Path = field(init=False)
    memories_dir: Path = field(init=False)
    session_id_path: Path = field(init=False)
    cc_logs_dir: Path = field(init=False)
    access_path: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", self.data_dir / "pyclaudir.db")
        object.__setattr__(self, "memories_dir", self.data_dir / "memories")
        object.__setattr__(self, "session_id_path", self.data_dir / "session_id")
        object.__setattr__(self, "cc_logs_dir", self.data_dir / "cc_logs")
        object.__setattr__(self, "access_path", self.data_dir / "access.json")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            owner_id=int(_required("PYCLAUDIR_OWNER_ID")),
            data_dir=Path(_env("PYCLAUDIR_DATA_DIR", "./data") or "./data").resolve(),
            model=_env("PYCLAUDIR_MODEL", "claude-opus-4-6") or "claude-opus-4-6",
            effort=_env("PYCLAUDIR_EFFORT", "high") or "high",
            debounce_ms=_int("PYCLAUDIR_DEBOUNCE_MS", 0),
            rate_limit_per_min=_int("PYCLAUDIR_RATE_LIMIT_PER_MIN", 20),
            claude_code_bin=_env("CLAUDE_CODE_BIN", "claude") or "claude",
        )

    @classmethod
    def for_test(cls, data_dir: Path) -> "Config":
        """Build a Config without consulting the environment (used by tests)."""
        return cls(
            telegram_bot_token="test-token",
            owner_id=0,
            data_dir=data_dir.resolve(),
            model="claude-opus-4-6",
            effort="high",
            debounce_ms=1000,
            rate_limit_per_min=20,
            claude_code_bin="claude",
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memories_dir.mkdir(parents=True, exist_ok=True)
        self.cc_logs_dir.mkdir(parents=True, exist_ok=True)
