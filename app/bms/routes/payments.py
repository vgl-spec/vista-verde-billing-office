"""Cashier, payment list, voids, bill cancellation and the audit trail."""
import json
import math
from datetime import date
from urllib.parse import urlencode

from .. import UserError, audit, billing, cash_drawer, db, ledger, receipts, statements, ui
from ..http import DATE_PATTERN, NotFound, Redirect, Response, route
from ..money import format_pesos
from ..receipts import allocate  # noqa: F401 - part of this module's API (tests, reports)
from .accounts import load_phases, office_notes, phase_field, search_clause
from . import drawer
from .signatures import signature_url

CASHIER_ROLES = ('admin', 'staff', 'cashier')
ADMIN_ONLY = ('admin',)
METHODS = receipts.METHOD_LABELS
SEARCH_RESULTS = 20
PAGE_SIZE = 50
AUDIT_PAGE_SIZE = 50
MAX_RECEIPT_LENGTH = 30
CASHIER_CRUMB = ('Cashier', '/payments/cashier')
PAYMENTS_CRUMB = ('Payments', '/payments')
# The cashier's three tabs (office request 2026-11-24): water bills, monthly dues and miscellaneous are separate
# transactions, each with its own receipt. Keys are ledger purposes: the money pays only that kind of charge.
PAY_PURPOSES = {'bill': 'Water Bills', 'dues': 'Monthly Dues', 'misc': 'Miscellaneous'}
# The papers each tab may give, the usual one first (office, 2026-11-24): water bills always get the pre-printed
# Water Management Official Receipt; dues and fees get our Acknowledgement Receipt, or an OR if the cashier picks it.
# The HOA's BIR Service Invoice is written by hand outside the system.
TAB_RECEIPTS = {'bill': ('OR',), 'dues': ('AR', 'OR'), 'misc': ('AR', 'OR')}
WATER_MEMO = 'Water bill and balance'
PURPOSE_SHORT = {'bill': 'Water bill', 'dues': 'Dues', 'misc': 'Misc', '': 'Balance'}
MAX_DUES_MONTHS = 12          # at most a year of dues ahead on one receipt
MAX_MISC_DETAILS = 120
# Sortable columns of the paged lists: column index -> public key, and key -> SQL (whitelists).
PAYMENT_SORT_COLUMNS = {0: 'date', 1: 'receipt', 2: 'name', 3: 'account', 5: 'method', 6: 'amount', 7: 'status'}
PAYMENT_ORDER = {'date': 'p.paid_on', 'receipt': 'p.receipt_no', 'name': 'LOWER(a.name)', 'account': 'a.account_no',
                 'method': 'p.method', 'amount': 'p.amount_cents', 'status': 'p.status'}
PAYMENT_STATUS = {'all': 'Posted and voided', 'posted': 'Posted', 'voided': 'Voided'}
AUDIT_SORT_COLUMNS = {0: 'when', 1: 'who', 2: 'what', 3: 'record'}
AUDIT_ORDER = {'when': 'l.at', 'who': "LOWER(COALESCE(u.display_name, ''))", 'what': 'l.action', 'record': 'l.entity'}


# --- cashier ---------------------------------------------------------------------------------------

@route('GET', '/payments/cashier', roles=CASHIER_ROLES)
def cashier(req):
    search = req.query.get('q', '').strip()[:80]
    account_id = req.query.get('account', '').strip()
    with db.connect() as c:
        if account_id:
            tab = req.list_filter('tab', PAY_PURPOSES, default='bill')
            account = c.one('SELECT * FROM accounts WHERE id = ?', (account_id,))
            if account is None:
                raise NotFound('That homeowner was not found.')
            _, banner = drawer.cashier_banner(c)
            return req.page('Cashier', ui.join([banner, _account_card(req, c, account, tab)]), active='payments',
                            live=('ledger_entries', 'payments', 'bills', 'accounts'))
        state, banner = drawer.cashier_banner(c)
        if state == drawer.NOT_OPENED and not search:
            # The day starts here (office, 2026-11-25): confirm the date, count the opening cash, then take payments.
            start = ui.join([ui.heading('Cashier', 'Start the day first: confirm the date and count the opening cash.'),
                             drawer.start_card_for_cashier(c)])
            return req.page('Cashier', start, active='payments')
        results = []
        if search:
            where, params = search_clause(search)
            results = c.all(
                'SELECT a.id, a.account_no, a.name, a.address, COALESCE(SUM(l.amount_cents), 0) AS balance_cents '
                f'FROM accounts a LEFT JOIN ledger_entries l ON l.account_id = a.id{where} '
                'GROUP BY a.id, a.account_no, a.name, a.address ORDER BY a.name LIMIT ?', params + (SEARCH_RESULTS,))
    search_form = ui.join([
        ui.raw('<form class="form cashier-search" method="get" action="/payments/cashier">'),
        ui.field('Who is paying? Type a name, account number or address', 'q', type='search', value=search,
                 attrs='autofocus autocomplete="off"'),
        ui.raw('<div class="form-actions"><button class="btn btn-primary" type="submit">Find homeowner</button></div></form>'),
    ])
    rows = [[ui.copyable(r['account_no']),
             ui.copyable(r['name'], ui.raw(f'<a href="/payments/cashier?account={ui.esc(r["id"])}">{ui.esc(r["name"])}</a>')), r['address'],
             ui.money(r['balance_cents']), ui.button_link(f'/payments/cashier?account={r["id"]}', 'Take payment')] for r in results]
    parts = [ui.heading('Cashier', 'Find the homeowner, then record the payment and the receipt you gave.'), banner, search_form]
    if search:
        parts.append(ui.table(['Account', 'Name', 'Address', 'Balance', ''], rows, empty='Nobody matches that search. Check the spelling.',
                              numeric_cols=(3,), keys=[r['id'] for r in results]))
    return req.page('Cashier', ui.join(parts), active='payments')


