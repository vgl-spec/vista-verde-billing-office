"""Database adapter: one API over SQLite (LAN) and Postgres (online).

Usage:
    with db.connect() as c:          # reads; autocommit; always closed
        c.one('SELECT ... WHERE id = ?', (x,))
    with db.transaction() as c:      # writes; BEGIN ... COMMIT/ROLLBACK; always closed
        c.insert('accounts', {...})

SQL is written with `?` placeholders; the Postgres path rewrites them (never put a literal `?`
or `%` inside SQL text - pass patterns as parameters). Rows are plain dicts.

Writes to SYNCED_TABLES must go through `c.insert` / `c.update` / `c.transition`: they stamp the
row's per-field Hybrid Logical Clock and append the change to the `events` outbox that the sync
worker ships. A raw `execute` write to a synced table silently skips replication - a bug.
"""
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

SYNCED_TABLES = frozenset({
    'phases', 'accounts', 'meters', 'settings', 'rate_plans', 'readings', 'bills',
    'ledger_entries', 'payments', 'history_notes', 'audit_log',
})
PRIMARY_KEY = {'settings': 'key'}  # every other table uses `id`
# Monotonic state machines: a state may only move to a later one, so replicas converge on max.
STATE_ORDER = {
    ('bills', 'status'): ('draft', 'issued', 'cancelled'),
    ('payments', 'status'): ('posted', 'voided'),
    ('cash_drawer_closings', 'status'): ('posted', 'voided'),  # local only; registered so voids use c.transition
}
RESERVED_COLUMNS = frozenset({'origin', 'field_hlc', 'version'})
# Rows people edit through forms. `version` goes up by one on every change (editing.py: conflicts and merges).
VERSIONED_TABLES = frozenset({'accounts', 'readings', 'bills', 'settings'})
IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]*')
HLC_PATTERN = re.compile(r'(\d{13})-(\d{4})-(.+)', re.DOTALL)
HLC_COUNTER_LIMIT = 9999
MAX_REMOTE_DRIFT_MS = 24 * 3600 * 1000  # a remote clock further ahead than this would poison ours
BUSY_TIMEOUT_MS = 15000
# The office is in the Philippines: UTC+8 all year (no daylight saving). Timestamps are STORED in UTC (db.now); dates
# and times shown to people, and "today", are the office's.
OFFICE_TZ = timezone(timedelta(hours=8), 'PHT')

_config = None
_config_lock = threading.Lock()


class IntegrityError(Exception):
    """UNIQUE / FOREIGN KEY / CHECK violation, identical for both drivers."""


def configure(config):
    global _config
    with _config_lock:
        _config = config


def new_id():
    return str(uuid.uuid4())


def now():
    """UTC timestamp with microseconds so created_at orders rows written in the same second."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def today():
    """The office's calendar date as YYYY-MM-DD, whatever time zone the server runs in (the online copy runs in UTC,
    where from midnight to 8 am Manila it would still be yesterday)."""
    return datetime.now(OFFICE_TZ).date().isoformat()


def office_time(timestamp):
    """A stored UTC timestamp (db.now) as the office's local datetime, or None when there is none."""
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(OFFICE_TZ)


# Staff read "November 25, 2026 2:06 PM", not "2026-11-25 14:06". Built by hand because strftime's no-padding
# flags (%-d, %-I) do not exist on Windows, where the office server runs.
def spoken_date(moment):
    """A date or datetime as 'November 5, 2026'. The one place the app's date wording is decided."""
    return f"{moment:%B} {moment.day}, {moment.year}"


def _spoken_clock(moment):
    return f"{moment.hour % 12 or 12}:{moment:%M %p}"


def office_minute(timestamp):
    """'2026-11-25T05:57:03.1Z' -> 'November 25, 2026 1:57 PM' (office time); '' when there is none."""
    moment = office_time(timestamp)
    return f"{spoken_date(moment)} {_spoken_clock(moment)}" if moment else ''


def office_clock(timestamp):
    """Only the office time of day, '1:57 PM', for places where the date is already on screen; '' when none."""
    moment = office_time(timestamp)
    return _spoken_clock(moment) if moment else ''


