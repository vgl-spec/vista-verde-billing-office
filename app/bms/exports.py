"""CSV and XLSX downloads of the office lists. Stdlib only (no openpyxl on the LAN path).

Each dataset is (title, roles, loader). A loader takes (c, query dict) and returns
(filename stem, headers, rows); cells are str, int or Decimal so Excel keeps numbers numeric.
"""
import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import UserError, billing, db, ledger, statements
from .http import DATE_PATTERN, PERIOD_PATTERN

ALL_ROLES = ('admin', 'staff', 'cashier')
OFFICE_ROLES = ('admin', 'staff')
FORMULA_START = ('=', '+', '-', '@', '\t', '\r')
XML_ILLEGAL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


@dataclass(frozen=True)
class Dataset:
    title: str
    roles: tuple
    loader: object


# --- file writers -----------------------------------------------------------------------------------

def to_csv(headers, rows):
    """UTF-8 with BOM: without it Excel on Windows reads ₱ and ñ as mojibake."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow([_safe_text(h) for h in headers])
    for row in rows:
        writer.writerow([_safe_text(cell) if isinstance(cell, str) else cell for cell in row])
    return ('﻿' + buffer.getvalue()).encode('utf-8')


def to_xlsx(sheet_name, headers, rows):
    """Minimal valid workbook: one sheet, inline strings, bold header row."""
    cells = [_xlsx_row(1, headers, header=True)] + [_xlsx_row(n, row) for n, row in enumerate(rows, start=2)]
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             f'</sheetView></sheetViews><sheetData>{"".join(cells)}</sheetData></worksheet>')
    name = _xml(re.sub(r'[\[\]:*?/\\]', ' ', sheet_name)[:31] or 'Sheet1')
    files = {
        '[Content_Types].xml': (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'),
        '_rels/.rels': (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'),
        'xl/workbook.xml': (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        'xl/_rels/workbook.xml.rels': (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'),
        'xl/styles.xml': (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
            '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
            '</styleSheet>'),
        'xl/worksheets/sheet1.xml': sheet,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return output.getvalue()


def _xlsx_row(number, values, *, header=False):
    cells = []
    for index, value in enumerate(values):
        ref = f'{_column(index)}{number}'
        if header:
            cells.append(f'<c r="{ref}" t="inlineStr" s="1"><is><t xml:space="preserve">{_xml(value)}</t></is></c>')
        elif isinstance(value, Decimal):
            cells.append(f'<c r="{ref}" s="2"><v>{value}</v></c>')
        elif isinstance(value, int) and not isinstance(value, bool):
            cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        elif value is not None and value != '':
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml(value)}</t></is></c>')
    return f'<row r="{number}">{"".join(cells)}</row>'


def _column(index):
    letters = ''
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xml(value):
    text = XML_ILLEGAL.sub('', str(value))
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _safe_text(value):
    """Neutralise spreadsheet formulas in text cells (a homeowner named '=HYPERLINK(...)')."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


# --- datasets ---------------------------------------------------------------------------------------

def _pesos(cents):
    return statements.money_decimal(cents)


def _period(query, c):
    period = query.get('period') or billing.current_period(c)
    if not PERIOD_PATTERN.fullmatch(period):
        raise UserError('Use a billing month in YYYY-MM format.')
    return period


def date_range(query):
    today = date.fromisoformat(db.today())
    start = _date(query.get('from'), today.replace(day=1).isoformat())
    end = _date(query.get('to'), today.isoformat())
    if start > end:
        raise UserError('The start date is after the end date.')
    return start, end


def _date(value, default):
    if not value:
        return default
    if not DATE_PATTERN.fullmatch(value):
        raise UserError('Dates must look like 2026-10-31.')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise UserError('Dates must be real dates.') from None


def load_homeowners(c, query):
    rows = c.all(
        'SELECT a.account_no, a.name, a.address, a.category, a.status, a.water_source, p.code AS phase, '
        'COALESCE(b.balance, 0) AS balance FROM accounts a JOIN phases p ON p.id = a.phase_id LEFT JOIN '
        '(SELECT account_id, SUM(amount_cents) AS balance FROM ledger_entries GROUP BY account_id) b ON b.account_id = a.id '
        'ORDER BY p.code, a.account_no')
    return ('homeowners', ['Account no.', 'Name', 'Address', 'Phase', 'Type', 'Status', 'Water source', 'Balance (PHP)'],
            [[r['account_no'], r['name'], r['address'], r['phase'], r['category'], r['status'], r['water_source'],
              _pesos(r['balance'])] for r in rows])


