"""Reports, disconnection notices, the alerts page and CSV/XLSX exports."""
import math
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from .. import UserError, alerts, audit, billing, db, exports, statements, ui
from ..http import HttpError, NotFound, Redirect, Response, route
from ..money import format_reading
from .accounts import load_phases, phase_field, search_clause

REPORT_ROLES = ('admin', 'staff')
ALL_ROLES = ('admin', 'staff', 'cashier')
RESOLVE_ROLES = ('admin', 'staff')
PAGE_SIZE = 50
# Sortable columns of the paged reports: column index -> public key. The keys map to Python sort keys
# (rows computed in memory) or SQL (usage) in each report's own whitelist below.
AGING_SORT_COLUMNS = {0: 'account', 1: 'name', 2: 'b0', 3: 'b1', 4: 'b2', 5: 'b3', 6: 'total'}
AGING_KEYS = {'account': lambda r: r['account_no'], 'name': lambda r: r['name'].lower(), 'total': lambda r: r['total'],
              **{f'b{i}': (lambda r, i=i: r['buckets'][i]) for i in range(4)}}
DISCONNECT_SORT_COLUMNS = {0: 'account', 1: 'name', 2: 'address', 3: 'since', 4: 'total'}
DISCONNECT_KEYS = {'account': lambda r: r['account_no'], 'name': lambda r: r['name'].lower(),
                   'address': lambda r: r['address'].lower(), 'since': lambda r: r['oldest_period'], 'total': lambda r: r['total']}
