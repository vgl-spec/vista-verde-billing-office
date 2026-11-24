"""Receipts: which paper the cashier gave for a payment, and printing it.

The office's papers (decided 2026-11-24):
  OR  Official Receipt         the pre-printed Water Management "RECEIPT" booklet. Always used for water bills, and
                               may be chosen for dues and miscellaneous. The system prints ONLY the details onto
                               the sheet fed into the printer (the office used to type them into a blank Word page).
  AR  Acknowledgement Receipt  our own layout on plain A4, landscape: the homeowner's copy and the office copy side
                               by side. The usual paper for monthly dues and miscellaneous fees.
  SI  Service Invoice          BIR invoice the HOA writes by hand; the cashier no longer records it here. Kept only so
                               payments recorded with one earlier still display.

OR positions are in mm, measured from photos of the booklet. Printers feed paper slightly differently, so the form
has a calibration (paper size, shift, stretch) an administrator adjusts from a test print (Admin > Receipt printing).
The on-screen preview draws the form's own lines in gray; they never print except on the test page.
"""
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from . import ledger, statements, ui
from .money import pesos_in_words

METHOD_LABELS = {'cash': 'Cash', 'check': 'Check', 'gcash': 'GCash', 'bank': 'Bank transfer', 'other': 'Other'}
RECEIPT_TYPES = {'OR': 'Official Receipt', 'AR': 'Acknowledgement Receipt', 'SI': 'Service Invoice'}
PRINTABLE_TYPES = ('OR', 'AR')
COPY_LABELS = ("Homeowner's copy", 'Office copy')
WATER_OFFICE = 'Water Management'   # the sub-title on the office's water receipt and statement
# What a payment line paid for, in the words printed on the receipt.
PURPOSE_WORDS = {'bill': 'water bill', 'dues': 'monthly dues', 'misc': 'miscellaneous fees', '': 'account balance'}
_EXCEL_TRACE = re.compile(r'\s*\(Excel [^)]*\)')   # import bookkeeping in old-balance memos; not for homeowners
# The booklet's own words for a charge, short enough for its narrow particulars column.
SHORT_KIND = {'water': 'Water Bill', 'system_loss': 'Sys Loss', 'penalty': 'Penalty', 'dues': 'Monthly Dues'}


@dataclass(frozen=True)
class Box:
    """Where one detail is printed, in mm from the top-left corner of the receipt paper.

    `indent` and `line` make a wrapping block whose first line starts further right (after a printed label such
    as "the sum of Pesos") and whose next lines fall on the form's next ruled line.
    """
    left: float
    top: float
    width: float
    indent: float = 0
    line: float = 0
    align: str = 'left'


@dataclass(frozen=True)
class Form:
    name: str
    paper: tuple                 # (width, height) in mm
    rows: tuple                  # ((key, printed label or '', top), ...) of the particulars box
    row_height: float
    label_column: tuple          # (left, width) of the particulars column
    amount_column: tuple         # (left, width) of the amount column
    fields: dict                 # detail key -> Box
    guides: tuple                # gray preview of the form: ('text', x, y, words) | ('line', x, y, w) | ('rect', x, y, w, h)


_OR_ROWS = (('connection', 'Connection Fee', 32.6), ('deposit', 'Refundable Deposit', 40.4), ('water', 'Water Bill', 48.0),
            ('previous', 'Previous Bill', 55.5), ('old', 'Old Accounts', 63.4), ('penalty', 'Penalty', 71.2),
            ('extra', '', 79.1), ('total', 'TOTAL', 86.9))

