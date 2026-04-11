"""Async SQLite wrapper with a tiny built-in migration runner.

Migrations are plain ``.sql`` files in ``pyclaudir/db/migrations/`` named
``NNN_description.sql``. ``Database.open()`` opens the file (creating it if
needed), enables WAL, and applies any unapplied migrations in order.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterable

import aiosqlite

log = logging.getLogger(__name__)

_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _discover_migrations() -> list[tuple[int, str, str]]:
    """Return ``[(version, filename, sql), ...]`` sorted ascending."""
    out: list[tuple[int, str, str]] = []
    pkg = resources.files("pyclaudir.db").joinpath("migrations")
    for entry in pkg.iterdir():
        name = entry.name
        m = _MIGRATION_RE.match(name)
        if not m:
            continue
        version = int(m.group(1))
        sql = entry.read_text(encoding="utf-8")
        out.append((version, name, sql))
    out.sort(key=lambda x: x[0])
    return out


class Database:
    """Thin async wrapper around aiosqlite + migrations."""

    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path

    @classmethod
    async def open(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.commit()
        db = cls(conn, path)
        await db._migrate()
        return db

    async def close(self) -> None:
        await self._conn.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        return self._conn

    async def _migrate(self) -> None:
        # Bootstrap the bookkeeping table by hand so we can run migration 001
        # idempotently — its CREATE TABLE for schema_migrations would race
        # with us querying it before any migrations have ever been applied.
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL)"
        )
        await self._conn.commit()

        applied = await self._applied_versions()
        for version, name, sql in _discover_migrations():
            if version in applied:
                continue
            log.info("applying migration %s", name)
            await self._conn.executescript(sql)
            await self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _now_iso()),
            )
            await self._conn.commit()

    async def _applied_versions(self) -> set[int]:
        cursor = await self._conn.execute("SELECT version FROM schema_migrations")
        rows = await cursor.fetchall()
        await cursor.close()
        return {int(r[0]) for r in rows}

    # ------------------------------------------------------------------
    # Convenience query helpers (used in later steps; kept tiny for now).
    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Iterable | None = None) -> None:
        await self._conn.execute(sql, tuple(params or ()))
        await self._conn.commit()

    async def fetch_all(self, sql: str, params: Iterable | None = None) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(sql, tuple(params or ()))
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetch_one(self, sql: str, params: Iterable | None = None) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(sql, tuple(params or ()))
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()
