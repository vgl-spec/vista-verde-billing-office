"""Import batches: stage a parsed workbook, list and resolve its issues, reconcile, apply.

A batch is staged in local tables (never synced) and changes nothing real until `apply`, which runs
once per batch in a single transaction. Accounts are matched by account number, so a later month's
workbook updates readings and details instead of duplicating homeowners.
"""
import json
from collections import defaultdict
from datetime import date, timedelta

from .. import UserError, alerts, audit, billing, db, ledger
from ..money import format_reading, parse_reading
from .workbook import CATEGORY_CODES, normalize_meter, normalize_name

PHASE_ID = '6f1d0000-0000-4000-8000-0000000001d0'
NOTE_SOURCE = 'workbook'
PREVIOUS_ONLY_FLAG = 'Imported: previous reading only'
LINK_PREFERENCE = {'formula': 0, 'manual': 1, 'name_meter': 2, 'name': 3}
ISSUE_KINDS = {
    'missing_address': ('Homeowners with no address', 'Leave this homeowner out'),
    'unknown_type': ('Type code is not R (residential) or COMM (commercial)', 'Leave this homeowner out'),
    'duplicate_name': ('Same name appears more than once', 'Leave this row out'),
    'reading_problem': ('Meter readings that cannot be used', 'Import without readings'),
    'unlinked_block': ('Statements (SOA) not matched to a homeowner', 'Ignore this statement and its balance'),
    'name_changed': ('Name is different from the one already saved', 'Keep the saved name'),
}
ACCOUNT_LEVEL_KINDS = ('missing_address', 'unknown_type', 'duplicate_name')
STATUSES = ('active', 'disconnected', 'closed')
# Our reading of the MRPH type column when it holds a status instead of R / COMM. These are only the
# defaults shown on the batch page; the office confirms or changes them before they are used.
TYPE_CODE_GUESSES = {
    '': ('residential', 'active', 'Nothing written; probably an ordinary home'),
    'RC': ('residential', 'active', 'Reconnected'),
    'TD': ('residential', 'disconnected', 'Temporarily disconnected'),
    'NC': ('residential', 'closed', 'No connection'),
    'NO CONNECTION': ('residential', 'closed', 'No connection'),
    'UNINSTALLED': ('residential', 'closed', 'Meter taken out'),
    'NO TENANT': ('residential', 'active', 'House is empty for now'),
    'REFUNDED': ('residential', 'closed', 'Deposit refunded; moved out'),
}
UNKNOWN_CODE_GUESS = ('residential', 'active', 'Not sure what this code means')
WORKBOOK_CODE_NOTE = 'Workbook code: '


# --- staging ---------------------------------------------------------------------------------------

def find_by_hash(c, sha256):
    return c.scalar('SELECT id FROM import_batches WHERE sha256 = ?', (sha256,))