FORMS = {
    # Water Management "RECEIPT" booklet (measured from photos of the office's booklet).
    'OR': Form(
        name='Water Management receipt (OR)', paper=(216, 127), rows=_OR_ROWS, row_height=7.8,
        label_column=(5.5, 38), amount_column=(45, 22),
        fields={
            'date': Box(176, 54.6, 34), 'name': Box(103, 63.1, 107), 'address': Box(73, 70.9, 137),
            'words': Box(73, 77.0, 97, indent=34, line=8.4), 'figure': Box(177, 85.4, 23, align='right'),
            'paid_for': Box(73, 93.3, 137, indent=29, line=7.2),
            'cash': Box(9.2, 101.0, 5), 'check': Box(9.2, 107.6, 5), 'check_note': Box(28, 107.0, 38),
            'signature': Box(166, 97.5, 42),
        },
        guides=(('rect', 4.6, 20.0, 64.2, 98.0), ('text', 6, 22.0, 'IN PAYMENT OF:'), ('text', 14, 27.5, 'Particulars'),
                ('text', 50, 27.5, 'Amount'), ('text', 6, 96.5, 'FORM OF PAYMENT:'), ('rect', 9.2, 101.3, 4.4, 4.4),
                ('text', 16, 101.5, 'Cash'), ('rect', 9.2, 107.9, 4.4, 4.4), ('text', 16, 108.0, 'Check'),
                ('text', 128, 38.0, 'RECEIPT   No. (pre-printed)'), ('text', 166, 55.5, 'Date:'), ('line', 176, 59.1, 34),
                ('text', 73, 64.0, 'Received from'), ('line', 103, 67.6, 107), ('line', 73, 75.4, 137),
                ('text', 73, 78.0, 'the sum of Pesos'), ('line', 107, 81.5, 103), ('line', 73, 89.9, 97),
                ('text', 172, 86.4, '(₱'), ('line', 177, 89.9, 23), ('text', 201, 86.4, ') only'),
                ('text', 73, 94.3, 'as payment for'), ('line', 102, 97.8, 108), ('line', 73, 105.0, 137),
                ('line', 166, 111.0, 42), ('text', 172, 112.0, 'Authorized Signature')),
    ),
}

CALIBRATION_FIELDS = (  # key, label, low, high
    ('width', 'Paper width (mm)', 50, 330), ('height', 'Paper height (mm)', 50, 330),
    ('offset_x', 'Move everything right (mm, negative = left)', -30, 30),
    ('offset_y', 'Move everything down (mm, negative = up)', -30, 30),
    ('scale_x', 'Stretch across (%)', 80, 120), ('scale_y', 'Stretch down (%)', 80, 120),
)


def receipt_label(payment):
    """'OR 01234': the paper and its number, as staff say it."""
    return f"{payment['receipt_type']} {payment['receipt_no']}"


# --- the payment's details ----------------------------------------------------------------------------------

def load_receipt(c, payment_id):
    """Everything a receipt shows, or None. A reprint matches the first print: only ledger lines up to this payment count."""
    payment = c.one('SELECT p.*, a.name, a.address, a.account_no, u.display_name AS cashier, '
                    '(SELECT COUNT(*) FROM signatures s WHERE s.user_id = p.created_by) AS has_signature '
                    'FROM payments p JOIN accounts a ON a.id = p.account_id LEFT JOIN users u ON u.id = p.created_by WHERE p.id = ?',
                    (payment_id,))
    if payment is None:
        return None
    entries = c.all(f'SELECT {statements.LEDGER_COLUMNS} FROM ledger_entries WHERE account_id = ? ORDER BY created_at, id',
                    (payment['account_id'],))
    own = [e for e in entries if e['payment_id'] == payment['id'] and e['kind'] != 'reversal']
    before = [e for e in entries if e['created_at'] < payment['created_at'] and e['payment_id'] != payment['id']]
    lines = [e for e in own if e['kind'] == 'payment']
    # Charges this payment created itself (dues paid ahead, a miscellaneous fee) are open until its own lines pay them.
    open_charges = ledger.fifo_open_charges(before + [e for e in own if e['kind'] != 'payment']).open_charges
    open_after = ledger.fifo_open_charges(before + own).open_charges
    allocations, paid_for = [], []
    for line in lines:
        purpose = line['applies_to']
        allocations += spread([ch for ch in open_charges if not purpose or ch.purpose == purpose], -line['amount_cents'])
        paid_for.append(_paid_for_words(line, allocations))
    # The receipt's "Balance" is what is still owed for what this payment was for (water, dues ...), so it agrees
    # with "full / partial payment". Other kinds of charges have their own receipts.
    purposes = {line['applies_to'] for line in lines}
    still_owed = sum(ch.open_cents for ch in open_after if '' in purposes or ch.purpose in purposes)
    return {'payment': payment, 'allocations': allocations,
            'particulars': [(_charge_label(charge), cents) for charge, cents in allocations],
            'paid_for': '; '.join(paid_for), 'is_full': still_owed == 0, 'still_owed_cents': still_owed,
            'is_water': purposes == {'bill'},
            'balance_after_cents': sum(e['amount_cents'] for e in before + own)}


