"""Homeowner accounts: search, add, edit and the account page with its ledger."""
import math
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from .. import UserError, audit, db, editing, ledger, ui
from ..http import NotFound, Redirect, ShowPage, route

READ_ROLES = ('admin', 'staff', 'cashier')
EDIT_ROLES = ('admin', 'staff')
PAGE_SIZE = 20
LEDGER_ROWS_SHOWN = 100
READING_ROWS_SHOWN = 24
CATEGORY_OPTIONS = (('residential', 'Residential'), ('commercial', 'Commercial'))
STATUS_OPTIONS = (('active', 'Active'), ('disconnected', 'Disconnected'), ('closed', 'Closed'))
# The edit form's fields, for locking, versions and the merge (editing.py).
EDIT_FIELDS = {'account_no': 'Account number', 'name': 'Name', 'address': 'Address', 'category': 'Type',
               'water_source': 'Water source', 'former_owner': 'Former owner', 'notes': 'Notes', 'status': 'Status'}
STATUS_PILLS = {'active': 'success', 'disconnected': 'warn', 'closed': 'neutral'}
KIND_LABELS = {
    'water': 'Water', 'dues': 'Association dues', 'penalty': 'Penalty', 'system_loss': 'System loss',
    'other': 'Other charge', 'opening': 'Opening balance', 'payment': 'Payment', 'reversal': 'Reversal',
    'adjustment': 'Adjustment',
}
# Sortable list columns: column index -> public key, and key -> SQL (whitelist; nothing else reaches ORDER BY).
LIST_SORT_COLUMNS = {0: 'account', 1: 'name', 2: 'address', 3: 'phase', 4: 'type', 5: 'status', 6: 'balance'}
LIST_ORDER = {
    'account': 'a.account_no', 'name': 'LOWER(a.name)', 'address': 'LOWER(a.address)', 'phase': 'p.code',
    'type': 'a.category', 'status': 'a.status',
    # The full expression, not the column alias: Postgres does not allow an alias inside an ORDER BY expression.
    'balance': '(SELECT COALESCE(SUM(l.amount_cents), 0) FROM ledger_entries l WHERE l.account_id = a.id)',
}
LIKE_ESCAPE = '!'


# --- list ------------------------------------------------------------------------------------------------------

@route('GET', '/accounts', roles=READ_ROLES)
def account_list(req):
    search = req.query.get('q', '').strip()[:80]
    status = req.list_filter('status', ('all',) + tuple(value for value, _ in STATUS_OPTIONS))
    category = req.list_filter('type', ('all',) + tuple(value for value, _ in CATEGORY_OPTIONS))
    where, params = search_clause(search)
    with db.connect() as c:
        phases = load_phases(c)
        phase_id = req.list_filter('phase', ('all',) + tuple(p['id'] for p in phases))
        for column, value in (('a.phase_id', phase_id), ('a.status', status), ('a.category', category)):
            if value != 'all':
                where, params = (where + ' AND' if where else ' WHERE') + f' {column} = ?', params + (value,)
        query = {k: v for k, v in (('q', search), ('phase', phase_id), ('status', status), ('type', category)) if v and v != 'all'}
        sort = ui.Sort.from_query(req.query, '/accounts' + ('?' + urlencode(query) if query else ''), LIST_SORT_COLUMNS)
        total = c.scalar(f'SELECT COUNT(*) FROM accounts a{where}', params)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        order_sql, order_params = sort.order(LIST_ORDER, 'LOWER(a.name), a.id', 'LOWER(a.name), a.id')
        rows = c.all(
            'SELECT a.id, a.account_no, a.name, a.address, a.category, a.status, p.code AS phase, '
            '(SELECT COALESCE(SUM(l.amount_cents), 0) FROM ledger_entries l WHERE l.account_id = a.id) AS balance_cents '
            f'FROM accounts a JOIN phases p ON p.id = a.phase_id{where} ORDER BY {order_sql} LIMIT ? OFFSET ?',
            params + order_params + (PAGE_SIZE, (page - 1) * PAGE_SIZE))
    table = ui.table(
        ['Account no.', 'Name', 'Address', 'Phase', 'Type', 'Status', 'Balance'],
        [[ui.copyable(r['account_no']), ui.copyable(r['name'], _link(f"/accounts/{r['id']}", r['name'])), r['address'], r['phase'],
          r['category'].title(), ui.pill(r['status'].title(), STATUS_PILLS[r['status']]), ui.money(r['balance_cents'])]
         for r in rows],
        empty=('No homeowners match that search.' if query == {'q': search} else 'No homeowners match these filters.')
        if query else 'No homeowners have been added yet.',
        numeric_cols=(6,), sort=sort, keys=[r['id'] for r in rows])
    actions = ui.button_link('/accounts/new', 'Add homeowner') if req.user['role'] in EDIT_ROLES else ''
    filters = ui.join([
        phase_field(phases, phase_id),
        ui.select('Status', 'status', [('all', 'Any status')] + list(STATUS_OPTIONS), value=status),
        ui.select('Type', 'type', [('all', 'Any type')] + list(CATEGORY_OPTIONS), value=category),
    ])
    body = ui.join([
        ui.heading('Homeowners', actions=actions),
        ui.search_form('/accounts', search, label='Find a homeowner', placeholder='Name, account, address', hidden=sort.params,
                       extra=filters, filtered=bool(query)),
        table,
        ui.pagination(page, pages, '/accounts?' + urlencode({**query, **sort.params}), total=total, noun='homeowner'),
    ])
    return req.page('Homeowners', body, active='accounts', live=('accounts', 'ledger_entries', 'meters'))


