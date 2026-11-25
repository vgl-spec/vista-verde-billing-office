"""The cashier's cash drawer: counting it, what each day should hold, and the rule that keeps cash payments in a day.

One business day = one row in `cash_drawers` (opened with a counted opening balance) plus at most one standing row in
`cash_drawer_closings` (the counted ending balance). Tally for a day:

    earned today      = closing - opening
    cash collections  = posted cash payments dated that day (the same figure as the Collections report's cash line)
    difference        = earned today - cash collections   (0 = tally, below 0 = short, above 0 = over)

Only cash goes into the drawer; check, GCash and bank payments are reported beside it, never in the tally.
"""
import json
from dataclasses import dataclass

from . import UserError, db
from .money import format_pesos, parse_pesos

# Philippine notes and coins counted by pieces, largest first, in centavos (the P20 coin and note count together).
DENOMINATIONS = (100000, 50000, 20000, 10000, 5000, 2000, 1000, 500, 100)
LOOSE_COINS = 'coins'          # centavo coins are typed as one amount; nobody counts 25-centavo pieces one by one
MAX_PIECES = 99999
MAX_LOOSE_COINS_CENTS = 100000  # more than P1,000 in centavo coins is a typing mistake, not a drawer


@dataclass(frozen=True)
class DaySummary:
    """One business day's figures. `closing_cents` and the closing-only fields are None while the day is open."""
    opening_cents: int
    cash_collected_cents: int        # live: recomputed from payments every time
    other_collected_cents: int       # check, GCash, bank, other: not in the drawer
    closing_cents: int | None
    removed_cents: int | None
    collected_at_closing_cents: int | None

    @property
    def expected_in_drawer_cents(self):
        return self.opening_cents + self.cash_collected_cents

    @property
    def earned_cents(self):
        return None if self.closing_cents is None else self.closing_cents - self.opening_cents

    @property
    def difference_cents(self):
        """Counted minus expected: negative = short, positive = over. None while the day is open."""
        return None if self.closing_cents is None else self.earned_cents - self.cash_collected_cents

    @property
    def left_in_drawer_cents(self):
        return None if self.closing_cents is None else self.closing_cents - self.removed_cents

    @property
    def changed_after_closing_cents(self):
        """Cash collections added or voided after the day was closed (0 when nothing moved)."""
        if self.collected_at_closing_cents is None:
            return 0
        return self.cash_collected_cents - self.collected_at_closing_cents


# --- counting -------------------------------------------------------------------------------------------

def field_name(denomination):
    return f'count_{denomination}'


def denomination_label(denomination):
    return f'₱{denomination // 100:,}'


def parse_counts(form):
    """The counted pieces from a count form -> {'100000': pieces, ..., 'coins': centavos}. Blank means none.

    Raises UserError naming the box that is wrong, so the cashier knows which line to fix.
    """
    counts = {}
    for denomination in DENOMINATIONS:
        text = str(form.get(field_name(denomination), '')).strip().replace(',', '')
        if text == '':
            pieces = 0
        elif text.isdigit() and len(text) <= len(str(MAX_PIECES)) and int(text) <= MAX_PIECES:
            pieces = int(text)
        else:
            raise UserError(f'{denomination_label(denomination)}: type how many pieces, a whole number from 0 to {MAX_PIECES:,}.')
        counts[str(denomination)] = pieces
    coins_text = str(form.get(LOOSE_COINS, '')).strip()
    coins = parse_pesos(coins_text) if coins_text else 0
    if coins > MAX_LOOSE_COINS_CENTS:
        raise UserError(f'Coins below ₱1: {format_pesos(coins)} is more than {format_pesos(MAX_LOOSE_COINS_CENTS)}. '
                        'Count the ₱1 and larger coins in their own boxes.')
    counts[LOOSE_COINS] = coins
    return counts


def count_total(counts):
    return sum(int(denomination) * counts.get(str(denomination), 0) for denomination in DENOMINATIONS) + counts.get(LOOSE_COINS, 0)


def dump_counts(counts):
    return json.dumps(counts, sort_keys=True)