def _account_card(req, c, account, tab):
    entries = c.all(f'SELECT {statements.LEDGER_COLUMNS} FROM ledger_entries WHERE account_id = ?', (account['id'],))
    aging = ledger.fifo_open_charges(entries)
    balance = sum(e['amount_cents'] for e in entries)
    buckets = statements.bucket_open_charges(aging.open_charges, date.fromisoformat(db.today()))
    settings = statements.office_settings(c)
    period = billing.current_period(c)
    last = c.one("SELECT period, total_cents, prior_balance_cents, due_on FROM bills "
                 "WHERE account_id = ? AND status = 'issued' ORDER BY period DESC, issued_on DESC LIMIT 1", (account['id'],))
    last_text = '—'
    if last:
        last_text = (f"{statements.month_label(last['period'])} · "
                     f"{ui.money(last['prior_balance_cents'] + last['total_cents'])} due {statements.long_date(last['due_on'])}")
    balance_note = 'Amount owed' if balance > 0 else 'Paid ahead (credit)' if balance < 0 else 'Nothing owed'
    paid_through = dues_paid_through(entries, aging)
    owed = {purpose: sum(ch.open_cents for ch in aging.open_charges if ch.purpose == purpose) for purpose in PAY_PURPOSES}
    tabs = ui.tabs([{'label': label, 'href': '/payments/cashier?' + urlencode({'account': account['id'], 'tab': key}),
                     'current': key == tab, 'dot': owed[key] > 0} for key, label in PAY_PURPOSES.items()])
    form_fields = ui.join([
        ui.raw(f'<input type="hidden" name="account_id" value="{ui.esc(account["id"])}">'
               f'<input type="hidden" name="purpose" value="{ui.esc(tab)}">'),
        _purpose_fields(tab, owed[tab], aging, entries, int(settings['dues_cents']), period, paid_through),
        _receipt_fields(tab),
        ui.field('Date paid', 'paid_on', type='date', value=db.today()),
        ui.select('How was it paid?', 'method', list(METHODS.items()), value='cash'),
    ])
    open_charges = [ch for ch in aging.open_charges if ch.purpose == tab]
    open_rows = [[statements.describe_open_charge(ch), ui.money(ch.open_cents)] for ch in open_charges]
    facts = [('Last statement', last_text), ('Status', account['status'].title()),
             ('Monthly dues paid through', statements.month_label(paid_through) if paid_through else 'No dues paid yet')]
    return ui.join([
        ui.heading(account['name'], f"Account {account['account_no']} · {account['address']}",
                   actions=ui.button_link('/payments/cashier', 'Different homeowner', kind='secondary'), crumbs=[CASHIER_CRUMB, account['name']]),
        office_notes(account),
        ui.figures([('Balance', str(ui.money(abs(balance))), balance_note)]
                   + [(label, str(ui.money(amount))) for label, amount in zip(statements.AGING_BUCKETS, buckets)]),
        ui.facts(facts),
        tabs,
        ui.well(ui.form('/payments/cashier', req.user['csrf'], form_fields, f'Save {PAY_PURPOSES[tab].lower()} payment')),
        ui.section(f'{PAY_PURPOSES[tab]}: still unpaid (oldest first)',
                   ui.table(['Charge', 'Unpaid'], open_rows, empty='Nothing is unpaid here.', numeric_cols=(1,),
                            keys=[ch.entry_id for ch in open_charges]),
                   lead='This payment pays only these charges, oldest first. Each tab is its own payment with its own receipt.'),
    ])


def _receipt_fields(tab):
    """Water bills always use the pre-printed Official Receipt; dues and fees let the cashier pick AR or OR."""
    allowed = TAB_RECEIPTS[tab]
    number_attrs = f'maxlength="{MAX_RECEIPT_LENGTH}" autocomplete="off"'
    if len(allowed) == 1:
        return ui.field(f'{receipts.RECEIPT_TYPES[allowed[0]]} number', 'receipt_no', attrs=number_attrs,
                        hint='The number printed on the Water Management receipt. Its details print onto it after saving.')
    return ui.join([
        ui.select('Receipt given', 'receipt_type', [(key, f'{receipts.RECEIPT_TYPES[key]} ({key})') for key in allowed],
                  value=allowed[0], hint='An Acknowledgement Receipt prints on plain A4 paper (two copies). '
                                         'An Official Receipt prints onto the pre-printed Water Management receipt.'),
        ui.field('Receipt number', 'receipt_no', hint='The number on the receipt you give.', attrs=number_attrs),
    ])