USAGE_SORT_COLUMNS = {0: 'account', 1: 'name', 2: 'm0', 3: 'm1', 4: 'm2', 5: 'avg', 6: 'flag'}
USAGE_FLAGS = {'all': 'Everyone', 'flagged': 'Flagged this month', 'zero use': 'Zero use', 'high use': 'High use', 'low use': 'Low use'}
# Aging filter: how overdue the oldest unpaid charge is -> first bucket index that must be owing.
AGING_AGES = {'all': ('Any age', 0), '30': ('Over 30 days', 1), '60': ('Over 60 days', 2), '90': ('Over 90 days', 3)}
MAX_LETTERS = 300
REPORTS_CRUMB = ('Reports', '/reports')
ALERT_GROUPS = {
    'readings_missing': 'Readings still needed', 'reading_flag': 'Readings to double-check', 'bills_draft': 'Bills waiting',
    'overdue': 'Overdue balances', 'disconnect_candidate': 'Possible disconnections', 'import_issue': 'Import problems',
    'sync_conflict': 'Sync conflicts', 'sync_error': 'Sync problems', 'policy_unconfirmed': 'Rules to confirm',
}
EXPORT_TYPES = {'csv': 'text/csv; charset=utf-8',
                'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}


# --- menu ------------------------------------------------------------------------------------------------

@route('GET', '/reports', roles=REPORT_ROLES)
def reports_home(req):
    items = [
        ('/reports/collections', 'Collections', 'Money received in a date range, by payment method and by cashier.'),
        ('/reports/aging', 'Unpaid balances', 'Who owes, and for how long: 0–30, 31–60, 61–90 and over 90 days.'),
        ('/reports/disconnections', 'Disconnection list', 'Homeowners past the allowed delay, with printable notice letters.'),
        ('/reports/consumption', 'Water use and system loss', 'Water used per phase compared with the Maynilad bulk rate.'),
        ('/reports/usage', '3-month comparison', 'Each homeowner’s last three months of water use, odd ones flagged.'),
        ('/alerts', 'Alerts', 'Everything that needs someone’s attention.'),
    ]
    rows = [[ui.raw(f'<a href="{href}">{ui.esc(title)}</a>'), text] for href, title, text in items]
    exports_rows = [[d.title, ui.join([ui.button_link(f'/exports/{name}?format=xlsx', 'Excel', kind='secondary'),
                                       ui.button_link(f'/exports/{name}?format=csv', 'CSV', kind='secondary')])]
                    for name, d in exports.DATASETS.items()]
    body = ui.join([ui.heading('Reports'), ui.table(['Report', 'What it shows'], rows),
                    ui.section('Download lists', ui.table(['List', 'Download'], exports_rows),
                               lead='Excel files open directly; CSV files keep the peso sign when opened in Excel.')])
    return req.page('Reports', body, active='reports')


def _export_buttons(name, query):
    return ui.join([ui.button_link(f'/exports/{name}?' + urlencode({**query, 'format': fmt}), label, kind='secondary')
                    for fmt, label in (('xlsx', 'Download Excel'), ('csv', 'Download CSV'))])


def _range_form(action, start, end):
    return ui.join([
        ui.raw(f'<form class="form filters" method="get" action="{ui.esc(action)}">'),
        ui.field('From', 'from', type='date', value=start), ui.field('To', 'to', type='date', value=end),
        ui.raw('<div class="form-actions"><button class="btn btn-primary" type="submit">Show</button></div></form>'),
    ])


# --- collections ----------------------------------------------------------------------------------------

@route('GET', '/reports/collections', roles=REPORT_ROLES)
def collections_report(req):
    start, end = exports.date_range(req.query)
    with db.connect() as c:
        by_method, by_cashier, voided = exports.collection_summary(c, start, end)
        by_purpose, by_phase = exports.collection_split(c, start, end)
    total = sum(r['total'] for r in by_method)
    split = {r['purpose']: r['total'] for r in by_purpose}
    body = ui.join([
        ui.heading('Collections', f'{statements.long_date(start)} to {statements.long_date(end)}',
                   actions=_export_buttons('collections', {'from': start, 'to': end}), crumbs=[REPORTS_CRUMB, 'Collections']),
        _range_form('/reports/collections', start, end),
        ui.figures([('Total received', str(ui.money(total))), ('Water bills', str(ui.money(split.get('bill', 0) + split.get('', 0)))),
                    ('Monthly dues', str(ui.money(split.get('dues', 0)))), ('Miscellaneous', str(ui.money(split.get('misc', 0)))),
                    ('Payments', f"{sum(r['n'] for r in by_method):,}"),
                    ('Voided (not counted)', f"{voided['n']:,}", str(ui.money(voided['total'])))]),
        ui.section('What was paid for', ui.table(
            ['Paid for', 'Payments', 'Total'],
            [[exports.PURPOSE_LABELS.get(r['purpose'], r['purpose']), f"{r['n']:,}", ui.money(r['total'])] for r in by_purpose],
            empty='No payments in this range.', numeric_cols=(1, 2)),
            lead='One receipt can pay several parts, so it is counted once in each part it paid.'),
        ui.section('By phase', ui.table(['Phase', 'Payments', 'Total'],
                                        [[r['phase'], f"{r['n']:,}", ui.money(r['total'])] for r in by_phase],
                                        empty='No payments in this range.', numeric_cols=(1, 2))),
        ui.section('By payment method', ui.table(['Method', 'Payments', 'Total'],
                                                 [[r['method'].title(), f"{r['n']:,}", ui.money(r['total'])] for r in by_method],
                                                 empty='No payments in this range.', numeric_cols=(1, 2))),
        ui.section('By cashier', ui.table(['Cashier', 'Payments', 'Total'],
                                          [[r['cashier'] or 'Other office', f"{r['n']:,}", ui.money(r['total'])] for r in by_cashier],
                                          empty='No payments in this range.', numeric_cols=(1, 2))),
    ])
    return req.page('Collections', body, active='reports')


# --- aging and disconnections ----------------------------------------------------------------------------

def _list_filters(req, phases):
    """Phase and search chosen above a report list; an unknown phase is a 404, never ignored."""
    phase_id = req.query.get('phase', 'all')
    if phase_id != 'all' and phase_id not in {p['id'] for p in phases}:
        raise NotFound('That phase does not exist.')
    return phase_id, req.query.get('q', '').strip()[:80]


def _filter_rows(rows, phase_id, search):
    terms = search.lower().split()
    def keep(row):
        text = f"{row['account_no']} {row['name']} {row['address']}".lower()
        return (phase_id == 'all' or row['phase_id'] == phase_id) and all(term in text for term in terms)
    return [row for row in rows if keep(row)]


def _report_search(action, phases, phase_id, search, sort, extra='', filtered=False):
    return ui.search_form(action, search, label='Find a homeowner', placeholder='Name, account, address', hidden=sort.params,
                          extra=ui.join([phase_field(phases, phase_id), extra]), filtered=filtered or phase_id != 'all')


def _filter_query(phase_id, search, **more):
    return {k: v for k, v in (('phase', phase_id), ('q', search), *more.items()) if v and v != 'all'}


def _usage_order(months):
    used = ('(SELECT CAST(u.present AS NUMERIC) - CAST(u.previous AS NUMERIC) FROM readings u '
            'WHERE u.account_id = a.id AND u.period = ?)')
    return {'account': 'a.account_no', 'name': 'LOWER(a.name)',
            **{f'm{i}': (used, (month,)) for i, month in enumerate(months)},
            'avg': ('(SELECT AVG(CAST(u.present AS NUMERIC) - CAST(u.previous AS NUMERIC)) FROM readings u '
                    'WHERE u.account_id = a.id AND u.period >= ? AND u.period <= ?)', (months[0], months[-1])),
            'flag': ("(SELECT NULLIF(u.flag, '') FROM readings u WHERE u.account_id = a.id AND u.period = ?)", (months[-1],))}


@route('GET', '/reports/aging', roles=REPORT_ROLES)
def aging_report(req):
    with db.connect() as c:  # alerts are not synced, so their upsert needs no write transaction
        all_rows = exports.aging_rows(c)
        _sync_overdue_alert(c, all_rows, billing.current_period(c))
        phases = load_phases(c)
    phase_id, search = _list_filters(req, phases)
    age = req.list_filter('age', AGING_AGES)
    query = _filter_query(phase_id, search, age=age)
    sort = ui.Sort.from_query(req.query, '/reports/aging' + ('?' + urlencode(query) if query else ''), AGING_SORT_COLUMNS)
    first_bucket = AGING_AGES[age][1]
    rows = sort.apply([r for r in _filter_rows(all_rows, phase_id, search) if any(r['buckets'][first_bucket:])], AGING_KEYS)
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
    shown = rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    totals = [sum(r['buckets'][i] for r in rows) for i in range(4)]
    table = ui.table(['Account', 'Name'] + list(statements.AGING_BUCKETS) + ['Total owed'],
                     [[ui.copyable(r['account_no']), _account_link(r)] + [ui.money(b) for b in r['buckets']] + [ui.money(r['total'])]
                      for r in shown],
                     empty='Nobody matches this search.' if query else 'Nobody owes anything.', numeric_cols=(2, 3, 4, 5, 6), sort=sort)
    body = ui.join([
        ui.heading('Unpaid balances', 'Biggest balances first. Age counts from the day each charge was posted.',
                   actions=_export_buttons('aging', {}), crumbs=[REPORTS_CRUMB, 'Unpaid balances']),
        ui.figures([(label, str(ui.money(amount))) for label, amount in zip(statements.AGING_BUCKETS, totals)]),
        _report_search('/reports/aging', phases, phase_id, search, sort, filtered=age != 'all',
                       extra=ui.select('Owing for', 'age', [(k, label) for k, (label, _) in AGING_AGES.items()], value=age)),
        table,
        ui.pagination(page, pages, '/reports/aging?' + urlencode({**query, **sort.params}), total=len(rows), noun='homeowner'),
    ])
    return req.page('Unpaid balances', body, active='reports')


def _account_link(row):
    return ui.copyable(row['name'], ui.raw(f'<a href="/accounts/{ui.esc(row["id"])}">{ui.esc(row["name"])}</a>'))


def _sync_overdue_alert(c, rows, period):
    # Alerts are a non-synced cache of what the reports show, refreshed whenever they are viewed.
    overdue = sum(1 for r in rows if any(r['buckets'][1:]))
    if overdue:
        alerts.raise_alert(c, 'overdue', period, f'{overdue:,} homeowners have balances unpaid for more than 30 days.', '/reports/aging', 'warn')
    else:
        alerts.resolve_alert(c, 'overdue', period, None)


def disconnection_candidates(rows, period, settings):
    """Owing for a month at least `disconnect_after_months` before the billing month."""
    months = int(settings['disconnect_after_months'])
    cutoff = statements.add_months(period, -months)
    return [r for r in rows if r['oldest_period'] and r['oldest_period'] <= cutoff and r['status'] == 'active']


@route('GET', '/reports/disconnections', roles=REPORT_ROLES)
def disconnections_report(req):
    with db.connect() as c:
        period = billing.current_period(c)
        settings = statements.office_settings(c)
        candidates = disconnection_candidates(exports.aging_rows(c), period, settings)
        phases = load_phases(c)
        if candidates:
            alerts.raise_alert(c, 'disconnect_candidate', period,
                               f'{len(candidates):,} homeowners are past the allowed payment delay and may be disconnected.',
                               '/reports/disconnections', 'urgent')
        else:
            alerts.resolve_alert(c, 'disconnect_candidate', period, None)
    phase_id, search = _list_filters(req, phases)
    query = _filter_query(phase_id, search)
    sort = ui.Sort.from_query(req.query, '/reports/disconnections' + ('?' + urlencode(query) if query else ''), DISCONNECT_SORT_COLUMNS)
    rows = sort.apply(_filter_rows(candidates, phase_id, search), DISCONNECT_KEYS)
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
    shown = rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    table = ui.table(['Account', 'Name', 'Address', 'Unpaid since', 'Total owed', ''],
                     [[ui.copyable(r['account_no']), _account_link(r), r['address'],
                       statements.month_label(r['oldest_period']), ui.money(r['total']),
                       ui.button_link(f'/reports/disconnections/letters?ids={r["id"]}', 'Notice letter', kind='secondary')]
                      for r in shown], empty='Nobody matches this search.' if query else 'Nobody is past the allowed delay.',
                     numeric_cols=(4,), sort=sort)
    actions = ui.button_link('/reports/disconnections/letters', 'Print all notice letters') if candidates else ''
    months = int(settings['disconnect_after_months'])
    body = ui.join([
        ui.heading('Disconnection list', f'Unpaid for {months} month{"s" if months != 1 else ""} or more before {statements.month_label(period)}.',
                   actions=actions, crumbs=[REPORTS_CRUMB, 'Disconnection list']),
        ui.notice('Confirm each case with the office before disconnecting. The rule is a setting the administrator can change.', 'warn')
        if not settings['policy_confirmed'] else '',
        _report_search('/reports/disconnections', phases, phase_id, search, sort) if candidates else '',
        table,
        ui.pagination(page, pages, '/reports/disconnections?' + urlencode({**query, **sort.params}), total=len(rows), noun='homeowner'),
    ])
    return req.page('Disconnection list', body, active='reports', dots={'reports'} if candidates else None)


@route('GET', '/reports/disconnections/letters', roles=REPORT_ROLES)
def disconnection_letters(req):
    wanted = {part for part in req.query.get('ids', '').split(',') if part}
    with db.connect() as c:
        period = billing.current_period(c)
        settings = statements.office_settings(c)
        candidates = disconnection_candidates(exports.aging_rows(c), period, settings)
    if wanted:
        candidates = [r for r in candidates if r['id'] in wanted]
    candidates = candidates[:MAX_LETTERS]
    e = ui.esc
    today = statements.long_date(db.today())
    letters = ''.join(f"""<article class="doc doc-page letter">
<header class="soa-head"><div class="soa-org"><img class="soa-logo" src="/assets/vistaverde-logo.jpg" alt="" width="64" height="64">
<div><p class="soa-org-name">{e(settings['association_name'])}</p><p class="soa-org-line">{e(settings['association_address'])}</p></div></div></header>
<p>{e(today)}</p>
<p><strong>{e(r['name'])}</strong><br>{e(r['address'])}<br>Account no. {e(r['account_no'])}</p>
<h1 class="letter-title">Notice of disconnection</h1>
<p>Our records show an unpaid balance of <strong>{ui.money(r['total'])}</strong> on your water account, unpaid since
{e(statements.month_label(r['oldest_period']))}.</p>
<p>{e(settings['disconnection_note'])}</p>
<p>Please settle the full amount at the association office within three (3) days from receipt of this notice to avoid
disconnection. If you have already paid, please bring your official receipt to the office so we can correct our records.</p>
<p>{e(settings['payment_instructions'])}</p>
<p class="letter-sign">For the Board of Directors<br><br>______________________________<br>{e(settings['association_name'])}</p>
<p class="letter-ack">Received by: ______________________ Date: ____________</p>
</article>""" for r in candidates)
    crumbs = ui.crumbs([REPORTS_CRUMB, ('Disconnection list', '/reports/disconnections'), 'Notices'])
    toolbar = (f'<div class="doc-toolbar no-print">{crumbs}<button class="btn btn-primary" type="button" data-print>Print</button>'
               f'<span class="muted">{len(candidates)} letter{"s" if len(candidates) != 1 else ""}, one per page.</span></div>')
    body = ui.raw(toolbar + (letters or '<p class="doc">Nobody is past the allowed delay.</p>'))
    return Response(200, ui.document('Disconnection notices', body).encode('utf-8'))


# --- consumption and usage -------------------------------------------------------------------------------

@route('GET', '/reports/consumption', roles=REPORT_ROLES)
def consumption_report(req):
    with db.connect() as c:
        period = req.query.get('period') or billing.current_period(c)
        if not billing.PERIOD_PATTERN.fullmatch(period):
            raise NotFound('That billing month is not valid.')
        settings = statements.office_settings(c)
        rows = c.all(
            'SELECT a.phase_id, r.previous, r.present FROM readings r JOIN accounts a ON a.id = r.account_id WHERE r.period = ?',
            (period,))
        phases = load_phases(c)
        month_rates = billing.period_bulk_rates(c, period)
        loss_billed = c.scalar("SELECT COALESCE(SUM(l.amount_cents), 0) FROM ledger_entries l JOIN bills b ON b.id = l.bill_id "
                               "WHERE b.period = ? AND b.status = 'issued' AND l.kind = 'system_loss'", (period,))
        billed = {row['phase_id']: row['water'] for row in c.all(
            "SELECT a.phase_id, SUM(b.water_cents) AS water FROM bills b JOIN accounts a ON a.id = b.account_id "
            "WHERE b.period = ? AND b.status = 'issued' GROUP BY a.phase_id", (period,))}
    # The month's own rate from Admin > Maynilad bill (bill ÷ m³); the fixed setting only when none is saved.
    rate = month_rates.maynilad_rate if month_rates else Decimal(str(settings['maynilad_rate_per_m3']))
    per_phase = defaultdict(lambda: [0, Decimal(0)])
    for row in rows:
        per_phase[row['phase_id']][0] += 1
        per_phase[row['phase_id']][1] += Decimal(row['present']) - Decimal(row['previous'])
    table_rows, total_m3, total_cost, total_billed = [], Decimal(0), 0, 0
    for phase in phases:  # every phase in the database, in the office's order; phases with no readings are skipped
        if phase['id'] not in per_phase:
            continue
        count, used = per_phase[phase['id']]
        cost = int((used * rate * 100).to_integral_value())
        water = billed.get(phase['id']) or 0
        total_m3, total_cost, total_billed = total_m3 + used, total_cost + cost, total_billed + water
        table_rows.append([phase['name'], f'{count:,}', f'{used:,}', ui.money(cost), ui.money(water)])
    bulk_text = req.query.get('bulk', '').strip()
    loss_section = ''
    if bulk_text:
        try:
            bulk = Decimal(bulk_text.replace(',', ''))
            if bulk < 0 or bulk > 10 ** 9:
                raise ValueError
        except (ArithmeticError, ValueError):
            raise UserError('Type the Maynilad mother-meter use as a number of cubic metres.') from None
        loss = bulk - total_m3
        loss_cost = int((loss * rate * 100).to_integral_value())
        loss_section = ui.section('System loss estimate', ui.facts([
            ('Maynilad mother meter', f'{bulk:,} m³'), ('Sum of homeowner meters', f'{total_m3:,} m³'),
            ('Water lost', f'{loss:,} m³'), ('Cost of lost water at the bulk rate', ui.money(loss_cost)),
            ('Per active homeowner (if shared equally)', ui.money(loss_cost // max(1, sum(v[0] for v in per_phase.values()))))]))
    form = ui.join([
        ui.raw('<form class="form filters" method="get" action="/reports/consumption">'),
        ui.field('Billing month', 'period', type='month', value=period),
        ui.field('Maynilad mother-meter use (m³)', 'bulk', value=bulk_text, required=False, attrs='inputmode="decimal"',
                 hint='From the Maynilad bill, to estimate system loss.'),
        ui.raw('<div class="form-actions"><button class="btn btn-primary" type="submit">Show</button></div></form>'),
    ])
    total_row = ['Total', f'{len(rows):,}', f'{total_m3:,}', ui.money(total_cost), ui.money(total_billed)] if len(table_rows) > 1 else None
    body = ui.join([
        ui.heading('Water use and system loss', f"{statements.month_label(period)} · Maynilad bulk rate ₱{rate} per m³",
                   crumbs=[REPORTS_CRUMB, 'Water use']),
        form,
        ui.table(['Phase', 'Meters read', 'Used (m³)', 'Cost at bulk rate', 'Water billed (issued)'], table_rows,
                 empty='No readings for this month.', numeric_cols=(1, 2, 3, 4), foot=total_row),
        loss_section,
        _maynilad_section(month_rates, loss_billed, period),
    ])
    return req.page('Water use', body, active='reports')


def _maynilad_section(bulk, loss_billed, period):
    if bulk is None:
        return ui.notice("No Maynilad bill saved for this month yet. Add it in Admin > Maynilad bill to get the month's rates.")
    return ui.section('Maynilad bill and system loss', ui.facts([
        ('Maynilad rate this month', f'₱{bulk.maynilad_rate} per m³'),
        ('System loss rate', f'₱{bulk.loss_rate} per m³'),
        ('HOA subsidy', f'{bulk.subsidy_percent}%'),
        ('Charged to homeowners', f'₱{bulk.charged_loss_rate} per m³'),
        ('System loss billed on issued statements', ui.money(loss_billed)),
    ]), lead=f'From Admin > Maynilad bill for {statements.month_label(period)}.')


@route('GET', '/reports/usage', roles=REPORT_ROLES)
def usage_report(req):
    with db.connect() as c:
        period = billing.current_period(c)
        months = [statements.add_months(period, -2), statements.add_months(period, -1), period]
        phases = load_phases(c)
        phase_id, search = _list_filters(req, phases)
        flag = req.list_filter('flag', USAGE_FLAGS)
        query = _filter_query(phase_id, search, flag=flag)
        sort = ui.Sort.from_query(req.query, '/reports/usage' + ('?' + urlencode(query) if query else ''), USAGE_SORT_COLUMNS)
        where, params = search_clause(search)
        where = where.replace(' WHERE ', " WHERE a.status = 'active' AND ", 1) if where else " WHERE a.status = 'active'"
        if phase_id != 'all':
            where, params = where + ' AND a.phase_id = ?', params + (phase_id,)
        if flag != 'all':
            flag_sql = "u.flag <> ''" if flag == 'flagged' else 'u.flag = ?'
            where += f' AND EXISTS (SELECT 1 FROM readings u WHERE u.account_id = a.id AND u.period = ? AND {flag_sql})'
            params += (period,) + (() if flag == 'flagged' else (flag,))
        total = c.scalar(f'SELECT COUNT(*) FROM accounts a{where}', params)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        order_sql, order_params = sort.order(_usage_order(months), 'a.account_no, a.id', 'a.account_no, a.id')
        accounts = c.all(f'SELECT a.id, a.account_no, a.name FROM accounts a{where} ORDER BY {order_sql} LIMIT ? OFFSET ?',
                         params + order_params + (PAGE_SIZE, (page - 1) * PAGE_SIZE))
        used = {}
        if accounts:
            marks = ', '.join('?' for _ in accounts)
            for r in c.all(f'SELECT account_id, period, previous, present, flag FROM readings WHERE period >= ? AND period <= ? '
                           f'AND account_id IN ({marks})', (months[0], period) + tuple(a['id'] for a in accounts)):
                used[(r['account_id'], r['period'])] = (Decimal(r['present']) - Decimal(r['previous']), r['flag'])
    rows = []
    for a in accounts:
        cells = [used.get((a['id'], m)) for m in months]
        flag = cells[2][1] if cells[2] else ''
        read = [cell[0] for cell in cells if cell]
        # Average of the months that have a reading (a new homeowner may have only one or two).
        average = format_reading((sum(read) / len(read)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) if read else '—'
        rows.append([ui.copyable(a['account_no']), ui.copyable(a['name'])] + [f'{cell[0]:,}' if cell else '—' for cell in cells]
                    + [average, ui.join([ui.pill(flag.capitalize(), 'warn'), ui.dot('Check this reading')]) if flag else ''])
    body = ui.join([
        ui.heading('3-month comparison', 'Water used (m³) and the 3-month average. Readings flagged as zero, high or low use '
                   'need a second look.',
                   crumbs=[REPORTS_CRUMB, '3-month comparison']),
        _report_search('/reports/usage', phases, phase_id, search, sort, filtered=flag != 'all',
                       extra=ui.select('Flag', 'flag', list(USAGE_FLAGS.items()), value=flag)),
        ui.table(['Account', 'Name'] + [statements.short_month(m) for m in months] + ['3-month average', 'Flag'], rows,
                 empty='Nobody matches these filters.' if query else 'No active homeowners.', numeric_cols=(2, 3, 4, 5), sort=sort),
        ui.pagination(page, pages, '/reports/usage?' + urlencode({**query, **sort.params}), total=total, noun='homeowner'),
    ])
    return req.page('3-month comparison', body, active='reports')


# --- alerts -----------------------------------------------------------------------------------------------

@route('GET', '/alerts', roles=ALL_ROLES)
def alerts_page(req):
    with db.connect() as c:
        rows = alerts.open_alerts(c)
    can_resolve = req.user['role'] in RESOLVE_ROLES
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['kind']].append(row)
    sections = []
    for kind, label in ALERT_GROUPS.items():
        if kind not in grouped:
            continue
        table_rows = []
        for alert in grouped[kind]:
            link = ui.button_link(alert['link'], 'Open', kind='secondary') if alert['link'] else ''
            resolve = ui.form(f"/alerts/{alert['id']}/resolve", req.user['csrf'], '', 'Mark as done') if can_resolve else ''
            severity = {'urgent': ui.pill('Urgent', 'danger'), 'warn': ui.pill('Check', 'warn'), 'info': ui.pill('Note', 'info')}[alert['severity']]
            table_rows.append([severity, alert['title'], alert['created_at'][:10], link, resolve])
        sections.append(ui.section(label, ui.table(['', 'What', 'Since', '', ''], table_rows)))
    body = ui.join([ui.heading('Alerts', f'{len(rows)} open. Each one links to the page where it can be fixed.', crumbs=[('Home', '/'), 'Alerts'])]
                   + (sections or [ui.empty('Nothing needs attention right now.')]))
    return req.page('Alerts', body)


@route('POST', '/alerts/{id}/resolve', roles=RESOLVE_ROLES)
def resolve_alert(req, c):
    alert = c.one('SELECT kind, key, title FROM alerts WHERE id = ?', (req.params['id'],))
    if alert is None:
        raise NotFound('That alert was not found.')
    if not alerts.resolve_alert(c, alert['kind'], alert['key'], req.user):
        return Redirect('/alerts', ('info', 'That alert was already marked as done.'))
    return Redirect('/alerts', ('success', 'Alert marked as done.'))


# --- exports -------------------------------------------------------------------------------------------------

@route('GET', '/exports/{name}', roles=ALL_ROLES)
def export(req):
    dataset = exports.DATASETS.get(req.params['name'])
    if dataset is None:
        raise NotFound('That list cannot be downloaded.')
    if req.user['role'] not in dataset.roles:
        raise HttpError(403, 'Your account is not allowed to download this list.')
    fmt = req.query.get('format', 'xlsx')
    if fmt not in EXPORT_TYPES:
        raise NotFound('Choose Excel or CSV.')
    with db.connect() as c:
        stem, headers, rows = dataset.loader(c, req.query)
    with db.transaction() as c:  # short write: the heavy read above must not hold the write lock
        audit.log(c, req.user, 'export', 'export', req.params['name'], None,
                  {'format': fmt, 'rows': len(rows), **{k: v for k, v in req.query.items() if k in ('period', 'phase', 'from', 'to')}})
    data = exports.to_csv(headers, rows) if fmt == 'csv' else exports.to_xlsx(dataset.title, headers, rows)
    return Response.download(data, EXPORT_TYPES[fmt], f'{stem}-{date.today().isoformat()}.{fmt}')


def _int_or(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