def stage(c, parsed, *, sha256, filename, user):
    """Store a parsed workbook as a batch in review; returns its id."""
    batch_id = db.new_id()
    c.execute('INSERT INTO import_batches (id, sha256, filename, period, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)',
              (batch_id, sha256, filename[:200], parsed.period, user['id'], db.now()))
    existing = {row['account_no']: row for row in c.all('SELECT account_no, name FROM accounts WHERE phase_id = ?', (PHASE_ID,))}
    row_ids, seen_names, issues = {}, {}, []
    for row in parsed.rows:
        row_id = db.new_id()
        row_ids[row.sheet_row] = row_id
        c.execute(
            'INSERT INTO import_rows (id, batch_id, sheet_row, serial, account_no, name, address, type_code, category, status, '
            'route, meter_no, present, present_on, previous, previous_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (row_id, batch_id, row.sheet_row, row.serial, row.account_no, row.name, row.address, row.type_code, row.category,
             'disconnected' if row.type_code == 'TD' else 'active', row.route, row.meter_no, row.present, row.present_on,
             row.previous, row.previous_on))
        issues += _row_issues(row, row_id, seen_names, existing)
    blocks = _link_blocks(parsed.rows, parsed.blocks)
    for block, sheet_row, method in blocks:
        block_id = db.new_id()
        c.execute(
            'INSERT INTO import_blocks (id, batch_id, sheet, sheet_row, name, meter_no, row_id, link_method, is_current, total_cents, '
            'opening, notes, status_hint, former_owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (block_id, batch_id, block.sheet, block.sheet_row, block.name or '(no name)', block.meter_no,
             row_ids.get(sheet_row), method, int(block.is_current), block.total_cents, json.dumps(block.opening),
             json.dumps(block.notes), block.status_hint, block.former_owner))
        if block.is_current and not sheet_row:
            amount = sum(line['cents'] for line in block.opening)
            issues.append(('unlinked_block', block_id,
                           f'{block.sheet} row {block.sheet_row}: statement for "{block.name or "(no name)"}" (meter {block.meter_no or "none"}) '
                           f'did not match any homeowner. Carried balance on it: ₱{amount / 100:,.2f}.'))
    for kind, target, message in issues:
        c.execute('INSERT INTO import_issues (id, batch_id, kind, target_id, message) VALUES (?, ?, ?, ?, ?)',
                  (db.new_id(), batch_id, kind, target, message))
    summary = {'rows': len(parsed.rows), 'blocks': len(parsed.blocks), 'skipped_rows': parsed.skipped_rows,
               'linked_blocks': sum(1 for _, sheet_row, _ in blocks if sheet_row)}
    c.execute('UPDATE import_batches SET summary = ? WHERE id = ?', (json.dumps(summary), batch_id))
    audit.log(c, user, 'import.upload', 'import_batch', batch_id, None, {'filename': filename, 'period': parsed.period, **summary})
    refresh_alert(c, batch_id)
    return batch_id


def _row_issues(row, row_id, seen_names, existing):
    issues = []
    where = f'Row {row.sheet_row} ({row.account_no}, {row.name})'
    if not row.address:
        issues.append(('missing_address', row_id, f'{where} has no address.'))
    if not row.category:
        code = f'type code "{row.type_code}"' if row.type_code else 'no type code'
        issues.append(('unknown_type', row_id, f'{where} has {code}.'))
    name = normalize_name(row.name)
    if name in seen_names:
        issues.append(('duplicate_name', row_id, f'{where} has the same name as row {seen_names[name]}.'))
    seen_names.setdefault(name, row.sheet_row)
    if not row.present or not row.previous:
        issues.append(('reading_problem', row_id, f'{where}: present or previous reading is missing or not a number.'))
    elif parse_reading(row.present) < parse_reading(row.previous):
        issues.append(('reading_problem', row_id, f'{where}: present reading {row.present} is lower than previous {row.previous}.'))
    saved = existing.get(row.account_no)
    if saved and normalize_name(saved['name']) != name:
        issues.append(('name_changed', row_id, f'{row.account_no} is saved as "{saved["name"]}" but the workbook says "{row.name}".'))
    return issues


def _link_blocks(rows, blocks):
    """[(block, linked MRPH sheet row or 0, method)]: the present-reading formula first (if the
    surname agrees, since old sheets point at rows that have since moved), then name + meter."""
    by_row = {row.sheet_row: row for row in rows}
    by_name_meter = {(normalize_name(row.name), normalize_meter(row.meter_no)): row for row in rows if row.meter_no}
    by_name = defaultdict(list)
    for row in rows:
        by_name[normalize_name(row.name)].append(row)
    linked = []
    for block in blocks:
        name, meter = normalize_name(block.name), normalize_meter(block.meter_no)
        target = by_row.get(block.link_row)
        if target and _surname(target.name) == _surname(block.name):
            linked.append((block, target.sheet_row, 'formula'))
        elif meter and (name, meter) in by_name_meter:
            linked.append((block, by_name_meter[(name, meter)].sheet_row, 'name_meter'))
        elif name and len(by_name.get(name, [])) == 1:
            linked.append((block, by_name[name][0].sheet_row, 'name'))
        else:
            linked.append((block, 0, ''))
    return linked


def _surname(name):
    return normalize_name(str(name).split(',')[0])


# --- reading a batch ------------------------------------------------------------------------------------

def load(c, batch_id):
    batch = c.one('SELECT * FROM import_batches WHERE id = ?', (batch_id,))
    if batch:
        batch['summary'] = json.loads(batch['summary'])
        batch['type_mapping'] = json.loads(batch['type_mapping'])
    return batch


