-- Rebuild rate_limits on user_id instead of chat_id.
-- The limiter is now applied inbound per-user in DMs only (see
-- telegram_io._on_message); owner bypass is handled at the call site.
-- Pre-existing chat-keyed rows (from migration 003) are discarded.
DROP TABLE IF EXISTS rate_limits;
CREATE TABLE rate_limits (
    user_id       INTEGER NOT NULL,
    bucket_start  INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    notice_sent   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket_start)
);
