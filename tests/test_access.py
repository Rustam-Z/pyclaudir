"""Access control: gate logic, hot-reload, atomic writes, owner commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyclaudir.access import AccessConfig, gate, load_access, save_access

OWNER = 42


# ---------------------------------------------------------------------------
# gate() logic
# ---------------------------------------------------------------------------


def test_owner_always_allowed_in_dm() -> None:
    for policy in ("owner_only", "allowlist", "open"):
        access = AccessConfig(policy=policy, allowed_users=[], allowed_chats=[])
        assert gate(access=access, owner_id=OWNER, chat_id=OWNER, user_id=OWNER, chat_type="private")


def test_owner_only_blocks_strangers() -> None:
    access = AccessConfig(policy="owner_only")
    assert not gate(access=access, owner_id=OWNER, chat_id=999, user_id=999, chat_type="private")


def test_allowlist_permits_listed_user() -> None:
    access = AccessConfig(policy="allowlist", allowed_users=[123])
    assert gate(access=access, owner_id=OWNER, chat_id=123, user_id=123, chat_type="private")


def test_allowlist_blocks_unlisted_user() -> None:
    access = AccessConfig(policy="allowlist", allowed_users=[123])
    assert not gate(access=access, owner_id=OWNER, chat_id=999, user_id=999, chat_type="private")


def test_open_allows_anyone() -> None:
    access = AccessConfig(policy="open")
    assert gate(access=access, owner_id=OWNER, chat_id=999, user_id=999, chat_type="private")


def test_group_allowed_if_in_list() -> None:
    access = AccessConfig(policy="allowlist", allowed_chats=[-100])
    assert gate(access=access, owner_id=OWNER, chat_id=-100, user_id=999, chat_type="supergroup")


def test_group_blocked_if_not_in_list() -> None:
    access = AccessConfig(policy="allowlist", allowed_chats=[-100])
    assert not gate(access=access, owner_id=OWNER, chat_id=-200, user_id=999, chat_type="supergroup")


def test_owner_only_blocks_groups_even_in_allowed_chats() -> None:
    access = AccessConfig(policy="owner_only", allowed_chats=[-100])
    assert not gate(access=access, owner_id=OWNER, chat_id=-100, user_id=OWNER, chat_type="supergroup")


def test_open_allows_any_group_without_allowlist() -> None:
    access = AccessConfig(policy="open", allowed_chats=[])
    assert gate(access=access, owner_id=OWNER, chat_id=-100, user_id=999, chat_type="supergroup")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    config = AccessConfig(policy="allowlist", allowed_users=[1, 2], allowed_chats=[-100])
    save_access(path, config)
    loaded = load_access(path)
    assert loaded.policy == "allowlist"
    assert loaded.allowed_users == [1, 2]
    assert loaded.allowed_chats == [-100]


def test_load_returns_defaults_on_missing(tmp_path: Path) -> None:
    loaded = load_access(tmp_path / "nonexistent.json")
    assert loaded.policy == "owner_only"
    assert loaded.allowed_users == []
    assert loaded.allowed_chats == []


def test_load_handles_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    path.write_text("{{{broken json", encoding="utf-8")
    loaded = load_access(path)
    assert loaded.policy == "owner_only"
    # Corrupt file should be renamed aside
    assert not path.exists()
    corrupt_files = list(tmp_path.glob("access.corrupt-*.json"))
    assert len(corrupt_files) == 1


def test_load_handles_invalid_policy(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    path.write_text('{"policy": "yolo"}', encoding="utf-8")
    loaded = load_access(path)
    assert loaded.policy == "owner_only"


def test_atomic_write_survives_read_during_write(tmp_path: Path) -> None:
    """The tmp+rename pattern means a reader never sees a half-written file."""
    path = tmp_path / "access.json"
    save_access(path, AccessConfig(policy="allowlist", allowed_users=[1]))
    # Simulate concurrent read
    loaded = load_access(path)
    assert loaded.policy == "allowlist"
    assert loaded.allowed_users == [1]


# ---------------------------------------------------------------------------
# Hot-reload
# ---------------------------------------------------------------------------


def test_hot_reload_picks_up_changes(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    save_access(path, AccessConfig(policy="owner_only"))

    # First gate: stranger blocked
    access = load_access(path)
    assert not gate(access=access, owner_id=OWNER, chat_id=99, user_id=99, chat_type="private")

    # Edit the file on disk (simulates operator or /allow command)
    save_access(path, AccessConfig(policy="allowlist", allowed_users=[99]))

    # Second gate: stranger now allowed
    access2 = load_access(path)
    assert gate(access=access2, owner_id=OWNER, chat_id=99, user_id=99, chat_type="private")


# ---------------------------------------------------------------------------
# Owner always allowed regardless of allowed_users content
# ---------------------------------------------------------------------------


def test_owner_not_in_allowed_users_still_passes() -> None:
    """Owner is implicitly allowed — doesn't need to be in the list."""
    access = AccessConfig(policy="allowlist", allowed_users=[123])
    assert gate(access=access, owner_id=OWNER, chat_id=OWNER, user_id=OWNER, chat_type="private")