def issue_counts(c, batch_id):
    """{kind: (open, total)} in display order."""
    counts = {kind: [0, 0] for kind in ISSUE_KINDS}
    for row in c.all('SELECT kind, state, COUNT(*) AS n FROM import_issues WHERE batch_id = ? GROUP BY kind, state', (batch_id,)):
        counts[row['kind']][1] += row['n']
        if row['state'] == 'open':
            counts[row['kind']][0] += row['n']
    return {kind: tuple(value) for kind, value in counts.items() if value[1]}


def type_codes(c, batch_id):
    """Every type code that needed a type and status, most common first: [{code, open, total,
    category, status, guess}]. category/status are the office's saved answer, else our guess."""
    saved = load(c, batch_id)['type_mapping']
    found = c.all(
        "SELECT r.type_code AS code, COUNT(*) AS total, SUM(CASE WHEN i.state = 'open' THEN 1 ELSE 0 END) AS open_count "
        "FROM import_issues i JOIN import_rows r ON r.id = i.target_id "
        "WHERE i.batch_id = ? AND i.kind = 'unknown_type' GROUP BY r.type_code ORDER BY COUNT(*) DESC, r.type_code", (batch_id,))
    codes = []
    for row in found:
        category, status, guess = TYPE_CODE_GUESSES.get(row['code'], UNKNOWN_CODE_GUESS)
        answer = saved.get(row['code'], {})
        codes.append({'code': row['code'], 'open': int(row['open_count']), 'total': int(row['total']), 'guess': guess,
                      'category': answer.get('category', category), 'status': answer.get('status', status)})
    return codes


def primary_blocks(c, batch_id):
    """{import row id: the statement whose balance is used}: current-month sheets only, best link first."""
    chosen = {}
    for block in c.all('SELECT * FROM import_blocks WHERE batch_id = ? AND row_id IS NOT NULL AND is_current = 1 AND skipped = 0 '
                       'ORDER BY sheet, sheet_row', (batch_id,)):
        current = chosen.get(block['row_id'])
        if current is None or LINK_PREFERENCE[block['link_method']] < LINK_PREFERENCE[current['link_method']]:
            chosen[block['row_id']] = block
    return chosen


def reconcile(c, batch_id):
    """Per homeowner: workbook TOTAL AMOUNT DUE vs opening + system-calculated water + dues."""
    batch = load(c, batch_id)
    plan_row = billing.current_plan(c, f"{batch['period']}-01")
    plan = billing.plan_config(plan_row) if plan_row else None
    dues = billing.get_setting(c, 'dues_cents')
    primary = primary_blocks(c, batch_id)
    results = []
    for row in c.all('SELECT * FROM import_rows WHERE batch_id = ? AND skip_account = 0 ORDER BY sheet_row', (batch_id,)):
        block = primary.get(row['id'])
        entry = {'row': row, 'block': block, 'workbook_cents': None, 'system_cents': None}
        if block is None:
            entry['state'] = 'unlinked'
        elif block['total_cents'] is None:
            entry['state'] = 'no_total'
        else:
            entry['workbook_cents'] = block['total_cents']
            water = _water_cents(row, plan)
            if water is None:
                entry['state'] = 'cannot_calculate'
            else:
                opening = sum(line['cents'] for line in json.loads(block['opening']))
                entry['system_cents'] = opening + water + dues
                entry['state'] = 'matched' if entry['system_cents'] == block['total_cents'] else 'differs'
        results.append(entry)
    return results


def _water_cents(row, plan):
    if plan is None or row['skip_reading'] or not row['category'] or not row['present'] or not row['previous']:
        return None
    try:
        return billing.calculate(row['previous'], row['present'], plan, row['category']).water_cents
    except UserError:
        return None


# --- resolving issues --------------------------------------------------------------------------------------

