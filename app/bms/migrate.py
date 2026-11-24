"""Ordered, portable SQL migrations: `migrations/NNNN_name.sql`, applied once each, in order.

Never edit an applied migration - add a new one. Number blocks per module are in ARCHITECTURE.md.
Two portability tokens are substituted per dialect before execution:
  {AUTO_PK}  an auto-numbered integer primary key (events.seq)

When portable SQL cannot express a change (SQLite cannot drop a constraint), the migration is two files,
`NNNN_name.sqlite.sql` and `NNNN_name.postgres.sql`: each database runs only its own, recorded as `NNNN_name`.
"""
from pathlib import Path

from . import db

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
AUTO_PK = {
    'sqlite': 'INTEGER PRIMARY KEY',
    'postgres': 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY',
}


def run():
    """Apply every pending migration; returns the list of versions applied."""
    applied_now = []
    with db.transaction() as c:
        _lock(c)
        refuse_legacy_prototype(c)
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
    for path in sorted(MIGRATIONS_DIR.glob('*.sql')):
        version = _apply(path)
        if version:
            applied_now.append(version)
    return applied_now


def _table_exists(c, name):
    if c.dialect == 'sqlite':
        return bool(c.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))
    return bool(c.scalar('SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = ?', (name,)))


def refuse_legacy_prototype(c):
    """The v1 pilot database (`app/bms.sqlite3`) has `accounts` but no migration ledger.
    Migrating over it would fail half-way on a raw SQL error, so stop with instructions instead."""
    if _table_exists(c, 'accounts') and not _table_exists(c, 'schema_migrations'):
        raise SystemExit(
            'This database was made by the old test pilot and cannot be upgraded in place. '
            'Point BMS_DB at a new file (default app/data/bms.sqlite3) and bring records in with Import Excel.')


# Serverless platforms can cold-start several instances at once; on Postgres they must not run the same
# migration concurrently. SQLite is already serialised by BEGIN IMMEDIATE.
MIGRATION_LOCK_ID = 727_401_001


def _lock(c):
    if c.dialect == 'postgres':
        c.execute('SELECT pg_advisory_xact_lock(?)', (MIGRATION_LOCK_ID,))


def _apply(path):
    """Run one migration file if it is pending and meant for this database; returns its version or ''."""
    version, _, only_for = path.stem.partition('.')
    if only_for and only_for not in AUTO_PK:
        raise SystemExit(f'Migration {path.name}: unknown database "{only_for}" (expected sqlite or postgres).')
    with db.transaction() as c:
        if only_for and only_for != c.dialect:
            return ''
        _lock(c)
        if c.scalar('SELECT 1 FROM schema_migrations WHERE version = ?', (version,)):
            return ''
        sql = path.read_text(encoding='utf-8').replace('{AUTO_PK}', AUTO_PK[c.dialect])
        for statement in split_statements(sql):
            c.execute(statement)
        c.execute('INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)', (version, db.now()))
    return version


def split_statements(sql):
    """Split on semicolons outside single-quoted strings; drops `--` comments."""
    statements, current, in_string, i = [], [], False, 0
    while i < len(sql):
        char = sql[i]
        if in_string:
            current.append(char)
            if char == "'":
                if sql[i + 1:i + 2] == "'":  # escaped quote
                    current.append("'")
                    i += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
            current.append(char)
        elif sql.startswith('--', i):
            i = sql.find('\n', i)
            if i == -1:
                break
            continue
        elif char == ';':
            _flush(statements, current)
        else:
            current.append(char)
        i += 1
    _flush(statements, current)
    return statements


def _flush(statements, current):
    statement = ''.join(current).strip()
    current.clear()
    if statement:
        statements.append(statement)