def phase_field(phases, value, waiting=None):
    """Phase filter built from the phases in the database, however many there are (shown even with one).

    It is a filterable drop-down (type part of a phase name, Enter picks it). `waiting` (phase id -> count)
    adds how many homeowners in each phase still need something done.
    """
    def label(name, count):
        return f'{name} ({count:,} to go)' if waiting is not None and count else name
    every = label('All phases', sum(waiting.values()) if waiting else 0)
    options = [('all', every)] + [(p['id'], label(p['name'], (waiting or {}).get(p['id'], 0))) for p in phases]
    return ui.select('Phase', 'phase', options, value=value, attrs='data-search')


def search_clause(search):
    if not search:
        return '', ()
    escaped = search.lower().replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace('%', f'{LIKE_ESCAPE}%').replace('_', f'{LIKE_ESCAPE}_')
    pattern = f'%{escaped}%'
    # LOWER(...) LIKE works the same on SQLite and Postgres (Postgres LIKE is case-sensitive).
    clause = f" WHERE (LOWER(a.name) LIKE ? ESCAPE '{LIKE_ESCAPE}' OR LOWER(a.account_no) LIKE ? ESCAPE '{LIKE_ESCAPE}' OR LOWER(a.address) LIKE ? ESCAPE '{LIKE_ESCAPE}')"
    return clause, (pattern, pattern, pattern)


def load_phases(c):
    """Phases in the office's order: Phase 1D (the pilot) first, then the rest by code."""
    return c.all("SELECT id, code, name FROM phases ORDER BY CASE WHEN code = '1D' THEN 0 ELSE 1 END, code")


# --- add ---------------------------------------------------------------------------------------------------------

@route('GET', '/accounts/new', roles=EDIT_ROLES)
def new_account_page(req):
    with db.connect() as c:
        phases = load_phases(c)
    fields = ui.join([
        ui.select('Phase', 'phase_id', [(p['id'], p['name']) for p in phases]),
        ui.field('Account number', 'account_no', hint='The number used in the Excel sheet.'),
        _shared_fields({}),
        ui.field('Meter number', 'meter_no', required=False),
        ui.field('Starting meter reading', 'start_reading', required=False, hint='Needed only when a meter number is entered.', attrs='inputmode="decimal"'),
    ])
    body = ui.heading('Add homeowner', crumbs=[('Homeowners', '/accounts'), 'Add homeowner']) + ui.form('/accounts/new', req.user['csrf'], fields, 'Save homeowner',
                                                  secondary=ui.button_link('/accounts', 'Cancel', kind='secondary'))
    return req.page('Add homeowner', body, active='accounts', live=False)


@route('POST', '/accounts/new', roles=EDIT_ROLES)
def create_account(req, c):
    phase_ids = [row['id'] for row in c.all('SELECT id FROM phases')]
    phase_id = req.choice('phase_id', phase_ids, 'phase')
    values = _read_account_fields(req)
    meter_no = req.text('meter_no', 'Meter number', limit=40, required=False)
    start_reading = req.reading('start_reading', 'Starting meter reading') if req.form.get('start_reading', '').strip() else None
    if start_reading is not None and not meter_no:
        raise UserError('Enter the meter number too, or clear the starting reading.')
    if c.scalar('SELECT 1 FROM accounts WHERE phase_id = ? AND account_no = ?', (phase_id, values['account_no'])):
        raise UserError(f"Account number {values['account_no']} is already used in this phase.")
    now = db.now()
    account_id = c.insert('accounts', {
        'phase_id': phase_id, **values, 'status': 'active',
        'created_at': now, 'updated_at': now, 'updated_by': req.user['id'],
    })
    if meter_no:
        c.insert('meters', {'account_id': account_id, 'meter_no': meter_no, 'start_reading': start_reading})
    audit.log(c, req.user, 'account.create', 'account', account_id, None, {**values, 'phase_id': phase_id})
    return Redirect(f'/accounts/{account_id}', ('success', f"{values['name']} was added."))