def office_day(timestamp):
    """The office's calendar date of a stored UTC timestamp, 'November 25, 2026'; '' when there is none."""
    moment = office_time(timestamp)
    return spoken_date(moment) if moment else ''


def office_day_start(iso_day):
    """The stored-timestamp form (UTC) of midnight at the office on that date, for filtering timestamps by day."""
    midnight = datetime.combine(date.fromisoformat(iso_day), datetime.min.time(), OFFICE_TZ)
    return midnight.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def connect():
    if _config is None:
        raise RuntimeError('db.configure(config) must be called before connecting.')
    if _config.uses_postgres:
        return _connect_postgres(_config.database_url)
    return _connect_sqlite(_config.db_path)


@contextmanager
def transaction():
    c = connect()
    try:
        c.begin()
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
    if c.wrote_events:
        _signal_change()


# --- live updates ---------------------------------------------------------------------------
# Every write to a synced table appends a numbered row to `events`, so MAX(events.seq) is a change counter
# that every office computer can watch (routes/live.py). This condition wakes the waiting requests the moment
# a transaction in THIS process commits; a write from elsewhere (a sync pull, another process) is still seen
# because waiters also re-read the counter every few seconds.
_change = threading.Condition()


def _signal_change():
    with _change:
        _change.notify_all()


def wait_for_change(timeout):
    """Block up to `timeout` seconds or until a local transaction that changed records commits."""
    with _change:
        _change.wait(timeout)


def latest_change(c):
    """The newest change number (0 when nothing was ever written). One read of the primary key's end."""
    return c.scalar('SELECT COALESCE(MAX(seq), 0) FROM events') or 0


def _connect_sqlite(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    raw.row_factory = _sqlite_dict_row
    raw.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
    raw.execute('PRAGMA foreign_keys=ON')
    raw.execute('PRAGMA journal_mode=WAL')
    return Connection(raw, 'sqlite')


def _connect_postgres(url):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise RuntimeError('Online mode needs psycopg: pip install -r requirements.txt') from None
    # prepare_threshold=None: automatic server-side prepared statements break behind poolers (Neon, PgBouncer).
    raw = psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=None)
    raw.adapters.register_loader('numeric', _whole_number_loader())
    return Connection(raw, 'postgres')


def _whole_number_loader():
    """Postgres returns SUM()/COALESCE over BIGINT centavos as `numeric` (Decimal), while SQLite returns int.
    Money code requires int (fail closed), so every money page crashed on the hub. Whole values load as int;
    genuinely fractional values (averages) stay Decimal."""
    from psycopg.types.numeric import NumericLoader

    class WholeNumberLoader(NumericLoader):
        def load(self, data):
            value = super().load(data)
            return int(value) if value.is_finite() and value == value.to_integral_value() else value

    return WholeNumberLoader


def _sqlite_dict_row(cursor, row):
    return {column[0]: value for column, value in zip(cursor.description, row)}


def _check_identifier(name):
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f'Unsafe SQL identifier: {name!r}')
    return name


class Result:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def one(self):
        return self._cursor.fetchone()

    def all(self):
        return self._cursor.fetchall()

    def scalar(self):
        row = self._cursor.fetchone()
        return None if row is None else next(iter(row.values()))


