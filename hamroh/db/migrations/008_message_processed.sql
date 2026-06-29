-- Commit-on-success trust flag: inbound rows are inserted BEFORE Claude
-- Code processes them (crash-replay and audit need that), but only a
-- cleanly completed turn flips its messages to processed=1 (one UPDATE
-- per turn in the engine, never on the per-message hot path). Failed,
-- aborted, or crashed turns leave rows at 0, which permanently bars
-- them from the restored-context digest — a poisonous message can
-- never re-enter a fresh session. Edits reset the flag.

ALTER TABLE messages ADD COLUMN processed INTEGER DEFAULT 0;

-- History is settled — every pre-existing row was processed by definition.
UPDATE messages SET processed = 1;
