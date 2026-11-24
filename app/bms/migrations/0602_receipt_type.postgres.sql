-- Which paper the cashier gave: SI (BIR Service Invoice), OR (Official Receipt) or AR (Acknowledgement
-- Receipt). A receipt is unique per (type, number). Rationale: see 0602_receipt_type.sqlite.sql.
ALTER TABLE payments ADD COLUMN receipt_type TEXT NOT NULL DEFAULT 'OR' CHECK (receipt_type IN ('SI', 'OR', 'AR'));
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_receipt_no_key;
CREATE UNIQUE INDEX payments_receipt ON payments (receipt_type, receipt_no);
