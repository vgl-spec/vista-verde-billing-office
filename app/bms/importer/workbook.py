"""Read the Phase 1D SOA workbook (layout: docs/excel-workflow.md) into plain Python records.

Nothing here touches the database. The file is read from memory (never saved), with openpyxl in
read-only mode and every scan bounded by an explicit max_row: the SOA sheets report about a
million rows while their data ends near row 11,400.
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .. import UserError

MRPH_SHEET = 'MRPH 1D'
SOA_SHEETS = ('PH1D', 'PH1DCONT', '1D SOUTH SOA')
PHASE_CODE = '1D'
MAX_SHEETS_LISTED = 12  # sheet names shown when a workbook's layout is unknown
MRPH_FIRST_ROW = 9
MRPH_MAX_ROW = 2000
SOA_MAX_ROW = 12000
SOA_MAX_COL = 24
HEADER_ROWS = 8
BLOCK_SPAN = 30
NOTE_COLUMN_OFFSET = 10
MONTHS = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
MONTH_YEAR = re.compile(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*(?:\d{1,2}\s*,?\s*)?(\d{4})\b')
MONTH_DAY_YEAR = re.compile(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*(\d{1,2})\s*,?\s*(\d{4})\b')
MRPH_REFERENCE = re.compile(r"'?MRPH 1D\s*'?!\$?([A-Z]{1,2})\$?(\d+)", re.IGNORECASE)
BRACKET_LABEL = re.compile(r'^\d+\s*CUM\.?(\s*-\s*\d*\s*CUM\.?)?$')
DISCONNECT_NOTE = re.compile(r'TEMPORAR\w* DISCONNECT|REQUEST(ED)? TD|\bTD\b')
RECONNECT_NOTE = re.compile(r'RECONNECT')
FORMER_NOTE = re.compile(r'FORMER\s*(?:OWNER)?\s*[:\-]?\s*(.+)', re.IGNORECASE)
CATEGORY_CODES = {'R': 'residential', 'COMM': 'commercial'}
HEADER_LABELS = ('DATE ISSUED', 'ISSUED DATE', 'DUE DATE', 'WATER BILL BRACKETING', 'CONSUMPTION', 'PERIOD COVERED')
LINK_COLUMNS = ('H', 'J', 'L')  # present, previous, consumption: a formula to these names the MRPH row


@dataclass
class MeterRow:
    sheet_row: int
    serial: int
    account_no: str
    name: str
    address: str
    type_code: str
    category: str
    route: str
    meter_no: str
    present: str          # '' when the cell is not a number
    present_on: str
    previous: str
    previous_on: str


@dataclass
class SoaBlock:
    sheet: str
    sheet_row: int
    name: str
    meter_no: str
    link_row: int = 0          # MRPH sheet row named by the present-reading formula
    is_current: bool = True    # the sheet was issued for the workbook's billing month
    total_cents: int = None
    opening: list = field(default_factory=list)   # [{'memo', 'cents', 'row'}]
    notes: list = field(default_factory=list)     # free text, in sheet order
    status_hint: str = ''
    former_owner: str = ''


@dataclass
class Workbook:
    period: str
    rows: list
    blocks: list
    dues_cents_seen: int = 0
    skipped_rows: int = 0


def parse(data, dues_cents):
    """Parse workbook bytes. `dues_cents` (one month of dues) splits extra dues months into the
    opening balance. Raises UserError with a plain message for anything unreadable."""
    try:
        import openpyxl
    except ImportError:
        raise UserError('Reading Excel files needs the openpyxl package: pip install -r requirements-lan.txt') from None
    try:
        formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        values = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - openpyxl raises many types for a bad file; all mean the same to staff
        raise UserError('That file could not be opened as an Excel workbook (.xlsx).') from None
    try:
        names = {name.strip().upper(): name for name in formulas.sheetnames}
        if MRPH_SHEET not in names:
            # Another phase's workbook, or a changed layout: say what was found and import nothing, never guess.
            found = ', '.join(f'"{name}"' for name in formulas.sheetnames[:MAX_SHEETS_LISTED])
            more = ' and more' if len(formulas.sheetnames) > MAX_SHEETS_LISTED else ''
            raise UserError(f'Nothing was imported. This workbook is not in a layout the system knows yet: it has no '
                            f'"{MRPH_SHEET}" sheet (the Phase 1D meter reading sheet). Sheets in this file: {found}{more}. '
                            'Give this file to the developer (on a USB drive, not by email) so its layout can be added.')
        mrph_name = names[MRPH_SHEET]
        mrph = _grid(values[mrph_name], MRPH_MAX_ROW, 12)
        period = _find_period(mrph[:HEADER_ROWS])
        if period is None:
            raise UserError('Could not find the billing month at the top of the meter reading sheet (for example "PH 1D OCTOBER 2026").')
        rows, skipped = _meter_rows(mrph)
        if not rows:
            raise UserError('No numbered homeowner rows were found in the meter reading sheet.')
        blocks = []
        for sheet in SOA_SHEETS:
            if sheet in names:
                blocks += _soa_blocks(sheet, _grid(formulas[names[sheet]], SOA_MAX_ROW, SOA_MAX_COL),
                                      _grid(values[names[sheet]], SOA_MAX_ROW, SOA_MAX_COL), period, dues_cents)
        return Workbook(period, rows, blocks, dues_cents, skipped)
    finally:
        formulas.close()
        values.close()


def _grid(sheet, max_row, max_col):
    """Rows as lists, trailing empty rows dropped; reading stops at max_row whatever the sheet claims."""
    grid = [list(row) for row in sheet.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True)]
    while grid and not any(cell is not None for cell in grid[-1]):
        grid.pop()
    return grid


def _cell(grid, row, col):
    """1-based row/col access that tolerates ragged rows."""
    if row < 1 or row > len(grid) or col < 1:
        return None
    line = grid[row - 1]
    return line[col - 1] if col <= len(line) else None


def _text(value):
    return re.sub(r'\s+', ' ', str(value)).strip() if value is not None else ''


def normalize_name(value):
    return re.sub(r'[^A-Z0-9Ñ ]', '', _text(value).upper()).strip()


def normalize_meter(value):
    return re.sub(r'[^A-Z0-9]', '', _text(value).upper())


def _find_period(header_rows):
    for line in header_rows:
        for value in line:
            match = MONTH_YEAR.search(_text(value).upper())
            if match:
                return f'{int(match[2]):04d}-{MONTHS[match[1]]:02d}'
    return None


def parse_day(value):
    """'OCT. 11, 2026' / datetime -> '2026-10-11'; '' when not a date."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    match = MONTH_DAY_YEAR.search(_text(value).upper())
    if not match:
        return ''
    try:
        return date(int(match[3]), MONTHS[match[1]], int(match[2])).isoformat()
    except ValueError:
        return ''


