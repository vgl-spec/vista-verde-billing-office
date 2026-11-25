-- The cashier's cash drawer, one business day at a time (office request 2026-11-25).
-- A day is opened with a counted opening balance and closed with a counted ending balance; the ending (minus any cash
-- taken out for the bank) is what the next day's opening should be. Earned today = closing - opening, and it must
-- tally with the day's cash collections (payments method 'cash', status 'posted', paid_on = the day).
-- Not synced: the drawer is the physical box at this office's counter. Counts are JSON {denomination: pieces}.
CREATE TABLE cash_drawers (
    id TEXT PRIMARY KEY,
    business_date TEXT NOT NULL UNIQUE,
    -- 'closed' exactly while a standing (posted) closing exists; close and void flip it in the same transaction with a
    -- conditional UPDATE, so two people closing at once cannot both win.
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opening_cents BIGINT NOT NULL CHECK (opening_cents >= 0),
    opening_counts TEXT NOT NULL,
    -- What the previous closed day left in the drawer, NULL on the very first day. Kept as it was at opening, so a
    -- later void of that closing does not silently rewrite this day's history.
    expected_opening_cents BIGINT,
    previous_drawer_id TEXT REFERENCES cash_drawers (id),
    opening_note TEXT NOT NULL DEFAULT '',
    opened_by TEXT NOT NULL REFERENCES users (id),
    opened_at TEXT NOT NULL
);

-- A closing is its own row so an administrator can void one (someone typed an ending that is not what was counted)
-- and the day is counted again; the voided row stays as the record of what was typed.
CREATE TABLE cash_drawer_closings (
    id TEXT PRIMARY KEY,
    drawer_id TEXT NOT NULL REFERENCES cash_drawers (id),
    status TEXT NOT NULL CHECK (status IN ('posted', 'voided')),
    closing_cents BIGINT NOT NULL CHECK (closing_cents >= 0),
    closing_counts TEXT NOT NULL,
    -- The day's cash collections when the drawer was closed; the report compares it with the live figure, so a
    -- payment voided or back-dated after closing shows up instead of hiding inside the tally.
    cash_collected_cents BIGINT NOT NULL,
    removed_cents BIGINT NOT NULL DEFAULT 0 CHECK (removed_cents >= 0 AND removed_cents <= closing_cents),
    note TEXT NOT NULL DEFAULT '',
    closed_by TEXT NOT NULL REFERENCES users (id),
    closed_at TEXT NOT NULL,
    void_reason TEXT,
    voided_by TEXT REFERENCES users (id),
    voided_at TEXT
);

-- At most one standing closing per day: a double press or two cashiers closing at once cannot both count.
CREATE UNIQUE INDEX cash_drawer_closings_one_posted ON cash_drawer_closings (drawer_id) WHERE status = 'posted';
CREATE INDEX cash_drawer_closings_drawer ON cash_drawer_closings (drawer_id);
-- Every cashier page asks "is an earlier day still open?": only open days are in this index (normally one), so the
-- answer stays instant after years of closed days.
CREATE INDEX cash_drawers_open ON cash_drawers (business_date) WHERE status = 'open';
-- A day's cash and non-cash totals (drawer pages, cashier banner) are read from this index alone, never the table rows.
CREATE INDEX payments_day_method ON payments (paid_on, method, status, amount_cents);
