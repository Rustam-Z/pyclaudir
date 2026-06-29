-- Log of inbound messages from chats that failed the access gate.
-- Kept separate from `messages` so the main table stays clean.
-- `refusal_sent = 1` marks the single row in each chat that triggered
-- the one-time "private assistant" reply.

CREATE TABLE IF NOT EXISTS unauthorized_messages (
    id            INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    chat_type     TEXT,
    message_id    INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    username      TEXT,
    first_name    TEXT,
    timestamp     TEXT NOT NULL,
    text          TEXT NOT NULL,
    refusal_sent  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_unauthorized_messages_chat
    ON unauthorized_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_unauthorized_messages_timestamp
    ON unauthorized_messages(timestamp);