def _reading(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return ''
    return f'{Decimal(str(value)).normalize():f}'


def to_cents(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal('1')))
    except InvalidOperation:
        return None


def _meter_rows(grid):
    rows, skipped = [], 0
    for index in range(MRPH_FIRST_ROW, len(grid) + 1):
        serial = _cell(grid, index, 2)
        name = _text(_cell(grid, index, 3))
        if isinstance(serial, bool) or not isinstance(serial, int) or not name:
            skipped += 1 if name else 0
            continue
        type_code = _text(_cell(grid, index, 5)).upper()
        route = _text(_cell(grid, index, 6)).upper()
        rows.append(MeterRow(
            sheet_row=index, serial=serial, account_no=f'{PHASE_CODE}-{serial:04d}', name=name,
            address=_text(_cell(grid, index, 4)), type_code=type_code, category=CATEGORY_CODES.get(type_code, ''),
            route=route if route in ('S', 'N') else '', meter_no=_text(_cell(grid, index, 7)),
            present=_reading(_cell(grid, index, 8)), present_on=parse_day(_cell(grid, index, 9)),
            previous=_reading(_cell(grid, index, 10)), previous_on=parse_day(_cell(grid, index, 11))))
    return rows, skipped


# --- statement blocks ---------------------------------------------------------------------------

def _soa_blocks(sheet, formulas, values, period, dues_cents):
    starts = [(row, col) for row in range(1, len(formulas) + 1) for col in (1, 2)
              if _text(_cell(formulas, row, col)).upper().startswith('NAME')]
    is_current = _sheet_period(formulas, values) == period
    blocks = []
    for position, (row, base) in enumerate(starts):
        next_start = starts[position + 1][0] if position + 1 < len(starts) else row + BLOCK_SPAN
        last = min(next_start - 1, row + BLOCK_SPAN)
        blocks.append(_block(sheet, formulas, values, row, base, last, is_current, dues_cents))
    return blocks


