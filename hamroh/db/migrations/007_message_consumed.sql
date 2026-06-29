-- Pending-buffer persistence: track which inbound messages were actually
-- handed to the CC subprocess. Rows the engine drained into a turn get
-- consumed=1 (one UPDATE per turn, after the send — never on the
-- per-message hot path). On startup, unconsumed inbound rows are
-- replayed into the engine so a process crash can't silently drop
-- buffered messages.

ALTER TABLE messages ADD COLUMN consumed INTEGER DEFAULT 0;

-- History is settled — only rows written after this migration participate.
UPDATE messages SET consumed = 1;

-- Partial index: the boot query stays instant and the index stays tiny
-- (outbound rows excluded by predicate; inbound rows leave it once consumed).
CREATE INDEX IF NOT EXISTS idx_messages_unconsumed
    ON messages(consumed) WHERE consumed = 0 AND direction = 'in';
