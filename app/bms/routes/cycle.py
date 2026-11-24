"""Monthly cycle: route sheets, readings grid, draft bills, issuing, statements and penalties."""
import json
import math
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from .. import UserError, alerts, audit, billing, db, editing, ledger, statements, ui
from ..http import HttpError, NotFound, Redirect, Response, ShowPage, route
from ..money import format_reading, parse_reading, round_half_up
from . import accounts as account_routes, home

READ_ROLES = ('admin', 'staff')
EDIT_ROLES = ('admin', 'staff')
PRINT_ROLES = ('admin', 'staff', 'cashier')
# Breadcrumb parents shared by every page under Reading & SOA.
CYCLE_CRUMB = ('Reading & SOA', '/cycle')
BILLS_CRUMB = ('Bills', '/cycle/bills')
GRID_PAGE_SIZE = 40
# Sortable columns of the paged lists: column index -> public key, and key -> SQL (whitelists).
GRID_SORT_COLUMNS = {0: 'account', 1: 'name', 2: 'previous', 3: 'present', 4: 'used', 5: 'status'}
# Readings grid status filter: value -> (label, extra WHERE on the month's reading row `r`).
GRID_STATUS = {
    'all': ('Any status', ''), 'missing': ('Not entered', ' AND r.id IS NULL'),
    'flagged': ('To double-check', " AND r.flag <> ''"), 'saved': ('Saved', ' AND r.id IS NOT NULL'),
}
# Bills status filter: value -> (label, extra WHERE).
BILL_STATUS = {
    'all': ('Drafts and issued', ''), 'draft': ('Drafts', " AND b.status = 'draft'"),
    'issued': ('Issued', " AND b.status = 'issued'"), 'flagged': ('Flagged readings', " AND COALESCE(r.flag, '') <> ''"),
}
BILL_SORT_COLUMNS = {1: 'account', 2: 'name', 3: 'flag', 4: 'water', 5: 'other', 6: 'previous', 7: 'due', 8: 'status'}
BILL_ORDER = {
    'account': 'a.account_no', 'name': 'LOWER(a.name)', 'flag': "COALESCE(r.flag, '')", 'water': 'b.water_cents',
    'other': 'b.dues_cents + b.other_cents', 'previous': 'b.prior_balance_cents',
    'due': 'b.prior_balance_cents + b.total_cents', 'status': 'b.status',
}
BILL_PAGE_SIZE = 50
# Drafts first and, among them, flagged readings first: the order the office checks them in.
BILL_DEFAULT_ORDER = ("CASE b.status WHEN 'draft' THEN 0 ELSE 1 END, CASE WHEN COALESCE(r.flag, '') <> '' THEN 0 ELSE 1 END, "
                      'a.account_no, b.id')
MAX_ROWS_PER_SAVE = 100
MAX_BILLS_PER_ISSUE = 200
MAX_PRINT_IDS = 500
MAX_DUES_MONTHS = 12
MAX_NOTE_LENGTH = 300
MAX_REASON_LENGTH = 200
# The review window lists every saved reading for the current filter; past this it says how many were left out.
MAX_REVIEW_ROWS = 3000
MAX_CLASHES_SHOWN = 50   # rows that two people typed differently, shown on one choice page
# Per-statement choices stored in bills.detail (see statements.statement_notes).
STATEMENT_NOTE_KEYS = ('show_penalty', 'show_disconnection', 'note')
# A zero-use bill is still owed (monthly dues), but the office wants an administrator to accept it.
ADMIN_ONLY_FLAG = 'zero use'
SPIKE_HIGH, SPIKE_LOW = Decimal(2), Decimal('0.5')
# (label shown on the bill, ledger kind it posts as)
OTHER_KINDS = {
    'system_loss': ('System loss share', 'system_loss'),
    'maynilad_debt': ('Contribution for Maynilad debt', 'other'),
    'reconnection': ('Reconnection fee', 'other'),
    'custom': ('Other charge', 'other'),
}
# Fixed namespace: a penalty's ledger id is derived from (account, period), so the primary key
# itself makes "one penalty per account per month" impossible to break, on every node.
PENALTY_NAMESPACE = uuid.UUID('6f1d0000-0000-4000-8000-00000000be11')


# --- landing -----------------------------------------------------------------------------------

@route('GET', '/cycle', roles=PRINT_ROLES)
def cycle_home(req):
    with db.connect() as c:
        period = billing.current_period(c)
        counts = _cycle_counts(c, period)
    current = home.current_step(counts)
    missing = counts['accounts'] - counts['readings']
    items = [
        ('Print reading sheets', f"{counts['accounts']:,} homeowners, one sheet per phase", '/cycle/routes'),
        ('Enter readings', f"{counts['readings']:,} of {counts['accounts']:,} entered" + (f', {missing:,} to go' if missing > 0 else '')
         + (f", {counts['flagged']:,} to double-check" if counts['flagged'] else ''), '/cycle/readings'),
        ('Make, check and issue bills', f"{counts['bills']:,} made, {counts['drafts']:,} still drafts, {counts['issued']:,} issued", '/cycle/bills'),
        ('Record payments', 'Open the cashier screen', '/payments/cashier'),
    ]
    steps = [{'title': t, 'status': s, 'href': h, 'dot': n == current,
              'state': 'done' if n < current else 'current' if n == current else 'todo'}
             for n, (t, s, h) in enumerate(items, start=1)]
    others = ui.join([
        ui.button_link('/cycle/bills/print?' + urlencode({'period': period}), 'Print issued statements', kind='secondary'),
        ui.button_link('/cycle/penalties', 'Add late-payment penalties', kind='secondary'),
    ])
    body = [ui.heading('Reading & SOA', f'Billing month: {statements.month_label(period)} · covers {statements.period_covered(period)}'),
            ui.section('The four monthly steps', ui.steps(steps)),
            ui.section('Other jobs', ui.raw(f'<div class="page-actions">{others}</div>'))]
    if req.user['role'] == 'admin':
        body.append(ui.section('Change billing month', _period_form(req, period),
                               lead='Move to the next month once this month’s bills are issued.'))
    return req.page('Reading & SOA', ui.join(body), active='cycle', dots={'cycle'} if current < 4 else {'payments'})


def _cycle_counts(c, period):
    return c.one(
        "SELECT (SELECT COUNT(*) FROM accounts WHERE status = 'active') AS accounts, "
        '(SELECT COUNT(*) FROM readings WHERE period = ?) AS readings, '
        "(SELECT COUNT(*) FROM readings WHERE period = ? AND flag <> '') AS flagged, "
        "(SELECT COUNT(*) FROM bills WHERE period = ? AND status <> 'cancelled') AS bills, "
        "(SELECT COUNT(*) FROM bills WHERE period = ? AND status = 'draft') AS drafts, "
        "(SELECT COUNT(*) FROM bills WHERE period = ? AND status = 'issued') AS issued",
        (period,) * 5)


def _period_form(req, period):
    options = [(statements.add_months(period, 1), f'{statements.month_label(statements.add_months(period, 1))} (next month)'),
               (statements.add_months(period, -1), f'{statements.month_label(statements.add_months(period, -1))} (go back one month)')]
    return ui.form('/cycle/period', req.user['csrf'], ui.select('New billing month', 'period', options),
                   'Change billing month', confirm='Change the billing month for everyone in the office?')


@route('POST', '/cycle/period', roles=('admin',))
def change_period(req, c):
    current = billing.current_period(c)
    target = req.period('period')
    if target not in (statements.add_months(current, 1), statements.add_months(current, -1)):
        raise UserError('You can only move the billing month one month at a time.')
    billing.set_setting(c, 'current_period', target, req.user)
    audit.log(c, req.user, 'period.change', 'setting', 'current_period', {'value': current}, {'value': target})
    sync_readings_alert(c, target)
    return Redirect('/cycle', ('success', f'The billing month is now {statements.month_label(target)}.'))


# --- shared reading context ---------------------------------------------------------------------

load_phases = account_routes.load_phases


def _phase_clause(phase_id, phases):
    if phase_id in ('', 'all'):
        return '', ()
    if phase_id not in {p['id'] for p in phases}:
        raise NotFound('That phase does not exist.')
    return ' AND a.phase_id = ?', (phase_id,)


def _search_and(search):
    """accounts.search_clause starts with WHERE; here it joins an existing WHERE."""
    clause, params = account_routes.search_clause(search)
    return clause.replace(' WHERE ', ' AND ', 1), params


def _grid_order(period):
    """Readings grid sort keys -> SQL over `a` (account) and `r` (this month's reading, may be missing)."""
    return {
        'account': 'a.account_no', 'name': 'LOWER(a.name)',
        'meter': "(SELECT m.meter_no FROM meters m WHERE m.account_id = a.id AND m.removed_on IS NULL "
                 "ORDER BY COALESCE(m.installed_on, '') DESC, m.id DESC LIMIT 1)",
        # Not read yet: last month's present reading is the "previous" the page shows.
        'previous': ('COALESCE(CAST(r.previous AS NUMERIC), (SELECT CAST(p.present AS NUMERIC) FROM readings p '
                     'WHERE p.account_id = a.id AND p.period < ? ORDER BY p.period DESC LIMIT 1))', (period,)),
        'present': 'CAST(r.present AS NUMERIC)',
        'used': 'CAST(r.present AS NUMERIC) - CAST(r.previous AS NUMERIC)',
        'status': "CASE WHEN r.id IS NULL THEN 0 WHEN r.flag <> '' THEN 1 ELSE 2 END",
    }


def _status_select(label, name, choices, value):
    return ui.select(label, name, [(key, text) for key, (text, _) in choices.items()], value=value)


def _filter_form(action, phases, *, phase, search, hidden, waiting=None, extra='', filtered=False):
    """GET filter bar: phase (always shown, searchable), any page-specific filters, and a name / account / address search."""
    fields = ui.join([account_routes.phase_field(phases, phase or 'all', waiting), extra])
    return ui.search_form(action, search, label='Find a homeowner', placeholder='Name, account, address', hidden=hidden,
                          extra=fields, filtered=filtered or (phase not in ('', 'all')))


def load_reading_context(c, account_ids, period):
    """Everything needed to show or save readings for these accounts: five queries, any count."""
    ctx = {'current': {}, 'last': {}, 'meters': {}, 'history': defaultdict(list), 'bills': {}}
    if not account_ids:
        return ctx
    ids = tuple(account_ids)
    marks = ', '.join('?' for _ in ids)
    ctx['current'] = {r['account_id']: r for r in c.all(
        f'SELECT * FROM readings WHERE period = ? AND account_id IN ({marks})', (period,) + ids)}
    ctx['last'] = {r['account_id']: r for r in c.all(
        'SELECT r.account_id, r.present, r.meter_id, r.period FROM readings r JOIN ('
        f'SELECT account_id, MAX(period) AS period FROM readings WHERE period < ? AND account_id IN ({marks}) GROUP BY account_id'
        ') m ON m.account_id = r.account_id AND m.period = r.period', (period,) + ids)}
    for meter in c.all('SELECT id, account_id, meter_no, start_reading, installed_on FROM meters '
                       f"WHERE removed_on IS NULL AND account_id IN ({marks}) ORDER BY COALESCE(installed_on, ''), id", ids):
        ctx['meters'][meter['account_id']] = meter  # newest installed meter wins
    for row in c.all(f'SELECT account_id, previous, present FROM readings WHERE period < ? AND period >= ? AND account_id IN ({marks})',
                     (period, statements.add_months(period, -3)) + ids):
        ctx['history'][row['account_id']].append(Decimal(row['present']) - Decimal(row['previous']))
    ctx['bills'] = {r['account_id']: r for r in c.all(
        f"SELECT id, account_id, status FROM bills WHERE period = ? AND status <> 'cancelled' AND account_id IN ({marks})",
        (period,) + ids)}
    return ctx