def resolve_issue(c, batch_id, issue_id, action, values, user):
    """Fix (with `values` from the inline form) or skip one issue. Returns False if already resolved."""
    batch = _batch_in_review(c, batch_id)
    issue = c.one('SELECT * FROM import_issues WHERE id = ? AND batch_id = ?', (issue_id, batch['id']))
    if issue is None:
        raise UserError('That issue was not found.')
    if action not in ('fix', 'skip'):
        raise UserError('Choose fix or skip.')
    if issue['state'] != 'open':
        return False
    updates = _skip_effect(issue) if action == 'skip' else _fix_effect(c, batch['id'], issue, values)
    claimed = c.execute("UPDATE import_issues SET state = ?, resolved_by = ?, resolved_at = ? WHERE id = ? AND state = 'open'",
                        ('skipped' if action == 'skip' else 'fixed', user['id'], db.now(), issue['id'])).rowcount
    if not claimed:
        return False
    _apply_staging_update(c, updates)
    if action == 'skip' and issue['kind'] in ACCOUNT_LEVEL_KINDS:
        _close_row_issues(c, batch['id'], issue['target_id'], user)
    refresh_alert(c, batch['id'])
    return True


def skip_kind(c, batch_id, kind, user):
    """Skip every open issue of one kind; returns how many were skipped."""
    batch = _batch_in_review(c, batch_id)
    if kind not in ISSUE_KINDS:
        raise UserError('Unknown kind of issue.')
    issues = c.all("SELECT * FROM import_issues WHERE batch_id = ? AND kind = ? AND state = 'open'", (batch['id'], kind))
    for issue in issues:
        claimed = c.execute("UPDATE import_issues SET state = 'skipped', resolved_by = ?, resolved_at = ? WHERE id = ? AND state = 'open'",
                            (user['id'], db.now(), issue['id'])).rowcount
        if claimed:
            _apply_staging_update(c, _skip_effect(issue))
            if kind in ACCOUNT_LEVEL_KINDS:
                _close_row_issues(c, batch['id'], issue['target_id'], user)
    refresh_alert(c, batch['id'])
    return len(issues)


def map_type_codes(c, batch_id, mapping, user):
    """Fix every open `unknown_type` issue whose row carries a code in `mapping` ({code: (category,
    status)}). Returns how many homeowners were fixed; 0 when a repeat finds nothing left open."""
    batch = _batch_in_review(c, batch_id)
    if not mapping:
        raise UserError('There are no type codes to save.')
    for category, status in mapping.values():
        if category not in billing.CATEGORIES or status not in STATUSES:
            raise UserError('Choose a type and a status for every code.')
    known = {row['type_code'] for row in c.all(
        "SELECT DISTINCT r.type_code FROM import_issues i JOIN import_rows r ON r.id = i.target_id "
        "WHERE i.batch_id = ? AND i.kind = 'unknown_type'", (batch['id'],))}
    if not set(mapping) <= known:
        raise UserError('This page is out of date. Reload it and try again.')
    open_issues = c.all(
        "SELECT i.id, i.target_id, r.type_code FROM import_issues i JOIN import_rows r ON r.id = i.target_id "
        "WHERE i.batch_id = ? AND i.kind = 'unknown_type' AND i.state = 'open'", (batch['id'],))
    fixed = defaultdict(int)
    for issue in open_issues:
        if issue['type_code'] not in mapping:
            continue
        # Conditional claim: a concurrent or repeated submit finds the issue no longer open and changes nothing.
        claimed = c.execute("UPDATE import_issues SET state = 'fixed', resolved_by = ?, resolved_at = ? WHERE id = ? AND state = 'open'",
                            (user['id'], db.now(), issue['id'])).rowcount
        if claimed:
            category, status = mapping[issue['type_code']]
            c.execute('UPDATE import_rows SET category = ?, status = ? WHERE id = ?', (category, status, issue['target_id']))
            fixed[issue['type_code']] += 1
    if fixed:
        used = {code: {'category': mapping[code][0], 'status': mapping[code][1], 'homeowners': count} for code, count in fixed.items()}
        before = {code: batch['type_mapping'][code] for code in used if code in batch['type_mapping']}
        saved = {**batch['type_mapping'], **{code: {'category': v['category'], 'status': v['status']} for code, v in used.items()}}
        c.execute('UPDATE import_batches SET type_mapping = ? WHERE id = ?', (json.dumps(saved, sort_keys=True), batch['id']))
        audit.log(c, user, 'import.map_type_codes', 'import_batch', batch['id'], before or None, used)
        refresh_alert(c, batch['id'])
    return sum(fixed.values())