def load_readings(c, query):
    period = _period(query, c)
    phase = query.get('phase', 'all')
    clause, params = ('', ())
    if phase != 'all':
        if not c.scalar('SELECT 1 FROM phases WHERE code = ?', (phase,)):
            raise UserError('Choose a phase code from the list, or all.')
        clause, params = ' AND p.code = ?', (phase,)
    rows = c.all('SELECT a.account_no, a.name, p.code AS phase, r.previous, r.present, r.read_on, r.flag FROM readings r '
                 'JOIN accounts a ON a.id = r.account_id JOIN phases p ON p.id = a.phase_id '
                 f'WHERE r.period = ?{clause} ORDER BY p.code, a.account_no', (period,) + params)
    return (f'readings-{period}', ['Account no.', 'Name', 'Phase', 'Previous', 'Present', 'Used (m3)', 'Date read', 'Flag'],
            [[r['account_no'], r['name'], r['phase'], Decimal(r['previous']), Decimal(r['present']),
              Decimal(r['present']) - Decimal(r['previous']), r['read_on'] or '', r['flag']] for r in rows])


def load_bills(c, query):
    period = _period(query, c)
    rows = c.all('SELECT b.statement_no, b.status, b.water_cents, b.dues_cents, b.other_cents, b.total_cents, b.prior_balance_cents, '
                 'b.due_on, b.issued_on, a.account_no, a.name FROM bills b JOIN accounts a ON a.id = b.account_id '
                 "WHERE b.period = ? AND b.status <> 'cancelled' ORDER BY a.account_no", (period,))
    return (f'bills-{period}', ['Statement no.', 'Status', 'Account no.', 'Name', 'Water', 'Dues', 'Other', 'Current charges',
                                'Previous balance', 'Amount due', 'Issued', 'Due'],
            [[r['statement_no'] if r['status'] == 'issued' else '', r['status'], r['account_no'], r['name'], _pesos(r['water_cents']),
              _pesos(r['dues_cents']), _pesos(r['other_cents']), _pesos(r['total_cents']), _pesos(r['prior_balance_cents']),
              _pesos(r['prior_balance_cents'] + r['total_cents']), r['issued_on'] or '', r['due_on']] for r in rows])


def load_payments(c, query):
    start, end = date_range(query)
    # What each receipt paid for, one column per cashier tab (older payments count as water bill).
    rows = c.all('SELECT p.paid_on, p.receipt_type, p.receipt_no, p.amount_cents, p.method, p.status, p.void_reason, a.account_no, a.name, '
                 'u.display_name, COALESCE(s.bill, 0) AS bill, COALESCE(s.dues, 0) AS dues, COALESCE(s.misc, 0) AS misc, '
                 'COALESCE(s.details, \'\') AS details '
                 'FROM payments p JOIN accounts a ON a.id = p.account_id LEFT JOIN users u ON u.id = p.created_by '
                 "LEFT JOIN (SELECT payment_id, SUM(CASE WHEN applies_to IN ('', 'bill') THEN -amount_cents ELSE 0 END) AS bill, "
                 "SUM(CASE WHEN applies_to = 'dues' THEN -amount_cents ELSE 0 END) AS dues, "
                 "SUM(CASE WHEN applies_to = 'misc' THEN -amount_cents ELSE 0 END) AS misc, "
                 "MAX(CASE WHEN applies_to = 'misc' THEN memo ELSE '' END) AS details "
                 "FROM ledger_entries WHERE kind = 'payment' GROUP BY payment_id) s ON s.payment_id = p.id "
                 'WHERE p.paid_on >= ? AND p.paid_on <= ? ORDER BY p.paid_on, p.receipt_no', (start, end))
    return (f'payments-{start}-to-{end}', ['Date paid', 'Receipt', 'Receipt no.', 'Account no.', 'Name', 'Amount (PHP)', 'Water bill (PHP)',
                                           'Monthly dues (PHP)', 'Miscellaneous (PHP)', 'Miscellaneous details', 'Method', 'Status',
                                           'Void reason', 'Recorded by'],
            [[r['paid_on'], r['receipt_type'], r['receipt_no'], r['account_no'], r['name'], _pesos(r['amount_cents']), _pesos(r['bill']),
              _pesos(r['dues']), _pesos(r['misc']), r['details'].split(' · ', 1)[-1] if r['details'] else '', r['method'],
              r['status'], r['void_reason'], r['display_name'] or ''] for r in rows])


