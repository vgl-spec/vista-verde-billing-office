"""Put a prepared records file into this office server's database, safely. Started by LOAD RECORDS.bat.

The records file (for example Phase 1D's homeowners, meters, readings and balances, built from the office's
workbooks on the developer's PC with tools/build_real_db.py) arrives on a USB drive: it holds resident personal
data, so it never travels through GitHub, email or chat.

    python tools/load_records.py <records file> [--no-admin]

Nothing is changed, and the reason is printed, when:
  * the billing server is running (close its window first);
  * the file is not a sound billing database (damaged, or not made by this system);
  * it was made by a NEWER version of the system than this computer has (run UPDATE.bat first);
  * this server already holds homeowners (loading would replace the office's work).
Otherwise the empty database is moved into app/data/backups, the records are copied in with SQLite's backup API
(never a half-written copy), brought up to this version, and checked. When no administrator can sign in yet,
one is created here (the records file's own import account has no usable password).
"""
import argparse
import os
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))
DEFAULT_TARGET = ROOT / 'app' / 'data' / 'bms.sqlite3'
MIGRATIONS_DIR = ROOT / 'app' / 'bms' / 'migrations'
IMPORT_USERNAME = 'import-office'  # the account tools/build_real_db.py records the import under; never signs in


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('records', type=Path, help='the records file (.sqlite3) from the USB drive')
    parser.add_argument('--target', type=Path, default=DEFAULT_TARGET, help=argparse.SUPPRESS)
    parser.add_argument('--no-admin', action='store_true', help='do not ask for an administrator (tests)')
    args = parser.parse_args()
    try:
        summary = load(args.records.resolve(), args.target.resolve())
    except Refused as reason:
        print(f'\n  NOT LOADED: {reason}\n  Nothing was changed.')
        return 1
    print(f"\n  Loaded {summary['accounts']} homeowners ({', '.join(summary['phases'])}); readings for "
          f"{', '.join(summary['months']) or 'no month'}.")
    if not args.no_admin and not summary['admin_exists']:
        _create_first_admin()
    return 0


class Refused(Exception):
    """A plain-language reason the records were not loaded; nothing was changed."""


def load(records, target):
    if _server_running():
        raise Refused('the billing server is running. Close its window, then try again.')
    source = _read_records_file(records)
    _check_target_is_empty(target)

    backups = target.parent / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    if target.exists():  # an empty database from an earlier start: kept, never deleted
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        for suffix in ('', '-wal', '-shm'):
            part = Path(f'{target}{suffix}')
            if part.exists():
                part.rename(backups / f'{target.stem}-empty-before-load-{stamp}.sqlite3{suffix}')
    reader = sqlite3.connect(_source_uri(records), uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:  # sqlite3's own context manager commits but never closes; Windows keeps open files locked
        reader.close()
        writer.close()

    import bms
    from bms import db
    bms.bootstrap({'BMS_DB': str(target)})  # brings the copy up to this version's tables
    with db.connect() as c:
        loaded = c.scalar('SELECT COUNT(*) FROM accounts')
        if loaded != source['accounts']:
            raise RuntimeError(f'after loading, {loaded} homeowners were found instead of {source["accounts"]}')
        return {
            'accounts': loaded,
            'phases': [row['name'] for row in c.all('SELECT DISTINCT p.name FROM phases p JOIN accounts a ON a.phase_id = p.id ORDER BY p.name')],
            'months': [row['period'] for row in c.all('SELECT DISTINCT period FROM readings ORDER BY period')],
            'admin_exists': bool(c.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND username <> ?",
                                          (IMPORT_USERNAME,))),
        }


def _server_running():
    port = int(os.environ.get('BMS_PORT', '8765'))
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def _source_uri(records):
    # immutable: read without locks or "-wal"/"-shm" side files (a WAL-mode file opened read-only otherwise writes
    # them next to itself, on the USB drive). Safe because nothing writes the records file while it is loaded.
    return f'file:{records.as_posix()}?mode=ro&immutable=1'


def _read_records_file(records):
    if not records.is_file():
        raise Refused(f'there is no file at {records}.')
    partner = Path(f'{records}-wal')
    if partner.exists() and partner.stat().st_size:
        # Part of the records may still sit in that partner file; loading the main file alone could lose them.
        raise Refused(f'the records file is incomplete ({partner.name} is next to it). Ask the developer for a closed copy.')
    try:
        connection = sqlite3.connect(_source_uri(records), uri=True)
        try:
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Refused('the records file is damaged. Ask the developer for a new copy.')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {'accounts', 'schema_migrations', 'readings', 'ledger_entries'} <= tables:
                raise Refused('that file is not a billing records file.')
            versions = {row[0] for row in connection.execute('SELECT version FROM schema_migrations')}
            accounts = connection.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        raise Refused('that file is not a billing records file (it could not be opened as one).') from None
    known = {path.stem.partition('.')[0] for path in MIGRATIONS_DIR.glob('*.sql')}
    if versions - known:
        raise Refused('the records file was made by a newer version of the system. Run UPDATE.bat first, then try again.')
    if not accounts:
        raise Refused('the records file holds no homeowners.')
    return {'accounts': accounts}


def _check_target_is_empty(target):
    if not target.exists():
        return
    connection = sqlite3.connect(f'file:{target.as_posix()}?mode=ro', uri=True)
    try:
        has_accounts = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'").fetchone()
        count = connection.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] if has_accounts else 0
    finally:
        connection.close()
    if count:
        raise Refused(f'this server already holds {count} homeowners. Loading would replace them. '
                      'If that is really wanted, ask the developer.')


def _create_first_admin():
    import bms
    from bms import auth, db
    print('\n  No administrator can sign in yet. Create one now (the office manager, for example).')
    username = input('  Administrator username: ').strip()
    if not username:
        print('  Skipped. Later, in PowerShell in C:\\VistaVerde: py -3 app\\server.py create-admin <name>')
        return
    bms._create_admin(username, auth, db)


if __name__ == '__main__':
    sys.exit(main())
