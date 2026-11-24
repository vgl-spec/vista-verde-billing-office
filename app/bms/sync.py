"""Offline-first sync: one online hub, LAN spokes. Contract: docs/ARCHITECTURE.md "Sync".

Each node writes its own changes to the `events` outbox (db.insert/update). A spoke worker pushes
unpushed events to the hub (`/sync/push`) and pulls the hub log (`/sync/pull?after=`). Applying a
remote event is idempotent by event id and conflict-free by construction:

* per-field last-writer-wins on the Hybrid Logical Clock stored in each row's `field_hlc`;
* state machines (bills, payments) merge to the furthest state;
* a row whose natural key (receipt no., account + period ...) already exists locally is either the
  same fact recorded twice (an *alias*: later events for it land on the local row, money is never
  doubled) or a real clash, held in `sync_conflicts` for an admin to decide;
* a row whose parent has not arrived yet waits in `sync_conflicts` and is retried after every batch.

Remote events are applied with raw SQL on purpose: going through db.insert/update would stamp a new
local clock and echo the change back as a new event.
"""
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import UserError, alerts, audit, auth, db, ledger

MAX_BATCH = 500
MAX_PUSH_BODY = 16 * 1024 * 1024
MAX_TEXT_VALUE = 100_000
INTERVAL_SECONDS = 15
MAX_BACKOFF_SECONDS = 300
LEASE_SECONDS = 120
MAX_PUSH_ATTEMPTS = 5
ALERT_AFTER_FAILURES = 4
MAX_RETRY_PASSES = 10
HTTP_TIMEOUT_SECONDS = 30
TOKEN_SECRET_BYTES = 32
NODE_NAME_LIMIT = 60
CONFLICT_ALERT_KEY = 'open'
ERROR_ALERT_KEY = 'worker'
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')

# Columns that point at another synced row: rewritten through aliases and checked before insert.
REFERENCES = {
    'accounts': {'phase_id': 'phases'},
    'meters': {'account_id': 'accounts'},
    'readings': {'account_id': 'accounts', 'meter_id': 'meters'},
    'bills': {'account_id': 'accounts', 'reading_id': 'readings', 'rate_plan_id': 'rate_plans'},
    'ledger_entries': {'account_id': 'accounts', 'bill_id': 'bills', 'payment_id': 'payments',
                       'reverses_id': 'ledger_entries'},
    'payments': {'account_id': 'accounts'},
    'history_notes': {'account_id': 'accounts'},
}
CONFLICT_LABELS = {
    'duplicate_receipt': 'The same receipt number was recorded at two offices with different details.',
    'duplicate_bill': 'Two different bills exist for the same homeowner and month.',
    'duplicate_statement_no': 'Two different bills carry the same statement number.',
    'duplicate_reading': 'Two different meter readings exist for the same homeowner and month.',
    'duplicate_account_no': 'The same account number belongs to two different names.',
    'duplicate_ledger_entry': 'The same ledger line arrived with a different amount.',
}
RESOLUTIONS = ('keep_mine', 'keep_theirs', 'keep_both')


class TransportError(Exception):
    """The hub could not be reached or refused the request. `status` is None when unreachable."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# --- local state ---------------------------------------------------------------------------------

def get_state(c, key, default=''):
    value = c.scalar('SELECT value FROM sync_state WHERE key = ?', (key,))
    return default if value is None else value


def set_state(c, key, value):
    c.execute('INSERT INTO sync_state (key, value) VALUES (?, ?) '
              'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, str(value)))


def is_enabled(c):
    return get_state(c, 'enabled') == '1'


def hub_settings(c, config):
    """(hub url, token) for a spoke: environment first, else what an admin saved."""
    if config is not None and config.sync_url:
        return config.sync_url, config.sync_token
    return get_state(c, 'hub_url'), get_state(c, 'hub_token')


# --- consent gate --------------------------------------------------------------------------------

def consent_problems(c):
    """Plain-language reasons sync may not be switched on yet (empty list = allowed)."""
    problems = []
    for admin in c.all("SELECT username, password_hash, must_change_password FROM users WHERE role = 'admin' AND active = 1"):
        if admin['must_change_password'] or not _hash_meets_policy(admin['password_hash']):
            problems.append(f"Administrator {admin['username']} must choose a new password "
                            f"(at least {auth.PASSWORD_MIN_LENGTH} characters) before sync can be switched on.")
    return problems


def _hash_meets_policy(stored):
    # The length rule is enforced when a password is set; a hash made with fewer PBKDF2 rounds
    # (an older scheme) is the only thing we can detect afterwards.
    try:
        scheme, iterations, _, _ = stored.split('$')
        return scheme == 'pbkdf2_sha256' and int(iterations) >= auth.PBKDF2_ITERATIONS
    except ValueError:
        return False


def enable(c, user, consent_given):
    if not consent_given:
        raise UserError('Tick the box confirming the HOA agreed to keep resident records online.')
    problems = consent_problems(c)
    if problems:
        raise UserError(problems[0])
    if is_enabled(c):
        return False
    set_state(c, 'enabled', '1')
    set_state(c, 'consent_by', user['id'])
    set_state(c, 'consent_at', db.now())
    audit.log(c, user, 'sync.enable', 'sync', 'consent', None, {'consent': True, 'by': user['username']})
    return True


def disable(c, user):
    if not is_enabled(c):
        return False
    set_state(c, 'enabled', '0')
    audit.log(c, user, 'sync.disable', 'sync', 'consent', {'enabled': True}, {'enabled': False})
    return True


# --- node registration (hub) -----------------------------------------------------------------------

def create_node(c, name, user):
    """Register an office computer; returns (node_id, token). Only the token's hash is stored."""
    cleaned = name.strip()
    if not cleaned or len(cleaned) > NODE_NAME_LIMIT:
        raise UserError(f'Give the office computer a name of up to {NODE_NAME_LIMIT} characters.')
    node_id = db.new_id()
    secret = secrets.token_urlsafe(TOKEN_SECRET_BYTES)
    c.execute('INSERT INTO sync_nodes (node_id, name, token_hash, created_at, created_by) VALUES (?, ?, ?, ?, ?)',
              (node_id, cleaned, _hash_secret(secret), db.now(), user['id'] if user else None))
    audit.log(c, user, 'sync.node_create', 'sync_node', node_id, None, {'name': cleaned})
    return node_id, f'{node_id}.{secret}'


