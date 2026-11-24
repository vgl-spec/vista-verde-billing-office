"""Make a consistent copy of the office database while it may be in use, and keep the newest N copies.

    python tools/backup_db.py [database path] [--keep 30]

Uses SQLite's online backup API, so a copy taken while people are saving is still a valid database
(copying the file by hand could catch it half-written). Copies go to app/data/backups/ next to the
database and are named <database>-YYYYMMDD-HHMMSS.sqlite3. Resident data: keep them on this computer
or a locked USB drive; never email them.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / 'app' / 'data' / 'bms.sqlite3'


def copy_to(backup_file: Path, folder: Path, keep: int) -> None:
    """Second copy in another folder, e.g. the HOA's Google Drive folder (synced by Google Drive for desktop)."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / backup_file.name
    target.write_bytes(backup_file.read_bytes())
    stem = backup_file.name.rsplit('-', 2)[0]
    for old in sorted(folder.glob(f'{stem}-*.sqlite3'))[:-keep] if keep > 0 else []:
        old.unlink()
    print(f'Second copy saved: {target}')


def backup(database: Path, keep: int) -> Path | None:
    if not database.exists():
        print(f'No database yet at {database}; nothing to back up.')
        return None
    folder = database.parent / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{database.stem}-{datetime.now():%Y%m%d-%H%M%S}.sqlite3'
    source = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    # Oldest copies go once there are more than `keep` of this database.
    copies = sorted(folder.glob(f'{database.stem}-*.sqlite3'))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
    print(f'Backup saved: {target}')
    return target


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('database', nargs='?', default=str(DEFAULT_DB))
    parser.add_argument('--keep', type=int, default=30, help='how many copies of this database to keep (default 30)')
    parser.add_argument('--copy-to', default='', help='also keep a copy in this folder (for example a Google Drive folder)')
    args = parser.parse_args(argv)
    made = backup(Path(args.database), args.keep)
    if made and args.copy_to.strip():
        try:
            copy_to(made, Path(args.copy_to.strip()), args.keep)
        except OSError as error:  # a missing USB/Drive folder must never stop the office from starting the server
            print(f'Could not save the second copy in {args.copy_to}: {error}')


if __name__ == '__main__':
    main(sys.argv[1:])