def previous_reading(ctx, account_id):
    """Last month's present reading, or the current meter's start reading after a replacement."""
    current = ctx['current'].get(account_id)
    if current:
        return current['previous']
    meter, last = ctx['meters'].get(account_id), ctx['last'].get(account_id)
    replaced = meter is not None and last is not None and last['meter_id'] not in (None, meter['id'])
    if last and not replaced:
        return last['present']
    if meter and meter['start_reading']:
        return meter['start_reading']
    return last['present'] if last else None


def reading_flag(usage, history):
    """Plain-words flag for odd usage; '' when nothing looks unusual."""
    if usage == 0:
        return 'zero use'
    usable = [value for value in history if value >= 0]
    if usable:
        average = sum(usable) / len(usable)
        if average > 0 and usage > SPIKE_HIGH * average:
            return 'high use'
        if average > 0 and usage < SPIKE_LOW * average:
            return 'low use'
    return ''


def sync_readings_alert(c, period):
    missing = c.scalar(
        "SELECT COUNT(*) FROM accounts a WHERE a.status = 'active' AND NOT EXISTS "
        '(SELECT 1 FROM readings r WHERE r.account_id = a.id AND r.period = ?)', (period,))
    if missing:
        alerts.raise_alert(c, 'readings_missing', period,
                           f'{missing:,} homeowners still need a meter reading for {statements.month_label(period)}.',
                           '/cycle/readings', 'warn')
    else:
        alerts.resolve_alert(c, 'readings_missing', period, None)


def sync_drafts_alert(c, period):
    drafts = c.scalar("SELECT COUNT(*) FROM bills WHERE period = ? AND status = 'draft'", (period,))
    if drafts:
        alerts.raise_alert(c, 'bills_draft', period,
                           f'{drafts:,} draft bills for {statements.month_label(period)} are waiting to be checked and issued.',
                           '/cycle/bills', 'info')
    else:
        alerts.resolve_alert(c, 'bills_draft', period, None)


# --- route sheets ---------------------------------------------------------------------------------

@route('GET', '/cycle/routes', roles=READ_ROLES)
def route_sheets(req):
    with db.connect() as c:
        period = billing.current_period(c)
        phases = load_phases(c)
        counts = {row['phase_id']: row['n'] for row in c.all(
            "SELECT phase_id, COUNT(*) AS n FROM accounts WHERE status = 'active' GROUP BY phase_id")}
    # One sheet per phase (Phase 1D first), so each reader's pile stays within one phase.
    rows = [[phase['name'], f"{counts[phase['id']]:,}", ui.button_link(
        '/cycle/routes/print?' + urlencode({'phase': phase['id']}), 'Open printable sheet', kind='secondary')]
        for phase in phases if counts.get(phase['id'])]
    every = [ui.raw('<strong>All phases</strong>'), f'{sum(counts.values()):,}',
             ui.button_link('/cycle/routes/print?phase=all', 'Open printable sheet')] if len(rows) > 1 else None
    body = ui.join([
        ui.heading('Print reading sheets', f'For the meter reader · {statements.month_label(period)}', crumbs=[CYCLE_CRUMB, 'Reading sheets']),
        ui.table(['Phase', 'Homeowners', ''], rows, numeric_cols=(1,), foot=every, empty='No active homeowners yet.'),
    ])
    return req.page('Reading sheets', body, active='cycle')


@route('GET', '/cycle/routes/print', roles=READ_ROLES)
def route_sheet_print(req):
    with db.connect() as c:
        period = billing.current_period(c)
        settings = statements.office_settings(c)
        phases = load_phases(c)
        phase_id = req.query.get('phase', 'all')
        phase_sql, phase_params = _phase_clause(phase_id, phases)
        homeowners = c.all(f"SELECT a.id, a.account_no, a.name, a.address FROM accounts a WHERE a.status = 'active'{phase_sql} "
                           'ORDER BY a.account_no', phase_params)
        ctx = load_reading_context(c, [a['id'] for a in homeowners], period)
    phase_name = next((p['name'] for p in phases if p['id'] == phase_id), 'All phases')
    e = ui.esc
    rows = ''.join(
        f"<tr><td class=\"num\">{n}</td><td>{e(a['account_no'])}</td><td>{e(a['name'])}</td><td>{e(a['address'])}</td>"
        f"<td>{e(_meter_no(ctx, a['id']))}</td><td class=\"num\">{e(previous_reading(ctx, a['id']) or '')}</td>"
        '<td class="rs-blank"></td><td class="rs-blank"></td></tr>'
        for n, a in enumerate(homeowners, start=1))
    title = f"Meter reading sheet – {phase_name} – {statements.month_label(period)}"
    crumbs = ui.crumbs([CYCLE_CRUMB, ('Reading sheets', '/cycle/routes'), phase_name])
    page = ui.raw(f"""<div class="doc-toolbar no-print">{crumbs}<button class="btn btn-primary" type="button" data-print>Print this sheet</button>
<a class="btn btn-secondary" href="/cycle/routes">Back</a></div>
<article class="doc route-sheet">
<header class="soa-head"><div class="soa-org"><img class="soa-logo" src="/assets/vistaverde-logo.jpg" alt="" width="56" height="56">
<div><p class="soa-org-name">{e(settings['association_name'])}</p><p class="soa-org-line">{e(title)}</p></div></div>
<dl class="soa-meta"><div><dt>Reading day</dt><dd>{e(statements.long_date(statements.day_in(period, 11).isoformat()))}</dd></div>
<div><dt>Reader</dt><dd class="rs-line"></dd></div></dl></header>
<table class="soa-table rs-table"><thead><tr><th class="num">#</th><th>Account no.</th><th>Name</th><th>Address</th><th>Meter no.</th>
<th class="num">Previous reading</th><th>Present reading</th><th>Date read</th></tr></thead><tbody>{rows}</tbody></table>
<p class="rs-foot">{len(homeowners):,} homeowners. Write clearly; cross out and rewrite mistakes.</p>
</article>""")
    return Response(200, ui.document(title, page).encode('utf-8'))


def _meter_no(ctx, account_id):
    meter = ctx['meters'].get(account_id)
    return meter['meter_no'] if meter else ''


# --- readings grid ---------------------------------------------------------------------------------