# --- edit --------------------------------------------------------------------------------------------------------

@route('GET', '/accounts/{id}/edit', roles=EDIT_ROLES)
def edit_account_page(req):
    with db.connect() as c:
        account = _load_account(c, req.params['id'])
    holder = editing.acquire('accounts', account['id'], req.user)
    fields = ui.join([ui.facts([('Phase', account['phase_name'])]), ui.field('Account number', 'account_no', value=account['account_no']),
                      _shared_fields(account), ui.select('Status', 'status', STATUS_OPTIONS, value=account['status'])])
    crumbs = [('Homeowners', '/accounts'), (account['name'], f"/accounts/{account['id']}"), 'Edit details']
    heading = ui.heading(f"Edit {account['name']}", crumbs=crumbs)
    if holder:  # someone else is editing: show the details, read-only, until they are done
        banner = editing.lock_banner(holder, 'accounts', account['id'], can_take_over=req.user['role'] == 'admin',
                                     csrf=req.user['csrf'], back=f"/accounts/{account['id']}/edit")
        body = heading + banner + ui.raw('<fieldset class="locked-fields" disabled>') + fields + ui.raw('</fieldset>')
        return req.page('Edit homeowner', body, active='accounts', live=False)
    form_fields = ui.join([fields, editing.version_fields(account['version'], _edit_text(account)),
                           editing.lock_marker('accounts', account['id'])])
    body = heading + ui.form(f"/accounts/{account['id']}/edit", req.user['csrf'], form_fields, 'Save changes',
                             secondary=ui.button_link(f"/accounts/{account['id']}", 'Cancel', kind='secondary'))
    return req.page('Edit homeowner', body, active='accounts', live=False)


def _edit_text(account):
    """The record as the edit form shows it (raw form text), for the merge."""
    return {name: account[name] or '' for name in EDIT_FIELDS}


@route('POST', '/accounts/{id}/edit', roles=EDIT_ROLES)
def update_account(req, c):
    account = _load_account(c, req.params['id'])
    other = editing.holder_other_than(c, 'accounts', account['id'], req.user)
    if other:
        raise UserError(f"{other['user_name']} is editing this homeowner now, so your changes were not saved. "
                        'Open it again when they are done.')
    version = editing.posted_version(req)
    if version is None:
        raise UserError('This page is out of date. Open the homeowner again, then make your change.')
    kept, who = [], ''
    if version != account['version']:  # saved by someone else since this form was drawn: merge field by field
        conflicts, kept = editing.merge_form(req, _edit_text(account), EDIT_FIELDS)
        who, when = editing.last_change(c, 'account', account['id'])
        if conflicts:
            raise ShowPage('Edit homeowner', editing.resolver_page(
                req, title=f"Edit {account['name']}: choose what to keep",
                crumbs=[('Homeowners', '/accounts'), (account['name'], f"/accounts/{account['id']}"), 'Choose what to keep'],
                action=f"/accounts/{account['id']}/edit", labels=EDIT_FIELDS, conflicts=conflicts,
                saved_text=_edit_text(account), version=account['version'], who=who, when=when,
                cancel=f"/accounts/{account['id']}"), active='accounts')
    new_values = {**_read_account_fields(req), 'status': req.choice('status', [s for s, _ in STATUS_OPTIONS], 'status')}
    changes = {name: value for name, value in new_values.items() if account[name] != value}
    target = f"/accounts/{account['id']}"
    editing.release(c, 'accounts', account['id'], req.user)
    if not changes:
        return Redirect(target, ('info', 'Nothing was changed.' + editing.kept_note(kept, EDIT_FIELDS, who)))
    if 'account_no' in changes and c.scalar(
            'SELECT 1 FROM accounts WHERE phase_id = ? AND account_no = ? AND id <> ?',
            (account['phase_id'], changes['account_no'], account['id'])):
        raise UserError(f"Account number {changes['account_no']} is already used in this phase.")
    if not c.update('accounts', account['id'], {**changes, 'updated_at': db.now(), 'updated_by': req.user['id']},
                    expected_version=account['version']):
        raise UserError('Someone saved this homeowner at the same moment. Open it again to see their change.')
    audit.log(c, req.user, 'account.update', 'account', account['id'],
              {name: account[name] for name in changes}, changes)
    return Redirect(target, ('success', 'Changes saved.' + editing.kept_note(kept, EDIT_FIELDS, who)))