def _skip_effect(issue):
    target = issue['target_id']
    return {
        'missing_address': ('import_rows', target, {'skip_account': 1}),
        'unknown_type': ('import_rows', target, {'skip_account': 1}),
        'duplicate_name': ('import_rows', target, {'skip_account': 1}),
        'reading_problem': ('import_rows', target, {'skip_reading': 1}),
        'unlinked_block': ('import_blocks', target, {'skipped': 1}),
        'name_changed': ('import_rows', target, {'keep_name': 1}),
    }[issue['kind']]


def _fix_effect(c, batch_id, issue, values):
    target, kind = issue['target_id'], issue['kind']
    if kind == 'missing_address':
        address = values.get('address', '').strip()
        if not address or len(address) > 200:
            raise UserError('Type the address (up to 200 characters).')
        return ('import_rows', target, {'address': address})
    if kind == 'unknown_type':
        category, status = values.get('category', ''), values.get('status', '')
        if category not in billing.CATEGORIES or status not in STATUSES:
            raise UserError('Choose the type and the status.')
        return ('import_rows', target, {'category': category, 'status': status})
    if kind == 'reading_problem':
        previous = format_reading(parse_reading(values.get('previous', '')))
        present = format_reading(parse_reading(values.get('present', '')))
        if parse_reading(present) < parse_reading(previous):
            raise UserError('The present reading cannot be lower than the previous reading.')
        return ('import_rows', target, {'previous': previous, 'present': present})
    if kind == 'unlinked_block':
        row_id = c.scalar('SELECT id FROM import_rows WHERE batch_id = ? AND account_no = ?', (batch_id, values.get('account_no', '').strip().upper()))
        if row_id is None:
            raise UserError('No homeowner in this workbook has that account number (for example 1D-0042).')
        return ('import_blocks', target, {'row_id': row_id, 'link_method': 'manual'})
    return ('import_rows', target, {})  # duplicate_name: keep as its own homeowner; name_changed: use the new name


def _apply_staging_update(c, update):
    table, target, changes = update
    if changes:
        assignments = ', '.join(f'{column} = ?' for column in changes)
        c.execute(f'UPDATE {table} SET {assignments} WHERE id = ?', list(changes.values()) + [target])


def _close_row_issues(c, batch_id, row_id, user):
    """A homeowner left out has nothing left to fix."""
    c.execute("UPDATE import_issues SET state = 'skipped', resolved_by = ?, resolved_at = ? "
              "WHERE batch_id = ? AND target_id = ? AND state = 'open'", (user['id'], db.now(), batch_id, row_id))


def _batch_in_review(c, batch_id):
    batch = load(c, batch_id)
    if batch is None:
        raise UserError('That import was not found.')
    if batch['status'] != 'review':
        raise UserError('This import was already applied; it can no longer be changed.')
    return batch


def refresh_alert(c, batch_id):
    open_count = c.scalar("SELECT COUNT(*) FROM import_issues WHERE batch_id = ? AND state = 'open'", (batch_id,))
    if open_count:
        alerts.raise_alert(c, 'import_issue', batch_id, f'Excel import: {open_count} things to check before it can be applied.',
                           f'/import/{batch_id}', 'warn')
    else:
        alerts.resolve_alert(c, 'import_issue', batch_id, None)


# --- apply ------------------------------------------------------------------------------------------------

