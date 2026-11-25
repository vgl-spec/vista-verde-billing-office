"""Statements of account, the billing calendar, office settings with defaults, and account aging.

Shared by the cycle, payments and reports screens. Everything here reads; nothing writes.

Billing calendar: period 'YYYY-MM' is the month the meter is read (on the 11th). It covers the
12th of the month before through the 11th of the period month; bills are issued and fall due in
the period month (issue_day / due_day settings) and penalties start on penalty_day.
"""
import calendar
import json
import re
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape as xml_escape

from . import ledger, ui
from .config import APP_DIR

DOCX_TEMPLATE = APP_DIR.parent / 'templates' / 'Statement of Account Template.docx'
DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

# Settings the admin screen may not have saved yet. Seeded keys (0001) are repeated so a missing
# row never crashes a statement; the stored value always wins.
SETTING_DEFAULTS = {
    'association_name': 'Vista Verde Homeowners Association',
    'association_address': '',
    'association_contact': '',
    'dues_cents': 10000,
    'penalty_rate_percent': 3,
    'penalty_day': 28,
    'due_day': 27,
    'issue_day': 22,
    'policy_confirmed': False,
    'maynilad_rate_per_m3': '56.68',
    'disconnect_after_months': 1,
    # The office stopped charging penalties (site visit 2026-11-09); a statement shows the
    # penalty note only when this is on or the bill itself asks for it.
    'show_penalty_note': False,
    # Site visit notes (2026-11-09, statement format): monthly dues are a separate billing with a BIR
    # invoice, and the disconnection notice is not printed. Admin settings can turn either back on.
    'dues_on_water_bill': False,
    'show_disconnection_note': False,
    'penalty_note': '',
    'disconnection_note': ('Only one (1) month delay of water payment is allowed. An account with an unpaid '
                           'balance past that is subject to disconnection without further notice.'),
    'payment_instructions': ('Pay at the HOA office during office hours and bring this statement. '
                             'Always ask for an official receipt (OR).'),
}
KIND_LABELS = {
    'water': 'Water', 'dues': 'Association dues', 'penalty': 'Penalty', 'system_loss': 'System loss',
    'other': 'Other charge', 'opening': 'Balance from old records', 'payment': 'Payment',
    'reversal': 'Correction', 'adjustment': 'Adjustment',
}
AGING_BUCKETS = ('0–30 days', '31–60 days', '61–90 days', 'Over 90 days')
_TABLE_ROW_START = re.compile(r'<w:tr[ >]')


# --- settings ---------------------------------------------------------------------------------

def office_settings(c):
    """Every known setting (stored value, else default) in one query."""
    stored = {row['key']: json.loads(row['value']) for row in c.all('SELECT key, value FROM settings')}
    return {key: stored.get(key, default) for key, default in SETTING_DEFAULTS.items()}


def penalty_note(settings):
    if settings['penalty_note']:
        return settings['penalty_note']
    return (f"A {settings['penalty_rate_percent']}% penalty per month is added to any unpaid amount "
            f"from the {ordinal(settings['penalty_day'])} day of the month.")


def statement_notes(detail, settings):
    """Which notes this statement prints. A draft's own choice (detail) beats the office default."""
    return {
        'penalty': detail.get('show_penalty', bool(settings['show_penalty_note'])),
        'disconnection': detail.get('show_disconnection', bool(settings['show_disconnection_note'])),
        'note': detail.get('note', ''),
    }


def ordinal(number):
    number = int(number)
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    return f'{number}{suffix}'


# --- calendar ----------------------------------------------------------------------------------

def add_months(period, months):
    year, month = (int(part) for part in period.split('-'))
    index = year * 12 + month - 1 + months
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def day_in(period, day):
    """Date for `day` of the period month, clamped to the month's length (due day 31 in February)."""
    year, month = (int(part) for part in period.split('-'))
    return date(year, month, min(int(day), calendar.monthrange(year, month)[1]))


def month_label(period):
    return day_in(period, 1).strftime('%B %Y')


def short_month(period):
    return day_in(period, 1).strftime('%b %Y')


