-- Excel import staging (local only, never synced). The uploaded workbook itself is never stored:
-- only the values parsed out of it, so a batch can be reviewed and applied later.

CREATE TABLE import_batches (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    period TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'review' CHECK (status IN ('review', 'applied')),
    summary TEXT NOT NULL DEFAULT '{}',
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL,
    applied_by TEXT,
    applied_at TEXT
);

CREATE TABLE import_rows (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches (id),
    sheet_row INTEGER NOT NULL,
    serial INTEGER NOT NULL,
    account_no TEXT NOT NULL,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    type_code TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    route TEXT NOT NULL DEFAULT '',
    meter_no TEXT NOT NULL DEFAULT '',
    present TEXT NOT NULL DEFAULT '',
    present_on TEXT NOT NULL DEFAULT '',
    previous TEXT NOT NULL DEFAULT '',
    previous_on TEXT NOT NULL DEFAULT '',
    former_owner TEXT NOT NULL DEFAULT '',
    skip_account INTEGER NOT NULL DEFAULT 0,
    skip_reading INTEGER NOT NULL DEFAULT 0,
    keep_name INTEGER NOT NULL DEFAULT 0,
    account_id TEXT,
    UNIQUE (batch_id, account_no)
);

CREATE TABLE import_blocks (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches (id),
    sheet TEXT NOT NULL,
    sheet_row INTEGER NOT NULL,
    name TEXT NOT NULL,
    meter_no TEXT NOT NULL DEFAULT '',
    row_id TEXT REFERENCES import_rows (id),
    link_method TEXT NOT NULL DEFAULT '',
    is_current INTEGER NOT NULL DEFAULT 1,
    total_cents BIGINT,
    opening TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '[]',
    status_hint TEXT NOT NULL DEFAULT '',
    former_owner TEXT NOT NULL DEFAULT '',
    skipped INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX import_blocks_batch ON import_blocks (batch_id, row_id);

CREATE TABLE import_issues (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches (id),
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    message TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'fixed', 'skipped')),
    resolved_by TEXT,
    resolved_at TEXT,
    UNIQUE (batch_id, kind, target_id)
);
CREATE INDEX import_issues_open ON import_issues (batch_id, state);
