-- Sync (hub + spokes). All local to a node, never replicated.

CREATE TABLE sync_nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    last_seen TEXT,
    last_pushed_seq INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT
);

-- Remote events that could not be applied yet: natural-key clashes (kind 'conflict', needs an
-- admin) or rows whose parent has not arrived / is itself held back (kind 'waiting', retried).
CREATE TABLE sync_conflicts (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    tbl TEXT NOT NULL,
    row_id TEXT NOT NULL,
    op TEXT NOT NULL,
    hlc TEXT NOT NULL,
    origin TEXT NOT NULL,
    payload TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('conflict', 'waiting')),
    reason TEXT NOT NULL,
    local_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT
);
CREATE INDEX sync_conflicts_open ON sync_conflicts (resolved_at, kind);
CREATE INDEX sync_conflicts_row ON sync_conflicts (tbl, row_id);

-- Remote event ids already applied here (idempotent apply on spokes; the hub uses events.id).
CREATE TABLE sync_applied (
    event_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- One worker per database: a lease claimed with a conditional upsert.
CREATE TABLE sync_lease (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

ALTER TABLE events ADD COLUMN push_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN dead_at TEXT;
CREATE INDEX events_origin_seq ON events (origin, seq);

-- The same fact recorded at two offices under different ids (same receipt, same account number):
-- later events for the remote id land on the local row instead of doubling it.
CREATE TABLE sync_aliases (
    tbl TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    local_id TEXT NOT NULL,
    PRIMARY KEY (tbl, remote_id)
);