def _purpose_fields(tab, owed_cents, aging, entries, dues_rate, period, paid_through):
    """The fields of one cashier tab. Amounts shown here are a guide: the server works out every peso again on save."""
    if tab == 'bill':
        return ui.field('Amount paid', 'amount', value=format_pesos(owed_cents) if owed_cents > 0 else '',
                        hint=f'Water bills owed now: {ui.money(owed_cents)}. Part payments are fine; '
                             'paying more leaves credit for the next water bill.',
                        attrs='inputmode="decimal" autocomplete="off"')
    if tab == 'dues':
        unpaid_months = sum(1 for ch in aging.open_charges if ch.purpose == 'dues')
        options = []
        for months in range(1, MAX_DUES_MONTHS + 1):
            covered, new_periods = plan_dues(entries, months, period, dues_rate)
            amount = sum(cents for _, cents in covered) + dues_rate * len(new_periods)
            options.append((str(months), f"{months} month{'s' if months != 1 else ''} · {ui.money(amount)}"))
        hint = (f"{ui.money(dues_rate)} a month · {unpaid_months} unpaid month{'s' if unpaid_months != 1 else ''} · "
                + (f"paid through {statements.month_label(paid_through)}" if paid_through else 'none paid yet')
                + '. Unpaid months are paid first, then the next months ahead.')
        return ui.select('How many months', 'dues_months', options, value=str(max(1, min(unpaid_months, MAX_DUES_MONTHS))), hint=hint)
    return ui.join([
        ui.field('Amount paid', 'misc_amount', hint='Basketball court, certificates, other fees.',
                 attrs='inputmode="decimal" autocomplete="off"'),
        ui.field('What is it for?', 'misc_details',
                 attrs=f'maxlength="{MAX_MISC_DETAILS}" autocomplete="off" placeholder="e.g. Basketball court, Nov 20"'),
    ])


def dues_paid_through(entries, aging):
    """The latest month whose dues are fully paid ('' when none)."""
    reversed_ids = {e['reverses_id'] for e in entries if e['reverses_id']}
    open_ids = {ch.entry_id for ch in aging.open_charges}
    paid = [e['period'] for e in entries if e['kind'] == 'dues' and e['period'] and e['id'] not in reversed_ids
            and e['id'] not in open_ids]
    return max(paid, default='')


def plan_dues(entries, months, period, rate):
    """Which dues a payment of `months` months covers: ([(open dues charge, open cents)], [new months to charge]).

    Unpaid dues months are paid first, oldest first; the rest are the next months from the billing month on that
    have no dues charge yet (dues paid ahead). Pure function, so the screen's preview and the save agree.
    """
    aging = ledger.fifo_open_charges(entries)
    open_dues = [ch for ch in aging.open_charges if ch.purpose == 'dues'][:months]
    covered = [(ch, ch.open_cents) for ch in open_dues]
    reversed_ids = {e['reverses_id'] for e in entries if e['reverses_id']}
    charged = {e['period'] for e in entries if e['kind'] == 'dues' and e['period'] and e['id'] not in reversed_ids}
    new_periods, cursor = [], period
    while len(covered) + len(new_periods) < months:
        if cursor not in charged:
            new_periods.append(cursor)
        cursor = statements.add_months(cursor, 1)
    return covered, new_periods


def _months_text(periods):
    if not periods:
        return ''
    if len(periods) == 1:
        return statements.short_month(periods[0])
    return f"{statements.short_month(periods[0])} to {statements.short_month(periods[-1])}"