def _shared_fields(account):
    """Fields shared by the add and edit forms; `account` is {} when adding."""
    return ui.join([
        ui.field('Name', 'name', value=account.get('name', '')),
        ui.field('Address', 'address', value=account.get('address', '')),
        ui.select('Type', 'category', CATEGORY_OPTIONS, value=account.get('category', 'residential')),
        ui.field('Water source', 'water_source', value=account.get('water_source', ''), required=False),
        ui.field('Former owner', 'former_owner', value=account.get('former_owner', ''), required=False),
        ui.field('Notes', 'notes', type='textarea', value=account.get('notes', ''), required=False),
    ])


def _read_account_fields(req):
    return {
        'account_no': req.text('account_no', 'Account number', limit=20),
        'name': req.text('name', 'Name', limit=120),
        'address': req.text('address', 'Address', limit=200),
        'category': req.choice('category', [value for value, _ in CATEGORY_OPTIONS], 'type'),
        'water_source': req.text('water_source', 'Water source', limit=60, required=False),
        'former_owner': req.text('former_owner', 'Former owner', limit=120, required=False),
        'notes': req.text('notes', 'Notes', limit=1000, required=False, multiline=True),
    }


# --- detail --------------------------------------------------------------------------------------------------------

@route('GET', '/accounts/{id}', roles=READ_ROLES)
def account_detail(req):
    account_id = req.params['id']
    with db.connect() as c:
        account = _load_account(c, account_id)
        entries = c.all(
            'SELECT posted_on, kind, amount_cents, period, memo FROM ledger_entries WHERE account_id = ? '
            'ORDER BY posted_on, created_at, id', (account_id,))
        meters = c.all('SELECT meter_no, installed_on, removed_on, start_reading FROM meters WHERE account_id = ? ORDER BY meter_no', (account_id,))
        readings = c.all(
            'SELECT period, previous, present, flag FROM readings WHERE account_id = ? ORDER BY period DESC LIMIT ?',
            (account_id, READING_ROWS_SHOWN))
        notes = c.all('SELECT note, source, created_at FROM history_notes WHERE account_id = ? ORDER BY created_at DESC', (account_id,))
    rows, balance = running_balance(entries)
    can_edit = req.user['role'] in EDIT_ROLES
    actions = ui.join([
        ui.button_link(f'/payments/cashier?account={account_id}', 'Record payment'),
        ui.button_link(f'/cycle/bills?account={account_id}', 'Make bill', kind='secondary'),
        ui.button_link(f'/accounts/{account_id}/edit', 'Edit details', kind='secondary') if can_edit else '',
    ])
    body = ui.join([
        ui.heading(account['name'], f"Account {account['account_no']} · {account['address']}", actions=actions,
                   crumbs=[('Homeowners', '/accounts'), account['name']]),
        office_notes(account),
        ui.facts([
            ('Account no.', ui.copyable(account['account_no'])), ('Phase', account['phase_name']),
            ('Type', account['category'].title()), ('Status', account['status'].title()),
            ('Water source', account['water_source'] or '—'), ('Former owner', account['former_owner'] or '—'),
        ]),
        ui.figures([_balance_figure(balance)]),
        ui.section('Account ledger', _ledger_table(rows), lead=_ledger_lead(len(rows))),
        ui.section('Water meters', ui.table(
            ['Meter no.', 'Installed', 'Removed', 'Starting reading'],
            [[ui.copyable(m['meter_no']), m['installed_on'] or '—', m['removed_on'] or '—', m['start_reading'] or '—'] for m in meters],
            empty='No meter on file.')),
        ui.section('Meter readings', ui.table(
            ['Month', 'Previous', 'Present', 'Used (m³)', 'Flag'],
            [[_month_label(r['period']), r['previous'], r['present'], _used(r['previous'], r['present']), r['flag'] or '—'] for r in readings],
            empty='No readings entered yet.', numeric_cols=(1, 2, 3))),
        ui.section('Notes from the old records', ui.table(
            ['Date', 'Note', 'Source'], [[n['created_at'][:10], n['note'], n['source'] or '—'] for n in notes],
            empty='No notes.')),
        _adjust_section(req, account) if req.user['role'] == 'admin' else '',
    ])
    return req.page(account['name'], body, active='accounts')