def load_counts(text):
    stored = json.loads(text or '{}')
    return {str(d): int(stored.get(str(d), 0)) for d in DENOMINATIONS} | {LOOSE_COINS: int(stored.get(LOOSE_COINS, 0))}


def count_form_values(counts):
    """Counts -> the form's own field values (for prefilling a count form or carrying it to the review page)."""
    values = {field_name(d): str(counts[str(d)]) if counts.get(str(d)) else '' for d in DENOMINATIONS}
    values[LOOSE_COINS] = format_pesos(counts[LOOSE_COINS]).lstrip('₱') if counts.get(LOOSE_COINS) else ''
    return values


# --- reading the drawer -------------------------------------------------------------------------------

DRAWER_COLUMNS = ('d.id, d.business_date, d.status, d.opening_cents, d.opening_counts, d.expected_opening_cents, '
                  'd.previous_drawer_id, d.opening_note, d.opened_by, d.opened_at')


def drawer_on(c, day):
    """The drawer opened for this business date, or None."""
    return c.one(f'SELECT {DRAWER_COLUMNS} FROM cash_drawers d WHERE d.business_date = ?', (day,))


def load_drawer(c, drawer_id):
    return c.one(f'SELECT {DRAWER_COLUMNS} FROM cash_drawers d WHERE d.id = ?', (drawer_id,))


def posted_closing(c, drawer_id):
    return c.one("SELECT * FROM cash_drawer_closings WHERE drawer_id = ? AND status = 'posted'", (drawer_id,))


def unclosed_before(c, day):
    """The earliest day before `day` that was opened and never closed (its closing missing or voided), or None.
    Answered from the partial index of open days, so it stays instant however many days have been closed."""
    return c.one("SELECT id, business_date FROM cash_drawers WHERE status = 'open' AND business_date < ? "
                 'ORDER BY business_date LIMIT 1', (day,))


def last_closed_before(c, day):
    """The latest day before `day` with a standing closing: (drawer row, closing row), or (None, None)."""
    drawer = c.one(f"SELECT {DRAWER_COLUMNS} FROM cash_drawers d WHERE d.status = 'closed' "
                   'AND d.business_date < ? ORDER BY d.business_date DESC LIMIT 1', (day,))
    if drawer is None:
        return None, None
    return drawer, posted_closing(c, drawer['id'])


def day_collections(c, day):
    """(cash, everything else) collected on this day, posted payments only, in one index-only query."""
    row = c.one("SELECT COALESCE(SUM(CASE WHEN method = 'cash' THEN amount_cents ELSE 0 END), 0) AS cash, "
                "COALESCE(SUM(CASE WHEN method <> 'cash' THEN amount_cents ELSE 0 END), 0) AS other "
                "FROM payments WHERE paid_on = ? AND status = 'posted'", (day,))
    return row['cash'], row['other']


def summarize(c, drawer, closing=None):
    """The day's figures; pass the standing closing (or None while the day is open)."""
    cash, other = day_collections(c, drawer['business_date'])
    return DaySummary(
        opening_cents=drawer['opening_cents'],
        cash_collected_cents=cash,
        other_collected_cents=other,
        closing_cents=closing['closing_cents'] if closing else None,
        removed_cents=closing['removed_cents'] if closing else None,
        collected_at_closing_cents=closing['cash_collected_cents'] if closing else None,
    )


# --- the cash payment rule ----------------------------------------------------------------------------

def check_cash_payment(c, paid_on):
    """Refuse a cash payment that would make a day's drawer impossible to tally.

    Today's cash needs today's drawer opened first. A day already closed takes no more cash (its ending was counted);
    an administrator voids that closing first if a payment really belongs there. Days with no drawer at all are
    earlier history (before the drawer was used, or recorded late) and are not blocked.
    """
    drawer = drawer_on(c, paid_on)
    if drawer is None:
        if paid_on == db.today():
            raise UserError('Open today’s cash drawer first: Cashier > Cash drawer, count the opening cash. '
                            'Cash payments are saved only after the day is opened.')
        return
    if drawer['status'] == 'closed':
        raise UserError(f'The cash drawer for {paid_on} is already closed, so no more cash can be recorded on that day. '
                        'If this payment really belongs to that day, an administrator voids that closing first.')
