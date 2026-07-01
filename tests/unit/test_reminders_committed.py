"""Startup reconciliation of reminders against ``default-reminders.json``.

Verifies the reconciler makes the table match the file: declared reminders are
seeded once, edits cancel the stale row and seed a fresh one, removed entries
are cancelled, and rows from other sources (self-reflection, user-created) are
never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.db.reminders import NewReminder, insert_reminder
from hamroh.startup import _reconcile_committed_reminders, _seed_default_reminders

_OWNER = 42

_TRENDS = {
    "name": "morning-trends",
    "cron": "0 6 * * *",
    "text": "Post today's trends digest.",
}
_REVIEW = {"name": "weekly-review", "cron": "0 18 * * 5", "text": "Summarize the week."}

_ONE = json.dumps({"reminders": [_TRENDS]})
_TWO = json.dumps({"reminders": [_TRENDS, _REVIEW]})
_EMPTY = json.dumps({"reminders": []})
_REVIEW_OFF = {**_REVIEW, "enabled": False}
_TWO_REVIEW_OFF = json.dumps({"reminders": [_TRENDS, _REVIEW_OFF]})


async def _open(tmp_path: Path) -> tuple[Database, Config]:
    cfg = Config.for_test(tmp_path)
    object.__setattr__(cfg, "owner_id", _OWNER)
    cfg.ensure_dirs()
    db = await Database.open(cfg.db_path)
    return db, cfg


def _write_reminders(cfg: Config, body: str) -> None:
    cfg.committed_reminders_path.write_text(body, encoding="utf-8")


async def _pending(db: Database) -> list:
    return await db.fetch_all(
        "SELECT text, cron_expr, status, auto_seed_key FROM reminders "
        "WHERE status = 'pending' AND auto_seed_key LIKE 'committed:%'"
    )


# ---------------------------------------------------------------------------
# seed + idempotence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_boot_seeds_all_declared(tmp_path: Path) -> None:
    """Given two declared reminders, when reconciled, then both are pending."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(cfg, _TWO)

        await _reconcile_committed_reminders(db, cfg)

        rows = await _pending(db)
        assert {r["text"] for r in rows} == {
            "Post today's trends digest.",
            "Summarize the week.",
        }, "every declared reminder must be seeded as a pending row"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_numeric_chat_seeds_to_that_chat(tmp_path: Path) -> None:
    """An explicit numeric chat is seeded against that chat id, not the owner."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(
            cfg,
            json.dumps(
                {
                    "reminders": [
                        {
                            "name": "group-digest",
                            "cron": "0 9 * * *",
                            "chat": -100123,
                            "text": "Daily group digest.",
                        }
                    ]
                }
            ),
        )

        await _reconcile_committed_reminders(db, cfg)

        row = await db.fetch_one(
            "SELECT chat_id FROM reminders WHERE status = 'pending' "
            "AND auto_seed_key LIKE 'committed:%'"
        )
        assert row is not None and row["chat_id"] == -100123, (
            "a numeric chat must seed the row against that chat id, not the owner"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_second_boot_is_idempotent(tmp_path: Path) -> None:
    """Reconciling an unchanged file twice must not duplicate rows."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(cfg, _TWO)
        await _reconcile_committed_reminders(db, cfg)
        await _reconcile_committed_reminders(db, cfg)

        assert len(await _pending(db)) == 2, "a second boot must add no new rows"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# reconcile-on-change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_editing_cron_cancels_old_and_seeds_new(tmp_path: Path) -> None:
    """Editing a reminder's cron cancels the stale row and seeds the new one."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(cfg, _ONE)
        await _reconcile_committed_reminders(db, cfg)

        _write_reminders(cfg, _ONE.replace("0 6 * * *", "0 9 * * *"))
        await _reconcile_committed_reminders(db, cfg)

        pending = await _pending(db)
        assert len(pending) == 1, "exactly one pending row must remain after the edit"
        assert pending[0]["cron_expr"] == "0 9 * * *", "the new cron must be live"
        cancelled = await db.fetch_all(
            "SELECT cron_expr FROM reminders WHERE status = 'cancelled'"
        )
        assert [c["cron_expr"] for c in cancelled] == ["0 6 * * *"], (
            "the old reminder must be cancelled, not deleted"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_removing_an_entry_cancels_it(tmp_path: Path) -> None:
    """Dropping an entry from the file cancels its pending row on next boot."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(cfg, _TWO)
        await _reconcile_committed_reminders(db, cfg)

        _write_reminders(cfg, _ONE)  # weekly-review removed
        await _reconcile_committed_reminders(db, cfg)

        pending = await _pending(db)
        assert [r["text"] for r in pending] == ["Post today's trends digest."], (
            "only the still-declared reminder may remain pending"
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# enabled toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_reminder_is_not_seeded(tmp_path: Path) -> None:
    """A reminder with 'enabled': false is never seeded, unlike its enabled sibling."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(cfg, _TWO_REVIEW_OFF)

        await _reconcile_committed_reminders(db, cfg)

        rows = await _pending(db)
        assert [r["text"] for r in rows] == ["Post today's trends digest."], (
            "only the enabled reminder may be seeded; the disabled one is skipped"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_disabling_a_reminder_cancels_it(tmp_path: Path) -> None:
    """Flipping 'enabled' to false cancels the pending row, like removing the entry."""
    db, cfg = await _open(tmp_path)
    try:
        _write_reminders(cfg, _TWO)
        await _reconcile_committed_reminders(db, cfg)

        _write_reminders(cfg, _TWO_REVIEW_OFF)  # weekly-review turned off in place
        await _reconcile_committed_reminders(db, cfg)

        pending = await _pending(db)
        assert [r["text"] for r in pending] == ["Post today's trends digest."], (
            "the disabled reminder must be cancelled, leaving only the enabled one"
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# isolation from other reminder sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_reflection_row_is_untouched(tmp_path: Path) -> None:
    """The committed reconciler must not cancel the self-reflection loop."""
    db, cfg = await _open(tmp_path)
    object.__setattr__(cfg, "self_reflection_enabled", True)
    try:
        await _seed_default_reminders(db, cfg)
        _write_reminders(cfg, _ONE)
        await _reconcile_committed_reminders(db, cfg)

        row = await db.fetch_one(
            "SELECT status FROM reminders "
            "WHERE auto_seed_key = 'self-reflection-default'"
        )
        assert row is not None and row["status"] == "pending", (
            "self-reflection must stay pending — its key is outside 'committed:'"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_user_created_reminder_is_untouched(tmp_path: Path) -> None:
    """A user reminder (NULL auto_seed_key) is never cancelled by reconcile."""
    db, cfg = await _open(tmp_path)
    try:
        await insert_reminder(
            db,
            NewReminder(
                chat_id=_OWNER,
                user_id=7,
                text="call mom",
                trigger_at="2999-01-01 00:00:00",
            ),
        )
        _write_reminders(cfg, _EMPTY)  # no committed reminders declared
        await _reconcile_committed_reminders(db, cfg)

        row = await db.fetch_one("SELECT status FROM reminders WHERE user_id = 7")
        assert row is not None and row["status"] == "pending", (
            "a user-created reminder must survive reconciliation untouched"
        )
    finally:
        await db.close()