def long_date(iso_day):
    return date.fromisoformat(iso_day).strftime('%B %d, %Y') if iso_day else ''


def weekday_date(iso_day):
    """'2026-11-25' -> 'Wednesday, November 25, 2026' (the cash drawer asks staff to confirm the day by name)."""
    return date.fromisoformat(iso_day).strftime('%A, %B %d, %Y') if iso_day else ''


def period_covered(period):
    start, end = day_in(add_months(period, -1), 12), day_in(period, 11)
    return f"{start.strftime('%B %d, %Y')} to {end.strftime('%B %d, %Y')}"


# --- aging ---------------------------------------------------------------------------------------

LEDGER_COLUMNS = 'id, account_id, kind, amount_cents, period, posted_on, created_at, memo, reverses_id, bill_id, applies_to, payment_id'


def entries_by_account(c, account_filter_sql, params=()):
    """Ledger entries grouped per account in one query. `account_filter_sql` is trusted SQL
    selecting account ids (e.g. a subquery), never user text."""
    grouped = defaultdict(list)
    for entry in c.all(f'SELECT {LEDGER_COLUMNS} FROM ledger_entries WHERE account_id IN ({account_filter_sql})', params):
        grouped[entry['account_id']].append(entry)
    return grouped


def bucket_open_charges(open_charges, today):
    """Sum open amounts into the four aging buckets by days since the charge was posted."""
    totals = [0, 0, 0, 0]
    for charge in open_charges:
        age = (today - date.fromisoformat(charge.posted_on)).days
        index = 0 if age <= 30 else 1 if age <= 60 else 2 if age <= 90 else 3
        totals[index] += charge.open_cents
    return totals


def describe_open_charge(charge):
    label = KIND_LABELS.get(charge.kind, charge.kind)
    if charge.memo and charge.kind in ('other', 'opening', 'adjustment', 'system_loss'):
        # A memo that already names the charge ("System loss: 15 m³ × ₱20.27") replaces the label instead of repeating it.
        label = charge.memo if charge.memo.lower().startswith(label.lower()) else f'{label} ({charge.memo})'
    return f'{label} – {short_month(charge.period)}'


# --- statements ---------------------------------------------------------------------------------

def load_statements(c, bill_ids):
    """View models for the given bills, ordered by route then account number. Four queries total."""
    if not bill_ids:
        return []
    marks = ', '.join('?' for _ in bill_ids)
    bills = c.all(
        'SELECT b.*, a.account_no, a.name, a.address, a.route, a.category, p.name AS phase_name '
        'FROM bills b JOIN accounts a ON a.id = b.account_id JOIN phases p ON p.id = a.phase_id '
        f"WHERE b.id IN ({marks}) AND b.status <> 'cancelled' ORDER BY a.route, a.account_no, b.period",
        tuple(bill_ids))
    if not bills:
        return []
    account_sql = f'SELECT account_id FROM bills WHERE id IN ({marks})'
    entries = entries_by_account(c, account_sql, tuple(bill_ids))
    issued = defaultdict(list)
    for row in c.all(f"SELECT account_id, period, issued_on FROM bills WHERE status = 'issued' AND account_id IN ({account_sql})",
                     tuple(bill_ids)):
        issued[row['account_id']].append(row)
    payments = defaultdict(list)
    for row in c.all('SELECT account_id, receipt_type, receipt_no, paid_on, amount_cents, created_at FROM payments '
                     f"WHERE status = 'posted' AND account_id IN ({account_sql}) ORDER BY paid_on, created_at",
                     tuple(bill_ids)):
        payments[row['account_id']].append(row)
    return [_statement_model(bill, entries[bill['account_id']], issued[bill['account_id']], payments[bill['account_id']])
            for bill in bills]