def apply(c, batch_id, user):
    """Write the batch into real records, exactly once. Returns a summary dict, or None if this
    batch was already applied (a replay)."""
    batch = load(c, batch_id)
    if batch is None:
        raise UserError('That import was not found.')
    if batch['status'] == 'review' and c.scalar("SELECT COUNT(*) FROM import_issues WHERE batch_id = ? AND state = 'open'", (batch_id,)):
        raise UserError('Some things still need checking. Fix or skip each one first.')
    # The conditional transition is the once-only guard: a concurrent second apply matches nothing.
    if c.execute("UPDATE import_batches SET status = 'applied', applied_by = ?, applied_at = ? WHERE id = ? AND status = 'review'",
                 (user['id'], db.now(), batch_id)).rowcount != 1:
        return None
    period = batch['period']
    previous_period, opening_day = _previous_period(period)
    rows = c.all('SELECT * FROM import_rows WHERE batch_id = ? AND skip_account = 0 ORDER BY sheet_row', (batch_id,))
    primary = primary_blocks(c, batch_id)
    notes_by_row = defaultdict(list)
    for block in c.all('SELECT row_id, notes FROM import_blocks WHERE batch_id = ? AND row_id IS NOT NULL AND skipped = 0 '
                       'ORDER BY is_current DESC, sheet, sheet_row', (batch_id,)):
        notes_by_row[block['row_id']] += json.loads(block['notes'])
    accounts = {row['account_no']: row for row in c.all('SELECT * FROM accounts WHERE phase_id = ?', (PHASE_ID,))}
    with_ledger = {row['account_id'] for row in c.all('SELECT DISTINCT account_id FROM ledger_entries')}
    meters = defaultdict(dict)
    for meter in c.all('SELECT id, account_id, meter_no FROM meters'):
        meters[meter['account_id']][meter['meter_no']] = meter['id']
    readings = {(r['account_id'], r['period']) for r in c.all('SELECT account_id, period FROM readings WHERE period IN (?, ?)', (period, previous_period))}
    saved_notes = {(n['account_id'], n['note']) for n in c.all('SELECT account_id, note FROM history_notes WHERE source = ?', (NOTE_SOURCE,))}
    totals = defaultdict(int)
    for row in rows:
        block = primary.get(row['id'])
        account_id, created = _upsert_account(c, row, block, accounts.get(row['account_no']), user)
        totals['created' if created else 'updated'] += 1
        meter_id = _ensure_meter(c, account_id, row['meter_no'], meters)
        if block and account_id not in with_ledger:
            for line in json.loads(block['opening']):
                ledger.post(c, account_id, 'opening', line['cents'], period=previous_period, posted_on=opening_day, user=user,
                            memo=f"{line['memo']} (Excel {block['sheet']} row {line['row']})", applies_to=line.get('purpose', ''))
                totals['opening_lines'] += 1
        totals['readings'] += _add_readings(c, row, account_id, meter_id, period, previous_period, readings, user)
        for note in dict.fromkeys(notes_by_row[row['id']]):
            if (account_id, note) not in saved_notes:
                c.insert('history_notes', {'account_id': account_id, 'note': note[:1000], 'source': NOTE_SOURCE, 'created_at': db.now()})
                saved_notes.add((account_id, note))
                totals['notes'] += 1
        c.execute('UPDATE import_rows SET account_id = ? WHERE id = ?', (account_id, row['id']))
    summary = {**batch['summary'], **totals}
    c.execute('UPDATE import_batches SET summary = ? WHERE id = ?', (json.dumps(summary), batch_id))
    audit.log(c, user, 'import.apply', 'import_batch', batch_id, None, summary)
    alerts.resolve_alert(c, 'import_issue', batch_id, user)
    return summary


def _upsert_account(c, row, block, existing, user):
    # Without R / COMM the office chose the type and status itself, which beats a hint read from SOA notes.
    is_office_choice = row['type_code'] not in CATEGORY_CODES
    status = block['status_hint'] if block and block['status_hint'] and not is_office_choice else row['status']
    values = {'name': row['name'], 'address': row['address'], 'category': row['category'], 'route': row['route'], 'status': status}
    if is_office_choice:
        values['notes'] = _with_workbook_code(existing['notes'] if existing else '', row['type_code'])
    if block and block['former_owner']:
        values['former_owner'] = block['former_owner'][:120]
    now = db.now()
    if existing is None:
        account_id = c.insert('accounts', {'phase_id': PHASE_ID, 'account_no': row['account_no'], 'former_owner': '', **values,
                                           'created_at': now, 'updated_at': now, 'updated_by': user['id']})
        audit.log(c, user, 'account.import_create', 'account', account_id, None, {'account_no': row['account_no'], **values})
        return account_id, True
    if row['keep_name']:
        values.pop('name')
    changes = {key: value for key, value in values.items() if existing[key] != value}
    if changes:
        c.update('accounts', existing['id'], {**changes, 'updated_at': now, 'updated_by': user['id']})
        audit.log(c, user, 'account.import_update', 'account', existing['id'], {key: existing[key] for key in changes}, changes)
    return existing['id'], False


