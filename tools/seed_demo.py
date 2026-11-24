"""Fill an empty database with FAKE homeowners for the online demo (no real resident data).

Run against the online database from this PC, never against the office database:
    DATABASE_URL=<neon url> BMS_MODE=online python tools/seed_demo.py

Creates ~120 invented homeowners with meters, last month's and this month's readings and a few old balances,
so every screen has something to show. Refuses to run if the database already has homeowners.
"""
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))

import bms  # noqa: E402
from bms import billing, db, ledger  # noqa: E402

PHASE_ID = '6f1d0000-0000-4000-8000-0000000001d0'  # seeded phase 1D (0001_core)
SURNAMES = ['DELA CRUZ', 'REYES', 'SANTOS', 'GARCIA', 'MENDOZA', 'TORRES', 'FLORES', 'RAMOS', 'AQUINO', 'BAUTISTA',
            'CASTILLO', 'VILLANUEVA', 'NAVARRO', 'SALAZAR', 'DOMINGO', 'PASCUAL', 'MERCADO', 'SORIANO', 'TOLENTINO', 'MAGBANUA']
FIRST_NAMES = ['JUAN', 'MARIA', 'JOSE', 'ANA', 'PEDRO', 'LUZ', 'RAMON', 'CARMEN', 'ANTONIO', 'ROSARIO', 'MIGUEL', 'TERESA',
               'RICARDO', 'ELENA', 'DANILO', 'GLORIA', 'ROMEO', 'CORAZON', 'NESTOR', 'LOURDES']
STREETS = ['ACACIA ST.', 'NARRA ST.', 'MOLAVE ST.', 'SAMPAGUITA ST.', 'ILANG-ILANG ST.', 'DAMA DE NOCHE ST.', 'KAMIA ST.']
HOMEOWNERS = 120
RNG = random.Random(20261031)  # fixed seed: the same demo every time


def previous_period(period):
    year, month = map(int, period.split('-'))
    return f'{year - 1}-12' if month == 1 else f'{year}-{month - 1:02d}'


def seed(c):
    if c.scalar('SELECT COUNT(*) FROM accounts'):
        raise SystemExit('This database already has homeowners; the demo seed only fills an empty one.')
    period = billing.current_period(c)
    last = previous_period(period)
    now, today = db.now(), date.today().isoformat()
    for number in range(1, HOMEOWNERS + 1):
        commercial = number % 15 == 0
        name = f'{RNG.choice(SURNAMES)}, {RNG.choice(FIRST_NAMES)}' + (' (STORE)' if commercial else '')
        account_id = c.insert('accounts', {
            'phase_id': PHASE_ID, 'account_no': f'DEMO-{number:04d}', 'name': name,
            'address': f'BLK {RNG.randint(1, 40)} LOT {RNG.randint(1, 30)} {RNG.choice(STREETS)} (demo)',
            'category': 'commercial' if commercial else 'residential', 'route': 'S' if number % 2 else 'N',
            'status': 'disconnected' if number % 37 == 0 else 'active', 'water_source': '', 'former_owner': '',
            'notes': 'Demo record: invented person, not a real resident.', 'created_at': now, 'updated_at': now,
        })
        meter_id = c.insert('meters', {'account_id': account_id, 'meter_no': f'DM-{100000 + number}'})
        start = RNG.randint(50, 3000)
        used = RNG.choice([0, 3, 7, 9, 12, 15, 18, 22, 27, 34, 45, 71]) if number % 37 else 0
        c.insert('readings', {'account_id': account_id, 'meter_id': meter_id, 'period': last, 'previous': str(start),
                              'present': str(start), 'read_on': None, 'flag': 'Demo start reading', 'entered_at': now})
        if number % 9:  # leave a few without this month's reading so "Enter readings" has work to show
            c.insert('readings', {'account_id': account_id, 'meter_id': meter_id, 'period': period, 'previous': str(start),
                                  'present': str(start + used), 'read_on': today, 'flag': '', 'entered_at': now})
        if number % 4 == 0:
            ledger.post(c, account_id, 'opening', RNG.choice([30000, 65000, 105000, 175000]), period=last,
                        memo='Demo old balance', user=None, posted_on=today)
    return HOMEOWNERS


if __name__ == '__main__':
    bms.bootstrap()
    with db.transaction() as connection:
        count = seed(connection)
    print(f'Added {count} demo homeowners (all invented).')