def _sheet_period(formulas, values):
    for row in range(1, HEADER_ROWS + 1):
        for col in (1, 2):
            if 'DATE ISSUED' in _text(_cell(formulas, row, col)).upper():
                for value_col in range(col + 1, col + 8):
                    found = _find_period([[_cell(values, row, value_col)]])
                    if found:
                        return found
    return None


def _block(sheet, formulas, values, start, base, last, is_current, dues_cents):
    block = SoaBlock(sheet, start, _text(_cell(values, start, base + 1)), _text(_cell(values, start + 2, base + 1)), is_current=is_current)
    texts = []
    for row in range(start, last + 1):
        label_formula = _cell(formulas, row, base)
        if _text(label_formula).upper().startswith('PRESENT READING'):
            block.link_row = _link_row(_cell(formulas, row, base + 3))
        if block.total_cents is None:
            _classify_line(block, row, _text(_cell(values, row, base + 5)), _cell(values, row, base + 9), dues_cents, texts)
        waiting_for_former = False
        for col in range(base + NOTE_COLUMN_OFFSET, SOA_MAX_COL + 1):
            raw = _cell(formulas, row, col)
            if isinstance(raw, str) and raw.strip() and not raw.lstrip().startswith('='):
                text = _text(raw)
                # The former owner goes to accounts.former_owner, so neither the bare 'FORMER:' label nor
                # the name it introduces is kept as a note too. A different second name still is.
                if waiting_for_former:  # 'FORMER:' in one cell, the name in the next
                    waiting_for_former = False
                    block.former_owner = block.former_owner or text
                    if text == block.former_owner:
                        continue
                former = FORMER_NOTE.search(text)
                if former:
                    name = former[1].strip(' :')
                    waiting_for_former = not name
                    block.former_owner = block.former_owner or name
                    if not name or name == block.former_owner:
                        continue
                texts.append(text)
    block.notes = texts
    block.status_hint = status_from_notes(texts)
    return block


def _link_row(formula):
    match = MRPH_REFERENCE.search(_text(formula))
    if match and match[1].upper() in LINK_COLUMNS:
        return int(match[2])
    return 0


def _classify_line(block, row, label, amount, dues_cents, texts):
    """Sort one SOA line into current charges, carried (opening) amounts, or notes."""
    upper = label.upper()
    cents = to_cents(amount)
    if not upper:
        return
    if 'TOTAL AMOUNT DUE' in upper:
        block.total_cents = cents if cents is not None else 0
        return
    if upper.startswith('TOTAL') or any(upper.startswith(h) for h in HEADER_LABELS):
        return
    if BRACKET_LABEL.match(upper) or upper == 'WATER BILL':
        return  # this month's water, recalculated by the system
    if upper.startswith('MONTHLY DUES'):
        # Only extra months are carried; a smaller or empty dues line is left for the reconciliation
        # report to show, never turned into a credit nobody paid.
        extra = (cents or 0) - dues_cents
        if extra > 0:
            # Dues arrears are paid with the cashier's "monthly dues" box, not with the water bill.
            block.opening.append({'memo': 'Monthly dues - extra months', 'cents': extra, 'row': row, 'purpose': 'dues'})
        return
    if cents:
        block.opening.append({'memo': label, 'cents': cents, 'row': row})
    elif cents is None and not upper.startswith('=') and not re.fullmatch(r'[A-Z]', upper):
        texts.append(label)


def status_from_notes(texts):
    """'disconnected' / 'active' from the newest TD / RECONNECTED note; '' when there is none."""
    latest = {}
    undated = set()
    for text in texts:
        upper = text.upper()
        kind = 'active' if RECONNECT_NOTE.search(upper) else 'disconnected' if DISCONNECT_NOTE.search(upper) else None
        if kind is None:
            continue
        day = parse_day(upper)
        if day:
            latest[kind] = max(latest.get(kind, ''), day)
        else:
            undated.add(kind)
    if latest:
        return max(latest, key=latest.get)
    if undated == {'disconnected'}:
        return 'disconnected'
    return ''