@route('POST', '/payments/cashier', roles=CASHIER_ROLES)
def record_payment(req, c):
    account = c.one('SELECT id, name, account_no FROM accounts WHERE id = ?', (req.form.get('account_id', ''),))
    if account is None:
        raise UserError('Choose the homeowner again; that account was not found.')
    if 'purpose' in req.form:
        purpose = req.choice('purpose', PAY_PURPOSES, 'payment type')
        allowed = TAB_RECEIPTS[purpose]
        # A one-paper tab sends no choice; anything not offered for the tab (e.g. an AR for water) is refused.
        receipt_type = allowed[0] if len(allowed) == 1 and 'receipt_type' not in req.form else req.choice(
            'receipt_type', allowed, 'receipt')
    elif 'pay_form' not in req.form:
        # A cashier page opened before the update had one amount box and an "OR number": it paid water bills with an OR.
        purpose, receipt_type = 'bill', 'OR'
    else:
        raise UserError('This cashier page is out of date. Reload it and enter the payment again.')
    receipt_no = req.text('receipt_no', 'Receipt number', limit=MAX_RECEIPT_LENGTH)
    paid_on = req.date('paid_on', 'Date paid')
    if paid_on > db.today():
        raise UserError('The date paid cannot be in the future.')
    method = req.choice('method', METHODS, 'payment method')

    amount_cents, months, misc_details = 0, 0, ''
    if purpose == 'bill':
        amount_cents = req.pesos('amount', 'Amount paid')
    elif purpose == 'dues':
        months = _int_or(req.form.get('dues_months'), 0)
        if not 1 <= months <= MAX_DUES_MONTHS:
            raise UserError(f'Choose from 1 to {MAX_DUES_MONTHS} months of dues.')
    else:
        amount_cents = req.pesos('misc_amount', 'Amount paid')
        misc_details = req.text('misc_details', 'What the miscellaneous payment is for', limit=MAX_MISC_DETAILS)
    if purpose != 'dues' and amount_cents <= 0:
        raise UserError('The amount paid must be more than zero.')
    if method == 'cash':
        cash_drawer.check_cash_payment(c, paid_on)

    existing = c.scalar('SELECT id FROM payments WHERE receipt_type = ? AND receipt_no = ?', (receipt_type, receipt_no))
    if existing:
        # A second payment would only hide the first one; show the recorded receipt instead.
        return Redirect(f'/payments/{existing}', ('warn', f'{receipt_type} {receipt_no} is already recorded, shown below. '
                                                          'Nothing new was saved.'))

    period = billing.current_period(c)
    covered, new_periods = [], []
    dues_rate = 0
    if months:
        dues_rate = int(statements.office_settings(c)['dues_cents'])
        entries = c.all(f'SELECT {statements.LEDGER_COLUMNS} FROM ledger_entries WHERE account_id = ?', (account['id'],))
        covered, new_periods = plan_dues(entries, months, period, dues_rate)
        if new_periods and dues_rate <= 0:
            raise UserError('The monthly dues amount is not set. An administrator sets it in Admin > Settings.')
        amount_cents = sum(cents for _, cents in covered) + dues_rate * len(new_periods)

    payment_id = c.insert('payments', {
        'account_id': account['id'], 'receipt_type': receipt_type, 'receipt_no': receipt_no, 'amount_cents': amount_cents,
        'paid_on': paid_on, 'method': method, 'status': 'posted', 'created_by': req.user['id'], 'created_at': db.now()})
    # Charges this payment creates carry its payment_id, so voiding the payment takes them back too.
    for month in new_periods:
        ledger.post(c, account['id'], 'dues', dues_rate, period=month, memo='Monthly dues (paid at the cashier)', user=req.user,
                    payment_id=payment_id, posted_on=paid_on)
    if purpose == 'misc':
        ledger.post(c, account['id'], 'other', amount_cents, period=period, memo=f'Miscellaneous: {misc_details}', user=req.user,
                    payment_id=payment_id, posted_on=paid_on, applies_to='misc')
    dues_months = sorted({ch.period for ch, _ in covered} | set(new_periods))
    label = {'bill': WATER_MEMO, 'dues': f'Monthly dues {_months_text(dues_months)}', 'misc': f'Miscellaneous: {misc_details}'}[purpose]
    ledger.post(c, account['id'], 'payment', -amount_cents, period=None, memo=f'{receipt_type} {receipt_no} · {label}', user=req.user,
                payment_id=payment_id, posted_on=paid_on, applies_to=purpose)
    audit.log(c, req.user, 'payment.record', 'payment', payment_id, None,
              {'receipt_type': receipt_type, 'receipt_no': receipt_no, 'amount_cents': amount_cents, 'account_id': account['id'],
               'method': method, 'purpose': purpose, 'dues_months': dues_months})
    return Redirect(f'/payments/{payment_id}/done', ('success', f"Payment of {ui.money(amount_cents)} saved for {account['name']}."))