def spread(open_charges, amount_cents):
    """[(open charge, share)] in FIFO order; a remainder is an advance credit, shown as (None, cents)."""
    applied, remaining = [], amount_cents
    for charge in open_charges:
        if remaining <= 0:
            break
        share = min(remaining, charge.open_cents)
        applied.append((charge, share))
        remaining -= share
    if remaining > 0:
        applied.append((None, remaining))
    return applied


def allocate(open_charges, amount_cents):
    """Spread a payment over open charges in FIFO order, labelled for the screen."""
    return [(_charge_label(charge), cents) for charge, cents in spread(open_charges, amount_cents)]


def _charge_label(charge):
    if charge is None:
        return 'Advance payment (credit for future bills)'
    return _EXCEL_TRACE.sub('', statements.describe_open_charge(charge))


def short_label(charge):
    """'Water Bill Nov 2026', 'Old Accounts', 'Basketball court Nov 20': a particulars line as the office writes it."""
    if charge is None:
        return 'Advance payment'
    if charge.kind in SHORT_KIND and charge.period:
        return f'{SHORT_KIND[charge.kind]} {statements.short_month(charge.period)}'
    if charge.kind == 'opening':
        return 'Old Accounts'  # the unpaid balance carried over from the Excel statements
    memo = _EXCEL_TRACE.sub('', charge.memo or '').removeprefix('Miscellaneous:').strip()
    return memo or statements.KIND_LABELS.get(charge.kind, charge.kind)


def _paid_for_words(line, allocations):
    purpose = line['applies_to']
    if purpose in ('dues', 'misc'):
        return line['memo'].split(' · ', 1)[-1]
    if purpose == 'bill':
        periods = [ch.period for ch, _ in allocations if ch and ch.kind == 'water' and ch.period]
        return f'Water bill {period_span(max(periods))}' if periods else 'Water bill, old balance'
    return PURPOSE_WORDS[purpose].capitalize()


def period_span(period):
    """'Oct 12 – Nov 11, 2026': the reading period a water bill covers, as the office writes it on receipts."""
    start, end = statements.day_in(statements.add_months(period, -1), 12), statements.day_in(period, 11)
    first = start.strftime('%b %d') if start.year == end.year else start.strftime('%b %d, %Y')
    return f"{first} – {end.strftime('%b %d, %Y')}"


# --- what lands in the particulars box ----------------------------------------------------------------------

def particular_rows(allocations):
    """[(row key, label to print or '', cents)] for the Official Receipt's particulars box, plus the total row."""
    return _water_receipt_rows(allocations) + [('total', '', sum(cents for charge, cents in allocations))]