def revoke_node(c, node_id, user):
    affected = c.execute('UPDATE sync_nodes SET revoked_at = ? WHERE node_id = ? AND revoked_at IS NULL',
                         (db.now(), node_id)).rowcount
    if affected:
        audit.log(c, user, 'sync.node_revoke', 'sync_node', node_id)
    return affected == 1


def authenticate_node(c, authorization):
    """Node row for a valid `Bearer <node_id>.<secret>` header, else None. Constant-time compare."""
    scheme, _, token = (authorization or '').partition(' ')
    node_id, _, secret = token.strip().partition('.')
    if scheme != 'Bearer' or not node_id or not secret or len(token) > 300:
        return None
    node = c.one('SELECT * FROM sync_nodes WHERE node_id = ? AND revoked_at IS NULL', (node_id,))
    expected = node['token_hash'] if node else _hash_secret('no-such-node')
    matched = hmac.compare_digest(_hash_secret(secret).encode(), expected.encode())
    return node if node and matched else None


def _hash_secret(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


# --- hub endpoints -----------------------------------------------------------------------------------

def receive_push(c, node, events):
    """Store and apply a spoke's batch in the caller's transaction. Returns the count of new events."""
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        raise ValueError(f'A batch is a list of at most {MAX_BATCH} events.')
    for event in events:
        _validate_event(event)
    origin = _bind_origin(c, node, {event['origin'] for event in events})
    if any(event['origin'] != origin for event in events):
        raise ValueError('A node may only push its own changes.')
    c.hlc_now()  # takes the clock lock first, so hub_seq order equals commit order (Postgres)
    accepted = 0
    for event in events:
        inserted = c.execute(
            'INSERT INTO events (id, tbl, row_id, op, payload, hlc, origin, created_at, pushed_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING',
            (event['id'], event['tbl'], event['row_id'], event['op'], json.dumps(event['payload'], sort_keys=True),
             event['hlc'], event['origin'], event['created_at'], db.now())).rowcount
        if inserted:
            accepted += 1
            _apply(c, event)
    retry_parked(c)
    c.execute('UPDATE sync_nodes SET last_seen = ?, last_pushed_seq = last_pushed_seq + ? WHERE node_id = ?',
              (db.now(), accepted, node['node_id']))
    return accepted


def _bind_origin(c, node, origins):
    """The pushing computer's own node id: bound on its first push, fixed afterwards."""
    if node['origin_id']:
        return node['origin_id']
    if len(origins) != 1:
        raise ValueError('A first push must come from a single office computer.')
    origin = origins.pop()
    if origin == db.ensure_node(c):
        raise ValueError('An office computer cannot claim the identity of the online server.')
    c.execute('UPDATE sync_nodes SET origin_id = ? WHERE node_id = ? AND origin_id IS NULL', (origin, node['node_id']))
    return origin


def serve_pull(c, node, after, limit=MAX_BATCH):
    """Hub log after `after`, minus the caller's own events; `next_after` always advances."""
    rows = c.all('SELECT seq, id, tbl, row_id, op, payload, hlc, origin, created_at FROM events '
                 'WHERE seq > ? ORDER BY seq LIMIT ?', (after, limit))
    c.execute('UPDATE sync_nodes SET last_seen = ? WHERE node_id = ?', (db.now(), node['node_id']))
    events = [_wire_event(row) for row in rows if row['origin'] != node['origin_id']]
    return {'events': events, 'next_after': rows[-1]['seq'] if rows else after, 'more': len(rows) == limit}


def _wire_event(row):
    return {'id': row['id'], 'tbl': row['tbl'], 'row_id': row['row_id'], 'op': row['op'],
            'payload': json.loads(row['payload']), 'hlc': row['hlc'], 'origin': row['origin'],
            'created_at': row['created_at']}


# --- applying remote events ------------------------------------------------------------------------

def apply_remote(c, event):
    """Spoke side: apply one hub-log event once. Returns 'applied', 'aliased', 'parked' or 'seen'."""
    _validate_event(event)
    if c.scalar('SELECT 1 FROM sync_applied WHERE event_id = ?', (event['id'],)):
        return 'seen'
    c.execute('INSERT INTO sync_applied (event_id, applied_at) VALUES (?, ?)', (event['id'], db.now()))
    return _apply(c, event)


def _apply(c, event):
    c.hlc_receive(event['hlc'])
    table, row_id, hlc = event['tbl'], event['row_id'], event['hlc']
    values = dict(event['payload'])
    _check_columns(c, table, values)
    if event['op'] == 'insert':
        outcome = _apply_insert(c, table, row_id, values, {column: hlc for column in values}, event['origin'])
    else:
        outcome = _apply_update(c, table, row_id, values, hlc)
    if outcome[0] in ('waiting', 'conflict'):
        _park(c, event, outcome)
        return 'parked'
    return outcome[0]


def _apply_insert(c, table, row_id, values, stamps, origin):
    primary_key = db.PRIMARY_KEY.get(table, 'id')
    values.pop(primary_key, None)
    stamps.pop(primary_key, None)
    aliased = _rewrite_references(c, table, values)
    local_id = _local_id(c, table, row_id)
    existing = _row(c, table, local_id)
    if existing:
        _merge(c, table, existing, values, stamps)
        return ('applied',)
    missing = _missing_parent(c, table, values)
    if missing:
        return ('waiting', f'waiting for {missing}')
    twin, reason = _find_twin(c, table, row_id, values, aliased)
    if twin is not None:
        if _is_same_fact(table, twin, values):
            c.execute('INSERT INTO sync_aliases (tbl, remote_id, local_id) VALUES (?, ?, ?) ON CONFLICT DO NOTHING',
                      (table, row_id, twin[primary_key]))
            _merge(c, table, twin, values, stamps, skip=_natural_key_columns(table) | set(REFERENCES.get(table, {})))
            return ('aliased',)
        return ('conflict', reason, twin[primary_key])
    _insert_row(c, table, row_id, values, stamps, origin)
    return ('applied',)


def _apply_update(c, table, row_id, values, hlc):
    _rewrite_references(c, table, values)
    existing = _row(c, table, _local_id(c, table, row_id))
    if existing is None:
        return ('waiting', f'waiting for {table} {row_id}')
    _merge(c, table, existing, values, {column: hlc for column in values})
    return ('applied',)


def _merge(c, table, existing, values, stamps, skip=frozenset()):
    """Per-field LWW by HLC; registered state machines take the furthest state instead."""
    primary_key = db.PRIMARY_KEY.get(table, 'id')
    stored = json.loads(existing['field_hlc'] or '{}')
    changes = {}
    for column, value in values.items():
        if column in skip or column == primary_key:
            continue
        stamp = stamps[column]
        order = db.STATE_ORDER.get((table, column))
        if order:
            if value not in order:
                raise ValueError(f'Unknown {table}.{column} state {value!r}.')
            current = existing[column]
            if current not in order or order.index(value) > order.index(current):
                changes[column] = value
            if stamp > stored.get(column, ''):
                stored[column] = stamp
        elif stamp > stored.get(column, ''):
            stored[column] = stamp
            if existing[column] != value:
                changes[column] = value
    new_stamps = json.dumps(stored, sort_keys=True)
    if not changes and new_stamps == existing['field_hlc']:
        return
    assignments = {**changes, 'field_hlc': new_stamps}
    set_sql = ', '.join(f'{column} = ?' for column in assignments)
    if changes and table in db.VERSIONED_TABLES:
        set_sql += ', version = version + 1'  # an edit form opened before this arrived must notice it
    c.execute(f'UPDATE {table} SET {set_sql} WHERE {primary_key} = ?', list(assignments.values()) + [existing[primary_key]])


def _insert_row(c, table, row_id, values, stamps, origin):
    primary_key = db.PRIMARY_KEY.get(table, 'id')
    data = {primary_key: row_id, **values, 'origin': origin,
            'field_hlc': json.dumps({column: stamps[column] for column in values}, sort_keys=True)}
    columns = ', '.join(data)
    marks = ', '.join('?' for _ in data)
    c.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', list(data.values()))


def _local_id(c, table, remote_id):
    return c.scalar('SELECT local_id FROM sync_aliases WHERE tbl = ? AND remote_id = ?', (table, remote_id)) or remote_id


def _row(c, table, row_id):
    primary_key = db.PRIMARY_KEY.get(table, 'id')
    return c.one(f'SELECT * FROM {table} WHERE {primary_key} = ?', (row_id,))


def _rewrite_references(c, table, values):
    """Point references at local rows; returns the columns whose parent is an alias."""
    aliased = set()
    for column, parent in REFERENCES.get(table, {}).items():
        if values.get(column):
            local = _local_id(c, parent, values[column])
            if local != values[column]:
                values[column] = local
                aliased.add(column)
    return aliased


def _missing_parent(c, table, values):
    for column, parent in REFERENCES.get(table, {}).items():
        if values.get(column) and _row(c, parent, values[column]) is None:
            return f'{parent} {values[column]}'
    return None


def _natural_key_columns(table):
    return {
        'phases': {'code'}, 'accounts': {'phase_id', 'account_no'}, 'meters': {'account_id', 'meter_no'},
        'readings': {'account_id', 'period'}, 'bills': {'account_id', 'period', 'statement_no'},
        'payments': {'receipt_type', 'receipt_no'}, 'history_notes': {'account_id', 'note', 'source'},
    }.get(table, set())


def _find_twin(c, table, row_id, v, aliased):
    """A local row with the same natural key (another id), and the clash reason if it differs.

    Keys the database enforces (receipt, account + period ...) are always checked. The softer
    keys (meter, note, ledger line) only when the parent is itself an alias, i.e. the same
    homeowner / bill / payment was recorded at two offices: two identical lines on one bill are
    legitimate and must both arrive."""
    if table == 'phases':
        return c.one('SELECT * FROM phases WHERE code = ? AND id <> ?', (v.get('code'), row_id)), ''
    if table == 'accounts':
        return c.one('SELECT * FROM accounts WHERE phase_id = ? AND account_no = ? AND id <> ?',
                     (v.get('phase_id'), v.get('account_no'), row_id)), 'duplicate_account_no'
    if table == 'meters' and 'account_id' in aliased:
        return c.one('SELECT * FROM meters WHERE account_id = ? AND meter_no = ? AND id <> ?',
                     (v.get('account_id'), v.get('meter_no'), row_id)), ''
    if table == 'readings':
        return c.one('SELECT * FROM readings WHERE account_id = ? AND period = ? AND id <> ?',
                     (v.get('account_id'), v.get('period'), row_id)), 'duplicate_reading'
    if table == 'bills':
        twin = c.one('SELECT * FROM bills WHERE statement_no = ? AND id <> ?', (v.get('statement_no'), row_id))
        if twin:
            return twin, 'duplicate_statement_no'
        if v.get('status') == 'cancelled':
            return None, ''
        return c.one("SELECT * FROM bills WHERE account_id = ? AND period = ? AND status <> 'cancelled' AND id <> ?",
                     (v.get('account_id'), v.get('period'), row_id)), 'duplicate_bill'
    if table == 'payments':
        # An office still on the version before receipt types sends no type: everything it recorded was an OR.
        return c.one('SELECT * FROM payments WHERE receipt_type = ? AND receipt_no = ? AND id <> ?',
                     (v.get('receipt_type') or 'OR', v.get('receipt_no'), row_id)), 'duplicate_receipt'
    if table == 'history_notes' and 'account_id' in aliased:
        return c.one('SELECT * FROM history_notes WHERE account_id = ? AND note = ? AND source = ? AND id <> ?',
                     (v.get('account_id'), v.get('note'), v.get('source'), row_id)), ''
    if table == 'ledger_entries':
        return _ledger_twin(c, row_id, v, aliased), 'duplicate_ledger_entry'
    return None, ''


def _ledger_twin(c, row_id, v, aliased):
    if v.get('reverses_id'):  # UNIQUE in the schema: an entry is reversed at most once
        return c.one('SELECT * FROM ledger_entries WHERE reverses_id = ? AND id <> ?', (v['reverses_id'], row_id))
    if v.get('kind') == 'payment' and 'payment_id' in aliased:
        # One payment can have a line per purpose (bill, dues, misc): the twin is the line for the same purpose.
        return c.one("SELECT * FROM ledger_entries WHERE payment_id = ? AND kind = 'payment' AND applies_to = ? AND id <> ?",
                     (v['payment_id'], v.get('applies_to', ''), row_id))
    if v.get('kind') == 'dues' and v.get('payment_id') and 'payment_id' in aliased:  # dues months charged at the cashier
        return c.one("SELECT * FROM ledger_entries WHERE payment_id = ? AND kind = 'dues' AND period = ? AND id <> ?",
                     (v['payment_id'], v.get('period'), row_id))
    if v.get('applies_to') == 'misc' and 'payment_id' in aliased:
        return c.one("SELECT * FROM ledger_entries WHERE payment_id = ? AND kind = 'other' AND applies_to = 'misc' AND id <> ?",
                     (v['payment_id'], row_id))
    if 'bill_id' in aliased:
        return c.one('SELECT * FROM ledger_entries WHERE bill_id = ? AND kind = ? AND memo = ? AND amount_cents = ? '
                     'AND reverses_id IS NULL AND id <> ?',
                     (v['bill_id'], v.get('kind'), v.get('memo', ''), v.get('amount_cents'), row_id))
    if v.get('kind') == 'opening' and 'account_id' in aliased:
        return c.one("SELECT * FROM ledger_entries WHERE account_id = ? AND kind = 'opening' AND memo = ? "
                     'AND amount_cents = ? AND id <> ?', (v.get('account_id'), v.get('memo', ''), v.get('amount_cents'), row_id))
    return None


def _is_same_fact(table, twin, v):
    if table == 'accounts':
        return _normalize(twin['name']) == _normalize(v.get('name', ''))
    if table == 'readings':
        return _same_number(twin['previous'], v.get('previous')) and _same_number(twin['present'], v.get('present'))
    if table == 'bills':
        return twin['statement_no'] != v.get('statement_no') and all(
            twin[column] == v.get(column) for column in ('water_cents', 'dues_cents', 'other_cents', 'total_cents'))
    if table == 'payments':
        return all(twin[column] == v.get(column) for column in ('account_id', 'amount_cents', 'paid_on'))
    if table == 'ledger_entries':
        return twin['amount_cents'] == v.get('amount_cents')
    return True


def _normalize(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().upper()


def _same_number(left, right):
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except InvalidOperation:
        return False


def _park(c, event, outcome):
    kind, reason = outcome[0], outcome[1]
    local_id = outcome[2] if len(outcome) > 2 else None
    c.execute(
        'INSERT INTO sync_conflicts (id, event_id, tbl, row_id, op, hlc, origin, payload, kind, reason, local_id, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING',
        (db.new_id(), event['id'], event['tbl'], event['row_id'], event['op'], event['hlc'], event['origin'],
         json.dumps(event['payload'], sort_keys=True), kind, reason, local_id, db.now()))


def retry_parked(c):
    """Re-try held events (parents may have arrived, clashes may be resolved); refresh the alert."""
    for _ in range(MAX_RETRY_PASSES):
        if not _retry_pass(c):
            break
    _refresh_conflict_alert(c)


def _retry_pass(c):
    progressed = False
    parked = c.all('SELECT * FROM sync_conflicts WHERE resolved_at IS NULL ORDER BY hlc, created_at')
    done = set()
    for item in parked:
        if item['id'] in done:
            continue
        if item['op'] == 'insert':
            values, stamps, folded = _fold(item, parked)
            outcome = _apply_insert(c, item['tbl'], item['row_id'], values, stamps, item['origin'])
        else:
            folded = []
            outcome = _apply_update(c, item['tbl'], item['row_id'], json.loads(item['payload']), item['hlc'])
        if outcome[0] in ('applied', 'aliased'):
            for resolved in [item] + folded:
                _close(c, resolved['id'], 'auto', None)
                done.add(resolved['id'])
            progressed = True
        elif outcome[0] == 'conflict' and (item['kind'] != 'conflict' or item['reason'] != outcome[1]):
            c.execute("UPDATE sync_conflicts SET kind = 'conflict', reason = ?, local_id = ? WHERE id = ?",
                      (outcome[1], outcome[2], item['id']))
    return progressed


def _fold(item, parked):
    """An insert plus any held updates of the same row, newest value per field with its clock."""
    values = json.loads(item['payload'])
    stamps = {column: item['hlc'] for column in values}
    folded = [p for p in parked if p['op'] == 'update' and p['tbl'] == item['tbl'] and p['row_id'] == item['row_id']]
    for update in folded:
        for column, value in json.loads(update['payload']).items():
            if update['hlc'] > stamps.get(column, ''):
                values[column], stamps[column] = value, update['hlc']
    return values, stamps, folded


def _close(c, conflict_id, resolution, user):
    c.execute('UPDATE sync_conflicts SET resolved_at = ?, resolved_by = ?, resolution = ? WHERE id = ? AND resolved_at IS NULL',
              (db.now(), user['id'] if user else None, resolution, conflict_id))


def _refresh_conflict_alert(c):
    count = c.scalar("SELECT COUNT(*) FROM sync_conflicts WHERE resolved_at IS NULL AND kind = 'conflict'")
    if count:
        noun = 'record needs' if count == 1 else 'records need'
        alerts.raise_alert(c, 'sync_conflict', CONFLICT_ALERT_KEY,
                           f'{count} {noun} a decision: two offices recorded the same thing differently.',
                           '/admin/sync/conflicts', 'urgent')
    else:
        alerts.resolve_alert(c, 'sync_conflict', CONFLICT_ALERT_KEY, None)


# --- admin conflict resolution ----------------------------------------------------------------------

def open_conflicts(c, limit=200):
    return c.all("SELECT * FROM sync_conflicts WHERE resolved_at IS NULL AND kind = 'conflict' ORDER BY created_at LIMIT ?", (limit,))


def resolve(c, conflict_id, choice, user, new_receipt=''):
    """Admin decision on one clash. Every resulting change is a normal tracked write, so it reaches
    the other offices, where the matching held row then applies (or clashes) on its own."""
    item = c.one("SELECT * FROM sync_conflicts WHERE id = ? AND kind = 'conflict' AND resolved_at IS NULL", (conflict_id,))
    if item is None:
        raise UserError('That conflict was already decided.')
    if choice not in RESOLUTIONS:
        raise UserError('Choose keep mine, keep theirs or keep both.')
    table = item['tbl']
    theirs = json.loads(item['payload'])
    mine = _row(c, table, item['local_id']) if item['local_id'] else None
    if item['op'] != 'insert' or mine is None:
        raise UserError('This conflict can no longer be decided here. Ask for support.')
    # Close first: the retries below must not pick this row up and record it as 'auto'.
    _close(c, item['id'], choice, user)
    if table == 'payments':
        _resolve_payment(c, item, theirs, mine, choice, user, new_receipt)
    elif table == 'bills':
        _resolve_bill(c, item, theirs, mine, choice, user)
    else:
        _resolve_by_alias(c, item, theirs, mine, choice, user)
    audit.log(c, user, 'sync.conflict_resolve', table, item['row_id'], None, {'choice': choice, 'local_id': mine['id']})
    retry_parked(c)


def _resolve_payment(c, item, theirs, mine, choice, user, new_receipt):
    receipt, receipt_type = theirs['receipt_no'], theirs.get('receipt_type') or 'OR'
    reason = f'Duplicate receipt {receipt_type} {receipt} found during sync'
    if choice == 'keep_both':
        cleaned = new_receipt.strip()
        if not cleaned or len(cleaned) > 40:
            raise UserError("Enter the new receipt number for the other office's payment.")
        if c.scalar('SELECT 1 FROM payments WHERE receipt_type = ? AND receipt_no = ?', (receipt_type, cleaned)):
            raise UserError(f'Receipt {receipt_type} {cleaned} is already used.')
        _insert_theirs(c, item, theirs, {'receipt_no': cleaned})
    elif choice == 'keep_mine':
        _insert_theirs(c, item, theirs, {'receipt_no': _retired_receipt(receipt, item['row_id'])})
        _void_payment(c, item['row_id'], reason, user)
    else:
        c.update('payments', mine['id'], {'receipt_no': _retired_receipt(receipt, mine['id'])})
        _void_payment(c, mine['id'], reason, user)
        _insert_theirs(c, item, theirs, {})


def _resolve_bill(c, item, theirs, mine, choice, user):
    if choice == 'keep_both':
        raise UserError('A homeowner can have only one bill per month. Choose keep mine or keep theirs.')
    reason = 'Duplicate bill found during sync'
    if choice == 'keep_mine':
        _insert_theirs(c, item, theirs, {'status': 'cancelled', 'cancel_reason': reason})
        _reverse_live_entries(c, 'bill_id', item['row_id'], reason, user)
    else:
        if mine['status'] != 'cancelled':
            c.transition('bills', mine['id'], 'status', mine['status'], 'cancelled', {'cancel_reason': reason})
        _reverse_live_entries(c, 'bill_id', mine['id'], reason, user)
        _insert_theirs(c, item, theirs, {})


def _resolve_by_alias(c, item, theirs, mine, choice, user):
    if choice == 'keep_both':
        raise UserError('These records cannot both be kept. Choose keep mine or keep theirs.')
    table = item['tbl']
    primary_key = db.PRIMARY_KEY.get(table, 'id')
    c.execute('INSERT INTO sync_aliases (tbl, remote_id, local_id) VALUES (?, ?, ?) ON CONFLICT DO NOTHING',
              (table, item['row_id'], mine[primary_key]))
    chosen = theirs if choice == 'keep_theirs' else mine
    skip = {primary_key} | set(REFERENCES.get(table, {}))
    # Re-stamp the chosen values with a fresh clock so the decision wins on every office.
    changes = {column: chosen[column] for column in theirs if column not in skip and column in mine}
    if changes:
        c.update(table, mine[primary_key], changes)


def _insert_theirs(c, item, theirs, overrides):
    """Insert the other office's row, with `overrides` sent back out as a tracked change."""
    primary_key = db.PRIMARY_KEY.get(item['tbl'], 'id')
    values = {column: value for column, value in theirs.items() if column != primary_key}
    _rewrite_references(c, item['tbl'], values)
    stamps = {column: item['hlc'] for column in values}
    values.update(overrides)
    _insert_row(c, item['tbl'], item['row_id'], values, stamps, item['origin'])
    if overrides:
        c.update(item['tbl'], item['row_id'], overrides)
    retry_parked(c)  # its own ledger lines were waiting for it


def _void_payment(c, payment_id, reason, user):
    payment = _row(c, 'payments', payment_id)
    if payment['status'] == 'posted':
        c.transition('payments', payment_id, 'status', 'posted', 'voided',
                     {'void_reason': reason, 'voided_by': user['id'], 'voided_at': db.now()})
    _reverse_live_entries(c, 'payment_id', payment_id, reason, user)


def _reverse_live_entries(c, column, row_id, reason, user):
    entries = c.all(f"SELECT id FROM ledger_entries WHERE {column} = ? AND kind <> 'reversal' "
                    'AND id NOT IN (SELECT reverses_id FROM ledger_entries WHERE reverses_id IS NOT NULL)', (row_id,))
    for entry in entries:
        ledger.reverse(c, entry['id'], reason, user)


def _retired_receipt(receipt, row_id):
    return f'{receipt}-DUP-{row_id[:8]}'


# --- validation --------------------------------------------------------------------------------------

_column_cache = {}


def _columns(c, table):
    key = (c.dialect, table)
    if key not in _column_cache:
        if c.dialect == 'sqlite':
            names = {row['name'] for row in c.all(f'PRAGMA table_info({table})')}
        else:
            names = {row['column_name'] for row in c.all(
                'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?', (table,))}
        _column_cache[key] = frozenset(names)
    return _column_cache[key]


def _check_columns(c, table, values):
    unknown = set(values) - (_columns(c, table) - db.RESERVED_COLUMNS)
    if unknown:
        raise ValueError(f'Event for {table} has unknown columns: {sorted(unknown)}')


def _validate_event(event):
    """Fail closed on anything that is not a well-formed change of a synced table."""
    if not isinstance(event, dict):
        raise ValueError('An event must be an object.')
    for field in ('id', 'tbl', 'row_id', 'op', 'hlc', 'origin', 'created_at'):
        if not isinstance(event.get(field), str) or not event[field] or len(event[field]) > 200:
            raise ValueError(f'Event field {field} is missing or invalid.')
    try:
        uuid.UUID(event['id'])
    except ValueError:
        raise ValueError('Event id must be a UUID.') from None
    if event['tbl'] not in db.SYNCED_TABLES or event['op'] not in ('insert', 'update'):
        raise ValueError('Event table or operation is not allowed.')
    if not db.HLC_PATTERN.fullmatch(event['hlc']):
        raise ValueError('Event clock is malformed.')
    payload = event.get('payload')
    if not isinstance(payload, dict) or not payload:
        raise ValueError('Event payload must be a non-empty object.')
    for column, value in payload.items():
        if not db.IDENTIFIER.fullmatch(column) or column in db.RESERVED_COLUMNS:
            raise ValueError('Event payload has a forbidden column.')
        if isinstance(value, bool) or not isinstance(value, (str, int, float, type(None))):
            raise ValueError('Event payload values must be text, numbers or empty.')
        if isinstance(value, str) and len(value) > MAX_TEXT_VALUE:
            raise ValueError('Event payload value is too long.')


# --- spoke side ----------------------------------------------------------------------------------------

class HttpTransport:
    """Talks to the hub. HTTPS only, except loopback or an explicit LAN playground override."""

    def __init__(self, base_url, token, *, allow_http=False):
        parts = urllib.parse.urlsplit(base_url.strip())
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise UserError('The online server address must start with https://')
        if parts.scheme == 'http' and not allow_http and parts.hostname not in LOOPBACK_HOSTS:
            raise UserError('The online server address must use https:// so records travel encrypted.')
        self.base = base_url.strip().rstrip('/')
        self._token = token

    def push(self, events):
        body = urllib.parse.urlencode({'batch': json.dumps(events)}).encode()
        return self._call('/sync/push', body)

    def pull(self, after):
        return self._call(f'/sync/pull?after={int(after)}', None)

    def _call(self, path, body):
        request = urllib.request.Request(self.base + path, body, {'Authorization': f'Bearer {self._token}'})
        if body is not None:
            request.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            raise TransportError(f'hub answered {error.code}', error.code) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise TransportError(f'hub unreachable ({type(error).__name__})') from None


def sync_once(transport):
    """One push + pull round. Returns {'pushed': n, 'pulled': n}. Raises TransportError."""
    pushed = _push_all(transport)
    pulled = _pull_all(transport)
    return {'pushed': pushed, 'pulled': pulled}


def _push_all(transport):
    total = 0
    while True:
        with db.connect() as c:
            rows = c.all('SELECT id, tbl, row_id, op, payload, hlc, origin, created_at FROM events '
                         'WHERE pushed_at IS NULL AND dead_at IS NULL ORDER BY seq LIMIT ?', (MAX_BATCH,))
        if not rows:
            return total
        batch = [_wire_event(row) for row in rows]
        ids = [row['id'] for row in rows]
        try:
            transport.push(batch)
        except TransportError as error:
            if error.status is not None and 400 <= error.status < 500 and error.status not in (401, 403, 429):
                _record_rejected_batch(ids)
            raise
        with db.transaction() as c:
            marks = ', '.join('?' for _ in ids)
            c.execute(f'UPDATE events SET pushed_at = ? WHERE id IN ({marks})', [db.now()] + ids)
        total += len(ids)


def _record_rejected_batch(ids):
    """The hub refused this batch as invalid: count it, and after MAX_PUSH_ATTEMPTS park it for good."""
    marks = ', '.join('?' for _ in ids)
    with db.transaction() as c:
        c.execute(f'UPDATE events SET push_attempts = push_attempts + 1 WHERE id IN ({marks})', ids)
        dead = c.execute(f'UPDATE events SET dead_at = ? WHERE id IN ({marks}) AND push_attempts >= ?',
                         [db.now()] + ids + [MAX_PUSH_ATTEMPTS]).rowcount
        if dead:
            alerts.raise_alert(c, 'sync_error', 'rejected',
                               f'{dead} changes were refused by the online server and will not be sent. Ask for support.',
                               '/admin/sync', 'urgent')


def _pull_all(transport):
    total = 0
    while True:
        with db.connect() as c:
            after = int(get_state(c, 'hub_after', '0'))
        page = transport.pull(after)
        events, next_after = page.get('events'), page.get('next_after')
        if not isinstance(events, list) or not isinstance(next_after, int) or next_after < after:
            raise TransportError('hub sent a malformed page')
        with db.transaction() as c:
            own = db.ensure_node(c)
            for event in events:
                if isinstance(event, dict) and event.get('origin') == own:
                    continue
                if apply_remote(c, event) != 'seen':
                    total += 1
            retry_parked(c)
            set_state(c, 'hub_after', next_after)
            set_state(c, 'last_pull_at', db.now())
        if not page.get('more'):
            return total


# --- worker thread ---------------------------------------------------------------------------------------

def claim_lease(owner, now=None):
    """True if `owner` holds the worker lease for the next LEASE_SECONDS (conditional upsert)."""
    moment = now or datetime.now(timezone.utc)
    expires = (moment + timedelta(seconds=LEASE_SECONDS)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    current = moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    with db.transaction() as c:
        affected = c.execute(
            "INSERT INTO sync_lease (name, owner, expires_at) VALUES ('worker', ?, ?) "
            'ON CONFLICT (name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at '
            'WHERE sync_lease.owner = excluded.owner OR sync_lease.expires_at < ?',
            (owner, expires, current)).rowcount
    return affected == 1


class Worker(threading.Thread):
    """Spoke background loop: every 15 s while the hub answers, backing off to 5 min when not."""

    def __init__(self, config):
        super().__init__(name='bms-sync', daemon=True)
        self.config = config
        self.owner = db.new_id()
        self.stop_event = threading.Event()
        self.failures = 0

    def run(self):
        delay = INTERVAL_SECONDS
        while not self.stop_event.is_set():
            delay = self.tick(delay)
            self.stop_event.wait(delay)

    def tick(self, delay):
        """One loop step; returns the next delay. Never raises (the thread must survive)."""
        try:
            transport = self._transport()
            if transport is None or not claim_lease(self.owner):
                return INTERVAL_SECONDS
            sync_once(transport)
        except TransportError as error:
            return self._failed(delay, 'The online server could not be reached or refused this office.'
                                if error.status in (None, 502, 503, 504) else
                                'The online server refused this office\'s sync key. Check Admin → Sync.')
        except Exception as error:  # noqa: BLE001 - logged by type only: messages may hold resident data
            print(f'ERROR sync worker {type(error).__name__}', file=sys.stderr, flush=True)
            return self._failed(delay, 'Sync stopped on an unexpected error. Nothing was lost; it will retry.')
        if self.failures:
            with db.transaction() as c:
                alerts.resolve_alert(c, 'sync_error', ERROR_ALERT_KEY, None)
        self.failures = 0
        return INTERVAL_SECONDS

    def _transport(self):
        with db.connect() as c:
            if not is_enabled(c):
                return None
            url, token = hub_settings(c, self.config)
        if not url or not token:
            return None
        return HttpTransport(url, token, allow_http=self.config.sync_allow_http)

    def _failed(self, delay, message):
        self.failures += 1
        if self.failures >= ALERT_AFTER_FAILURES:
            with db.transaction() as c:
                alerts.raise_alert(c, 'sync_error', ERROR_ALERT_KEY, message, '/admin/sync', 'warn')
        return min(max(delay, INTERVAL_SECONDS) * 2, MAX_BACKOFF_SECONDS)


_worker = None
_worker_lock = threading.Lock()


def start_worker(config):
    """Start the background worker once per process; it idles until sync is set up and enabled."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = Worker(config)
            _worker.start()
    return _worker


def status(c, config):
    """Facts for the Admin → Sync screen (never includes a token)."""
    url, token = hub_settings(c, config)
    return {
        'enabled': is_enabled(c),
        'consent_at': get_state(c, 'consent_at'),
        'consent_by': get_state(c, 'consent_by'),
        'hub_url': url,
        'has_token': bool(token),
        'from_env': bool(config is not None and config.sync_url),
        'unpushed': c.scalar('SELECT COUNT(*) FROM events WHERE pushed_at IS NULL AND dead_at IS NULL') if url else 0,
        'dead': c.scalar('SELECT COUNT(*) FROM events WHERE dead_at IS NOT NULL'),
        'last_pull_at': get_state(c, 'last_pull_at'),
        'waiting': c.scalar("SELECT COUNT(*) FROM sync_conflicts WHERE resolved_at IS NULL AND kind = 'waiting'"),
        'conflicts': c.scalar("SELECT COUNT(*) FROM sync_conflicts WHERE resolved_at IS NULL AND kind = 'conflict'"),
        'node_id': get_state(c, 'node_id'),
    }


def save_hub(c, url, token, user):
    HttpTransport(url, token or 'x', allow_http=True)  # shape check only; scheme enforced when used
    set_state(c, 'hub_url', url.strip().rstrip('/'))
    if token:
        if '.' not in token or len(token) > 300:
            raise UserError('That sync key does not look right. Copy it again from the online server.')
        set_state(c, 'hub_token', token.strip())
    audit.log(c, user, 'sync.hub_set', 'sync', 'hub', None, {'hub_url': url.strip()})
