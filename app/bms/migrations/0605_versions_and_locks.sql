-- Safe editing by several people at once (app/bms/editing.py).
-- `version`: raised by db.update on every change (also by changes that arrive from another office), so an edit
-- form opened at version 3 knows, when saved, that someone changed the record in between. Local to this
-- database; never sent between offices (they merge field by field with their own clocks).
ALTER TABLE accounts ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE readings ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE bills ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE settings ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

-- Who is editing what right now: a short lease, renewed while the edit page is open. Never synced. Looked up by
-- its key only; an expired row is simply taken over by the next person, so no clean-up job is needed.
CREATE TABLE edit_locks (
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (entity, entity_id)
);
