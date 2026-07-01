"""Read-only loader for the git-tracked ``default-reminders.json`` file.

Operators declare recurring reminders in a single JSON file at the repo root.
The startup reconciler (``hamroh.startup``) reads them through this module and
seeds/cancels database rows so the table matches the file — git is the source
of truth. This module is pure: it parses, validates, and derives keys, but
touches neither the database nor the clock.

File format (all times UTC)::

    {
      "reminders": [
        {
          "name": "morning-trends",   // unique; used in the seed key
          "cron": "0 6 * * *",        // required, 5-field cron, UTC
          "chat": "owner",            // optional: "owner" (default) or chat id
          "enabled": true,            // optional: true (default) or false to
                                      //           turn the reminder off in place
          "text": "Post today's trends digest."
          // "text" may also be a list of strings, joined with newlines,
          // for readable multi-paragraph prompts.
        }
      ]
    }

Only recurring (cron) reminders are supported here: a recurring row returns to
``pending`` after firing, so the reconciler's desired-vs-pending diff stays
stable. One-shot reminders remain a runtime concern of the ``reminder_set`` tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from croniter import croniter

#: Seed-key namespace for committed reminders. Chosen so it never matches the
#: self-reflection key (``self-reflection-default``); the two seeders are blind
#: to each other's rows.
KEY_PREFIX = "committed:"


class ReminderConfigError(Exception):
    """Raised when ``default-reminders.json`` is present but malformed.

    The bot fails fast at boot rather than silently dropping a reminder, so a
    typo in the operator's file can never quietly disable a scheduled task.
    """


@dataclass(frozen=True)
class DeclaredReminder:
    """One ``[[reminder]]`` entry, validated."""

    name: str
    cron_expr: str
    text: str
    chat: str | int
    enabled: bool = True


def load_declared_reminders(path: Path) -> list[DeclaredReminder]:
    """Parse and validate every reminder declared in ``path``.

    Returns an empty list when the file is absent (an instance that ships no
    reminders is valid). Raises :class:`ReminderConfigError` on malformed input.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReminderConfigError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ReminderConfigError(f"{path}: top level must be a JSON object")
    raw_entries = data.get("reminders", [])
    if not isinstance(raw_entries, list):
        raise ReminderConfigError(f"{path}: 'reminders' must be a list")

    reminders = [_parse_entry(raw, index) for index, raw in enumerate(raw_entries)]
    _reject_duplicate_names(reminders, path)
    return reminders


def resolve_chat(reminder: DeclaredReminder, owner_id: int) -> int:
    """Resolve the target chat id: ``"owner"`` maps to ``owner_id``."""
    if reminder.chat == "owner":
        return owner_id
    return int(reminder.chat)


def committed_key(reminder: DeclaredReminder, owner_id: int) -> str:
    """Derive the stable, content-addressed ``auto_seed_key`` for a reminder.

    The key folds in the cron, text and resolved chat, so editing any of them
    yields a new key. The reconciler then cancels the stale row and seeds the
    new one — that is how file edits propagate without a schema change.
    """
    resolved_chat = resolve_chat(reminder, owner_id)
    payload = f"{reminder.cron_expr}\n{reminder.text}\n{resolved_chat}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{KEY_PREFIX}{reminder.name}:{digest}"


def _parse_entry(raw: Any, index: int) -> DeclaredReminder:
    """Validate one raw TOML table into a :class:`DeclaredReminder`."""
    where = f"reminders[{index}]"
    if not isinstance(raw, dict):
        raise ReminderConfigError(f"{where} must be an object")

    name = _require_str(raw, "name", where)
    cron_expr = _require_str(raw, "cron", where)
    text = _require_text(raw, where)
    if not croniter.is_valid(cron_expr):
        raise ReminderConfigError(f"{where}: invalid cron expression {cron_expr!r}")

    chat = raw.get("chat", "owner")
    # JSON ``true``/``false`` parse to bool, a subclass of int — exclude them
    # so a stray boolean isn't silently accepted as chat id 0/1.
    if chat != "owner" and (isinstance(chat, bool) or not isinstance(chat, int)):
        raise ReminderConfigError(
            f"{where}: 'chat' must be \"owner\" or a numeric chat id, got {chat!r}"
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ReminderConfigError(
            f"{where}: 'enabled' must be true or false, got {enabled!r}"
        )
    return DeclaredReminder(
        name=name, cron_expr=cron_expr, text=text, chat=chat, enabled=enabled
    )


def _require_str(raw: dict, key: str, where: str) -> str:
    """Return a non-empty string field or raise a clear config error."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReminderConfigError(f"{where}: '{key}' is required and must be non-empty")
    return value


def _require_text(raw: dict, where: str) -> str:
    """Return the reminder text, accepting a string or a list of lines.

    A plain string is used verbatim; a list of strings is joined with newlines
    so long, multi-paragraph prompts stay readable in JSON (which has no
    multi-line literals). Both forms yield identical text — and thus the same
    seed key — for the same content.
    """
    value = raw.get("text")
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        if not all(isinstance(line, str) for line in value):
            raise ReminderConfigError(f"{where}: 'text' list items must be strings")
        text = "\n".join(value)
    else:
        raise ReminderConfigError(
            f"{where}: 'text' is required and must be a string or a list of strings"
        )
    if not text.strip():
        raise ReminderConfigError(f"{where}: 'text' must be non-empty")
    return text


def _reject_duplicate_names(reminders: list[DeclaredReminder], path: Path) -> None:
    """Names must be unique — they identify a reminder across edits."""
    seen: set[str] = set()
    for reminder in reminders:
        if reminder.name in seen:
            raise ReminderConfigError(
                f"{path}: duplicate reminder name {reminder.name!r}"
            )
        seen.add(reminder.name)
