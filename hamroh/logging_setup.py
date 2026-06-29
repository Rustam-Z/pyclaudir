"""Logging wiring: human-readable console plus a rotating JSON log file.

The console keeps the existing text format so the transcript stays the star
in ``docker logs``. Alongside it, every record is also written as one JSON
object per line to ``<data_dir>/logs/hamroh.log`` (rotated daily, 7 days
kept) for structured, greppable history and the ``/logs`` owner command.

``component`` is derived from the logger name — no log call site has to
change. ``event`` / ``data`` are optional and only appear when a call passes
them via ``extra={"event": ..., "data": ...}``.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

#: Console format — unchanged from the original ``_setup_logging``.
_TEXT_FORMAT = "%(asctime)s %(levelname)-5s %(name)-22s %(message)s"
_TEXT_DATEFMT = "%H:%M:%S"

#: Library loggers whose chatter buries the conversation; pinned to WARNING.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "mcp.server.lowlevel.server",
    "mcp.server.streamable_http_manager",
)

#: Logger-name first-segment → #14 component name. Anything not listed keeps
#: its own segment (``cc_worker``, ``tx``, ``cc``, ``engine``, ``db`` …).
_COMPONENT_MAP = {
    "telegram_io": "dispatcher",
    "mcp_server": "mcp",
    "reminder_scheduler": "reminder",
    "reminder": "reminder",
}


def _component(logger_name: str) -> str:
    """Map a logger name to a coarse component label for structured logs."""
    if logger_name == "hamroh":
        return "core"
    name = logger_name.removeprefix("hamroh.")
    first = name.split(".", 1)[0]
    return _COMPONENT_MAP.get(first, first)


class JsonLogFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "component": _component(record.name),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(cfg: Config) -> None:
    """Configure root logging: text console + rotating JSON file.

    Idempotent — clears existing root handlers first, so tests (and a second
    call) don't stack duplicate handlers.
    """
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(cfg.log_level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt=_TEXT_DATEFMT))
    root.addHandler(console)

    json_file = TimedRotatingFileHandler(
        cfg.log_dir / "hamroh.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    json_file.setFormatter(JsonLogFormatter())
    root.addHandler(json_file)

    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def tail_log(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path`` (oldest-first), ``[]`` if absent."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=n)]
    except FileNotFoundError:
        return []


def format_log_line(raw: str) -> str:
    """Render one JSON log line as ``HH:MM:SS LEVEL component | msg``.

    Falls back to the raw line if it is not the JSON we wrote.
    """
    try:
        rec = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    try:
        clock = datetime.fromisoformat(rec.get("ts", "")).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        clock = rec.get("ts", "")
    return (
        f"{clock} {rec.get('level', '')} "
        f"{rec.get('component', '')} | {rec.get('msg', '')}"
    )