@route('GET', '/cycle/readings', roles=READ_ROLES)
def readings_grid(req):
    phase_id = req.query.get('phase', 'all')
    status = req.list_filter('status', GRID_STATUS)
    search = req.query.get('q', '').strip()[:80]
    with db.connect() as c:
        period = billing.current_period(c)
        phases = load_phases(c)
        phase_sql, phase_params = _phase_clause(phase_id, phases)
        search_sql, search_params = _search_and(search)
        clause, params = phase_sql + search_sql + GRID_STATUS[status][1], phase_params + search_params
        # Carried by the sort links, the pager and the save redirect.
        keep = {k: v for k, v in (('phase', phase_id), ('status', status), ('q', search)) if v and v != 'all'}
        sort = ui.Sort.from_query(req.query, '/cycle/readings' + ('?' + urlencode(keep) if keep else ''), GRID_SORT_COLUMNS)
        from_sql = "FROM accounts a LEFT JOIN readings r ON r.account_id = a.id AND r.period = ? WHERE a.status = 'active'"
        total = c.scalar(f'SELECT COUNT(*) {from_sql}{clause}', (period,) + params)
        pages = max(1, math.ceil(total / GRID_PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        order_sql, order_params = sort.order(_grid_order(period), 'a.account_no, a.id', 'a.account_no, a.id')
        accounts = c.all(f'SELECT a.id, a.account_no, a.name, a.address {from_sql}{clause} ORDER BY {order_sql} LIMIT ? OFFSET ?',
                         (period,) + params + order_params + (GRID_PAGE_SIZE, (page - 1) * GRID_PAGE_SIZE))
        ctx = load_reading_context(c, [a['id'] for a in accounts], period)
        review = _review_panel(c, period, phase_sql + search_sql, phase_params + search_params)
        missing = {row['phase_id']: row['n'] for row in c.all(
            "SELECT a.phase_id, COUNT(*) AS n FROM accounts a WHERE a.status = 'active' AND NOT EXISTS "
            '(SELECT 1 FROM readings r WHERE r.account_id = a.id AND r.period = ?) GROUP BY a.phase_id', (period,))}
    keep.update(sort.params)
    back = '/cycle/readings?' + urlencode({**keep, 'page': page})
    rows = [_grid_row(a, ctx, back) for a in accounts]
    # The meter number sits under the account number: one column fewer keeps the grid and its actions on a laptop screen.
    grid = ui.table(['Account / meter', 'Name', 'Previous', 'Present reading', 'Used (m³)', 'Status', 'Actions'], rows,
                    empty='No homeowner matches these filters.' if keep else 'No active homeowners yet.',
                    numeric_cols=(2, 4), sort=sort, keys=[a['id'] for a in accounts])
    e = ui.esc
    fields = ui.join([
        ui.raw(f'<input type="hidden" name="period" value="{e(period)}"><input type="hidden" name="page" value="{page}">'
               + ''.join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in keep.items())),
        ui.field('Date read', 'read_on', type='date', value=db.today(), hint='Used for every reading saved from this page.'),
        ui.raw(f'<div class="grid-wrap">{grid}</div>'),
    ])
    saved_here = sum(1 for a in accounts if a['id'] in ctx['current'])
    body = ui.join([
        ui.heading('Enter readings', f'{statements.month_label(period)} · page {page} of {pages} · {saved_here} of {len(accounts)} on this page saved',
                   actions=ui.join([
                       ui.raw('<button class="btn btn-primary js-only" type="button" data-dialog-open="review-readings">Review readings</button>'),
                       ui.button_link('/accounts/new', 'Add homeowner', kind='secondary')]),
                   crumbs=[CYCLE_CRUMB, 'Enter readings']),
        _filter_form('/cycle/readings', phases, phase=phase_id, search=search, hidden=sort.params, waiting=missing,
                     extra=_status_select('Status', 'status', GRID_STATUS, status), filtered=status != 'all'),
        ui.notice('Type each present reading and press Enter to move down. Blank rows are skipped. '
                  'A reading lower than last month is not saved — use “Meter replaced” for that homeowner.', 'info'),
        ui.raw('<div class="wide" id="readings-grid">') + ui.form('/cycle/readings', req.user['csrf'], fields, 'Save these readings')
        + ui.raw('</div>'),
        ui.pagination(page, pages, '/cycle/readings?' + urlencode(keep), total=total, noun='homeowner'),
        review,
    ])
    return req.page('Enter readings', body, active='cycle', dots={'cycle'} if sum(missing.values()) else None,
                    live=('readings', 'bills', 'meters', 'accounts'))


def _grid_row(account, ctx, back):
    account_id = account['id']
    previous = previous_reading(ctx, account_id)
    current = ctx['current'].get(account_id)
    bill = ctx['bills'].get(account_id)
    e = ui.esc
    name_attr = e(f"for {account['name']}")
    locked = bill is not None and bill['status'] == 'issued'
    lock_attr = ' disabled' if locked else ''
    present_value = e(current['present']) if current else ''
    if previous is None:
        previous_cell = ui.raw(f'<input class="grid-input" name="previous_{e(account_id)}" inputmode="decimal" autocomplete="off" '
                               f'aria-label="Previous reading {name_attr}" data-prev-input{lock_attr}>')
    else:
        previous_cell = previous
    # The saved reading's version and value travel with the row: saving over a reading someone else changed
    # meanwhile becomes a choice (save_readings), never a silent overwrite.
    present_cell = ui.raw(
        f'<input class="grid-input" name="present_{e(account_id)}" value="{present_value}" inputmode="decimal" autocomplete="off" '
        f'data-grid="present" data-prev="{e(previous or "")}" aria-label="Present reading {name_attr}"{lock_attr}>'
        f'<input type="hidden" name="v_{e(account_id)}" value="{current["version"] if current else 0}">'
        f'<input type="hidden" name="base_present_{e(account_id)}" value="{present_value}">')
    used = ''
    if current:
        used = format_reading(Decimal(current['present']) - Decimal(current['previous']))
    used_cell = ui.raw(f'<output class="grid-use" data-use>{e(used)}</output>')
    if locked:
        status = ui.pill('Bill issued', 'neutral')
    elif current and current['flag']:
        status = ui.join([ui.pill(current['flag'].capitalize(), 'warn'), ui.dot('Double-check this reading')])
    elif current:
        status = ui.pill('Saved', 'success')
    else:
        status = ui.join([ui.pill('Not entered', 'info'), ui.dot('Reading still needed')])
    status = ui.raw('<span class="grid-status">') + status + ui.raw('</span>')  # pill and its dot stay on one line
    # The name opens the homeowner's page (ledger, bills, past readings); the grid warns first if readings are unsaved.
    name_cell = ui.raw(f'<a class="grid-name" href="/accounts/{e(account_id)}">{e(account["name"])}</a>'
                       f'<span class="grid-sub">{e(account["address"])}</span>')
    meter_no = _meter_no(ctx, account_id)
    account_cell = ui.copyable(account['account_no']) + (
        ui.raw('<span class="grid-sub">Meter ') + ui.copyable(meter_no) + ui.raw('</span>') if meter_no else '')
    return [account_cell, name_cell, previous_cell, present_cell, used_cell, status, _grid_actions(account_id, current, bill, back)]


def _grid_actions(account_id, current, bill, back):
    """Edit a saved reading (with a reason, logged); an issued bill locks it, so link to the bill instead."""
    e = ui.esc
    links = []
    if bill is not None and bill['status'] == 'issued':
        links.append(f'<a class="btn btn-secondary btn-small" href="/cycle/bills/{e(bill["id"])}">View bill</a>')
    else:
        if current:
            query = urlencode({'account': account_id, 'back': back})
            links.append(f'<a class="btn btn-secondary btn-small" href="/cycle/readings/edit?{e(query)}">Edit</a>')
        links.append(f'<a class="grid-link" href="/cycle/readings/meter?account={e(account_id)}">Meter replaced</a>')
    return ui.raw(f'<span class="grid-actions">{"".join(links)}</span>')


def _review_panel(c, period, clause, params):
    """A scrollable window with every reading saved this month for the current phase and search (all pages),
    so the office can read them through once for typing mistakes. Each row links to its edit page."""
    rows = c.all(
        'SELECT a.id AS account_id, a.account_no, a.name, r.previous, r.present, r.flag, r.read_on, '
        "u.display_name AS entered_by, b.id AS bill_id, b.status AS bill_status "
        'FROM readings r JOIN accounts a ON a.id = r.account_id LEFT JOIN users u ON u.id = r.entered_by '
        "LEFT JOIN bills b ON b.account_id = a.id AND b.period = r.period AND b.status <> 'cancelled' "
        f"WHERE r.period = ? AND a.status = 'active'{clause} ORDER BY a.account_no, a.id LIMIT ?",
        (period,) + params + (MAX_REVIEW_ROWS + 1,))
    cut = len(rows) > MAX_REVIEW_ROWS
    rows = rows[:MAX_REVIEW_ROWS]
    e = ui.esc
    table_rows, total, flagged = [], Decimal(0), 0
    back = '/cycle/readings'
    for r in rows:
        used = Decimal(r['present']) - Decimal(r['previous'])
        total += used
        flagged += 1 if r['flag'] else 0
        if r['bill_status'] == 'issued':
            action = ui.raw(f'<a class="btn btn-secondary btn-small" href="/cycle/bills/{e(r["bill_id"])}">View bill</a>')
        else:
            query = urlencode({'account': r['account_id'], 'back': back})
            action = ui.raw(f'<a class="btn btn-secondary btn-small" href="/cycle/readings/edit?{e(query)}">Edit</a>')
        table_rows.append([
            ui.copyable(r['account_no']), ui.copyable(r['name'], ui.raw(f'<a href="/accounts/{e(r["account_id"])}">{e(r["name"])}</a>')),
            r['previous'], r['present'], format_reading(used),
            ui.pill(r['flag'].capitalize(), 'warn') if r['flag'] else '—', r['read_on'] or '—', r['entered_by'] or '—', action])
    summary = (f'{len(rows):,} saved · {flagged:,} to double-check · {format_reading(total)} m³ in all'
               + (f' · showing the first {MAX_REVIEW_ROWS:,}; narrow the phase or search to see the rest' if cut else ''))
    table = ui.table(['Account', 'Name', 'Previous', 'Present', 'Used (m³)', 'Flag', 'Date read', 'Typed by', ''], table_rows,
                     numeric_cols=(2, 3, 4), empty='No readings saved yet for this phase and search.')
    return ui.raw(
        '<dialog class="review-panel" id="review-readings" aria-labelledby="review-title">'
        '<header class="review-head"><div>'
        f'<h2 id="review-title">Review readings · {e(statements.month_label(period))}</h2>'
        f'<p class="lead">{e(summary)}. Read each row against the reading sheet; use Edit to correct a typing mistake.</p></div>'
        '<button class="btn btn-secondary" type="button" data-dialog-close>Close</button></header>'
        '<section class="review-unsaved" data-review-unsaved hidden aria-labelledby="review-unsaved-title">'
        '<h3 id="review-unsaved-title">Typed on this page, not saved yet</h3><div data-review-unsaved-list></div>'
        '<button class="btn btn-primary" type="button" data-review-save>Save these readings</button></section>'
        f'<div class="review-body">{table}</div></dialog>')


@route('POST', '/cycle/readings', roles=EDIT_ROLES)
def save_readings(req, c):
    period = billing.current_period(c)
    if req.form.get('period') != period:
        raise UserError('The billing month was changed while this page was open. Reload the page and type the readings again.')
    page = _int_or(req.form.get('page'), 1)
    # Only known list choices go back into the redirect; the readings page re-validates each of them.
    keep = {name: req.form.get(name, '')[:80] for name in ('phase', 'status', 'q', 'sort', 'dir') if req.form.get(name)}
    back = '/cycle/readings?' + urlencode({**keep, 'page': page})
    read_on = req.date('read_on', 'Date read')
    typed = {name[len('present_'):]: value.strip() for name, value in req.form.items()
             if name.startswith('present_') and value.strip()}
    if len(typed) > MAX_ROWS_PER_SAVE:
        raise UserError(f'Save at most {MAX_ROWS_PER_SAVE} readings at a time.')
    if not typed:
        return Redirect(back, ('info', 'No readings were typed, so nothing was saved.'))
    marks = ', '.join('?' for _ in typed)
    accounts = c.all(f"SELECT id, account_no, name FROM accounts WHERE status = 'active' AND id IN ({marks}) ORDER BY account_no",
                     tuple(typed))
    if len(accounts) != len(typed):
        raise UserError('Some rows belong to homeowners that are no longer active. Reload the page and try again.')
    ctx = load_reading_context(c, list(typed), period)
    plan, settings = None, None
    saved, unchanged, problems, clashes = 0, 0, [], []
    for account in accounts:
        clash = _grid_clash(req, ctx, account['id'], typed[account['id']])
        if clash == 'theirs':      # untouched row whose reading someone saved meanwhile: keep theirs
            unchanged += 1
            continue
        if clash == 'conflict':    # both typed different numbers: the person chooses (readings_resolve_page)
            clashes.append(account['id'])
            continue
        try:
            outcome = _save_reading(c, req, account, ctx, typed[account['id']], period, read_on,
                                    expected_version=(ctx['current'][account['id']]['version']
                                                      if account['id'] in ctx['current'] else None))
        except UserError as error:
            problems.append(f"{account['account_no']} {error}")
            continue
        if outcome is None:
            unchanged += 1
            continue
        saved += 1
        bill = ctx['bills'].get(account['id'])
        if bill:  # a draft: its water charge must follow the corrected reading
            plan = plan or _plan_for(c, period)
            settings = settings or statements.office_settings(c)
            refresh_draft(c, bill['id'], plan, settings)
    sync_readings_alert(c, period)
    message = f'Saved {saved} reading{"s" if saved != 1 else ""}.'
    if unchanged:
        message += f' {unchanged} unchanged.'
    if clashes:
        query = [('back', back), ('read_on', read_on)] + [('a', account_id) for account_id in clashes[:MAX_CLASHES_SHOWN]]
        query += [(f'm_{account_id}', typed[account_id]) for account_id in clashes[:MAX_CLASHES_SHOWN]]
        return Redirect('/cycle/readings/resolve?' + urlencode(query),
                        ('warn', f'{message} {len(clashes)} reading{"s" if len(clashes) != 1 else ""} were saved by someone else '
                                 'while you typed: choose which number to keep.'))
    if problems:
        shown = '; '.join(problems[:8]) + ('; …' if len(problems) > 8 else '')
        return Redirect(back, ('warn', f'{message} Not saved ({len(problems)}): {shown}'))
    return Redirect(back, ('success', message))


def _grid_clash(req, ctx, account_id, typed_text):
    """'' (save normally), 'theirs' (someone saved this row meanwhile and this person did not change it) or
    'conflict' (both changed it to different numbers). Compared as numbers, so '599' and '599.0' agree."""
    existing = ctx['current'].get(account_id)
    if existing is None:
        return ''

    def number(text):  # Decimal equality ignores trailing zeros; text that is not a reading compares as text
        try:
            return parse_reading(text)
        except UserError:
            return (text or '').strip()
    mine, theirs = number(typed_text), number(existing['present'])
    if mine == theirs:  # same number as saved: nothing to write (date corrections use the edit page)
        return 'theirs'
    if req.form.get(f'v_{account_id}', '') == str(existing['version']):
        return ''
    if mine == number(req.form.get(f'base_present_{account_id}', '')):
        return 'theirs'
    return 'conflict'


@route('GET', '/cycle/readings/resolve', roles=EDIT_ROLES)
def readings_resolve_page(req):
    """Readings two people typed differently: show both numbers, the person ticks which to keep and saves again."""
    back = _readings_back(req.query.get('back'))
    ids = req.query_lists.get('a', [])[:MAX_CLASHES_SHOWN]
    with db.connect() as c:
        period = billing.current_period(c)
        found = {a['id']: a for a in c.all(f"SELECT id, account_no, name FROM accounts WHERE id IN ({', '.join('?' for _ in ids)})",
                                            tuple(ids))} if ids else {}
        ctx = load_reading_context(c, list(found), period)
        typists = {row['id']: row['display_name'] for row in c.all('SELECT id, display_name FROM users')}
    e = ui.esc
    rows, hidden = [], [f'<input type="hidden" name="period" value="{e(period)}">',
                        f'<input type="hidden" name="read_on" value="{e(req.query.get("read_on", db.today())[:10])}">']
    for account_id in ids:
        account, current = found.get(account_id), ctx['current'].get(account_id)
        mine = (req.query.get(f'm_{account_id}') or '').strip()[:20]
        if account is None or current is None or not mine:
            continue
        who = typists.get(current['entered_by'], 'Someone else')
        field = f'present_{e(account_id)}'
        rows.append(f'<tr><th scope="row">{e(account["account_no"])}<span class="grid-sub">{e(account["name"])}</span></th>'
                    f'<td><label class="pick"><input type="radio" name="{field}" value="{e(current["present"])}"> '
                    f'<span>{e(current["present"])}<span class="grid-sub">{e(who)}, {e(current["entered_at"][11:16])} UTC</span></span></label></td>'
                    f'<td><label class="pick"><input type="radio" name="{field}" value="{e(mine)}" checked> <span>{e(mine)}</span></label></td></tr>')
        hidden.append(f'<input type="hidden" name="v_{e(account_id)}" value="{current["version"]}">'
                      f'<input type="hidden" name="base_present_{e(account_id)}" value="{e(current["present"])}">')
    crumbs = [CYCLE_CRUMB, ('Enter readings', back), 'Choose which reading to keep']
    if not rows:
        return req.page('Readings', ui.join([ui.heading('Nothing to choose', crumbs=crumbs),
                                             ui.empty('These readings were already settled.', ui.button_link(back, 'Back to readings'))]),
                        active='cycle', live=False)
    table = ui.raw('<div class="table-wrap"><table class="ledger resolver"><thead><tr><th scope="col">Homeowner</th>'
                   '<th scope="col">Saved by someone else</th><th scope="col">What you typed</th></tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table></div>')
    form = ui.form('/cycle/readings', req.user['csrf'], ui.join([ui.raw(''.join(hidden)), table]), 'Save my choices',
                   secondary=ui.button_link(back, 'Cancel', kind='secondary'))
    body = ui.join([ui.heading('Choose which reading to keep', 'Another computer saved these readings while you were typing. '
                               'Nothing of yours was saved for them yet.', crumbs=crumbs), form])
    return req.page('Choose readings', body, active='cycle', live=False)


def _save_reading(c, req, account, ctx, text, period, read_on, *, previous=None, reason='', expected_version=None):
    """Validate then write one reading. Returns the reading id, or None if nothing changed.
    Raises UserError before any write, so a bad row never leaves half a change behind.

    `previous` corrects a hand-typed first reading (edit page only); `reason` goes into the audit entry."""
    account_id = account['id']
    try:
        present = format_reading(parse_reading(text))
    except UserError:
        raise UserError('has a reading that is not a number.') from None
    previous = previous if previous is not None else previous_reading(ctx, account_id)
    if previous is None:
        typed_previous = req.form.get(f'previous_{account_id}', '').strip()
        if not typed_previous:
            raise UserError('needs the previous reading too (no earlier reading on file).')
        try:
            previous = format_reading(parse_reading(typed_previous))
        except UserError:
            raise UserError('has a previous reading that is not a number.') from None
    bill = ctx['bills'].get(account_id)
    if bill and bill['status'] == 'issued':
        raise UserError('already has an issued bill; cancel that bill first to change the reading.')
    usage = Decimal(present) - Decimal(previous)
    if usage < 0:
        raise UserError(f'is lower than last month ({previous}); if the meter was replaced use “Meter replaced”.')
    existing = ctx['current'].get(account_id)
    if existing and existing['present'] == present and existing['previous'] == previous and existing['read_on'] == read_on:
        return None
    flag = reading_flag(usage, ctx['history'][account_id])
    if existing and existing['flag'] == 'meter replaced':
        flag = 'meter replaced'
    meter = ctx['meters'].get(account_id)
    values = {'previous': previous, 'present': present, 'read_on': read_on, 'flag': flag,
              'entered_by': req.user['id'], 'entered_at': db.now()}
    if existing:
        changes = {k: v for k, v in values.items() if existing[k] != v}
        if not c.update('readings', existing['id'], changes, expected_version=expected_version):
            raise UserError('was changed on another computer at the same moment; reload the page and check it.')
        reading_id = existing['id']
        # The office asked to see who corrected a saved reading (e.g. 600 typed, meant 599).
        audit.log(c, req.user, 'reading.edit', 'reading', reading_id,
                  {k: existing[k] for k in changes}, {**changes, **({'reason': reason} if reason else {})})
    else:
        reading_id = c.insert('readings', {'account_id': account_id, 'period': period,
                                           'meter_id': meter['id'] if meter else None, **values})
    _sync_flag_alert(c, account, period, flag, usage)
    return reading_id


def _sync_flag_alert(c, account, period, flag, usage):
    key = f"{account['id']}:{period}"
    if flag in ('zero use', 'high use', 'low use'):
        alerts.raise_alert(c, 'reading_flag', key,
                           f"{account['name']} ({account['account_no']}): {flag}, {format_reading(usage)} m³ in {statements.month_label(period)}.",
                           f"/accounts/{account['id']}", 'warn')
    else:
        alerts.resolve_alert(c, 'reading_flag', key, None)


# --- correct a saved reading ------------------------------------------------------------------------------

def _readings_back(value):
    """Only a readings page may be the return address (never another site, never another screen)."""
    value = (value or '').strip()
    if value.startswith('/cycle/readings') and '//' not in value and '\\' not in value and len(value) <= 400:
        return value
    return '/cycle/readings'


def _first_reading_on_file(ctx, account_id):
    """No earlier reading and no meter start: the previous reading was typed by hand, so it may be corrected too."""
    meter = ctx['meters'].get(account_id)
    return ctx['last'].get(account_id) is None and not (meter and meter['start_reading'])


@route('GET', '/cycle/readings/edit', roles=EDIT_ROLES)
def edit_reading_page(req):
    back = _readings_back(req.query.get('back'))
    with db.connect() as c:
        account = _load_account(c, req.query.get('account', ''))
        period = billing.current_period(c)
        ctx = load_reading_context(c, [account['id']], period)
        current = ctx['current'].get(account['id'])
        if current is None:
            raise NotFound('No reading is saved for this homeowner this month yet. Type it in the readings grid.')
        typed_by = c.scalar('SELECT display_name FROM users WHERE id = ?', (current['entered_by'],)) if current['entered_by'] else None
    bill = ctx['bills'].get(account['id'])
    used = Decimal(current['present']) - Decimal(current['previous'])
    facts = ui.facts([
        ('Homeowner', ui.raw(f'<a href="/accounts/{ui.esc(account["id"])}">{ui.esc(account["name"])}</a>')),
        ('Account no.', ui.copyable(account['account_no'])), ('Meter no.', _meter_no(ctx, account['id']) or '—'),
        ('Previous reading', current['previous']), ('Saved present reading', current['present']),
        ('Used', f'{format_reading(used)} m³'), ('Flag', current['flag'].capitalize() or '—'),
        ('Typed by', f"{typed_by or '—'} · {current['entered_at'][:16].replace('T', ' ')} UTC"),
    ])
    crumbs = [CYCLE_CRUMB, ('Enter readings', back), f"Edit reading · {account['name']}"]
    heading = ui.heading('Correct a saved reading', f"{account['name']} · {statements.month_label(period)}", crumbs=crumbs)
    if bill is not None and bill['status'] == 'issued':
        locked = ui.notice('This month’s bill is already issued, so the reading is locked. Cancel the bill first, then correct the reading.',
                           'warn', action=ui.button_link(f"/cycle/bills/{bill['id']}", 'Open the bill', kind='secondary'))
        return req.page('Edit reading', ui.join([heading, facts, locked]), active='cycle', live=False)
    e = ui.esc
    holder = editing.acquire('readings', current['id'], req.user)
    fields = [
        ui.raw(f'<input type="hidden" name="account_id" value="{e(account["id"])}"><input type="hidden" name="period" value="{e(period)}">'
               f'<input type="hidden" name="back" value="{e(back)}">'),
        facts,
        editing.version_fields(current['version'], _reading_text(current, account['id'], ctx)),
        editing.lock_marker('readings', current['id']),
    ]
    if _first_reading_on_file(ctx, account['id']):
        fields.append(ui.field('Previous reading', f"previous_{account['id']}", value=current['previous'], attrs='inputmode="decimal"',
                               hint='No earlier reading is on file, so this one was typed by hand and can be corrected too.'))
    fields += [
        ui.field('Correct present reading', 'present', value=current['present'], attrs='inputmode="decimal" autocomplete="off" autofocus',
                 hint='Type the number exactly as written on the reading sheet.'),
        ui.field('Date read', 'read_on', type='date', value=current['read_on'] or db.today()),
        ui.field('Why is it being changed?', 'reason', attrs=f'maxlength="{MAX_REASON_LENGTH}"',
                 hint='For example “typed 600, sheet says 599”. Kept in the audit trail.'),
    ]
    if holder:
        banner = editing.lock_banner(holder, 'readings', current['id'], can_take_over=req.user['role'] == 'admin',
                                     csrf=req.user['csrf'], back=f"/cycle/readings/edit?{urlencode({'account': account['id'], 'back': back})}")
        return req.page('Edit reading', ui.join([heading, banner, facts]), active='cycle', live=False)
    form = ui.form('/cycle/readings/edit', req.user['csrf'], ui.join(fields), 'Save correction',
                   secondary=ui.button_link(back, 'Cancel', kind='secondary'))
    note = ui.notice('A draft bill for this month is recalculated from the corrected reading.', 'info') if bill else ''
    return req.page('Edit reading', ui.join([heading, note, form]), active='cycle', live=False)


READING_LABELS = {'present': 'Present reading', 'read_on': 'Date read'}


def _reading_text(current, account_id, ctx):
    """The saved reading as the correction form shows it (raw text), for the merge."""
    text = {'present': current['present'], 'read_on': current['read_on'] or ''}
    if _first_reading_on_file(ctx, account_id):
        text[f'previous_{account_id}'] = current['previous']
    return text


@route('POST', '/cycle/readings/edit', roles=EDIT_ROLES)
def edit_reading(req, c):
    period = billing.current_period(c)
    if req.form.get('period') != period:
        raise UserError('The billing month was changed while this page was open. Go back to the readings and open the reading again.')
    back = _readings_back(req.form.get('back'))
    account = _load_account(c, req.form.get('account_id', ''))
    ctx = load_reading_context(c, [account['id']], period)
    current = ctx['current'].get(account['id'])
    if current is None:
        raise UserError('No reading is saved for this homeowner this month yet. Type it in the readings grid.')
    other = editing.holder_other_than(c, 'readings', current['id'], req.user)
    if other:
        raise UserError(f"{other['user_name']} is correcting this reading now, so your change was not saved. Open it again when they are done.")
    version = editing.posted_version(req)
    if version is None:
        raise UserError('This page is out of date. Open the reading again, then make your correction.')
    saved_text = _reading_text(current, account['id'], ctx)
    labels = {**READING_LABELS, f"previous_{account['id']}": 'Previous reading'}
    kept, who = [], ''
    if version != current['version']:  # someone saved this reading since the page was drawn
        conflicts, kept = editing.merge_form(req, saved_text, saved_text.keys(),
                                             {field: parse_reading for field in saved_text if field != 'read_on'})
        who, when = editing.last_change(c, 'reading', current['id'])
        if conflicts:
            raise ShowPage('Edit reading', editing.resolver_page(
                req, title=f"Reading for {account['name']}: choose what to keep",
                crumbs=[CYCLE_CRUMB, ('Enter readings', back), 'Choose what to keep'], action='/cycle/readings/edit',
                labels=labels, conflicts=conflicts, saved_text=saved_text, version=current['version'], who=who, when=when,
                cancel=back), active='cycle')
    reason = req.text('reason', 'Why is it being changed?', limit=MAX_REASON_LENGTH)
    read_on = req.date('read_on', 'Date read')
    previous = None
    if _first_reading_on_file(ctx, account['id']):
        typed_previous = req.form.get(f"previous_{account['id']}", '').strip()
        try:
            previous = format_reading(parse_reading(typed_previous))
        except UserError:
            raise UserError('The previous reading must be a number.') from None
    try:
        reading_id = _save_reading(c, req, account, ctx, req.form.get('present', '').strip(), period, read_on,
                                   previous=previous, reason=reason, expected_version=current['version'])
    except UserError as error:
        raise UserError(f'This reading {error}') from None
    editing.release(c, 'readings', current['id'], req.user)
    if reading_id is None:  # same numbers again (or a double press): one change, never two
        return Redirect(back, ('info', f"Nothing changed for {account['name']}; the saved reading already matches."
                               + editing.kept_note(kept, labels, who)))
    bill = ctx['bills'].get(account['id'])
    if bill:
        refresh_draft(c, bill['id'], _plan_for(c, period), statements.office_settings(c))
    sync_readings_alert(c, period)
    return Redirect(back, ('success', f"Reading for {account['name']} corrected. The change and its reason are in the audit trail."
                           + editing.kept_note(kept, labels, who)))


# --- meter replacement ---------------------------------------------------------------------------------

@route('GET', '/cycle/readings/meter', roles=EDIT_ROLES)
def meter_page(req):
    with db.connect() as c:
        account = _load_account(c, req.query.get('account', ''))
        period = billing.current_period(c)
        ctx = load_reading_context(c, [account['id']], period)
    fields = ui.join([
        ui.raw(f'<input type="hidden" name="account_id" value="{ui.esc(account["id"])}">'),
        ui.facts([('Homeowner', f"{account['name']} ({account['account_no']})"), ('Old meter', _meter_no(ctx, account['id']) or '—'),
                  ('Old meter’s last reading', previous_reading(ctx, account['id']) or '—')]),
        ui.field('New meter number', 'meter_no'),
        ui.field('New meter’s starting reading', 'start_reading', value='0', attrs='inputmode="decimal"',
                 hint='The number on the new meter when it was installed (often 0).'),
        ui.field('Date installed', 'installed_on', type='date', value=db.today()),
        ui.field('Present reading on the new meter', 'present', required=False, attrs='inputmode="decimal"',
                 hint=f'For {statements.month_label(period)}. Leave blank to enter it later in the grid.'),
    ])
    crumbs = [CYCLE_CRUMB, ('Enter readings', '/cycle/readings'), 'Meter replaced']
    body = ui.heading('Meter replaced', 'Record the new meter so this month’s usage starts from its first reading.', crumbs=crumbs) + ui.form(
        '/cycle/readings/meter', req.user['csrf'], fields, 'Save new meter',
        secondary=ui.button_link('/cycle/readings', 'Cancel', kind='secondary'))
    return req.page('Meter replaced', body, active='cycle', live=False)


@route('POST', '/cycle/readings/meter', roles=EDIT_ROLES)
def replace_meter(req, c):
    account = _load_account(c, req.form.get('account_id', ''))
    period = billing.current_period(c)
    meter_no = req.text('meter_no', 'New meter number', limit=40)
    start = req.reading('start_reading', 'Starting reading')
    installed_on = req.date('installed_on', 'Date installed')
    present = req.reading('present', 'Present reading') if req.form.get('present', '').strip() else None
    ctx = load_reading_context(c, [account['id']], period)
    bill = ctx['bills'].get(account['id'])
    if bill and bill['status'] == 'issued':
        raise UserError('This homeowner already has an issued bill for this month. Cancel it first.')
    if present is not None and Decimal(present) < Decimal(start):
        raise UserError('The present reading cannot be lower than the new meter’s starting reading.')
    old = ctx['meters'].get(account['id'])
    if old:
        c.update('meters', old['id'], {'removed_on': installed_on})
    meter_id = c.insert('meters', {'account_id': account['id'], 'meter_no': meter_no, 'installed_on': installed_on,
                                   'start_reading': start})
    audit.log(c, req.user, 'meter.replace', 'account', account['id'],
              {'meter_no': old['meter_no']} if old else None, {'meter_no': meter_no, 'start_reading': start})
    if present is not None:
        values = {'meter_id': meter_id, 'previous': start, 'present': present, 'read_on': installed_on,
                  'flag': 'meter replaced', 'entered_by': req.user['id'], 'entered_at': db.now()}
        existing = ctx['current'].get(account['id'])
        if existing:
            c.update('readings', existing['id'], values)
        else:
            c.insert('readings', {'account_id': account['id'], 'period': period, **values})
        if bill:
            refresh_draft(c, bill['id'], _plan_for(c, period), statements.office_settings(c))
        sync_readings_alert(c, period)
    return Redirect('/cycle/readings', ('success', f"New meter {meter_no} saved for {account['name']}."))


# --- bills: building -------------------------------------------------------------------------------------

def _plan_for(c, period):
    plan = billing.current_plan(c, statements.day_in(period, 11).isoformat())
    if plan is None:
        raise UserError('No water rate is in effect for this billing month. Ask the administrator to add one.')
    return plan


def system_loss_line(usage, bulk):
    """Consumption × the month's charged system loss rate, or None (no figures saved, or no use)."""
    if bulk is None or usage <= 0 or bulk.charged_loss_rate <= 0:
        return None
    return {'m3': format_reading(usage), 'rate_cents': int(bulk.charged_loss_rate * 100),
            'full_rate_cents': int(bulk.loss_rate * 100), 'subsidy_percent': bulk.subsidy_percent,
            'amount_cents': round_half_up(usage * bulk.charged_loss_rate * 100)}


def bill_values(account, reading, plan, settings, *, dues_months, other_lines, prior_cents, meter_no, bulk=None):
    """All money columns + detail for a draft. The server's numbers are the only numbers."""
    calc = billing.calculate(reading['previous'], reading['present'], billing.plan_config(plan), account['category'])
    dues_rate = int(settings['dues_cents'])
    dues = dues_rate * dues_months
    loss = system_loss_line(calc.usage, bulk)
    # System loss is billed with the "other" charges (bills has no column of its own); detail keeps it apart.
    other = sum(line['amount_cents'] for line in other_lines) + (loss['amount_cents'] if loss else 0)
    detail = {
        'previous': reading['previous'], 'present': reading['present'], 'usage': format_reading(calc.usage),
        'category': account['category'], 'meter_no': meter_no, 'flag': reading['flag'],
        'lines': [line.to_dict() for line in calc.lines],
        'dues_months': dues_months, 'dues_rate_cents': dues_rate, 'other_lines': other_lines,
        'system_loss': loss,
    }
    return {
        'reading_id': reading['id'], 'rate_plan_id': plan['id'], 'water_cents': calc.water_cents, 'dues_cents': dues,
        'other_cents': other, 'total_cents': calc.water_cents + dues + other, 'prior_balance_cents': prior_cents,
        'detail': json.dumps(detail, sort_keys=True),
    }


def refresh_draft(c, bill_id, plan, settings, *, dues_months=None, other_lines=None):
    """Recalculate a draft from its reading (and optionally new dues/other lines). Draft-only."""
    bill = c.one("SELECT * FROM bills WHERE id = ? AND status = 'draft'", (bill_id,))
    if bill is None:
        raise UserError('This bill is no longer a draft, so it cannot be changed. Reload the page.')
    account = c.one('SELECT id, category FROM accounts WHERE id = ?', (bill['account_id'],))
    reading = c.one('SELECT * FROM readings WHERE account_id = ? AND period = ?', (bill['account_id'], bill['period']))
    detail = json.loads(bill['detail'])
    values = bill_values(
        account, reading, plan, settings,
        dues_months=detail['dues_months'] if dues_months is None else dues_months,
        other_lines=detail['other_lines'] if other_lines is None else other_lines,
        prior_cents=ledger.balance(c, bill['account_id']), meter_no=detail.get('meter_no', ''),
        bulk=billing.period_bulk_rates(c, bill['period']))
    # bill_values rebuilds detail from scratch; the office's per-statement note choices must survive it.
    kept = {k: detail[k] for k in STATEMENT_NOTE_KEYS if k in detail}
    if kept:
        values['detail'] = json.dumps({**json.loads(values['detail']), **kept}, sort_keys=True)
    changes = {k: v for k, v in values.items() if bill[k] != v}
    if changes and not c.update('bills', bill_id, changes, only_if={'status': 'draft'}):
        raise UserError('This bill is no longer a draft, so it cannot be changed. Reload the page.')
    return bill


@route('POST', '/cycle/bills/make', roles=EDIT_ROLES)
def make_drafts(req, c):
    period = req.period('period')
    account_id = req.form.get('account_id', '').strip()
    only_account = ' AND r.account_id = ?' if account_id else ''
    params = (period, account_id) if account_id else (period,)
    readings = c.all(
        'SELECT r.*, a.category, a.name FROM readings r JOIN accounts a ON a.id = r.account_id '
        f"WHERE r.period = ?{only_account} AND a.status <> 'closed' AND NOT EXISTS "
        "(SELECT 1 FROM bills b WHERE b.account_id = r.account_id AND b.period = r.period AND b.status <> 'cancelled')",
        params)
    target = '/cycle/bills?' + urlencode({'period': period, **({'account': account_id} if account_id else {})})
    if not readings:
        message = ('This homeowner has no reading for this month yet, or already has a bill.' if account_id
                   else 'Every homeowner with a reading already has a bill for this month.')
        return Redirect(target, ('info', message))
    plan = _plan_for(c, period)
    settings = statements.office_settings(c)
    bulk = billing.period_bulk_rates(c, period)
    # Monthly dues are a separate billing with their own BIR invoice unless the office turns them back on.
    dues_months = 1 if settings['dues_on_water_bill'] else 0
    balances = {row['account_id']: row['total'] for row in c.all(
        'SELECT account_id, SUM(amount_cents) AS total FROM ledger_entries '
        'WHERE account_id IN (SELECT account_id FROM readings WHERE period = ?) GROUP BY account_id', (period,))}
    meters = load_reading_context(c, [r['account_id'] for r in readings], period)['meters']
    due_on = statements.day_in(period, settings['due_day']).isoformat()
    now = db.now()
    for reading in readings:
        meter = meters.get(reading['account_id'])
        values = bill_values({'category': reading['category']}, reading, plan, settings, dues_months=dues_months, other_lines=[],
                             prior_cents=balances.get(reading['account_id'], 0), meter_no=meter['meter_no'] if meter else '',
                             bulk=bulk)
        bill_id = db.new_id()
        # statement_no is NOT NULL UNIQUE; the real number is given only when the bill is issued.
        c.insert('bills', {'id': bill_id, 'statement_no': f'DRAFT-{bill_id}', 'account_id': reading['account_id'],
                           'period': period, 'due_on': due_on, 'status': 'draft', 'created_by': req.user['id'],
                           'created_at': now, **values})
    audit.log(c, req.user, 'bill.make_drafts', 'period', period, None, {'count': len(readings)})
    sync_drafts_alert(c, period)
    return Redirect(target, ('success', f'Made {len(readings)} draft bill{"s" if len(readings) != 1 else ""}. Check the flagged ones, then issue.'))


# --- bills: review and issue -------------------------------------------------------------------------------

@route('GET', '/cycle/bills', roles=READ_ROLES)
def bills_page(req):
    with db.connect() as c:
        period = req.query.get('period') or billing.current_period(c)
        if not billing.PERIOD_PATTERN.fullmatch(period):
            raise NotFound('That billing month is not valid.')
        account_id = req.query.get('account', '').strip()
        account = _load_account(c, account_id) if account_id else None
        only = ' AND b.account_id = ?' if account else ''
        params = (period, account_id) if account else (period,)
        summary = c.one(
            "SELECT (SELECT COUNT(*) FROM readings r WHERE r.period = ? AND NOT EXISTS (SELECT 1 FROM bills b WHERE b.account_id = r.account_id "
            "AND b.period = r.period AND b.status <> 'cancelled')" + (' AND r.account_id = ?' if account else '') + ') AS to_make, '
            f"(SELECT COUNT(*) FROM bills b WHERE b.period = ? AND b.status = 'draft'{only}) AS drafts, "
            f"(SELECT COUNT(*) FROM bills b WHERE b.period = ? AND b.status = 'issued'{only}) AS issued",
            params * 3)
        search = req.query.get('q', '').strip()[:80]
        phases = load_phases(c)
        phase_id = req.query.get('phase', 'all')
        status = req.list_filter('status', BILL_STATUS)
        phase_sql, phase_params = _phase_clause(phase_id, phases)
        search_sql, search_params = _search_and(search)
        search_sql, search_params = phase_sql + search_sql + BILL_STATUS[status][1], phase_params + search_params
        query = {'period': period, **({'account': account_id} if account else {}), **({'q': search} if search else {}),
                 **({'phase': phase_id} if phase_id != 'all' else {}), **({'status': status} if status != 'all' else {})}
        sort = ui.Sort.from_query(req.query, '/cycle/bills?' + urlencode(query), BILL_SORT_COLUMNS)
        total = c.scalar('SELECT COUNT(*) FROM bills b JOIN accounts a ON a.id = b.account_id LEFT JOIN readings r ON r.id = b.reading_id '
                         f"WHERE b.period = ? AND b.status <> 'cancelled'{only}{search_sql}", params + search_params)
        order_sql, order_params = sort.order(BILL_ORDER, BILL_DEFAULT_ORDER, 'a.account_no, b.id')
        pages = max(1, math.ceil(total / BILL_PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        bills = c.all(
            'SELECT b.id, b.status, b.statement_no, b.water_cents, b.dues_cents, b.other_cents, b.total_cents, '
            "b.prior_balance_cents, a.account_no, a.name, COALESCE(r.flag, '') AS flag "
            'FROM bills b JOIN accounts a ON a.id = b.account_id LEFT JOIN readings r ON r.id = b.reading_id '
            f"WHERE b.period = ? AND b.status <> 'cancelled'{only}{search_sql} "
            f'ORDER BY {order_sql} LIMIT ? OFFSET ?', params + search_params + order_params + (BILL_PAGE_SIZE, (page - 1) * BILL_PAGE_SIZE))
    make_form = ui.form('/cycle/bills/make', req.user['csrf'], ui.raw(
        f'<input type="hidden" name="period" value="{ui.esc(period)}">'
        + (f'<input type="hidden" name="account_id" value="{ui.esc(account_id)}">' if account else '')),
        'Make draft bill for this homeowner' if account else 'Make draft bills for everyone with a reading')
    query.update(sort.params)
    print_link = ui.button_link('/cycle/bills/print?' + urlencode({'period': period}), 'Print issued statements', kind='secondary')
    lead = f"{statements.month_label(period)}" + (f" · {account['name']} ({account['account_no']})" if account else '')
    # Opened from a homeowner's "Make bill" button: lead back to that homeowner.
    crumbs = [('Homeowners', '/accounts'), (account['name'], f"/accounts/{account_id}"), 'Bills'] if account else [CYCLE_CRUMB, 'Bills']
    parts = [ui.heading('Bills', lead, actions=print_link if summary['issued'] else '', crumbs=crumbs),
             ui.figures([('Readings without a bill', f"{summary['to_make']:,}"), ('Drafts to check', f"{summary['drafts']:,}"),
                         ('Issued', f"{summary['issued']:,}")])]
    if summary['to_make']:
        parts.append(ui.section('Step 1 · Make draft bills', ui.raw('<span class="lead-dot">') + ui.dot('Do this next') + ui.raw('</span>') + make_form))
    if not account:
        bill_filters = ui.join([ui.field('Billing month', 'period', type='month', value=period),
                                _status_select('Status', 'status', BILL_STATUS, status)])
        parts.append(_filter_form('/cycle/bills', phases, phase=phase_id, search=search, hidden=sort.params,
                                  extra=bill_filters, filtered=status != 'all'))
    parts.append(ui.section('Step 2 · Check and issue', _issue_form(req, bills, period, sort) if bills else ui.empty(
        'No bill matches these filters.' if search or len(query) > 1 else 'No bills yet for this month. Enter readings, then make draft bills.'),
        lead='Flagged readings are listed first and are not ticked: open them before issuing. '
             'Zero-use bills are issued by an administrator.'))
    parts.append(ui.pagination(page, pages, '/cycle/bills?' + urlencode(query), total=total, noun='bill'))
    return req.page('Bills', ui.join(parts), active='cycle', dots={'cycle'} if summary['to_make'] or summary['drafts'] else None,
                    live=('bills', 'readings', 'ledger_entries', 'accounts'))


def _issue_form(req, bills, period, sort):
    e = ui.esc
    rows, has_draft = [], False
    for b in bills:
        if b['status'] == 'draft':
            has_draft = True
            checked = '' if b['flag'] else ' checked'
            needs_admin = b['flag'] == ADMIN_ONLY_FLAG and req.user['role'] != 'admin'
            tick = ui.raw(f'<input class="tick" type="checkbox" name="bill[]" value="{e(b["id"])}"{checked}'
                          f'{" disabled" if needs_admin else ""} aria-label="Issue the bill for {e(b["name"])}">')
            state = ui.join([ui.pill('Draft', 'info'), ui.pill('Administrator issues', 'neutral') if needs_admin else '',
                             ui.dot('Check this bill') if b['flag'] else ''])
        else:
            tick, state = '', ui.pill('Issued', 'success')
        flag = ui.pill(b['flag'].capitalize(), 'warn') if b['flag'] else ''
        rows.append([tick, ui.copyable(b['account_no']),
                     ui.copyable(b['name'], ui.raw(f'<a href="/cycle/bills/{e(b["id"])}">{e(b["name"])}</a>')), flag,
                     ui.money(b['water_cents']), ui.money(b['dues_cents'] + b['other_cents']), ui.money(b['prior_balance_cents']),
                     ui.money(b['prior_balance_cents'] + b['total_cents']), state])
    table = ui.table(['Issue', 'Account', 'Name', 'Flag', 'Water', 'Dues & other', 'Previous balance', 'Amount due', 'Status'],
                     rows, numeric_cols=(4, 5, 6, 7), sort=sort, keys=[b['id'] for b in bills])
    if not has_draft:
        return table
    body = ui.raw(f'<input type="hidden" name="period" value="{e(period)}"><div class="grid-wrap">{table}</div>')
    return ui.raw('<div class="wide">') + ui.form('/cycle/bills/issue', req.user['csrf'], body, 'Issue all ticked drafts',
                                                  confirm='Issue the ticked bills? Their charges go onto each homeowner’s account.') + ui.raw('</div>')


@route('POST', '/cycle/bills/issue', roles=EDIT_ROLES)
def issue_bills(req, c):
    period = req.period('period')
    ids = req.form_lists.get('bill[]', [])
    back = '/cycle/bills?' + urlencode({'period': period})
    if not ids:
        return Redirect(back, ('info', 'No bills were ticked, so nothing was issued.'))
    if len(ids) > MAX_BILLS_PER_ISSUE or len(set(ids)) != len(ids):
        raise UserError(f'Issue at most {MAX_BILLS_PER_ISSUE} bills at a time.')
    marks = ', '.join('?' for _ in ids)
    drafts = c.all(f"SELECT * FROM bills WHERE id IN ({marks}) AND period = ? AND status = 'draft'", tuple(ids) + (period,))
    held = []
    if req.user['role'] != 'admin':
        held = [b for b in drafts if json.loads(b['detail']).get('flag') == ADMIN_ONLY_FLAG]
        drafts = [b for b in drafts if b not in held]
    today = db.today()
    settings = statements.office_settings(c)
    for bill in drafts:
        issue_one(c, req.user, bill, today, settings)
    sync_drafts_alert(c, period)
    skipped = len(ids) - len(drafts) - len(held)
    message = f'Issued {len(drafts)} bill{"s" if len(drafts) != 1 else ""}.'
    if skipped:
        message += f' {skipped} were already issued or cancelled and were left alone.'
    if held:
        message += f' {len(held)} zero-use bill{"s" if len(held) != 1 else ""} need an administrator to issue.'
        return Redirect(back, ('warn', message))
    return Redirect(back, ('success', message))


def issue_one(c, user, bill, today, settings):
    """draft -> issued; posts the charges only for the single winner of the transition."""
    prior = ledger.balance(c, bill['account_id'])
    statement_no = billing.next_statement_no(c, bill['period'])
    # A statement issued late must still give the homeowner the usual days to pay, never arrive overdue.
    grace = timedelta(days=max(1, int(settings['due_day']) - int(settings['issue_day'])))
    due_on = max(bill['due_on'], (date.fromisoformat(today) + grace).isoformat())
    if not c.transition('bills', bill['id'], 'status', 'draft', 'issued',
                        {'statement_no': statement_no, 'issued_on': today, 'prior_balance_cents': prior, 'due_on': due_on}):
        return False
    detail = json.loads(bill['detail'])
    post = dict(period=bill['period'], user=user, bill_id=bill['id'], posted_on=today)
    ledger.post(c, bill['account_id'], 'water', bill['water_cents'], memo=f"{detail['usage']} m³", **post)
    if bill['dues_cents']:
        ledger.post(c, bill['account_id'], 'dues', bill['dues_cents'], memo=f"{detail['dues_months']} month(s)", **post)
    for line in detail['other_lines']:
        ledger.post(c, bill['account_id'], OTHER_KINDS[line['kind']][1], line['amount_cents'],
                    memo=statements.other_line_label(line), **post)
    loss = detail.get('system_loss')
    if loss:
        ledger.post(c, bill['account_id'], 'system_loss', loss['amount_cents'], memo=statements.system_loss_label(loss), **post)
    audit.log(c, user, 'bill.issue', 'bill', bill['id'], {'status': 'draft'}, {'status': 'issued', 'statement_no': statement_no})
    return True


# --- bills: one bill ---------------------------------------------------------------------------------------

@route('GET', '/cycle/bills/{id}', roles=READ_ROLES)
def bill_page(req):
    with db.connect() as c:
        bill = _load_bill(c, req.params['id'])
        live_prior = ledger.balance(c, bill['account_id']) if bill['status'] == 'draft' else bill['prior_balance_cents']
        settings = statements.office_settings(c)
    detail = json.loads(bill['detail'])
    is_draft = bill['status'] == 'draft'
    rows = [[line['label'], line['qty'] or '', ui.money(line['rate_cents']) if line['rate_cents'] is not None else 'flat',
             ui.money(line['amount_cents'])] for line in detail['lines']]
    rows.append([ui.raw('<strong>Water subtotal</strong>'), detail['usage'] + ' m³', '', ui.money(bill['water_cents'])])
    if detail['dues_months']:
        rows.append([f"Association dues ({detail['dues_months']} month(s) × {ui.money(detail['dues_rate_cents'])})", '', '', ui.money(bill['dues_cents'])])
    if detail.get('system_loss'):
        loss = detail['system_loss']
        rows.append(['System loss', loss['m3'], ui.money(loss['rate_cents']), ui.money(loss['amount_cents'])])
    for line in detail['other_lines']:
        rows.append([statements.other_line_label(line), '', '', ui.money(line['amount_cents'])])
    rows.append([ui.raw('<strong>Current charges</strong>'), '', '', ui.money(bill['total_cents'])])
    rows.append(['Previous balance from the account ledger' + (' (as of today)' if is_draft else ''), '', '', ui.money(live_prior)])
    rows.append([ui.raw('<strong>Total amount due</strong>'), '', '', ui.raw(f'<strong>{ui.money(live_prior + bill["total_cents"])}</strong>')])
    status_pill = {'draft': ui.pill('Draft', 'info'), 'issued': ui.pill('Issued', 'success'), 'cancelled': ui.pill('Cancelled', 'neutral')}[bill['status']]
    actions = ui.join([
        ui.button_link(f"/cycle/bills/{bill['id']}/statement", 'Print statement', kind='secondary'),
        ui.button_link(f"/cycle/bills/{bill['id']}/word", 'Download Word copy', kind='secondary'),
    ]) if bill['status'] != 'cancelled' else ''
    parts = [
        ui.heading(f"Bill for {bill['name']}", f"Account {bill['account_no']} · {statements.month_label(bill['period'])}", actions=actions,
                   crumbs=[CYCLE_CRUMB, BILLS_CRUMB, bill['name']]),
        ui.facts([('Status', status_pill), ('Statement no.', bill['statement_no'] if not is_draft else 'Given when issued'),
                  ('Readings', f"{detail['previous']} → {detail['present']} ({detail['usage']} m³)"),
                  ('Flag', detail.get('flag') or '—'), ('Due date', statements.long_date(bill['due_on']))]),
        ui.section('How the amount adds up', ui.table(['Item', 'm³', 'Rate', 'Amount'], rows, numeric_cols=(1, 2, 3))),
    ]
    if detail.get('flag') and is_draft:
        extra = ' An administrator issues zero-use bills.' if detail['flag'] == ADMIN_ONLY_FLAG else ''
        parts.insert(1, ui.notice(f"This reading was flagged: {detail['flag']}. Check the meter reading before issuing.{extra}", 'warn'))
    if bill['status'] == 'cancelled':
        parts.insert(1, ui.notice(f"This bill was cancelled: {bill['cancel_reason']}", 'warn'))
    if is_draft:
        parts.append(_draft_tools(req, bill, detail, settings))
    elif bill['status'] == 'issued' and req.user['role'] == 'admin':
        parts.append(ui.section('Cancel this bill', ui.form(
            f"/cycle/bills/{bill['id']}/cancel", req.user['csrf'],
            ui.field('Reason', 'reason', hint='Required. The charges are reversed on the account; the bill stays on record.'),
            'Cancel bill', danger=True, confirm='Cancel this issued bill and reverse its charges?')))
    return req.page('Bill', ui.join(parts), active='cycle')


def _draft_tools(req, bill, detail, settings):
    bill_id = bill['id']
    charge_form = ui.form(f'/cycle/bills/{bill_id}/charges', req.user['csrf'], ui.join([
        ui.select('Kind of charge', 'kind', [(k, label) for k, (label, _) in OTHER_KINDS.items()]),
        ui.field('Note on the statement', 'memo', required=False, hint='For example “1 of 18” or “from OR 14413”.'),
        ui.field('Amount', 'amount', attrs='inputmode="decimal"', hint='For example 200 or 1,050.00'),
    ]), 'Add charge')
    version = bill['version']
    # Charges are removed by their place in the list, so the form carries the bill's version: if anyone changed
    # the list meanwhile, the remove is refused instead of taking off the wrong charge.
    remove_rows = [[statements.other_line_label(line), ui.money(line['amount_cents']), ui.form(
        f'/cycle/bills/{bill_id}/charges/remove', req.user['csrf'],
        ui.join([ui.raw(f'<input type="hidden" name="index" value="{index}">'), editing.version_fields(version, {})]), 'Remove', danger=True)]
        for index, line in enumerate(detail['other_lines'])]
    dues_form = ui.form(f'/cycle/bills/{bill_id}/dues', req.user['csrf'], ui.join([ui.field(
        'Months of dues on this bill', 'months', type='number', value=detail['dues_months'],
        attrs=f'min="0" max="{MAX_DUES_MONTHS}" inputmode="numeric"', hint='Usually 1. Use 2 or more to catch up missed months.'),
        editing.version_fields(version, _dues_text(detail))]), 'Save dues months')
    discard = ui.form(f'/cycle/bills/{bill_id}/discard', req.user['csrf'], '', 'Discard this draft', danger=True,
                      confirm='Discard this draft bill? You can make it again from the Bills page.')
    notes = statements.statement_notes(detail, settings)
    notes_form = ui.form(f'/cycle/bills/{bill_id}/notes', req.user['csrf'], ui.join([
        ui.checkbox('Print the penalty note', 'show_penalty', checked=notes['penalty']),
        ui.checkbox('Print the disconnection notice', 'show_disconnection', checked=notes['disconnection']),
        ui.field('Note for this homeowner', 'note', type='textarea', value=notes['note'], required=False,
                 hint=f'Printed on both copies of this statement. Up to {MAX_NOTE_LENGTH} characters.'),
        editing.version_fields(version, _notes_text(notes)),
    ]), 'Save statement notes')
    return ui.join([
        ui.section('Notes on the statement', notes_form),
        ui.section('Add another charge', charge_form, lead='System loss share, Maynilad debt contribution, reconnection fee, or anything else.'),
        ui.section('Other charges on this bill', ui.table(['Charge', 'Amount', ''], remove_rows, empty='None.', numeric_cols=(1,))),
        ui.section('Association dues', dues_form),
        ui.section('Discard', discard),
    ])


@route('POST', '/cycle/bills/{id}/charges', roles=EDIT_ROLES)
def add_charge(req, c):
    bill = _load_bill(c, req.params['id'])
    kind = req.choice('kind', OTHER_KINDS, 'kind of charge')
    memo = req.text('memo', 'Note', limit=80, required=False)
    amount = req.pesos('amount')
    if amount <= 0:
        raise UserError('The amount must be more than zero.')
    lines = json.loads(bill['detail'])['other_lines'] + [
        {'kind': kind, 'label': OTHER_KINDS[kind][0], 'memo': memo, 'amount_cents': amount}]
    refresh_draft(c, bill['id'], _plan_for(c, bill['period']), statements.office_settings(c), other_lines=lines)
    audit.log(c, req.user, 'bill.add_charge', 'bill', bill['id'], None, lines[-1])
    return Redirect(f"/cycle/bills/{bill['id']}", ('success', f'{OTHER_KINDS[kind][0]} of {ui.money(amount)} added.'))


BILL_LABELS = {'months': 'Months of dues', 'show_penalty': 'Print the penalty note',
               'show_disconnection': 'Print the disconnection notice', 'note': 'Note for this homeowner'}


def _dues_text(detail):
    return {'months': str(detail['dues_months'])}


def _notes_text(notes):
    return {'show_penalty': 'yes' if notes['penalty'] else '', 'show_disconnection': 'yes' if notes['disconnection'] else '',
            'note': notes['note'] or ''}


def _merge_bill_form(req, c, bill, saved_text):
    """Three-way merge for a draft bill form saved after someone else changed the bill; resolver on a real clash."""
    version = editing.posted_version(req)
    if version is None:
        raise UserError('This page is out of date. Open the bill again, then make your change.')
    if version == bill['version']:
        return [], ''
    conflicts, kept = editing.merge_form(req, saved_text, saved_text.keys(), {'months': int})
    who, when = editing.last_change(c, 'bill', bill['id'])
    if conflicts:
        raise ShowPage('Bill', editing.resolver_page(
            req, title=f"Bill for {bill['name']}: choose what to keep",
            crumbs=[CYCLE_CRUMB, BILLS_CRUMB, (bill['name'], f"/cycle/bills/{bill['id']}"), 'Choose what to keep'],
            action=req.path, labels=BILL_LABELS, conflicts=conflicts, saved_text=saved_text, version=bill['version'],
            who=who, when=when, cancel=f"/cycle/bills/{bill['id']}"), active='cycle')
    return kept, who


@route('POST', '/cycle/bills/{id}/charges/remove', roles=EDIT_ROLES)
def remove_charge(req, c):
    bill = _load_bill(c, req.params['id'])
    if editing.posted_version(req) != bill['version']:
        raise UserError('This bill changed on another computer since you opened it, so nothing was removed. '
                        'Check the list of charges again, then remove the one you meant.')
    lines = json.loads(bill['detail'])['other_lines']
    index = _int_or(req.form.get('index'), -1)
    if not 0 <= index < len(lines):
        raise UserError('That charge is no longer on this bill. Reload the page.')
    removed = lines.pop(index)
    refresh_draft(c, bill['id'], _plan_for(c, bill['period']), statements.office_settings(c), other_lines=lines)
    audit.log(c, req.user, 'bill.remove_charge', 'bill', bill['id'], removed, None)
    return Redirect(f"/cycle/bills/{bill['id']}", ('success', 'Charge removed.'))


@route('POST', '/cycle/bills/{id}/dues', roles=EDIT_ROLES)
def set_dues_months(req, c):
    bill = _load_bill(c, req.params['id'])
    kept, who = _merge_bill_form(req, c, bill, _dues_text(json.loads(bill['detail'])))
    months = _int_or(req.form.get('months'), -1)
    if not 0 <= months <= MAX_DUES_MONTHS:
        raise UserError(f'Months of dues must be a whole number from 0 to {MAX_DUES_MONTHS}.')
    refresh_draft(c, bill['id'], _plan_for(c, bill['period']), statements.office_settings(c), dues_months=months)
    audit.log(c, req.user, 'bill.dues_months', 'bill', bill['id'], None, {'months': months})
    return Redirect(f"/cycle/bills/{bill['id']}", ('success', 'Dues updated.' + editing.kept_note(kept, BILL_LABELS, who)))


@route('POST', '/cycle/bills/{id}/notes', roles=EDIT_ROLES)
def set_statement_notes(req, c):
    bill = _load_bill(c, req.params['id'])
    saved_notes = statements.statement_notes(json.loads(bill['detail']), statements.office_settings(c))
    kept, who = _merge_bill_form(req, c, bill, _notes_text(saved_notes))
    chosen = {
        'show_penalty': req.checkbox('show_penalty'),
        'show_disconnection': req.checkbox('show_disconnection'),
        'note': req.text('note', 'Note', limit=MAX_NOTE_LENGTH, required=False, multiline=True),
    }
    detail = json.loads(bill['detail'])
    before = {k: detail.get(k) for k in STATEMENT_NOTE_KEYS}
    detail.update(chosen)
    # Same values twice is the same result, so a double submit is harmless; only_if keeps issued bills frozen.
    if not c.update('bills', bill['id'], {'detail': json.dumps(detail, sort_keys=True)}, only_if={'status': 'draft'},
                    expected_version=bill['version']):
        raise UserError('This bill is no longer a draft, or it was changed at the same moment. Reload the page.')
    audit.log(c, req.user, 'bill.notes', 'bill', bill['id'], before, chosen)
    return Redirect(f"/cycle/bills/{bill['id']}", ('success', 'Statement notes saved.' + editing.kept_note(kept, BILL_LABELS, who)))


@route('POST', '/cycle/bills/{id}/discard', roles=EDIT_ROLES)
def discard_draft(req, c):
    bill = _load_bill(c, req.params['id'])
    if not c.transition('bills', bill['id'], 'status', 'draft', 'cancelled', {'cancel_reason': 'Draft discarded'}):
        raise UserError('Only a draft can be discarded. An issued bill must be cancelled by an administrator.')
    audit.log(c, req.user, 'bill.discard', 'bill', bill['id'], {'status': 'draft'}, {'status': 'cancelled'})
    sync_drafts_alert(c, bill['period'])
    return Redirect('/cycle/bills?' + urlencode({'period': bill['period']}), ('success', f"Draft for {bill['name']} discarded."))


# --- statements -------------------------------------------------------------------------------------------

@route('GET', '/cycle/bills/print', roles=PRINT_ROLES)
def print_statements(req):
    raw_ids = req.query.get('ids', '').strip()
    with db.connect() as c:
        if raw_ids:
            ids = [part for part in raw_ids.split(',') if part]
            if len(ids) > MAX_PRINT_IDS or not all(_is_uuid(i) for i in ids):
                raise NotFound('That list of bills is not valid.')
        part_note = ''
        if not raw_ids:
            # A whole month can exceed one printable page (Phase 1D alone is ~1,800 statements): print it in
            # numbered parts of MAX_PRINT_IDS, each telling the office which part it is and linking the next.
            period = req.query.get('period') or billing.current_period(c)
            if not billing.PERIOD_PATTERN.fullmatch(period):
                raise NotFound('That billing month is not valid.')
            phase_id = req.query.get('phase', 'all')
            ids, total, part, parts = print_part(c, period, phase_id, _int_or(req.query.get('part'), 1))
            if parts > 1:
                first = (part - 1) * MAX_PRINT_IDS + 1
                part_note = ui.raw(f'<strong class="print-part">Part {part} of {parts}: statements {first:,} to {first + len(ids) - 1:,} '
                                   f'of {total:,}.</strong>')
                if part < parts:
                    query = urlencode({'period': period, 'phase': phase_id, 'part': part + 1})
                    part_note += ui.raw(f' <a class="btn btn-secondary" href="/cycle/bills/print?{query}">After printing, open part {part + 1}</a>')
        models = statements.load_statements(c, ids)
        settings = statements.office_settings(c)
    return _statement_document('Statements of account', models, settings, [CYCLE_CRUMB, BILLS_CRUMB, 'Print statements'], part_note)


@route('GET', '/cycle/bills/{id}/statement', roles=PRINT_ROLES)
def print_one_statement(req):
    with db.connect() as c:
        models = statements.load_statements(c, [req.params['id']])
        settings = statements.office_settings(c)
    if not models:
        raise NotFound('That bill was not found or was cancelled.')
    bill = models[0]['bill']
    crumbs = [CYCLE_CRUMB, BILLS_CRUMB, (bill['name'], f"/cycle/bills/{req.params['id']}"), 'Statement']
    return _statement_document(f"Statement – {bill['name']}", models, settings, crumbs)


@route('GET', '/cycle/bills/{id}/word', roles=PRINT_ROLES)
def word_statement(req):
    with db.connect() as c:
        models = statements.load_statements(c, [req.params['id']])
        settings = statements.office_settings(c)
    if not models:
        raise NotFound('That bill was not found or was cancelled.')
    data = statements.render_docx(models[0], settings)
    if data is None:
        raise HttpError(404, 'The Word template is not installed on this computer. Use “Print statement” instead.')
    bill = models[0]['bill']
    return Response.download(data, statements.DOCX_TYPE, f"statement-{bill['account_no']}-{bill['period']}.docx")


def print_part(c, period, phase_id, part, size=None):
    """Issued bills of one month (and phase) for one printable part: (ids, total, part, parts). Every bill is on
    exactly one part, in account-number order."""
    size = size or MAX_PRINT_IDS
    clause, params = _phase_clause(phase_id, load_phases(c))
    base_sql = f"FROM bills b JOIN accounts a ON a.id = b.account_id WHERE b.period = ? AND b.status = 'issued'{clause}"
    total = c.scalar(f'SELECT COUNT(*) {base_sql}', (period,) + params)
    parts = max(1, math.ceil(total / size))
    part = min(max(part, 1), parts)
    ids = [row['id'] for row in c.all(f'SELECT b.id {base_sql} ORDER BY a.account_no, b.id LIMIT ? OFFSET ?',
                                      (period,) + params + (size, (part - 1) * size))]
    return ids, total, part, parts


def _statement_document(title, models, settings, crumbs, part_note=''):
    toolbar = ui.raw(f'<div class="doc-toolbar no-print">{ui.crumbs(crumbs)}<button class="btn btn-primary" type="button" data-print>Print</button>'
                     f'{part_note}<span class="muted">{len(models)} statement{"s" if len(models) != 1 else ""}: one A4 sheet each, printed landscape, '
                     'with the member’s copy and the HOA copy side by side. Cut down the middle.</span></div>')
    pages = ui.join(statements.render_statement(model, settings) for model in models) if models else ui.empty(
        'There are no issued statements to print for this choice.', ui.button_link('/cycle/bills', 'Back to bills'))
    return Response(200, ui.document(title, toolbar + pages, stylesheet='print-landscape.css').encode('utf-8'))


# --- penalties ------------------------------------------------------------------------------------------------

def penalty_candidates(c, period, settings):
    """Accounts owing charges from `period` or earlier, not yet penalised for `period`.
    Base = open non-penalty charges (no compounding); amount = base × rate, rounded once."""
    rate = Decimal(str(settings['penalty_rate_percent']))
    done = {row['account_id'] for row in c.all("SELECT account_id FROM ledger_entries WHERE kind = 'penalty' AND period = ?", (period,))}
    # Only charges past their statement's due date are late. A bill issued after the penalty day (e.g. issued
    # Nov 1, due Nov 6) was being penalised at once. Charges without a bill (Excel opening balances) are already late.
    today = db.today()
    due_by_entry = {row['id']: row['due_on'] for row in c.all(
        'SELECT l.id, b.due_on FROM ledger_entries l JOIN bills b ON b.id = l.bill_id WHERE l.amount_cents > 0')}
    grouped = statements.entries_by_account(
        c, "SELECT DISTINCT account_id FROM ledger_entries WHERE amount_cents > 0 AND kind <> 'payment'")
    owing = {}
    for account_id, entries in grouped.items():
        if account_id in done:
            continue
        base = sum(ch.open_cents for ch in ledger.fifo_open_charges(entries).open_charges
                   if ch.kind != 'penalty' and ch.period <= period and due_by_entry.get(ch.entry_id, '') < today)
        amount = round_half_up(Decimal(base) * rate / 100) if base > 0 else 0
        if amount > 0:
            owing[account_id] = (base, amount)
    if not owing:
        return []
    marks = ', '.join('?' for _ in owing)
    names = c.all(f'SELECT id, account_no, name FROM accounts WHERE id IN ({marks}) ORDER BY account_no', tuple(owing))
    return [{'account_id': a['id'], 'account_no': a['account_no'], 'name': a['name'],
             'base_cents': owing[a['id']][0], 'penalty_cents': owing[a['id']][1]} for a in names]


@route('GET', '/cycle/penalties', roles=READ_ROLES)
def penalties_page(req):
    with db.connect() as c:
        period = billing.current_period(c)
        settings = statements.office_settings(c)
        starts = statements.day_in(period, settings['penalty_day'])
        candidates = penalty_candidates(c, period, settings) if date.fromisoformat(db.today()) >= starts else []
    parts = [ui.heading('Late-payment penalties', f"{statements.month_label(period)} · {settings['penalty_rate_percent']}% "
                        f"on unpaid charges from {statements.long_date(starts.isoformat())}", crumbs=[CYCLE_CRUMB, 'Penalties'])]
    if not settings['policy_confirmed']:
        parts.append(ui.notice('The penalty rule has not been confirmed by the office yet. Confirm it with the office before adding penalties.', 'warn'))
    if date.fromisoformat(db.today()) < starts:
        parts.append(ui.empty(f'Penalties for this month can be added from {statements.long_date(starts.isoformat())}.'))
        return req.page('Penalties', ui.join(parts), active='cycle')
    rows = [[ui.copyable(r['account_no']), ui.copyable(r['name']), ui.money(r['base_cents']), ui.money(r['penalty_cents'])]
            for r in candidates]
    total = sum(r['penalty_cents'] for r in candidates)
    parts.append(ui.table(['Account', 'Name', 'Unpaid charges', 'Penalty'], rows,
                          empty='No overdue balances to penalise for this month.', numeric_cols=(2, 3)))
    if candidates:
        parts.append(ui.form('/cycle/penalties', req.user['csrf'], ui.raw(f'<input type="hidden" name="period" value="{ui.esc(period)}">'),
                             f'Add {len(candidates)} penalties ({ui.money(total)})',
                             confirm='Add these penalties to the homeowners’ accounts? Each account gets at most one penalty per month.'))
    return req.page('Penalties', ui.join(parts), active='cycle')


@route('POST', '/cycle/penalties', roles=EDIT_ROLES)
def post_penalties(req, c):
    period = billing.current_period(c)
    if req.form.get('period') != period:
        raise UserError('The billing month changed while this page was open. Reload it and check the list again.')
    settings = statements.office_settings(c)
    starts = statements.day_in(period, settings['penalty_day'])
    if date.fromisoformat(db.today()) < starts:
        raise UserError(f'Penalties for this month can only be added from {statements.long_date(starts.isoformat())}.')
    candidates = penalty_candidates(c, period, settings)
    today = db.today()
    for row in candidates:
        ledger.post(c, row['account_id'], 'penalty', row['penalty_cents'], period=period, user=req.user, posted_on=today,
                    memo=f"{settings['penalty_rate_percent']}% of {ui.money(row['base_cents'])} unpaid",
                    entry_id=str(uuid.uuid5(PENALTY_NAMESPACE, f"{row['account_id']}:{period}")))
    total = sum(r['penalty_cents'] for r in candidates)
    audit.log(c, req.user, 'penalty.post', 'period', period, None, {'count': len(candidates), 'total_cents': total})
    if candidates:
        alerts.raise_alert(c, 'overdue', period, f'{len(candidates):,} homeowners have overdue balances for {statements.month_label(period)}.',
                           '/reports/aging', 'warn')
    return Redirect('/cycle/penalties', ('success', f'Added {len(candidates)} penalties totalling {ui.money(total)}.'))


# --- helpers ---------------------------------------------------------------------------------------------------

def _load_account(c, account_id):
    account = c.one('SELECT * FROM accounts WHERE id = ?', (account_id,)) if account_id else None
    if account is None:
        raise NotFound('That homeowner was not found.')
    return account


def _load_bill(c, bill_id):
    bill = c.one('SELECT b.*, a.account_no, a.name FROM bills b JOIN accounts a ON a.id = b.account_id WHERE b.id = ?', (bill_id,))
    if bill is None:
        raise NotFound('That bill was not found.')
    return bill


def _is_uuid(text):
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def _int_or(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
