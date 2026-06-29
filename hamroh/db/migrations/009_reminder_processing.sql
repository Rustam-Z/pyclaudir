-- Add a 'processing' status so a reminder can be claimed while it is
-- being delivered. The scheduler's fetch_due query only returns
-- 'pending' rows, so claiming a reminder (pending -> processing) stops
-- the next 60s poll from re-firing one that is still in flight — the
-- root cause of #44 / #48 (reminders re-firing every minute).
--
-- SQLite can't ALTER a CHECK constraint in place, so rebuild the table
-- with the widened CHECK. reminders is a leaf table (nothing references
-- it), so a straight copy is safe.

CREATE TABLE reminders_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    trigger_at    TEXT NOT NULL,          -- UTC ISO8601
    cron_expr     TEXT,                   -- NULL for one-shot, cron string for recurring
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'sent', 'cancelled')),
    created_at    TEXT NOT NULL,
    auto_seed_key TEXT
);

INSERT INTO reminders_new
    (id, chat_id, user_id, text, trigger_at, cron_expr, status, created_at, auto_seed_key)
SELECT
    id, chat_id, user_id, text, trigger_at, cron_expr, status, created_at, auto_seed_key
FROM reminders;

DROP TABLE reminders;
ALTER TABLE reminders_new RENAME TO reminders;

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, trigger_at);
CREATE INDEX IF NOT EXISTS idx_reminders_auto_seed_key ON reminders(auto_seed_key);
