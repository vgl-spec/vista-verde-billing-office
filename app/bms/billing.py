"""Rate plans, the progressive water calculation, settings access and statement numbers.

A rate plan config is {category: {"minimums": [m_0_5, m_6_10], "steps": [s_11_20 ... s_71_up]}}
with peso amounts as strings. Bills snapshot the plan id and the calculated lines, so changing
rates later never rewrites an existing bill.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple, Optional

from . import UserError, db
from .money import format_reading, parse_pesos, parse_reading, round_half_up

CATEGORIES = ('residential', 'commercial')
MINIMUM_LABELS = ('0–5 m³', '6–10 m³')
MINIMUM_CEILINGS = (Decimal(5), Decimal(10))
STEP_WIDTH = Decimal(10)
STEP_LABELS = ('11–20 m³', '21–30 m³', '31–40 m³', '41–50 m³', '51–60 m³', '61–70 m³', '71 m³ and above')
PERIOD_PATTERN = re.compile(r'\d{4}-(0[1-9]|1[0-2])')


@dataclass(frozen=True)
class BillLine:
    label: str
    qty: Optional[Decimal]       # None for a flat minimum
    rate_cents: Optional[int]    # per m³; None for a flat minimum
    amount_cents: int

    def to_dict(self):
        return {
            'label': self.label,
            'qty': None if self.qty is None else format_reading(self.qty),
            'rate_cents': self.rate_cents,
            'amount_cents': self.amount_cents,
        }


class Calculation(NamedTuple):
    usage: Decimal
    water_cents: int
    lines: list  # of BillLine; amounts sum exactly to water_cents


def calculate(previous, present, plan, category):
    """Water charge for one meter reading pair. Raises UserError on bad input (fail closed)."""
    if category not in CATEGORIES:
        raise UserError(f'Unknown account type: {category!r}.')
    usage = parse_reading(present) - parse_reading(previous)
    if usage < 0:
        raise UserError('Present reading cannot be lower than the previous reading. Check for a meter replacement or a typing mistake.')
    minimums, steps = _plan_rates(plan, category)
    tier = 0 if usage <= MINIMUM_CEILINGS[0] else 1
    lines = [BillLine(f'{MINIMUM_LABELS[tier]} (minimum)', None, None, minimums[tier])]
    for index, rate_cents in enumerate(steps):
        lower = MINIMUM_CEILINGS[1] + STEP_WIDTH * index
        qty = max(Decimal(0), usage - lower)
        if index < len(steps) - 1:
            qty = min(qty, STEP_WIDTH)
        if qty:
            lines.append(BillLine(STEP_LABELS[index], qty, rate_cents, round_half_up(qty * rate_cents)))
    return Calculation(usage, sum(line.amount_cents for line in lines), lines)


def lines_to_json(lines):
    return json.dumps([line.to_dict() for line in lines])


def validate_plan_config(config):
    """Reject anything that is not a complete, parseable plan (used before saving a new plan)."""
    if not isinstance(config, dict) or set(config) != set(CATEGORIES):
        raise UserError('A rate plan needs both residential and commercial rates.')
    for category in CATEGORIES:
        _plan_rates(config, category)


def _plan_rates(plan, category):
    try:
        entry = plan[category]
        minimums = [parse_pesos(value) for value in entry['minimums']]
        steps = [parse_pesos(value) for value in entry['steps']]
    except (KeyError, TypeError):
        raise UserError('Rate plan is incomplete.') from None
    if len(minimums) != len(MINIMUM_LABELS) or len(steps) != len(STEP_LABELS):
        raise UserError('A rate plan needs two minimums and seven per-m³ rates.')
    return minimums, steps


# --- rate plan access -------------------------------------------------------------------------

def get_plan(c, plan_id):
    return c.one('SELECT * FROM rate_plans WHERE id = ?', (plan_id,))


def current_plan(c, on=None):
    """Newest plan whose effective date has arrived (ties broken by creation time)."""
    return c.one(
        'SELECT * FROM rate_plans WHERE effective_from <= ? ORDER BY effective_from DESC, created_at DESC LIMIT 1',
        (on or db.today(),),
    )


def plan_config(plan_row):
    return json.loads(plan_row['config'])


# --- settings -----------------------------------------------------------------------------------

def get_setting(c, key):
    """JSON-decoded value; KeyError for an unknown key (settings are seeded by migration)."""
    raw = c.scalar('SELECT value FROM settings WHERE key = ?', (key,))
    if raw is None:
        raise KeyError(key)
    return json.loads(raw)


def set_setting(c, key, value, user):
    stamp = {'value': json.dumps(value), 'updated_at': db.now(), 'updated_by': user['id'] if user else None}
    if c.scalar('SELECT 1 FROM settings WHERE key = ?', (key,)):
        c.update('settings', key, stamp)
    else:
        c.insert('settings', {'key': key, **stamp})


# --- Maynilad bill and system loss (per billing month) ---------------------------------------------
#
# The office's own method (their written note, 2026-11-15): Maynilad rate = total Maynilad bill ÷ Maynilad m³
# (₱3,215,356.84 ÷ 55,619 = ₱57.81); system loss rate = system loss amount ÷ its m³
# (₱439,773.24 ÷ 21,692 = ₱20.27). Homeowners pay consumption × system loss rate as a separate line,
# less any HOA subsidy (June 2026 SOA: HOA paid 50% of ₱26.54, homeowner charged 16 m³ × ₱13.27).
# Rates are rounded to the centavo once, the way the office writes them on the statement.

BULK_KEY_PREFIX = 'bulk_water:'
CENTAVO = Decimal('0.01')


class BulkRates(NamedTuple):
    maynilad_rate: Decimal      # pesos per m³, 2 dp
    loss_rate: Decimal          # full system loss, pesos per m³, 2 dp
    charged_loss_rate: Decimal  # what a homeowner pays per m³ after the subsidy, 2 dp
    subsidy_percent: int


def bulk_key(period):
    return f'{BULK_KEY_PREFIX}{period}'


def get_bulk_water(c, period):
    """The saved Maynilad figures for a billing month, or None."""
    raw = c.scalar('SELECT value FROM settings WHERE key = ?', (bulk_key(period),))
    return json.loads(raw) if raw is not None else None


def bulk_rates(figures):
    """Per-m³ rates from saved figures; None when nothing is saved for the month."""
    if not figures:
        return None
    maynilad = (Decimal(figures['maynilad_bill_cents']) / 100 / Decimal(figures['maynilad_m3'])).quantize(CENTAVO, 'ROUND_HALF_UP')
    loss = (Decimal(figures['loss_cents']) / 100 / Decimal(figures['loss_m3'])).quantize(CENTAVO, 'ROUND_HALF_UP')
    subsidy = int(figures.get('subsidy_percent', 0))
    charged = (loss * (100 - subsidy) / 100).quantize(CENTAVO, 'ROUND_HALF_UP')
    return BulkRates(maynilad, loss, charged, subsidy)


def period_bulk_rates(c, period):
    return bulk_rates(get_bulk_water(c, period))


def parse_cubic_meters(text, label):
    """'55,619' or '55619.5' -> Decimal m³ above zero (a zero divisor would make no rate)."""
    value = str(text).strip().replace(',', '')
    try:
        m3 = parse_reading(value)
    except UserError:
        raise UserError(f'{label}: type cubic metres as a number, like 55,619.') from None
    if m3 <= 0:
        raise UserError(f'{label} must be more than zero.')
    return m3


def current_period(c):
    """Billing period being worked on (YYYY-MM): the `current_period` setting, else LAST month.

    The office bills a month after it ends (site visit: "if it is November now, October should appear"), so a
    new install starts on the previous month; the administrator moves it on from Reading & SOA.
    """
    raw = c.scalar("SELECT value FROM settings WHERE key = 'current_period'")
    if raw is not None:
        return json.loads(raw)
    first_of_month = date.fromisoformat(db.today()).replace(day=1)
    return (first_of_month - timedelta(days=1)).strftime('%Y-%m')


# --- statement numbers --------------------------------------------------------------------------

def next_statement_no(c, period):
    """'<node_short>-YYYYMM-<seq:05d>'; seq counts per node and period.

    Must run inside the issuing transaction: the write lock makes read-max-then-insert safe, and the
    node prefix means two offices never need to coordinate numbers.
    """
    if not PERIOD_PATTERN.fullmatch(period):
        raise UserError('Use a billing period in YYYY-MM format.')
    db.ensure_node(c)
    node_short = c.scalar("SELECT value FROM sync_state WHERE key = 'node_short'")
    prefix = f"{node_short}-{period.replace('-', '')}-"
    last = c.scalar('SELECT MAX(statement_no) FROM bills WHERE statement_no LIKE ?', (prefix + '%',))
    sequence = int(last[len(prefix):]) + 1 if last else 1
    return f'{prefix}{sequence:05d}'
