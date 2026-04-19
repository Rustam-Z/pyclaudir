"""Verify the migration runner produces the expected schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyclaudir.config import Config
from pyclaudir.db.database import Database


EXPECTED_TABLES = {
    "messages",
    "users",
    "tool_calls",
    "rate_limits",
    "reminders",
    "schema_migrations",
}

DROPPED_TABLES = {"reactions", "cc_sessions"}


async def _open(tmp_path: Path) -> Database:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    return await Database.open(cfg.db_path)


@pytest.mark.asyncio
async def test_migration_creates_expected_tables(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {row["name"] for row in rows}
        assert EXPECTED_TABLES.issubset(names), f"missing tables: {EXPECTED_TABLES - names}"
        leftover = DROPPED_TABLES & names
        assert not leftover, f"dead tables still present: {leftover}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_messages_pk_is_chat_and_message(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rows = await db.fetch_all("PRAGMA table_info(messages)")
        pk_cols = sorted(r["name"] for r in rows if r["pk"])
        assert pk_cols == ["chat_id", "message_id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_messages_has_reactions_column(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rows = await db.fetch_all("PRAGMA table_info(messages)")
        cols = {r["name"] for r in rows}
        assert "reactions" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limits_schema(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        rows = await db.fetch_all("PRAGMA table_info(rate_limits)")
        cols = {r["name"] for r in rows}
        assert cols == {"chat_id", "bucket_start", "count", "notice_sent"}
        pk_cols = sorted(r["name"] for r in rows if r["pk"])
        assert pk_cols == ["bucket_start", "chat_id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_messages_direction_check_constraint(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        # 'in' is allowed
        await db.execute(
            "INSERT INTO messages(chat_id, message_id, user_id, direction, timestamp, text)"
            " VALUES (1, 1, 99, 'in', '2026-01-01 00:00:00', 'hi')"
        )
        # garbage direction must fail the CHECK
        import aiosqlite

        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO messages(chat_id, message_id, user_id, direction, timestamp, text)"
                " VALUES (1, 2, 99, 'sideways', '2026-01-01 00:00:01', 'hi')"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    await db.close()
    # Re-open the same path; migrations must not double-apply.
    db = await Database.open(Config.for_test(tmp_path).db_path)
    try:
        rows = await db.fetch_all("SELECT version FROM schema_migrations")
        versions = [r["version"] for r in rows]
        assert versions == sorted(set(versions)), "duplicate migration rows"
        assert 1 in versions
        assert 3 in versions
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = await _open(tmp_path)
    try:
        row = await db.fetch_one("PRAGMA journal_mode")
        assert row is not None
        assert row[0].lower() == "wal"
    finally:
        await db.close()