def _statement_model(bill, entries, issued_bills, payments):
    own = [e for e in entries if e['bill_id'] == bill['id']]
    # The statement is a snapshot at issue: anything recorded after this bill's own ledger lines
    # (later payments, later corrections) belongs on the next statement.
    cutoff = min((e['created_at'] for e in own), default=None)
    before = [e for e in entries if e['bill_id'] != bill['id'] and (cutoff is None or e['created_at'] < cutoff)]
    aging = ledger.fifo_open_charges(before)
    prior_items = [(describe_open_charge(charge), charge.open_cents) for charge in aging.open_charges]
    if aging.credit_cents:
        prior_items.append(('Advance payment (credit)', -aging.credit_cents))
    prior_cents = sum(amount for _, amount in prior_items)
    last_issued = max((b['issued_on'] for b in issued_bills if b['period'] < bill['period'] and b['issued_on']), default='')
    received = [p for p in payments if p['paid_on'] > last_issued and (cutoff is None or p['created_at'] < cutoff)]
    return {
        'bill': bill,
        'detail': json.loads(bill['detail']),
        'is_draft': bill['status'] == 'draft',
        'prior_items': prior_items,
        'prior_cents': prior_cents,
        'payments': received,
        'amount_due_cents': prior_cents + bill['total_cents'],
    }


COPY_LABELS = ('Members copy', 'HOA copy')  # the office's own wording (Jema)


def render_statement(model, settings):
    """One A4 sheet (`.doc-page`, landscape) holding the same statement twice: the member keeps one
    half, the office files the other. The office asked for this to halve paper for ~1,800 bills."""
    copies = ''.join(_statement_copy(model, settings, label) for label in COPY_LABELS)
    return ui.raw(f'<article class="doc doc-page soa-sheet">{copies}</article>')