def office_notes(account):
    """The office's own notes on a homeowner, shown at the top wherever the account is opened (site visit item 22)."""
    if not (account.get('notes') or '').strip():
        return ''
    return ui.notice(f"Office notes: {account['notes'].strip()}", 'warn')


ADJUST_DIRECTIONS = (('credit', 'Lower the balance (credit)'), ('charge', 'Raise the balance (charge)'))
MAX_ADJUST_REASON = 200


def _adjust_section(req, account):
    fields = ui.join([
        ui.select('What to do', 'direction', ADJUST_DIRECTIONS),
        ui.field('Amount', 'amount', attrs='inputmode="decimal" autocomplete="off"'),
        ui.field('Reason', 'reason', attrs=f'maxlength="{MAX_ADJUST_REASON}"',
                 hint='Required, kept in the audit trail. For example: board approved waiver of Sep penalty.'),
    ])
    form = ui.form(f"/accounts/{account['id']}/adjust", req.user['csrf'], fields, 'Save correction',
                   confirm='Change this homeowner’s balance? It is recorded with your name and the reason.')
    return ui.section('Correct the balance (administrator)', form,
                      lead='For agreed corrections only. Nothing is erased: the correction is a new line in the ledger.')


@route('POST', '/accounts/{id}/adjust', roles=('admin',))
def adjust_balance(req, c):
    """A controlled balance correction (site visit 4.8): admin only, reason required, a new ledger line, audited."""
    account = _load_account(c, req.params['id'])
    direction = req.choice('direction', [value for value, _ in ADJUST_DIRECTIONS], 'correction')
    amount = req.pesos('amount', 'Amount')
    if amount <= 0:
        raise UserError('The amount must be more than zero.')
    reason = req.text('reason', 'Reason', limit=MAX_ADJUST_REASON)
    signed = amount if direction == 'charge' else -amount
    entry_id = ledger.post(c, account['id'], 'adjustment', signed, period=None, memo=reason, user=req.user)
    audit.log(c, req.user, 'account.adjust', 'account', account['id'], None,
              {'amount_cents': signed, 'reason': reason, 'ledger_entry': entry_id})
    word = 'raised' if signed > 0 else 'lowered'
    return Redirect(f"/accounts/{account['id']}", ('success', f'Balance {word} by {ui.money(amount)}. The reason is in the audit trail.'))


def running_balance(entries):
    """Ledger rows (oldest first) -> (display rows newest first, final balance), one pass in Python."""
    running, rows = 0, []
    for entry in entries:
        running += entry['amount_cents']
        rows.append({**entry, 'balance_cents': running})
    return rows[::-1], running


def _ledger_table(rows):
    shown = rows[:LEDGER_ROWS_SHOWN]
    return ui.table(
        ['Date', 'Description', 'Charge', 'Credit', 'Balance'],
        [[r['posted_on'], _describe(r), ui.money(r['amount_cents']) if r['amount_cents'] > 0 else '',
          ui.money(-r['amount_cents']) if r['amount_cents'] < 0 else '', ui.money(r['balance_cents'])] for r in shown],
        empty='Nothing has been billed or paid yet.', numeric_cols=(2, 3, 4))


def _ledger_lead(row_count):
    if row_count > LEDGER_ROWS_SHOWN:
        return f'Newest first. Showing the latest {LEDGER_ROWS_SHOWN} of {row_count} lines.'
    return 'Newest first.'


def _balance_figure(balance):
    if balance > 0:
        return ('Balance', str(ui.money(balance)), 'Amount owed')
    if balance < 0:
        return ('Balance', str(ui.money(-balance)), 'Paid ahead (credit)')
    return ('Balance', str(ui.money(0)), 'Nothing owed')


def _describe(entry):
    parts = [KIND_LABELS[entry['kind']]]
    if entry['period']:
        parts.append(f"for {_month_label(entry['period'])}")
    text = ' '.join(parts)
    return f"{text} — {entry['memo']}" if entry['memo'] else text


def _used(previous, present):
    return f'{Decimal(present) - Decimal(previous):f}'


def _month_label(period):
    return datetime.strptime(period, '%Y-%m').strftime('%b %Y')


def _load_account(c, account_id):
    account = c.one(
        'SELECT a.*, p.name AS phase_name FROM accounts a JOIN phases p ON p.id = a.phase_id WHERE a.id = ?', (account_id,))
    if account is None:
        raise NotFound('That homeowner was not found.')
    return account


def _link(href, text):
    return ui.raw(f'<a href="{ui.esc(href)}">{ui.esc(text)}</a>')


def _int_or(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
