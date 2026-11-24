-- "When was the last backup downloaded?" (Admin > Backup) asks for the newest audit row of one action. Every save
-- adds audit rows, so without this index that question scanned the whole year's trail backwards.
CREATE INDEX audit_action_at ON audit_log (action, at);