def _statement_copy(model, settings, copy_label):
    e = ui.esc
    bill, detail = model['bill'], model['detail']
    period = bill['period']
    draft_banner = '<p class="soa-draft">DRAFT – not yet issued. Do not deliver.</p>' if model['is_draft'] else ''
    contact = ' · '.join(part for part in (settings['association_address'], settings['association_contact']) if part)

    bracket_rows = ''.join(
        f"<tr><td>{e(line['label'])}</td><td class=\"num\">{e(line['qty'] or '')}</td>"
        f"<td class=\"num\">{e(ui.money(line['rate_cents'])) if line['rate_cents'] is not None else 'flat'}</td>"
        f"<td class=\"num\">{ui.money(line['amount_cents'])}</td></tr>"
        for line in detail['lines'])
    other_rows = ''.join(
        f"<tr><td colspan=\"3\">{e(other_line_label(line))}</td><td class=\"num\">{ui.money(line['amount_cents'])}</td></tr>"
        for line in detail.get('other_lines', []))
    prior_rows = ''.join(
        f'<tr><td>{e(label)}</td><td class="num">{ui.money(amount)}</td></tr>' for label, amount in model['prior_items']
    ) or '<tr><td>No unpaid balance from earlier statements.</td><td class="num">₱0.00</td></tr>'
    payment_rows = ''.join(
        f"<tr><td>{e(long_date(p['paid_on']))}</td><td>{e(p['receipt_type'])} {e(p['receipt_no'])}</td><td class=\"num\">{ui.money(p['amount_cents'])}</td></tr>"
        for p in model['payments']
    ) or '<tr><td colspan="3">No payments received since the last statement.</td></tr>'
    dues_label = f"Association dues ({detail['dues_months']} month{'s' if detail['dues_months'] != 1 else ''})"
    dues_row = (f'<tr><td colspan="3">{e(dues_label)}</td><td class="num">{ui.money(bill["dues_cents"])}</td></tr>'
                if detail['dues_months'] else '')
    loss = detail.get('system_loss')
    # Same line as the office's statement: "System Loss  16 cu.m. × 13.27".
    loss_row = (f'<tr><td>System loss</td><td class="num">{e(loss["m3"])}</td><td class="num">{ui.money(loss["rate_cents"])}</td>'
                f'<td class="num">{ui.money(loss["amount_cents"])}</td></tr>' if loss else '')
    issued_on = long_date(bill['issued_on']) if bill['issued_on'] else '—'
    notes = statement_notes(detail, settings)
    note_rows = ''.join(f'<p><strong>{e(title)}</strong> {e(text)}</p>' for title, text, shown in (
        ('Note.', notes['note'], bool(notes['note'])),
        ('System loss.', subsidy_note(loss, period), bool(loss and loss['subsidy_percent'])),
        ('Penalty.', penalty_note(settings), notes['penalty']),
        ('Disconnection notice.', settings['disconnection_note'], notes['disconnection']),
        ('How to pay.', settings['payment_instructions'], True),
    ) if shown)

    return f"""<section class="soa soa-copy" aria-label="{e(copy_label)}">
<p class="soa-copy-label">{e(copy_label)}</p>
{draft_banner}
<header class="soa-head">
  <div class="soa-org"><img class="soa-logo" src="/assets/vistaverde-logo.jpg" alt="" width="72" height="72">
    <div><p class="soa-org-name">{e(settings['association_name'])}</p><p class="soa-org-line">{e(contact)}</p></div></div>
  <div class="soa-title"><h1>Statement of Account</h1>
    <dl class="soa-meta"><div><dt>Date issued</dt><dd>{e(issued_on)}</dd></div>
    <div><dt>Due date</dt><dd>{e(long_date(bill['due_on']))}</dd></div></dl></div>
</header>
<section class="soa-parties">
  <dl class="soa-meta"><div><dt>Meter no.</dt><dd>{e(detail.get('meter_no') or '—')}</dd></div>
  <div><dt>Name</dt><dd>{e(bill['name'])}</dd></div>
  <div><dt>Address</dt><dd>{e(bill['address'])}</dd></div></dl>
  <dl class="soa-meta"><div><dt>Billing month</dt><dd>{e(month_label(period))}</dd></div>
  <div><dt>Period covered</dt><dd>{e(period_covered(period))}</dd></div></dl>
</section>
<p class="soa-cap">Meter reading</p>
<table class="soa-table">
  <thead><tr><th class="num">Previous reading</th><th class="num">Present reading</th><th class="num">Consumption</th></tr></thead>
  <tbody><tr><td class="num">{e(detail['previous'])}</td><td class="num">{e(detail['present'])}</td><td class="num">{e(detail['usage'])} m³</td></tr></tbody>
</table>
<p class="soa-cap">Current charges</p>
<table class="soa-table">
  <thead><tr><th>Water ({e(detail['category'])} rate)</th><th class="num">m³</th><th class="num">Rate per m³</th><th class="num">Amount</th></tr></thead>
  <tbody>{bracket_rows}
  <tr class="soa-sub"><td colspan="3">Water subtotal</td><td class="num">{ui.money(bill['water_cents'])}</td></tr>
  {loss_row}
  {dues_row}
  {other_rows}
  <tr class="soa-sub"><td colspan="3">Total current charges</td><td class="num">{ui.money(bill['total_cents'])}</td></tr></tbody>
</table>
<p class="soa-cap">Previous balance (unpaid amounts from earlier statements)</p>
<table class="soa-table">
  <tbody>{prior_rows}
  <tr class="soa-sub"><td>Total previous balance</td><td class="num">{ui.money(model['prior_cents'])}</td></tr></tbody>
</table>
<p class="soa-cap">Payments received since the last statement (already deducted above)</p>
<table class="soa-table">
  <tbody>{payment_rows}</tbody>
</table>
<p class="soa-total"><span>Total amount due</span><strong>{ui.money(model['amount_due_cents'])}</strong></p>
<section class="soa-notes">{note_rows}</section>
</section>"""


def system_loss_label(loss):
    return f"System loss: {loss['m3']} m³ × {ui.money(loss['rate_cents'])}"


def subsidy_note(loss, period):
    """The office's red line on the statement, e.g. 'HOA subsidized 13.27 pesos (50%) out of ...'."""
    if not loss or not loss['subsidy_percent']:
        return ''
    paid_by_hoa = loss['full_rate_cents'] - loss['rate_cents']
    return (f"The HOA subsidized {ui.money(paid_by_hoa)} per m³ ({loss['subsidy_percent']}%) out of the total system loss "
            f"of {ui.money(loss['full_rate_cents'])} per m³ for {month_label(period)}.")


