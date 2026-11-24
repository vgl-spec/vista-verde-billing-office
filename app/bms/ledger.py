"""Append-only account ledger. Balance = SUM(amount_cents); positive = owed, negative = credit.

Entries are never edited or deleted: a correction posts a `reversal` that points at the original
(UNIQUE reverses_id, so an entry can be reversed at most once).
"""
from collections import defaultdict
from dataclasses import dataclass, field

from . import UserError, db
from .billing import PERIOD_PATTERN

CHARGE_KINDS = frozenset({'water', 'dues', 'penalty', 'system_loss', 'other'})
SIGNED_KINDS = frozenset({'opening', 'adjustment'})  # either direction
POSTABLE_KINDS = CHARGE_KINDS | SIGNED_KINDS | {'payment'}  # 'reversal' only via reverse()
# What a line is for (column applies_to). A payment line tagged with a purpose pays charges of that purpose
# first; an untagged credit (older payments, opening credits, adjustments) pays the oldest charges first.
PURPOSES = ('bill', 'dues', 'misc')


@dataclass(frozen=True)
class OpenCharge:
    entry_id: str
    kind: str
    period: str
    posted_on: str
    memo: str
    original_cents: int
    open_cents: int
    purpose: str = 'bill'  # which cashier tick box pays it (charge_purpose)


@dataclass(frozen=True)
class Aging:
    open_charges: list     # oldest first, each with open_cents > 0
    credit_cents: int      # payments not yet matched to any charge
    # Part of credit_cents kept for one purpose, e.g. {'dues': 20000} = two months of dues paid ahead.
    credit_by_purpose: dict = field(default_factory=dict)


def post(c, account_id, kind, amount_cents, *, period, memo, user, bill_id=None, payment_id=None, posted_on=None,
         entry_id=None, applies_to=''):
    """Post one ledger entry and return its id. The sign must match the kind (fail closed).

    `entry_id` lets a caller derive a deterministic id (uuid5) so the same charge can never be posted twice,
    even from two nodes that later sync. `applies_to` is the line's purpose (see PURPOSES).
    """
    _check_entry(kind, amount_cents, period)
    if applies_to and applies_to not in PURPOSES:
        raise ValueError(f'Unknown purpose {applies_to!r}; use one of {PURPOSES}.')
    if applies_to and kind != 'payment' and (amount_cents < 0 or kind not in CHARGE_KINDS | {'opening'}):
        raise ValueError('Only payments and charges carry a purpose.')
    row = {
        'account_id': account_id,
        'posted_on': posted_on or db.today(),
        'kind': kind,
        'amount_cents': amount_cents,
        'period': period,
        'bill_id': bill_id,
        'payment_id': payment_id,
        'memo': memo,
        'created_by': user['id'] if user else None,
        'created_at': db.now(),
        'applies_to': applies_to,
    }
    if entry_id:
        row['id'] = entry_id
    return c.insert('ledger_entries', row)


def reverse(c, entry_id, reason, user):
    """Post the exact opposite of an entry. Returns the reversal's id; UserError if not allowed."""
    if not reason or not reason.strip():
        raise UserError('Give a reason for the reversal.')
    entry = c.one('SELECT * FROM ledger_entries WHERE id = ?', (entry_id,))
    if entry is None:
        raise UserError('That ledger entry does not exist.')
    if entry['kind'] == 'reversal':
        raise UserError('A reversal cannot be reversed. Post a new entry instead.')
    if c.scalar('SELECT 1 FROM ledger_entries WHERE reverses_id = ?', (entry_id,)):
        raise UserError('That entry was already reversed.')
    try:
        return c.insert('ledger_entries', {
            'account_id': entry['account_id'],
            'posted_on': db.today(),
            'kind': 'reversal',
            'amount_cents': -entry['amount_cents'],
            'period': entry['period'],
            'bill_id': entry['bill_id'],
            'payment_id': entry['payment_id'],
            'reverses_id': entry_id,
            'memo': reason.strip(),
            'created_by': user['id'] if user else None,
            'created_at': db.now(),
        })
    except db.IntegrityError:
        # Lost a race with a concurrent reversal of the same entry.
        raise UserError('That entry was already reversed.') from None


