-- Drop vestigial table: cc_sessions had no readers or writers anywhere.
DROP TABLE IF EXISTS cc_sessions;

-- Fold reactions into the messages row. Stored as JSON:
--   {"👍": [user_id, user_id, ...], "❤️": [user_id, ...]}
-- Populated from MessageReactionUpdated handler (inbound user reactions)
-- and from the add_reaction tool (bot reactions). NULL means no reactions.
DROP TABLE IF EXISTS reactions;
ALTER TABLE messages ADD COLUMN reactions TEXT;

-- Replace stubbed rate_limits with a schema we actually use.
-- bucket_start is unix epoch seconds, floored to the window (default 60s).
-- notice_sent is a flag so we only ping the user once per exhausted bucket
-- per chat.
DROP TABLE IF EXISTS rate_limits;
CREATE TABLE rate_limits (
    chat_id       INTEGER NOT NULL,
    bucket_start  INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    notice_sent   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, bucket_start)
);