def _with_workbook_code(notes, code):
    """Keep the original MRPH code on the account as one line that a later import replaces."""
    lines = [line for line in notes.splitlines() if not line.startswith(WORKBOOK_CODE_NOTE)]
    return '\n'.join(lines + [WORKBOOK_CODE_NOTE + (code or '(blank)')])


def _ensure_meter(c, account_id, meter_no, meters):
    if not meter_no:
        return None
    if meter_no not in meters[account_id]:
        meters[account_id][meter_no] = c.insert('meters', {'account_id': account_id, 'meter_no': meter_no[:40]})
    return meters[account_id][meter_no]


def _add_readings(c, row, account_id, meter_id, period, previous_period, existing, user):
    """This month's reading pair, plus last month's present as its own row. Readings already in the
    system are kept (they are facts entered by staff)."""
    if row['skip_reading'] or not row['present'] or not row['previous']:
        return 0
    added = 0
    for reading_period, previous, present, read_on, flag in (
            (period, row['previous'], row['present'], row['present_on'], ''),
            (previous_period, row['previous'], row['previous'], row['previous_on'], PREVIOUS_ONLY_FLAG)):
        if (account_id, reading_period) in existing:
            continue
        c.insert('readings', {'account_id': account_id, 'meter_id': meter_id, 'period': reading_period, 'previous': previous,
                              'present': present, 'read_on': read_on or None, 'flag': flag, 'entered_by': user['id'], 'entered_at': db.now()})
        existing.add((account_id, reading_period))
        added += 1
    return added


def _previous_period(period):
    first = date.fromisoformat(f'{period}-01')
    last_of_previous = first - timedelta(days=1)
    return last_of_previous.strftime('%Y-%m'), last_of_previous.isoformat()


# --- parallel run -------------------------------------------------------------------------------------------

def parallel_report(c, batch_id):
    """After the system issues this month's bills: per homeowner, the system's amount due vs the
    workbook's, and the checklist for retiring Excel."""
    batch = load(c, batch_id)
    primary = primary_blocks(c, batch_id)
    rows = c.all(
        'SELECT r.id, r.account_no, r.name, r.account_id, b.water_cents, b.dues_cents, b.other_cents, '
        "(SELECT COALESCE(SUM(l.amount_cents), 0) FROM ledger_entries l WHERE l.account_id = r.account_id AND l.kind = 'opening') AS opening_cents "
        "FROM import_rows r LEFT JOIN bills b ON b.account_id = r.account_id AND b.period = ? AND b.status = 'issued' "
        'WHERE r.batch_id = ? AND r.account_id IS NOT NULL ORDER BY r.sheet_row', (batch['period'], batch_id))
    lines, counts = [], defaultdict(int)
    for row in rows:
        block = primary.get(row['id'])
        workbook = block['total_cents'] if block else None
        if row['water_cents'] is None:
            state, system = 'not_billed', None
        else:
            system = row['opening_cents'] + row['water_cents'] + row['dues_cents'] + row['other_cents']
            state = 'no_workbook_total' if workbook is None else 'matched' if system == workbook else 'differs'
        counts[state] += 1
        lines.append({**row, 'workbook_cents': workbook, 'system_cents': system, 'state': state})
    open_issues = c.scalar("SELECT COUNT(*) FROM import_issues WHERE batch_id = ? AND state = 'open'", (batch_id,))
    backup_after = c.scalar("SELECT 1 FROM audit_log WHERE action = 'backup.download' AND at > ?", (batch['applied_at'] or '',))
    checklist = [
        ('Every imported homeowner has an issued bill for this month', counts['not_billed'] == 0 and bool(rows)),
        ('Every bill matches the Excel total (or the difference is explained)', counts['differs'] == 0 and bool(rows)),
        ('Nothing left to check in the import', open_issues == 0),
        ('The office confirmed the billing rules (Admin → Settings)', bool(billing.get_setting(c, 'policy_confirmed'))),
        ('A backup was downloaded after the import (Admin → Backup)', bool(backup_after)),
    ]
    return {'batch': batch, 'lines': lines, 'counts': dict(counts), 'checklist': checklist,
            'ready': all(done for _, done in checklist)}
