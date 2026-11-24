-- What a payment line is for, so the cashier's tick boxes decide where the money goes
-- (site visit 2026-11-09: water bill, monthly dues and miscellaneous are paid and reported separately).
--   on a payment (credit) line: 'bill' | 'dues' | 'misc' pays that kind of charge first; '' = oldest first
--   on a charge line:           'misc' marks a cashier miscellaneous charge; '' = classified by kind
-- Every existing row gets '' and keeps today's oldest-first behaviour, so no balance changes.
ALTER TABLE ledger_entries ADD COLUMN applies_to TEXT NOT NULL DEFAULT '' CHECK (applies_to IN ('', 'bill', 'dues', 'misc'));
