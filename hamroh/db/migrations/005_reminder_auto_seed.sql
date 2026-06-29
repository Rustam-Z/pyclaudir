-- Track reminders that were auto-seeded at startup by the harness (as
-- opposed to reminders the agent created via MCP tools). The key is a
-- short string identifying WHICH default ("self-reflection-default",
-- future skills etc.). Used so the startup seed hook can decide
-- whether to (re-)insert a default reminder: if any row with this key
-- already exists — in any status, including 'cancelled' — skip.
-- That makes cancellation sticky: the owner can cancel a default and
-- it won't respawn on restart.
ALTER TABLE reminders ADD COLUMN auto_seed_key TEXT;

CREATE INDEX IF NOT EXISTS idx_reminders_auto_seed_key
    ON reminders(auto_seed_key);
