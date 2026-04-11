-- pyclaudir initial schema. See the build prompt's "SQLite schema" section.

CREATE TABLE IF NOT EXISTS messages (
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    username         TEXT,
    first_name       TEXT,
    direction        TEXT NOT NULL CHECK (direction IN ('in','out')),
    timestamp        TEXT NOT NULL,
    text             TEXT NOT NULL,
    reply_to_id      INTEGER,
    reply_to_text    TEXT,
    edited           INTEGER DEFAULT 0,
    deleted          INTEGER DEFAULT 0,
    raw_update_json  TEXT,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);

CREATE TABLE IF NOT EXISTS reactions (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    emoji       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    chat_id           INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    username          TEXT,
    first_name        TEXT,
    join_date         TEXT NOT NULL,
    last_message_date TEXT,
    message_count     INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY,
    tool_name      TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    result_json    TEXT,
    error          TEXT,
    duration_ms    INTEGER,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cc_sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    ended_at     TEXT,
    crash_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rate_limits (
    chat_id        INTEGER NOT NULL,
    window_start   TEXT NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, window_start)
);

-- Bookkeeping for the migration runner itself.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
