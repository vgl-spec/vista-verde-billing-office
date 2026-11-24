-- Which paper the cashier gave (site visit follow-up 2026-11-24): a BIR Service Invoice (SI), an Official
-- Receipt (OR) or an Acknowledgement Receipt (AR). Each comes from its own pre-numbered booklet, so the same
-- number can appear in two booklets: a receipt is unique per (type, number), no longer per number alone.
-- Every payment recorded before this was an OR (the cashier typed the OR number).
--
-- SQLite cannot drop the inline UNIQUE on receipt_no, so the table is rebuilt (no table references payments).
-- The PostgreSQL twin of this migration is 0602_receipt_type.postgres.sql.
CREATE TABLE payments_new (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    receipt_type TEXT NOT NULL DEFAULT 'OR' CHECK (receipt_type IN ('SI', 'OR', 'AR')),
    receipt_no TEXT NOT NULL,
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
INSERT INTO payments_new (id, account_id, receipt_type, receipt_no, amount_cents, paid_on, method, status, void_reason,
                          voided_by, voided_at, created_by, created_at, origin, field_hlc)
SELECT id, account_id, 'OR', receipt_no, amount_cents, paid_on, method, status, void_reason,
       voided_by, voided_at, created_by, created_at, origin, field_hlc FROM payments;
DROP TABLE payments;
ALTER TABLE payments_new RENAME TO payments;
CREATE UNIQUE INDEX payments_receipt ON payments (receipt_type, receipt_no);
CREATE INDEX payments_account ON payments (account_id);
CREATE INDEX payments_paid_on ON payments (paid_on);