class Connection:
    def __init__(self, raw, dialect):
        self._raw = raw
        self.dialect = dialect
        self._in_transaction = False
        self._clock = None  # [node_id, last_ms, last_counter], loaded lazily inside a transaction
        self._clock_dirty = False
        self.wrote_events = False  # set when this connection appended to `events`: wakes live-update waiters

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # --- plumbing -------------------------------------------------------------------------

    def execute(self, sql, params=()):
        if self.dialect == 'postgres':
            sql = sql.replace('%', '%%').replace('?', '%s')
        try:
            return Result(self._raw.execute(sql, tuple(params)))
        except Exception as exc:
            if _is_integrity_error(exc):
                raise IntegrityError(str(exc)) from None
            raise

    def one(self, sql, params=()):
        return self.execute(sql, params).one()

    def all(self, sql, params=()):
        return self.execute(sql, params).all()

    def scalar(self, sql, params=()):
        return self.execute(sql, params).scalar()

    def begin(self):
        self.execute('BEGIN IMMEDIATE' if self.dialect == 'sqlite' else 'BEGIN')
        self._in_transaction = True

    def commit(self):
        if self._clock_dirty:
            self._save_clock()
        self.execute('COMMIT')
        self._in_transaction = False

    def rollback(self):
        if self._in_transaction:
            self._in_transaction = False
            self.execute('ROLLBACK')

    def close(self):
        self._raw.close()

    def _require_transaction(self):
        if not self._in_transaction:
            raise RuntimeError('Writes to synced data must run inside db.transaction().')

    # --- change-tracked writes ------------------------------------------------------------

    def insert(self, table, row):
        """Insert a row and return its primary key (a UUID is generated when `id` is absent)."""
        _check_identifier(table)
        self._require_transaction()
        data = dict(row)
        primary_key = PRIMARY_KEY.get(table, 'id')
        if primary_key == 'id':
            data.setdefault('id', new_id())
        elif primary_key not in data:
            raise ValueError(f'{table}.{primary_key} is required.')
        for column in data:
            _check_identifier(column)
        if RESERVED_COLUMNS & data.keys():
            raise ValueError('origin and field_hlc are managed by db.insert.')
        row_id = data[primary_key]
        if table in SYNCED_TABLES:
            hlc = self.hlc_now()
            payload = dict(data)
            data['origin'] = self._clock[0]
            data['field_hlc'] = json.dumps({column: hlc for column in payload if column != primary_key}, sort_keys=True)
        columns = ', '.join(data)
        marks = ', '.join('?' for _ in data)
        self.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', list(data.values()))
        if table in SYNCED_TABLES:
            self._append_event(table, row_id, 'insert', payload, hlc)
        return row_id

    def update(self, table, row_id, changes, *, only_if=None, expected_version=None):
        """Update changed columns; returns rows affected (0 or 1).

        `only_if` ({column: value}) makes it a conditional transition: a concurrent loser matches
        nothing, writes no event, and must not run side effects. `expected_version` does the same for an
        edit form: 0 rows when someone saved the record after the form was drawn (editing.py resolves it).
        """
        _check_identifier(table)
        self._require_transaction()
        if not changes:
            raise ValueError('update needs at least one change.')
        primary_key = PRIMARY_KEY.get(table, 'id')
        for column in changes:
            _check_identifier(column)
        if RESERVED_COLUMNS & changes.keys() or primary_key in changes:
            raise ValueError('Primary key, origin and field_hlc cannot be updated.')
        conditions = dict(only_if or {})
        if any(value is None for value in conditions.values()):
            raise ValueError('only_if compares to concrete values, not NULL.')
        if expected_version is not None:
            if table not in VERSIONED_TABLES:
                raise ValueError(f'{table} has no version column.')
            conditions['version'] = int(expected_version)
        assignments = dict(changes)
        synced = table in SYNCED_TABLES
        if synced:
            hlc = self.hlc_now()
            stored = self.scalar(f'SELECT field_hlc FROM {table} WHERE {primary_key} = ?', (row_id,))
            if stored is None:
                return 0
            stamps = json.loads(stored)
            stamps.update({column: hlc for column in changes})
            assignments['field_hlc'] = json.dumps(stamps, sort_keys=True)
        set_sql = ', '.join(f'{column} = ?' for column in assignments)
        if table in VERSIONED_TABLES:
            set_sql += ', version = version + 1'
        where_sql = ' AND '.join([f'{primary_key} = ?'] + [f'{_check_identifier(c)} = ?' for c in conditions])
        params = list(assignments.values()) + [row_id] + list(conditions.values())
        affected = self.execute(f'UPDATE {table} SET {set_sql} WHERE {where_sql}', params).rowcount
        if synced and affected == 1:
            self._append_event(table, row_id, 'update', changes, hlc)
        return affected

    def transition(self, table, row_id, field, from_state, to_state, extra=None):
        """Move a state column forward only; returns True for the single winner."""
        order = STATE_ORDER.get((table, field))
        if order is None:
            raise ValueError(f'{table}.{field} is not a registered state machine.')
        if from_state not in order or to_state not in order or order.index(to_state) <= order.index(from_state):
            raise ValueError(f'{table}.{field} may only move forward, not {from_state!r} -> {to_state!r}.')
        changes = {field: to_state, **(extra or {})}
        return self.update(table, row_id, changes, only_if={field: from_state}) == 1

    def _append_event(self, table, row_id, op, payload, hlc):
        self.wrote_events = True
        self.execute(
            'INSERT INTO events (id, tbl, row_id, op, payload, hlc, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (new_id(), table, str(row_id), op, json.dumps(payload, sort_keys=True, default=str), hlc, self._clock[0], now()),
        )

    # --- Hybrid Logical Clock -------------------------------------------------------------

    def hlc_now(self):
        """Next local timestamp: '<ms:013d>-<counter:04d>-<node_id>', strictly increasing."""
        return self._tick(None)

    def hlc_receive(self, remote_hlc):
        """Advance the local clock past a remote one so later local writes sort after it."""
        match = HLC_PATTERN.fullmatch(remote_hlc or '')
        if not match:
            raise ValueError(f'Malformed hybrid logical clock value: {remote_hlc!r}')
        return self._tick((int(match[1]), int(match[2])))

    def _tick(self, remote):
        self._require_transaction()
        if self._clock is None:
            self._load_clock()
        node_id, last_ms, last_counter = self._clock
        wall_ms = int(time.time() * 1000)
        if remote and remote[0] > wall_ms + MAX_REMOTE_DRIFT_MS:
            raise ValueError('Remote clock is more than a day ahead; refusing to adopt it.')
        remote_ms, remote_counter = remote if remote else (-1, -1)
        ms = max(wall_ms, last_ms, remote_ms)
        counter = -1
        if ms == last_ms:
            counter = last_counter
        if ms == remote_ms:
            counter = max(counter, remote_counter)
        counter += 1
        if counter > HLC_COUNTER_LIMIT:
            ms, counter = ms + 1, 0
        self._clock[1], self._clock[2] = ms, counter
        self._clock_dirty = True
        return f'{ms:013d}-{counter:04d}-{node_id}'

    def _load_clock(self):
        lock = ' FOR UPDATE' if self.dialect == 'postgres' else ''  # serialize clock use per node
        state = {row['key']: row['value'] for row in self.all(
            f"SELECT key, value FROM sync_state WHERE key IN ('node_id', 'hlc'){lock}")}
        if 'node_id' not in state:
            state['node_id'] = ensure_node(self)
        last_ms, _, last_counter = state.get('hlc', '0-0').partition('-')
        self._clock = [state['node_id'], int(last_ms), int(last_counter)]

    def _save_clock(self):
        _, ms, counter = self._clock
        self.execute(
            "INSERT INTO sync_state (key, value) VALUES ('hlc', ?) "
            'ON CONFLICT (key) DO UPDATE SET value = excluded.value',
            (f'{ms}-{counter}',),
        )
        self._clock_dirty = False