@route('GET', '/payments/{id}/done', roles=CASHIER_ROLES)
def payment_done(req):
    with db.connect() as c:
        model = receipts.load_receipt(c, req.params['id'])
    if model is None:
        raise NotFound('That payment was not found.')
    payment = model['payment']
    receipt_type = payment['receipt_type']
    printable = receipt_type in receipts.PRINTABLE_TYPES
    actions = [ui.button_link('/payments/cashier', 'Next payer')]
    if printable:
        label = 'Print on the receipt' if receipt_type == 'OR' else 'Print acknowledgement receipt'
        actions.insert(0, ui.button_link(f"/payments/{payment['id']}/receipt", label))
    actions.append(ui.button_link(f"/payments/cashier?account={payment['account_id']}", 'Another payment for this homeowner',
                                  kind='secondary'))
    if receipt_type == 'OR':
        guide = ui.notice(f"Put Official Receipt no. {payment['receipt_no']} in the printer, then press "
                          '“Print on the receipt”. Only the details print, into the blanks of the receipt.', 'info')
    elif printable:
        guide = ui.notice('Print the Acknowledgement Receipt on plain A4 paper: the homeowner’s copy and the office copy '
                          'come out side by side. Cut down the middle.', 'info')
    else:
        guide = ui.notice(f"Write {receipts.RECEIPT_TYPES[receipt_type]} no. {payment['receipt_no']} by hand from the BIR booklet: "
                          f"{ui.money(payment['amount_cents'])} from {payment['name']}, {model['paid_for'].lower()}. "
                          'BIR-registered invoices are never printed by this system.', 'info')  # recorded before 2026-11-24
    facts = [('Received from', f"{payment['name']} (account {payment['account_no']})"), ('Amount', ui.money(payment['amount_cents'])),
             ('Receipt', f"{receipts.RECEIPT_TYPES[receipt_type]} no. {payment['receipt_no']}"),
             ('Date paid', statements.long_date(payment['paid_on'])), ('Paid by', METHODS[payment['method']]),
             ('For', model['paid_for']), ('Still owed for this', ui.money(model['still_owed_cents'])),
             ('Whole account balance now', ui.money(model['balance_after_cents']))]
    body = ui.join([
        ui.heading('Payment saved', actions=ui.join(actions), crumbs=[CASHIER_CRUMB, 'Payment saved']),
        guide,
        ui.facts(facts),
        ui.section('How this payment was applied', ui.table(['Paid towards', 'Amount'],
                                                            [[label, ui.money(cents)] for label, cents in model['particulars']],
                                                            numeric_cols=(1,))),
    ])
    return req.page('Payment saved', body, active='payments')


@route('GET', '/payments/{id}/receipt', roles=CASHIER_ROLES)
def print_receipt(req):
    """An Official Receipt: only the details, placed for the pre-printed sheet fed into the printer.
    An Acknowledgement Receipt: our own layout on plain A4 landscape, homeowner's copy and office copy."""
    with db.connect() as c:
        model = receipts.load_receipt(c, req.params['id'])
        if model is None or model['payment']['receipt_type'] not in receipts.PRINTABLE_TYPES:
            raise NotFound('Only Official and Acknowledgement Receipts are printed. A Service Invoice is written by hand.')
        values = receipts.calibration(c, 'OR')
        settings = statements.office_settings(c)
        image = signature_url(c, model['payment']['created_by'])
    payment = model['payment']
    label = receipts.receipt_label(payment)
    crumbs = [CASHIER_CRUMB, PAYMENTS_CRUMB, (label, f"/payments/{payment['id']}"), 'Print']
    if payment['receipt_type'] == 'OR':
        return Response(200, receipt_document(req, f'Receipt {label}', crumbs, model, values, image).encode('utf-8'))
    toolbar = ui.raw(f'<div class="doc-toolbar no-print">{ui.crumbs(crumbs)}'
                     '<button class="btn btn-primary" type="button" data-print>Print</button>'
                     '<p class="muted">Plain A4 paper, landscape: the homeowner’s copy and the office copy side by side. '
                     'Cut down the middle.</p></div>')
    page = ui.document(f'Receipt {label}', toolbar + receipts.render_acknowledgement(model, settings, signature_url=image),
                       stylesheet='print-landscape.css')
    return Response(200, page.encode('utf-8'))


def receipt_document(req, title, crumbs, model, values, image, *, test=False):
    """The Official Receipt page: instructions on screen, then the details on a page the size of the form."""
    if test:
        what = ('Test page: the gray guides print too. Print it on plain paper and hold it over a real receipt against '
                'the light to see how far off the details are.')
    else:
        what = (f"Put Official Receipt no. {model['payment']['receipt_no']} in the printer. "
                'Only the black details print; the gray form below is a guide on screen.')
    adjust = ('<a href="/admin/receipts">Adjust where it prints</a>' if req.user['role'] == 'admin'
              else 'If the details land off the lines, ask the administrator to adjust the receipt printing.')
    toolbar = ui.raw(f'<div class="doc-toolbar no-print">{ui.crumbs(crumbs)}'
                     '<button class="btn btn-primary" type="button" data-print>Print</button>'
                     f'<p class="muted">{ui.esc(what)} In the print window choose paper size {values["width"]:g} × {values["height"]:g} mm '
                     f'(the receipt size set up on the printer), Margins: None, Scale: 100%. {adjust}</p></div>')
    sheet = receipts.render_official(model, signature_url=image)
    href = f'/receipts/OR/layout.css?v={receipts.layout_version(values)}'
    return ui.document(title, toolbar + sheet, body_class='doc-body rp-body' + (' rp-test' if test else ''), layout_href=href)


@route('GET', '/receipts/{type}/layout.css', roles=CASHIER_ROLES)
def receipt_layout(req):
    form_key = req.params['type']
    if form_key not in receipts.FORMS:
        raise NotFound('No such receipt form.')
    with db.connect() as c:
        values = receipts.calibration(c, form_key)
    return Response(200, receipts.layout_css(form_key, values).encode('utf-8'), 'text/css; charset=utf-8')