def _water_receipt_rows(allocations):
    """Fill the Water Management form's printed rows: this month's water on Water Bill, earlier months on Previous
    Bill, Excel balances on Old Accounts; system loss, dues and fees go on the blank row, as the office does."""
    water_periods = [ch.period for ch, _ in allocations if ch and ch.kind in ('water', 'system_loss') and ch.period]
    current = max(water_periods, default='')
    fixed, extras = defaultdict(int), defaultdict(int)
    for charge, cents in allocations:
        if charge is None:
            extras['Advance'] += cents
        elif charge.kind == 'water':
            fixed['water' if charge.period == current else 'previous'] += cents
        elif charge.kind == 'system_loss':
            if charge.period == current:
                extras['Sys Loss'] += cents
            else:
                fixed['previous'] += cents
        elif charge.kind == 'opening':
            fixed['old'] += cents
        elif charge.kind == 'penalty':
            fixed['penalty'] += cents
        else:
            extras[short_label(charge)] += cents
    rows = [(key, '', fixed[key]) for key, _, _ in _OR_ROWS if fixed.get(key)]
    if extras:  # one blank row on the form: several extras share it
        rows.append(('extra', ' + '.join(extras), sum(extras.values())))
    return rows


# --- printing ---------------------------------------------------------------------------------------------

def calibration(c, form_key):
    """The form's calibration: stored values over the measured defaults."""
    form = FORMS[form_key]
    stored = c.scalar('SELECT value FROM settings WHERE key = ?', (f'receipt_form_{form_key}',))
    values = {'width': form.paper[0], 'height': form.paper[1], 'offset_x': 0, 'offset_y': 0, 'scale_x': 100, 'scale_y': 100}
    values.update(json.loads(stored) if stored else {})
    return values


