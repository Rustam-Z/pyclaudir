-- Reminder / scheduling system.

CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    trigger_at    TEXT NOT NULL,          -- UTC ISO8601
    cron_expr     TEXT,                   -- NULL for one-shot, cron string for recurring
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sent', 'cancelled')),
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, trigger_at);