# --- payment list and detail ------------------------------------------------------------------------

@route('GET', '/payments', roles=CASHIER_ROLES)
def payment_list(req):
    today = date.fromisoformat(db.today())
    start = _query_date(req, 'from', today.replace(day=1).isoformat())
    end = _query_date(req, 'to', today.isoformat())
    if start > end:
        raise UserError('The “From” date must be on or before the “To” date.')
    search = req.query.get('q', '').strip()[:80]
    method = req.list_filter('method', ('all',) + tuple(METHODS))
    receipt_type = req.list_filter('receipt', ('all',) + tuple(receipts.RECEIPT_TYPES))
    status = req.list_filter('status', PAYMENT_STATUS)
    where = ' WHERE p.paid_on >= ? AND p.paid_on <= ?'
    params = (start, end)
    if search:
        pattern = '%' + search.lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        # The details of a miscellaneous payment ("basketball court") are searchable too.
        where += (" AND (LOWER(p.receipt_no) LIKE ? ESCAPE '!' OR LOWER(a.name) LIKE ? ESCAPE '!' OR LOWER(a.account_no) LIKE ? ESCAPE '!'"
                  " OR EXISTS (SELECT 1 FROM ledger_entries m WHERE m.payment_id = p.id AND LOWER(m.memo) LIKE ? ESCAPE '!'))")
        params += (pattern,) * 4
    for column, value in (('p.method', method), ('p.receipt_type', receipt_type), ('p.status', status)):
        if value != 'all':
            where, params = where + f' AND {column} = ?', params + (value,)
    with db.connect() as c:
        phases = load_phases(c)
        phase_id = req.list_filter('phase', ('all',) + tuple(p['id'] for p in phases))
        if phase_id != 'all':
            where, params = where + ' AND a.phase_id = ?', params + (phase_id,)
        base_sql = f'FROM payments p JOIN accounts a ON a.id = p.account_id{where}'
        chosen = {k: v for k, v in (('q', search), ('phase', phase_id), ('method', method), ('receipt', receipt_type), ('status', status))
                  if v and v != 'all'}
        query = {'from': start, 'to': end, **chosen}
        sort = ui.Sort.from_query(req.query, '/payments?' + urlencode(query), PAYMENT_SORT_COLUMNS)
        totals = c.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN p.status = 'posted' THEN p.amount_cents ELSE 0 END), 0) AS posted {base_sql}",
                       params)
        pages = max(1, math.ceil(totals['n'] / PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        order_sql, order_params = sort.order(PAYMENT_ORDER, 'p.paid_on DESC, p.created_at DESC', 'p.created_at DESC, p.id')
        rows = c.all(f'SELECT p.id, p.receipt_type, p.receipt_no, p.amount_cents, p.paid_on, p.method, p.status, a.name, a.account_no {base_sql} '
                     f'ORDER BY {order_sql} LIMIT ? OFFSET ?', params + order_params + (PAGE_SIZE, (page - 1) * PAGE_SIZE))
        paid_for = _paid_for(c, [r['id'] for r in rows])
    filters = ui.search_form('/payments', search, label='Find a payment', placeholder='Receipt no., name, account', hidden=sort.params,
                             filtered=bool(chosen), extra=ui.join([
        ui.field('From', 'from', type='date', value=start), ui.field('To', 'to', type='date', value=end),
        phase_field(phases, phase_id),
        ui.select('Method', 'method', [('all', 'Any method')] + list(METHODS.items()), value=method),
        ui.select('Receipt', 'receipt', [('all', 'Any receipt')] + [(k, f'{v} ({k})') for k, v in receipts.RECEIPT_TYPES.items()],
                  value=receipt_type),
        ui.select('Status', 'status', list(PAYMENT_STATUS.items()), value=status),
    ]))
    table = ui.table(
        ['Date', 'Receipt', 'Name', 'Account', 'For', 'Method', 'Amount', 'Status'],
        [[r['paid_on'], ui.copyable(r['receipt_no'], ui.raw(f'<a href="/payments/{ui.esc(r["id"])}">{ui.esc(receipts.receipt_label(r))}</a>')),
          ui.copyable(r['name']), ui.copyable(r['account_no']), paid_for.get(r['id'], ''), METHODS[r['method']], ui.money(r['amount_cents']),
          ui.pill('Voided', 'neutral') if r['status'] == 'voided' else ui.pill('Posted', 'success')] for r in rows],
        empty='No payments match these filters.', numeric_cols=(6,), sort=sort, keys=[r['id'] for r in rows])
    exports = ui.join([ui.button_link('/exports/payments?' + urlencode({**query, 'format': fmt}), label, kind='secondary')
                       for fmt, label in (('xlsx', 'Download Excel'), ('csv', 'Download CSV'))])
    actions = ui.join([ui.button_link('/payments/cashier', 'Record a payment'), exports]
                      + ([ui.button_link('/payments/audit', 'Audit trail', kind='secondary')] if req.user['role'] == 'admin' else []))
    body = ui.join([
        ui.heading('Payments', f"{statements.long_date(start)} to {statements.long_date(end)}", actions=actions,
                   crumbs=[CASHIER_CRUMB, 'Payments']),
        filters,
        ui.figures([('Payments', f"{totals['n']:,}"), ('Total received (not counting voided)', str(ui.money(totals['posted'])))]),
        table,
        ui.pagination(page, pages, '/payments?' + urlencode({**query, **sort.params}), total=totals['n'], noun='payment'),
    ])
    return req.page('Payments', body, active='payments', live=('payments', 'ledger_entries'))


def _paid_for(c, payment_ids):
    """payment id -> "Water bill, Dues" for one page of payments, in one query."""
    if not payment_ids:
        return {}
    marks = ', '.join('?' for _ in payment_ids)
    found = {}
    for row in c.all(f"SELECT payment_id, applies_to FROM ledger_entries WHERE kind = 'payment' AND payment_id IN ({marks})",
                     tuple(payment_ids)):
        found.setdefault(row['payment_id'], set()).add(row['applies_to'])
    order = ['bill', 'dues', 'misc', '']
    return {pid: ', '.join(PURPOSE_SHORT[p] for p in order if p in purposes) for pid, purposes in found.items()}


@route('GET', '/payments/{id}', roles=CASHIER_ROLES)
def payment_detail(req):
    with db.connect() as c:
        payment = _load_payment(c, req.params['id'])
    status = ui.pill('Voided', 'neutral') if payment['status'] == 'voided' else ui.pill('Posted', 'success')
    facts = [('Homeowner', f"{payment['name']} (account {payment['account_no']})"), ('Amount', ui.money(payment['amount_cents'])),
             ('Receipt', f"{receipts.RECEIPT_TYPES[payment['receipt_type']]} no. {payment['receipt_no']}"),
             ('Date paid', statements.long_date(payment['paid_on'])),
             ('Method', METHODS[payment['method']]), ('Status', status)]
    if payment['status'] == 'voided':
        facts += [('Void reason', payment['void_reason']), ('Voided on', (payment['voided_at'] or '')[:10])]
    label = receipts.receipt_label(payment)
    actions = [ui.button_link(f"/accounts/{payment['account_id']}", 'Open homeowner', kind='secondary')]
    if payment['receipt_type'] in receipts.PRINTABLE_TYPES:
        actions.insert(0, ui.button_link(f"/payments/{payment['id']}/receipt", 'Print on the receipt', kind='secondary'))
    parts = [ui.heading(f'Receipt {label}', actions=ui.join(actions), crumbs=[CASHIER_CRUMB, PAYMENTS_CRUMB, f'Receipt {label}']),
             ui.facts(facts)]
    if payment['status'] == 'posted' and req.user['role'] == 'admin':
        parts.append(ui.section('Void this payment', ui.form(
            f"/payments/{payment['id']}/void", req.user['csrf'],
            ui.field('Reason', 'reason', hint='Required. The receipt number stays used so it cannot be typed again by mistake.'),
            'Void payment', danger=True, confirm='Void this payment? The amount goes back onto the homeowner’s balance, and dues '
                                                 'months or miscellaneous charges made with this receipt are removed.')))
    return req.page('Payment', ui.join(parts), active='payments')


@route('POST', '/payments/{id}/void', roles=ADMIN_ONLY)
def void_payment(req, c):
    payment = _load_payment(c, req.params['id'])
    reason = req.text('reason', 'Reason', limit=200)
    if not c.transition('payments', payment['id'], 'status', 'posted', 'voided',
                        {'void_reason': reason, 'voided_by': req.user['id'], 'voided_at': db.now()}):
        return Redirect(f"/payments/{payment['id']}", ('info', 'This payment was already voided.'))
    # The receipt's payment lines and the dues months / miscellaneous charge it created go back together.
    for entry in c.all("SELECT id FROM ledger_entries WHERE payment_id = ? AND kind <> 'reversal' "
                       'AND id NOT IN (SELECT reverses_id FROM ledger_entries WHERE reverses_id IS NOT NULL)', (payment['id'],)):
        ledger.reverse(c, entry['id'], f"Void {receipts.receipt_label(payment)}: {reason}", req.user)
    audit.log(c, req.user, 'payment.void', 'payment', payment['id'], {'status': 'posted'}, {'status': 'voided', 'reason': reason})
    return Redirect(f"/payments/{payment['id']}", ('success', f"Receipt {receipts.receipt_label(payment)} was voided."))


# --- bill cancellation -----------------------------------------------------------------------------------

@route('POST', '/cycle/bills/{id}/cancel', roles=ADMIN_ONLY)
def cancel_bill(req, c):
    bill = c.one('SELECT id, status, period, statement_no FROM bills WHERE id = ?', (req.params['id'],))
    if bill is None:
        raise NotFound('That bill was not found.')
    reason = req.text('reason', 'Reason', limit=200)
    if bill['status'] == 'draft':
        raise UserError('This bill is still a draft. Use “Discard this draft” instead.')
    if not c.transition('bills', bill['id'], 'status', 'issued', 'cancelled', {'cancel_reason': reason}):
        return Redirect(f"/cycle/bills/{bill['id']}", ('info', 'This bill was already cancelled.'))
    for entry in c.all("SELECT id FROM ledger_entries WHERE bill_id = ? AND kind <> 'reversal' "
                       'AND id NOT IN (SELECT reverses_id FROM ledger_entries WHERE reverses_id IS NOT NULL)', (bill['id'],)):
        ledger.reverse(c, entry['id'], f"Bill {bill['statement_no']} cancelled: {reason}", req.user)
    audit.log(c, req.user, 'bill.cancel', 'bill', bill['id'], {'status': 'issued'}, {'status': 'cancelled', 'reason': reason})
    return Redirect(f"/cycle/bills/{bill['id']}", ('success', f"Bill {bill['statement_no']} was cancelled and its charges reversed."))


# --- audit trail -------------------------------------------------------------------------------------------

@route('GET', '/payments/audit', roles=ADMIN_ONLY)
def audit_view(req):
    action = req.query.get('action', '').strip()[:60]
    start = _query_date(req, 'from', '')
    end = _query_date(req, 'to', '')
    clauses, params = [], []
    if action:
        clauses.append('l.action = ?')
        params.append(action)
    if start:
        clauses.append('l.at >= ?')
        params.append(start)
    if end:
        clauses.append('l.at < ?')
        params.append(_next_day(end))
    if start and end and start > end:
        raise UserError('The “From” date must be on or before the “To” date.')
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    query = {k: v for k, v in (('action', action), ('from', start), ('to', end)) if v}
    sort = ui.Sort.from_query(req.query, '/payments/audit' + ('?' + urlencode(query) if query else ''), AUDIT_SORT_COLUMNS)
    order_sql, order_params = sort.order(AUDIT_ORDER, 'l.at DESC, l.id', 'l.at DESC, l.id')
    with db.connect() as c:
        total = c.scalar(f'SELECT COUNT(*) FROM audit_log l{where}', tuple(params))
        pages = max(1, math.ceil(total / AUDIT_PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        rows = c.all(f'SELECT l.at, l.action, l.entity, l.entity_id, l.after, u.display_name FROM audit_log l '
                     f'LEFT JOIN users u ON u.id = l.user_id{where} ORDER BY {order_sql} LIMIT ? OFFSET ?',
                     tuple(params) + order_params + (AUDIT_PAGE_SIZE, (page - 1) * AUDIT_PAGE_SIZE))
        actions = [row['action'] for row in c.all('SELECT DISTINCT action FROM audit_log ORDER BY action')]
    filters = ui.join([
        ui.raw('<form class="form filters" method="get" action="/payments/audit">'),
        ui.raw(''.join(f'<input type="hidden" name="{k}" value="{ui.esc(v)}">' for k, v in sort.params.items())),
        ui.select('What happened', 'action', [('', 'Everything')] + [(a, a) for a in actions], value=action, required=False),
        ui.field('From', 'from', type='date', value=start, required=False),
        ui.field('To', 'to', type='date', value=end, required=False),
        ui.raw('<div class="form-actions"><button class="btn btn-primary" type="submit">Show</button></div></form>'),
    ])
    table = ui.table(['When (UTC)', 'Who', 'What', 'Record', 'Details'],
                     [[r['at'][:16].replace('T', ' '), r['display_name'] or 'System or other office', r['action'], r['entity'],
                       _short_json(r['after'])] for r in rows], empty='Nothing recorded for this filter.', sort=sort)
    body = ui.join([ui.heading('Audit trail', 'Every change and export, newest first.', crumbs=[CASHIER_CRUMB, PAYMENTS_CRUMB, 'Audit trail']),
                    filters, table,
                    ui.pagination(page, pages, '/payments/audit?' + urlencode({**query, **sort.params}), total=total, noun='entry')])
    return req.page('Audit trail', body, active='payments')


def _short_json(text):
    if not text:
        return '—'
    try:
        data = json.loads(text)
    except ValueError:
        return text[:120]
    if isinstance(data, dict):
        return ', '.join(f'{k}: {v}' for k, v in data.items())[:160]
    return str(data)[:160]


# --- helpers -------------------------------------------------------------------------------------------------

def _load_payment(c, payment_id):
    payment = c.one('SELECT p.*, a.name, a.account_no FROM payments p JOIN accounts a ON a.id = p.account_id WHERE p.id = ?',
                    (payment_id,))
    if payment is None:
        raise NotFound('That payment was not found.')
    return payment


def _query_date(req, name, default):
    value = req.query.get(name, '').strip()
    if not value:
        return default
    try:
        if not DATE_PATTERN.fullmatch(value):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return default


def _next_day(iso_day):
    return date.fromordinal(date.fromisoformat(iso_day).toordinal() + 1).isoformat()


def _int_or(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