def layout_version(values):
    """Short hash of a calibration, so a browser never prints with yesterday's cached positions."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]


def layout_css(form_key, values):
    """The positions of every detail (and gray guide) of one form, generated from FORMS and the calibration.

    Served as a stylesheet because the security policy allows no inline styles.
    """
    form = FORMS[form_key]
    width, height = f"{values['width']:g}", f"{values['height']:g}"
    out = [f'@page {{ size: {width}mm {height}mm; margin: 0; }}',
           f'.rp-page {{ width: {width}mm; height: {height}mm; }}',
           f".rp-content {{ transform: translate({values['offset_x']:g}mm, {values['offset_y']:g}mm) "
           f"scale({values['scale_x'] / 100:g}, {values['scale_y'] / 100:g}); }}"]
    for key, box in form.fields.items():
        rule = f'left: {box.left}mm; top: {box.top}mm; width: {box.width}mm; text-align: {box.align};'
        if box.line:
            rule += f' text-indent: {box.indent}mm; line-height: {box.line}mm; white-space: normal;'
        out.append(f'.rp-{key} {{ {rule} }}')
    label_left, label_width = form.label_column
    amount_left, amount_width = form.amount_column
    for key, _, top in form.rows:
        middle = top + form.row_height / 2 - 2.2
        # Labels fill the row's height and wrap to a second, smaller line instead of being cut off.
        out.append(f'.rp-row-{key} {{ left: {label_left}mm; top: {top + 0.4:.1f}mm; width: {label_width}mm; '
                   f'height: {form.row_height - 0.8:.1f}mm; }}')
        out.append(f'.rp-amt-{key} {{ left: {amount_left}mm; top: {middle:.1f}mm; width: {amount_width}mm; text-align: right; }}')
    for index, guide in enumerate(form.guides):
        kind, x, y, *rest = guide
        if kind == 'text':
            out.append(f'.rp-g{index} {{ left: {x}mm; top: {y}mm; }}')
        elif kind == 'line':
            out.append(f'.rp-g{index} {{ left: {x}mm; top: {y}mm; width: {rest[0]}mm; }}')
        else:
            out.append(f'.rp-g{index} {{ left: {x}mm; top: {y}mm; width: {rest[0]}mm; height: {rest[1]}mm; }}')
    for key, label, top in form.rows:  # the printed row labels and row rules, for the preview
        out.append(f'.rp-gl-{key} {{ left: {label_left}mm; top: {top + form.row_height / 2 - 2:.1f}mm; }}')
        out.append(f'.rp-gr-{key} {{ left: {label_left - 0.6}mm; top: {top}mm; width: {amount_left + amount_width - label_left + 1}mm; }}')
    return '\n'.join(out) + '\n'


def render_official(model, *, signature_url=''):
    """The details of one payment placed for the pre-printed Official Receipt. Gray guides are for the screen only."""
    e = ui.esc
    payment = model['payment']
    form = FORMS['OR']
    amount = payment['amount_cents']
    method = payment['method']
    details = {
        'date': date.fromisoformat(payment['paid_on']).strftime('%m-%d-%Y'),  # as typed on the office's receipts
        'name': payment['name'].upper(),
        'address': payment['address'].upper(),
        'words': pesos_in_words(amount).upper(),
        'figure': _figure(amount),
        'paid_for': model['paid_for'],
        'cash': 'X' if method == 'cash' else '',
        'check': 'X' if method == 'check' else '',
        'check_note': '' if method in ('cash', 'check') else METHOD_LABELS[method].upper(),
    }
    texts = ''.join(f'<span class="rp-f rp-{key}">{e(value)}</span>' for key, value in details.items() if value)
    if signature_url:
        texts += f'<img class="rp-f rp-signature rp-sign-img" src="{e(signature_url)}" alt="Signature">'
    for key, label, cents in particular_rows(model['allocations']):
        if label:
            texts += f'<span class="rp-f rp-label rp-row-{key}">{e(label)}</span>'
        texts += f'<span class="rp-f rp-amt-{key}">{e(_figure(cents))}</span>'
    guides = ''.join(_guide(index, guide) for index, guide in enumerate(form.guides))
    guides += ''.join(f'<span class="rp-g rp-gr rp-gr-{key}"></span>' + (f'<span class="rp-g rp-gl-{key}">{e(label)}</span>' if label else '')
                      for key, label, _ in form.rows)
    voided = '<p class="rp-void no-print">VOIDED: do not hand this receipt over.</p>' if payment['status'] == 'voided' else ''
    return ui.raw(f'{voided}<div class="rp-sheet"><div class="rp-page" aria-label="{e(form.name)}">'
                  f'<div class="rp-content">{guides}{texts}</div></div></div>')


def _guide(index, guide):
    kind = guide[0]
    if kind == 'text':
        return f'<span class="rp-g rp-g{index}">{ui.esc(guide[3])}</span>'
    return f'<span class="rp-g rp-g{"line" if kind == "line" else "rect"} rp-g{index}"></span>'


def sample_model():
    """Invented details for the calibration test print: long enough to fill every blank."""
    payment = {'receipt_type': 'OR', 'receipt_no': 'TEST', 'name': 'Sample, Juan D.', 'address': 'Lot 1 Block 2 Sample Street',
               'paid_on': '2026-11-24', 'amount_cents': 123456, 'method': 'cash', 'status': 'posted', 'created_by': None,
               'has_signature': 0}
    water = ledger.OpenCharge('w', 'water', '2026-11', '2026-11-22', '', 91072, 91072)
    loss = ledger.OpenCharge('l', 'system_loss', '2026-11', '2026-11-22', 'System loss', 21232, 21232)
    old = ledger.OpenCharge('o', 'opening', '2026-10', '2026-10-01', 'PREV. BILL', 11152, 11152)
    return {'payment': payment, 'allocations': [(water, 91072), (loss, 21232), (old, 11152)],
            'paid_for': f"Water bill {period_span('2026-11')}", 'is_full': True, 'still_owed_cents': 0, 'balance_after_cents': 0,
            'is_water': True}


# --- the Acknowledgement Receipt (our own layout, plain paper) -----------------------------------------------

def render_acknowledgement(model, settings, *, signature_url=''):
    """One A4 landscape sheet: the homeowner's copy and the office copy of the same acknowledgement receipt."""
    halves = ''.join(_acknowledgement_copy(model, settings, label, signature_url) for label in COPY_LABELS)
    return ui.raw(f'<article class="doc doc-page rc-sheet">{halves}</article>')