def balance(c, account_id):
    return c.scalar('SELECT COALESCE(SUM(amount_cents), 0) FROM ledger_entries WHERE account_id = ?', (account_id,))


def aging(c, account_id):
    entries = c.all(
        'SELECT id, kind, amount_cents, period, posted_on, created_at, memo, reverses_id, bill_id, applies_to '
        'FROM ledger_entries WHERE account_id = ?',
        (account_id,),
    )
    return fifo_open_charges(entries)


def charge_purpose(entry):
    """Which tick box on the cashier screen pays this charge: dues, a cashier miscellaneous charge, or the bill.

    Dues printed on a water statement (setting "dues on the water bill") are part of that bill, so they go
    with the bill; dues charged on their own (at the cashier, or old balances) are the "monthly dues" box.
    """
    if entry.get('applies_to'):  # tagged when posted: cashier misc charges, imported dues arrears
        return entry['applies_to']
    return 'dues' if entry['kind'] == 'dues' and not entry.get('bill_id') else 'bill'


def fifo_open_charges(entries):
    """Match credits to charges; what is left open is owed, oldest first.

    1. A payment line tagged with a purpose (the cashier ticked "monthly dues", say) pays charges of that
       purpose, oldest first. What it does not use stays as credit for that purpose only (dues paid ahead
       never pay an old water bill).
    2. Untagged credits (payments recorded before tick boxes existed, opening credits, adjustments) then pay
       whatever is still open, oldest first; penalties before principal within a period.

    Reversed entries and their reversals cancel out and are skipped. Pure function over dicts with
    id, kind, amount_cents, period, posted_on, created_at, memo, reverses_id and (optional) applies_to.
    """
    reversed_ids = {entry['reverses_id'] for entry in entries if entry['reverses_id']}
    live = [e for e in entries if e['kind'] != 'reversal' and e['id'] not in reversed_ids]
    charges = sorted((e for e in live if e['amount_cents'] > 0), key=_charge_order)
    remaining = {e['id']: e['amount_cents'] for e in charges}

    targeted = defaultdict(int)
    general = 0
    for entry in live:
        if entry['amount_cents'] < 0:
            purpose = entry.get('applies_to') or ''
            if purpose:
                targeted[purpose] += -entry['amount_cents']
            else:
                general += -entry['amount_cents']

    left_by_purpose = {}
    for purpose, credit in targeted.items():
        for entry in charges:
            if credit == 0:
                break
            if charge_purpose(entry) == purpose:
                applied = min(credit, remaining[entry['id']])
                remaining[entry['id']] -= applied
                credit -= applied
        if credit:
            left_by_purpose[purpose] = credit

    open_charges = []
    for entry in charges:
        applied = min(general, remaining[entry['id']])
        general -= applied
        left = remaining[entry['id']] - applied
        if left:
            open_charges.append(OpenCharge(
                entry['id'], entry['kind'], _charge_period(entry), entry['posted_on'], entry['memo'],
                entry['amount_cents'], left, charge_purpose(entry)))
    return Aging(open_charges, general + sum(left_by_purpose.values()), left_by_purpose)


def _charge_period(entry):
    return entry['period'] or entry['posted_on'][:7]


def _charge_order(entry):
    # Penalty first inside a period is the proposed policy, flagged for the office to confirm.
    return (_charge_period(entry), 0 if entry['kind'] == 'penalty' else 1, entry['posted_on'], entry['created_at'], entry['id'])


def _check_entry(kind, amount_cents, period):
    if kind not in POSTABLE_KINDS:
        raise ValueError(f'Cannot post a ledger entry of kind {kind!r}.')
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents == 0:
        raise ValueError('Ledger amounts are non-zero whole centavos.')
    if kind in CHARGE_KINDS and amount_cents < 0:
        raise ValueError(f'A {kind} entry is a charge and must be positive.')
    if kind == 'payment' and amount_cents > 0:
        raise ValueError('A payment entry is a credit and must be negative.')
    if period is not None and not PERIOD_PATTERN.fullmatch(period):
        raise ValueError(f'Bad period {period!r}; use YYYY-MM.')
