"""Parsing and validation of the git-tracked ``default-reminders.json`` file.

Pins the contract of :mod:`hamroh.reminders_config`: a well-formed file parses
into validated reminders, a missing file is empty, malformed input fails fast,
and the content-addressed seed key is stable yet edit-sensitive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hamroh.reminders_config import (
    DeclaredReminder,
    ReminderConfigError,
    committed_key,
    load_declared_reminders,
    resolve_chat,
)

_OWNER = 42


def _write(tmp_path: Path, body: str) -> Path:
    """Write ``body`` to a default-reminders.json under ``tmp_path``."""
    path = tmp_path / "default-reminders.json"
    path.write_text(body, encoding="utf-8")
    return path


def _doc(*entries: dict) -> str:
    """Serialize reminder entries into a well-formed config document."""
    return json.dumps({"reminders": list(entries)})


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_valid_file_parses_every_entry(tmp_path: Path) -> None:
    """Given two well-formed entries, when loaded, then both come back parsed."""
    path = _write(
        tmp_path,
        _doc(
            {
                "name": "morning-trends",
                "cron": "0 6 * * *",
                "text": "Post today's trends digest.",
            },
            {
                "name": "weekly-review",
                "cron": "0 18 * * 5",
                "chat": 12345,
                "text": "Summarize the week.",
            },
        ),
    )

    reminders = load_declared_reminders(path)

    assert [r.name for r in reminders] == ["morning-trends", "weekly-review"], (
        "both declared reminders must be returned, in file order"
    )
    assert reminders[1].chat == 12345, "an explicit numeric chat must be preserved"


def test_chat_defaults_to_owner(tmp_path: Path) -> None:
    """When 'chat' is omitted, it defaults to 'owner' and resolves to owner_id."""
    path = _write(tmp_path, _doc({"name": "x", "cron": "0 6 * * *", "text": "hi"}))

    reminder = load_declared_reminders(path)[0]

    assert reminder.chat == "owner", "missing chat must default to 'owner'"
    assert resolve_chat(reminder, _OWNER) == _OWNER, "'owner' resolves to owner_id"


def test_text_list_is_joined_with_newlines(tmp_path: Path) -> None:
    """A 'text' list is joined with newlines so multi-paragraph prompts read well."""
    path = _write(
        tmp_path,
        _doc(
            {
                "name": "brief",
                "cron": "0 6 * * *",
                "text": ["Good morning.", "", "Put together today's brief."],
            }
        ),
    )

    reminder = load_declared_reminders(path)[0]

    assert reminder.text == "Good morning.\n\nPut together today's brief.", (
        "a 'text' list must join into one newline-separated string"
    )


def test_string_and_list_text_can_mix_in_one_file(tmp_path: Path) -> None:
    """One file may hold a string-text reminder and a list-text one together."""
    path = _write(
        tmp_path,
        _doc(
            {"name": "short", "cron": "0 6 * * *", "text": "One-line reminder."},
            {"name": "multi", "cron": "0 18 * * 5", "text": ["Line 1.", "", "Line 2."]},
        ),
    )

    reminders = load_declared_reminders(path)

    assert reminders[0].text == "One-line reminder.", "the string form stays verbatim"
    assert reminders[1].text == "Line 1.\n\nLine 2.", (
        "the list form is joined even when a sibling uses the string form"
    )


def test_text_string_and_list_yield_the_same_key(tmp_path: Path) -> None:
    """Equivalent string and list forms produce identical text and seed key."""
    as_string = load_declared_reminders(
        _write(tmp_path, _doc({"name": "b", "cron": "0 6 * * *", "text": "a\nb"}))
    )[0]
    as_list = load_declared_reminders(
        _write(tmp_path, _doc({"name": "b", "cron": "0 6 * * *", "text": ["a", "b"]}))
    )[0]

    assert as_string.text == as_list.text, "both forms must produce identical text"
    assert committed_key(as_string, _OWNER) == committed_key(as_list, _OWNER), (
        "identical content must not reseed just because the author switched forms"
    )


def test_enabled_defaults_to_true(tmp_path: Path) -> None:
    """When 'enabled' is omitted, the reminder is on — existing files keep working."""
    path = _write(tmp_path, _doc({"name": "x", "cron": "0 6 * * *", "text": "hi"}))

    reminder = load_declared_reminders(path)[0]

    assert reminder.enabled is True, "a missing 'enabled' must default to on"


def test_enabled_false_is_preserved(tmp_path: Path) -> None:
    """An explicit 'enabled': false turns the reminder off without removing it."""
    path = _write(
        tmp_path,
        _doc({"name": "x", "cron": "0 6 * * *", "text": "hi", "enabled": False}),
    )

    reminder = load_declared_reminders(path)[0]

    assert reminder.enabled is False, "'enabled': false must be preserved as off"


def test_missing_file_is_empty(tmp_path: Path) -> None:
    """An absent file is valid — an instance may ship no reminders."""
    assert load_declared_reminders(tmp_path / "nope.json") == [], (
        "a missing reminders file must yield an empty list, not an error"
    )


# ---------------------------------------------------------------------------
# validation failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"cron": "0 6 * * *", "text": "x"}, "name"),
        ({"name": "a", "text": "x"}, "cron"),
        ({"name": "a", "cron": "0 6 * * *"}, "text"),
        ({"name": "a", "cron": "not a cron", "text": "x"}, "cron"),
        ({"name": "a", "cron": "0 6 * * *", "text": "x", "chat": "nope"}, "chat"),
        ({"name": "a", "cron": "0 6 * * *", "text": "x", "chat": True}, "chat"),
        ({"name": "a", "cron": "0 6 * * *", "text": "x", "enabled": "yes"}, "enabled"),
        ({"name": "a", "cron": "0 6 * * *", "text": []}, "text"),
        ({"name": "a", "cron": "0 6 * * *", "text": ["ok", 5]}, "text"),
        ({"name": "a", "cron": "0 6 * * *", "text": 123}, "text"),
    ],
)
def test_malformed_entry_raises(tmp_path: Path, entry: dict, match: str) -> None:
    """A missing/invalid field fails fast with a message naming the field."""
    with pytest.raises(ReminderConfigError, match=match):
        load_declared_reminders(_write(tmp_path, _doc(entry)))


def test_duplicate_names_raise(tmp_path: Path) -> None:
    """Names identify a reminder across edits, so duplicates are rejected."""
    path = _write(
        tmp_path,
        _doc(
            {"name": "dup", "cron": "0 6 * * *", "text": "a"},
            {"name": "dup", "cron": "0 7 * * *", "text": "b"},
        ),
    )
    with pytest.raises(ReminderConfigError, match="duplicate"):
        load_declared_reminders(path)


def test_invalid_json_raises(tmp_path: Path) -> None:
    """A file that is not valid JSON reports a clear parse error."""
    with pytest.raises(ReminderConfigError, match="valid JSON"):
        load_declared_reminders(_write(tmp_path, "this is { not json"))


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    """A top-level array (or scalar) instead of an object is rejected."""
    with pytest.raises(ReminderConfigError, match="top level must be a JSON object"):
        load_declared_reminders(_write(tmp_path, "[1, 2, 3]"))


def test_reminders_key_must_be_a_list(tmp_path: Path) -> None:
    """A scalar 'reminders' (not a list) is rejected."""
    with pytest.raises(ReminderConfigError, match="must be a list"):
        load_declared_reminders(_write(tmp_path, json.dumps({"reminders": "oops"})))


def test_reminder_entry_must_be_an_object(tmp_path: Path) -> None:
    """A list whose elements are not objects is rejected."""
    with pytest.raises(ReminderConfigError, match="must be an object"):
        load_declared_reminders(_write(tmp_path, json.dumps({"reminders": ["oops"]})))


# ---------------------------------------------------------------------------
# committed_key — stable, but edit-sensitive
# ---------------------------------------------------------------------------


def _reminder(**overrides: object) -> DeclaredReminder:
    base = {"name": "n", "cron_expr": "0 6 * * *", "text": "hi", "chat": "owner"}
    base.update(overrides)
    return DeclaredReminder(**base)  # type: ignore[arg-type]


def test_committed_key_is_stable_and_namespaced() -> None:
    """The key is deterministic across calls and prefixed for the reconciler."""
    reminder = _reminder()

    key = committed_key(reminder, _OWNER)

    assert key == committed_key(reminder, _OWNER), "key must be deterministic"
    assert key.startswith("committed:n:"), "key must carry the prefix and name"


@pytest.mark.parametrize(
    "overrides",
    [{"cron_expr": "0 7 * * *"}, {"text": "changed"}, {"chat": 999}],
)
def test_committed_key_changes_with_content(overrides: dict) -> None:
    """Editing cron, text, or chat must shift the key so edits propagate."""
    before = committed_key(_reminder(), _OWNER)
    after = committed_key(_reminder(**overrides), _OWNER)

    assert before != after, f"changing {list(overrides)} must change the seed key"