def _acknowledgement_copy(model, settings, copy_label, signature_url):
    e = ui.esc
    payment = model['payment']
    amount = payment['amount_cents']
    merged = defaultdict(int)  # two lines with the same words (e.g. two old balances) are one line on paper
    for charge, cents in model['allocations']:
        merged[short_label(charge)] += cents
    rows = ''.join(f'<tr><td>{e(label)}</td><td class="num">{_figure(cents)}</td></tr>' for label, cents in merged.items())
    unit = WATER_OFFICE if model['is_water'] else 'Homeowners Association'
    voided = ('<p class="soa-draft">VOIDED. This payment was cancelled; this is not a valid receipt.</p>'
              if payment['status'] == 'voided' else '')
    image = f'<img class="rc-sign-img" src="{e(signature_url)}" alt="Signature">' if signature_url else ''
    paid_for = ('Full payment: ' if model['is_full'] else 'Partial payment: ') + model['paid_for']
    return f"""<section class="rc-copy" aria-label="{e(copy_label)}">
<p class="rc-copy-label">{e(copy_label)}</p>
{voided}
<header class="rc-org"><img class="rc-logo" src="/assets/vistaverde-logo.jpg" alt="" width="64" height="64">
  <div><p class="rc-org-name">{e(settings['association_name'])}</p><p class="rc-org-unit">{e(unit)}</p>
  <p class="rc-org-line">{e(settings['association_address'])}</p></div></header>
<p class="rc-title"><span>{e(RECEIPT_TYPES['AR'])}</span><span class="rc-no">No. <strong>{e(payment['receipt_no'])}</strong></span></p>
<div class="rc-grid">
  <div class="rc-box">
    <p class="rc-box-title">In payment of:</p>
    <table class="rc-lines"><thead><tr><th>Particulars</th><th class="num">Amount</th></tr></thead>
    <tbody>{rows}</tbody>
    <tfoot><tr><th>Total</th><td class="num">{ui.money(amount)}</td></tr></tfoot></table>
    <p class="rc-box-title">Form of payment:</p>
    {_methods(payment['method'])}
    <p class="rc-balance"><span>Balance</span><span>{ui.money(model['still_owed_cents'])}</span></p>
  </div>
  <dl class="rc-fill">
    <div><dt>Date</dt><dd>{e(statements.long_date(payment['paid_on']))}</dd></div>
    <div><dt>Received from</dt><dd>{e(payment['name'])}<span class="rc-sub">{e(payment['address'])}</span></dd></div>
    <div><dt>The sum of Pesos</dt><dd>{e(pesos_in_words(amount))} <span class="rc-figure">({ui.money(amount)}) only</span></dd></div>
    <div><dt>In full / partial payment of</dt><dd>{e(paid_for)}</dd></div>
  </dl>
</div>
<div class="rc-signature">{image}<p class="rc-sign"><span class="rc-sign-name">{e(payment['cashier'] or '')}</span>Authorized signature</p></div>
</section>"""


def _methods(method):
    """Cash / Check boxes as on the office's booklet; GCash or a bank transfer is written in as a third, ticked line."""
    choices = [('cash', 'Cash'), ('check', 'Check')] + ([] if method in ('cash', 'check') else [(method, METHOD_LABELS[method])])
    items = ''.join(f'<li><span class="rc-tick{" is-on" if key == method else ""}" aria-hidden="true"></span>{ui.esc(label)}'
                    f'{"<span class=sr> (ticked)</span>" if key == method else ""}</li>' for key, label in choices)
    return f'<ul class="rc-checks">{items}</ul>'

def _figure(cents):
    """Amounts as typed on the office's receipts: figures only, the ₱ is printed on the form."""
    whole, fraction = divmod(cents, 100)
    return f'{whole:,}.{fraction:02d}'