def aging_rows(c):
    """Every account that owes something: buckets by age of the unpaid charge. One ledger query."""
    today = date.fromisoformat(db.today())
    grouped = statements.entries_by_account(
        c, 'SELECT account_id FROM ledger_entries GROUP BY account_id HAVING SUM(amount_cents) > 0')
    if not grouped:
        return []
    marks = ', '.join('?' for _ in grouped)
    accounts = {a['id']: a for a in c.all(f'SELECT id, account_no, name, address, status, phase_id FROM accounts WHERE id IN ({marks})',
                                         tuple(grouped))}
    result = []
    for account_id, entries in grouped.items():
        aging = ledger.fifo_open_charges(entries)
        buckets = statements.bucket_open_charges(aging.open_charges, today)
        oldest = min((ch.period for ch in aging.open_charges), default='')
        result.append({**accounts[account_id], 'buckets': buckets, 'total': sum(buckets), 'oldest_period': oldest})
    result.sort(key=lambda r: (-r['total'], r['account_no']))
    return result


def load_aging(c, query):
    return ('aging', ['Account no.', 'Name', 'Address', 'Status'] + list(statements.AGING_BUCKETS) + ['Total owed', 'Oldest unpaid month'],
            [[r['account_no'], r['name'], r['address'], r['status']] + [_pesos(b) for b in r['buckets']]
             + [_pesos(r['total']), r['oldest_period']] for r in aging_rows(c)])


def collection_summary(c, start, end):
    by_method = c.all("SELECT method, COUNT(*) AS n, SUM(amount_cents) AS total FROM payments WHERE status = 'posted' "
                      'AND paid_on >= ? AND paid_on <= ? GROUP BY method ORDER BY method', (start, end))
    by_cashier = c.all("SELECT COALESCE(u.display_name, '') AS cashier, COUNT(*) AS n, SUM(p.amount_cents) AS total FROM payments p "
                       "LEFT JOIN users u ON u.id = p.created_by WHERE p.status = 'posted' AND p.paid_on >= ? AND p.paid_on <= ? "
                       "GROUP BY COALESCE(u.display_name, '') ORDER BY cashier", (start, end))
    voided = c.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total FROM payments WHERE status = 'voided' "
                   'AND paid_on >= ? AND paid_on <= ?', (start, end))
    return by_method, by_cashier, voided


# What a payment line paid for (ledger applies_to) -> the report's label. '' = recorded before the tick boxes.
PURPOSE_LABELS = {'bill': 'Water bill and balance', 'dues': 'Monthly dues', 'misc': 'Miscellaneous',
                  '': 'Earlier payments (oldest charges first)'}


def collection_split(c, start, end):
    """Posted collections by purpose and by phase (site visit: water, dues and misc are filed separately)."""
    by_purpose = c.all(
        "SELECT l.applies_to AS purpose, COUNT(DISTINCT p.id) AS n, -SUM(l.amount_cents) AS total "
        "FROM ledger_entries l JOIN payments p ON p.id = l.payment_id "
        "WHERE l.kind = 'payment' AND p.status = 'posted' AND p.paid_on >= ? AND p.paid_on <= ? "
        'GROUP BY l.applies_to', (start, end))
    order = list(PURPOSE_LABELS)
    by_purpose = sorted(by_purpose, key=lambda r: order.index(r['purpose']) if r['purpose'] in order else len(order))
    by_phase = c.all(
        "SELECT ph.name AS phase, COUNT(*) AS n, SUM(p.amount_cents) AS total FROM payments p "
        "JOIN accounts a ON a.id = p.account_id JOIN phases ph ON ph.id = a.phase_id "
        "WHERE p.status = 'posted' AND p.paid_on >= ? AND p.paid_on <= ? GROUP BY ph.name ORDER BY ph.name", (start, end))
    return by_purpose, by_phase


def load_collections(c, query):
    start, end = date_range(query)
    by_method, by_cashier, voided = collection_summary(c, start, end)
    by_purpose, by_phase = collection_split(c, start, end)
    rows = [['For', PURPOSE_LABELS.get(r['purpose'], r['purpose']), r['n'], _pesos(r['total'])] for r in by_purpose]
    rows += [['By phase', r['phase'], r['n'], _pesos(r['total'])] for r in by_phase]
    rows += [['By method', r['method'], r['n'], _pesos(r['total'])] for r in by_method]
    rows += [['By cashier', r['cashier'] or 'Other office', r['n'], _pesos(r['total'])] for r in by_cashier]
    rows.append(['Voided (not counted)', '', voided['n'], _pesos(voided['total'])])
    return (f'collections-{start}-to-{end}', ['Group', 'Name', 'Payments', 'Total (PHP)'], rows)


DATASETS = {
    'homeowners': Dataset('Homeowners', OFFICE_ROLES, load_homeowners),
    'readings': Dataset('Readings', OFFICE_ROLES, load_readings),
    'bills': Dataset('Bills', OFFICE_ROLES, load_bills),
    'payments': Dataset('Payments', ALL_ROLES, load_payments),
    'aging': Dataset('Unpaid balances', OFFICE_ROLES, load_aging),
    'collections': Dataset('Collections', OFFICE_ROLES, load_collections),
}