def ensure_node(c):
    """Create this node's stable identity in sync_state if absent; return the node id.

    Stored in sync_state, not settings: settings are replicated, and every node must keep its own.
    """
    configured = _config.node_id if _config else ''
    # The clock row must exist before the first write: Postgres locks it (FOR UPDATE) to serialize
    # writers, which keeps events.seq in commit order for the hub log.
    c.execute("INSERT INTO sync_state (key, value) VALUES ('hlc', '0-0') ON CONFLICT (key) DO NOTHING")
    stored = c.scalar("SELECT value FROM sync_state WHERE key = 'node_id'")
    if stored:
        if configured and configured != stored:
            raise RuntimeError('BMS_NODE_ID differs from this database\'s node id; refusing to change sync identity.')
        return stored
    node_id = configured or new_id()
    short = (_config.node_short if _config else '') or re.sub(r'[^A-Z0-9]', '', node_id.upper())[:4]
    c.execute("INSERT INTO sync_state (key, value) VALUES ('node_id', ?)", (node_id,))
    c.execute("INSERT INTO sync_state (key, value) VALUES ('node_short', ?)", (short,))
    return node_id


def _is_integrity_error(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return type(exc).__module__.startswith('psycopg') and type(exc).__name__ in (
        'IntegrityError', 'UniqueViolation', 'ForeignKeyViolation', 'CheckViolation', 'NotNullViolation')
