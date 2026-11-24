-- Core schema. Portable SQLite + Postgres: TEXT / INTEGER / BIGINT only, UUID text keys,
-- ISO-8601 UTC TEXT timestamps written by Python, money in integer centavos.
-- Synced tables carry `origin` (node that inserted the row) and `field_hlc` (per-field clock).

CREATE TABLE phases (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    phase_id TEXT NOT NULL REFERENCES phases (id),
    account_no TEXT NOT NULL,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('residential', 'commercial')),
    route TEXT NOT NULL DEFAULT '' CHECK (route IN ('S', 'N', '')),
    water_source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disconnected', 'closed')),
    former_owner TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}',
    UNIQUE (phase_id, account_no)
);
CREATE INDEX accounts_account_no ON accounts (account_no);
CREATE INDEX accounts_name ON accounts (name);

CREATE TABLE meters (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    meter_no TEXT NOT NULL,
    installed_on TEXT,
    removed_on TEXT,
    start_reading TEXT,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX meters_account ON meters (account_id);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE rate_plans (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    config TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE readings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    meter_id TEXT REFERENCES meters (id),
    period TEXT NOT NULL,
    previous TEXT NOT NULL,
    present TEXT NOT NULL,
    read_on TEXT,
    flag TEXT NOT NULL DEFAULT '',
    entered_by TEXT,
    entered_at TEXT NOT NULL,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}',
    UNIQUE (account_id, period)
);
CREATE INDEX readings_period ON readings (period);

CREATE TABLE bills (
    id TEXT PRIMARY KEY,
    statement_no TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    period TEXT NOT NULL,
    reading_id TEXT REFERENCES readings (id),
    rate_plan_id TEXT NOT NULL REFERENCES rate_plans (id),
    due_on TEXT NOT NULL,
    issued_on TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'issued', 'cancelled')),
    water_cents BIGINT NOT NULL,
    dues_cents BIGINT NOT NULL,
    other_cents BIGINT NOT NULL DEFAULT 0,
    total_cents BIGINT NOT NULL,
    prior_balance_cents BIGINT NOT NULL DEFAULT 0,
    detail TEXT NOT NULL,
    cancel_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX bills_one_live_per_period ON bills (account_id, period) WHERE status <> 'cancelled';
CREATE INDEX bills_period_status ON bills (period, status);

CREATE TABLE ledger_entries (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    posted_on TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('water', 'dues', 'penalty', 'system_loss', 'other', 'opening', 'payment', 'reversal', 'adjustment')),
    amount_cents BIGINT NOT NULL CHECK (amount_cents <> 0),
    period TEXT,
    bill_id TEXT REFERENCES bills (id),
    payment_id TEXT,
    reverses_id TEXT UNIQUE REFERENCES ledger_entries (id),
    memo TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TEXT NOT NULL,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX ledger_account_date ON ledger_entries (account_id, posted_on);
CREATE INDEX ledger_bill ON ledger_entries (bill_id);
CREATE INDEX ledger_payment ON ledger_entries (payment_id);

CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    receipt_no TEXT NOT NULL UNIQUE,
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    paid_on TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('cash', 'check', 'gcash', 'bank', 'other')),
    status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'voided')),
    void_reason TEXT NOT NULL DEFAULT '',
    voided_by TEXT,
    voided_at TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX payments_account ON payments (account_id);
CREATE INDEX payments_paid_on ON payments (paid_on);

CREATE TABLE history_notes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    note TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX history_notes_account ON history_notes (account_id);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    user_id TEXT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before TEXT,
    after TEXT,
    origin TEXT,
    field_hlc TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_entity ON audit_log (entity, entity_id);
CREATE INDEX audit_at ON audit_log (at);

-- Not synced from here on ------------------------------------------------------------------

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'cashier')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id),
    csrf TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX sessions_user ON sessions (user_id);
CREATE INDEX sessions_expires ON sessions (expires_at);

CREATE TABLE login_attempts (
    id TEXT PRIMARY KEY,
    peer TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX login_attempts_peer_at ON login_attempts (peer, at);

CREATE TABLE requests (
    key TEXT PRIMARY KEY,
    user_id TEXT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    flash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX requests_created ON requests (created_at);

CREATE TABLE alerts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'urgent')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    UNIQUE (kind, key)
);
CREATE INDEX alerts_resolved ON alerts (resolved_at);

CREATE TABLE events (
    seq {AUTO_PK},
    id TEXT NOT NULL UNIQUE,
    tbl TEXT NOT NULL,
    row_id TEXT NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('insert', 'update')),
    payload TEXT NOT NULL,
    hlc TEXT,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pushed_at TEXT,
    hub_seq INTEGER
);
CREATE INDEX events_unpushed ON events (seq) WHERE pushed_at IS NULL;

CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Seed data. Fixed ids so every node starts from identical rows; field_hlc stays '{}' so any real
-- edit made later wins over the seed on every replica.

INSERT INTO phases (id, code, name) VALUES ('6f1d0000-0000-4000-8000-0000000001d0', '1D', 'Phase 1D');

INSERT INTO rate_plans (id, label, config, effective_from, created_at) VALUES (
    '6f1d0000-0000-4000-8000-0000000000a1',
    'September 2026 brackets (office to confirm)',
    '{"residential": {"minimums": ["300", "600"], "steps": ["70", "75", "80", "85", "90", "100", "105"]}, "commercial": {"minimums": ["400", "800"], "steps": ["90", "95", "100", "105", "110", "115", "120"]}}',
    '2026-09-01',
    '2026-09-01T00:00:00.000000Z'
);

INSERT INTO settings (key, value, updated_at) VALUES ('dues_cents', '10000', '2026-09-01T00:00:00.000000Z');
INSERT INTO settings (key, value, updated_at) VALUES ('penalty_rate_percent', '3', '2026-09-01T00:00:00.000000Z');
INSERT INTO settings (key, value, updated_at) VALUES ('penalty_day', '28', '2026-09-01T00:00:00.000000Z');
INSERT INTO settings (key, value, updated_at) VALUES ('due_day', '27', '2026-09-01T00:00:00.000000Z');
INSERT INTO settings (key, value, updated_at) VALUES ('issue_day', '22', '2026-09-01T00:00:00.000000Z');
INSERT INTO settings (key, value, updated_at) VALUES ('policy_confirmed', 'false', '2026-09-01T00:00:00.000000Z');