def other_line_label(line):
    return f"{line['label']} – {line['memo']}" if line.get('memo') else line['label']


def render_docx(model, settings):
    """Fill the office's Word template; returns bytes, or None when the template is missing."""
    if not DOCX_TEMPLATE.is_file():
        return None
    with zipfile.ZipFile(DOCX_TEMPLATE) as template:
        parts = {info: template.read(info.filename) for info in template.infolist()}
    bill, detail = model['bill'], model['detail']
    notes = statement_notes(detail, settings)
    charges = [(line['label'], _line_basis(line), ui.money(line['amount_cents'])) for line in detail['lines']]
    loss = detail.get('system_loss')
    if loss:
        charges.append(('System loss', f"{loss['m3']} m³ × {ui.money(loss['rate_cents'])}", ui.money(loss['amount_cents'])))
    if detail['dues_months']:
        charges.append(('Association dues', f"{detail['dues_months']} month(s)", ui.money(bill['dues_cents'])))
    charges += [(other_line_label(line), '', ui.money(line['amount_cents'])) for line in detail.get('other_lines', [])]
    values = {
        'association_name': settings['association_name'],
        'association_address': settings['association_address'],
        'association_contact': settings['association_contact'],
        'account_name': f"{bill['name']} (account {bill['account_no']})",
        'statement_number': '(given when issued)' if model['is_draft'] else bill['statement_no'],
        'property_address': bill['address'],
        'billing_period': f"{month_label(bill['period'])} ({period_covered(bill['period'])})",
        'area_phase': bill['phase_name'],
        'due_date': long_date(bill['due_on']),
        'meter_service': f"Water, meter {detail.get('meter_no') or '—'}",
        'previous_reading': detail['previous'],
        'current_reading': detail['present'],
        'usage_and_unit': f"{detail['usage']} m³",
        'previous_balance': ui.money(model['prior_cents']),
        'payments_and_credits': ui.money(sum(p['amount_cents'] for p in model['payments'])) + ' (already deducted)',
        'current_charges': ui.money(bill['total_cents']),
        'total_amount_due': ui.money(model['amount_due_cents']),
        'payment_instructions': settings['payment_instructions'],
        'billing_notes': '\n'.join(text for text, shown in (
            (notes['note'], True), (penalty_note(settings), notes['penalty']),
            (settings['disconnection_note'], notes['disconnection'])) if shown and text),
    }
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
        for info, data in parts.items():
            if info.filename == 'word/document.xml':
                data = _fill_document(data.decode('utf-8'), values, charges, model['is_draft']).encode('utf-8')
            target.writestr(info, data)
    return output.getvalue()


def _line_basis(line):
    if line['rate_cents'] is None:
        return 'Minimum charge'
    return f"{line['qty']} m³ × {ui.money(line['rate_cents'])}"


def _fill_document(xml, values, charges, is_draft):
    anchor = xml.index('{charge_description}')
    start = [m.start() for m in _TABLE_ROW_START.finditer(xml, 0, anchor)][-1]
    end = xml.index('</w:tr>', anchor) + len('</w:tr>')
    row = xml[start:end]
    rows = ''.join(row.replace('{charge_description}', _docx_text(a)).replace('{charge_basis}', _docx_text(b))
                   .replace('{charge_amount}', _docx_text(c)) for a, b, c in charges)
    xml = xml[:start] + rows + xml[end:]
    xml = xml.replace('DRAFT FOR VALIDATION', 'DRAFT – NOT YET ISSUED' if is_draft else '')
    for name, value in values.items():
        xml = xml.replace('{' + name + '}', _docx_text(value))
    return xml


def _docx_text(value):
    """Escape for <w:t>; newlines become Word line breaks inside the same run."""
    lines = [xml_escape(str(part)) for part in str(value).split('\n')]
    return '</w:t><w:br/><w:t xml:space="preserve">'.join(lines)


def money_decimal(cents):
    """Centavos as a Decimal peso amount (exports keep numbers numeric for Excel)."""
    return Decimal(cents) / 100
