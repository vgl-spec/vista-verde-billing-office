"""Import Excel: upload the SOA workbook, check what it found, fix or skip each problem, apply once.

The uploaded file is parsed in memory and never saved; only the values read from it are staged.
"""
import hashlib
import io
import math
import zipfile
from datetime import datetime
from urllib.parse import quote

from .. import UserError, billing, db, ui
from ..http import MAX_BODY_UPLOAD, NotFound, Redirect, Response, route
from ..importer import batches, workbook

ADMIN = ('admin',)
PAGE_SIZE = 20
MAX_TYPE_CODES = 100  # the real workbook has ~10; a bound keeps a forged form from looping long
ZIP_MAGIC = b'PK\x03\x04'  # .xlsx / .xlsm are ZIP packages
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
OLD_OR_LOCKED_MESSAGE = ('This file is an old-style .xls or is password-protected. Open it in Excel, choose File → Save As → '
                         'Excel Workbook (*.xlsx), and upload that copy.')
RECONCILE_VIEWS = (
    ('differs', 'Different total'), ('matched', 'Same total'), ('unlinked', 'No statement found'),
    ('cannot_calculate', 'Cannot calculate'), ('no_total', 'Statement has no total'),
)
PARALLEL_VIEWS = (('differs', 'Different'), ('matched', 'Same'), ('not_billed', 'Not billed yet'), ('no_workbook_total', 'No Excel total'))
CATEGORY_OPTIONS = (('residential', 'Residential'), ('commercial', 'Commercial'))
STATUS_OPTIONS = (('active', 'Active'), ('disconnected', 'Disconnected'), ('closed', 'Closed'))
FIX_LABELS = {
    'missing_address': 'Save address', 'unknown_type': 'Save type', 'duplicate_name': 'Keep as a separate homeowner',
    'reading_problem': 'Save readings', 'unlinked_block': 'Match to this account', 'name_changed': 'Use the new name',
}
IMPORT_CRUMB = ('Import Excel', '/import')


# --- upload --------------------------------------------------------------------------------------------

@route('GET', '/import', roles=ADMIN)
def import_page(req):
    return _upload_page(req)


def _upload_page(req, problem='', status=200):
    """The upload form; a refused file comes back here with the reason on top so they can pick again."""
    with db.connect() as c:
        recent = c.all('SELECT id, filename, period, status, uploaded_at FROM import_batches ORDER BY uploaded_at DESC LIMIT 20')
    rows = [[_link(f"/import/{b['id']}", b['filename']), _month(b['period']), b['uploaded_at'][:10],
             ui.pill('Applied', 'success') if b['status'] == 'applied' else ui.pill('Being checked', 'warn')] for b in recent]
    form = ui.form('/import', req.user['csrf'], ui.field('Excel workbook (.xlsx)', 'workbook', type='file', attrs='accept=".xlsx"',
                                                         hint='The monthly SOA workbook, for example "PHASE 1D SOA OCTOBER 2026.xlsx". Up to 25 MB.'),
                   'Upload and check', multipart=True)
    body = ui.join([
        ui.notice(problem, 'danger') if problem else '',
        ui.heading('Import Excel', 'Bring homeowners, readings and old balances in from the SOA workbook.'),
        ui.notice('Nothing changes until you press Apply on the next screen. The file itself is not kept.'),
        form,
        ui.section('Earlier imports', ui.table(['File', 'Billing month', 'Uploaded', 'Status'], rows, empty='No imports yet.')),
    ])
    return req.page('Import Excel', body, active='import', status=status)


@route('POST', '/import', roles=ADMIN, raw=True, max_body=MAX_BODY_UPLOAD)
def upload(req):
    """Raw: parsing takes seconds and must not hold the write lock. The file's SHA-256 is the
    idempotency key: the same file twice (or two clicks) always lands on one batch."""
    upload_file = req.files.get('workbook')
    if upload_file is None or not upload_file.data:
        return _upload_page(req, 'Choose the Excel file first.', 400)
    problem = _workbook_problem(upload_file)
    if problem:
        return _upload_page(req, problem, 400)
    digest = hashlib.sha256(upload_file.data).hexdigest()
    with db.connect() as c:
        existing = batches.find_by_hash(c, digest)
        dues = billing.get_setting(c, 'dues_cents')
    if existing:
        return Response.redirect(f'/import/{existing}', ('info', 'This exact file was uploaded before. Here is that import.'))
    try:
        parsed = workbook.parse(upload_file.data, dues)
    except UserError as error:
        return _upload_page(req, f'{_short_name(upload_file.filename)}: {error}', 400)
    try:
        with db.transaction() as c:
            batch_id = batches.stage(c, parsed, sha256=digest, filename=upload_file.filename, user=req.user)
    except db.IntegrityError:
        with db.connect() as c:
            existing = batches.find_by_hash(c, digest)
        if existing is None:
            raise
        return Response.redirect(f'/import/{existing}', ('info', 'This exact file was uploaded before. Here is that import.'))
    return Response.redirect(f'/import/{batch_id}', ('success', f'Workbook read: {len(parsed.rows)} homeowners found. Check the list below.'))


def _workbook_problem(upload_file):
    """Judge the file by its bytes, not its name: staff rename files, and Excel saves several formats."""
    data = upload_file.data
    if data.startswith(OLE_MAGIC):
        # Both the pre-2007 .xls format and any password-protected workbook are OLE containers.
        return OLD_OR_LOCKED_MESSAGE
    if data.startswith(ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                if 'xl/workbook.xml' in package.namelist():
                    return ''
        except zipfile.BadZipFile:
            pass
    size_kb = max(1, round(len(data) / 1024))
    return f'This file ({_short_name(upload_file.filename)}, {size_kb:,} KB) is not an Excel workbook.'


def _short_name(filename):
    return (filename or 'no name')[:120]


# --- review ----------------------------------------------------------------------------------------------

@route('GET', '/import/{id}', roles=ADMIN)
def review_page(req):
    with db.connect() as c:
        batch = _load(c, req.params['id'])
        counts = batches.issue_counts(c, batch['id'])
        codes = batches.type_codes(c, batch['id'])
        results = batches.reconcile(c, batch['id']) if batch['status'] == 'review' else []
    open_total = sum(open_count for open_count, _ in counts.values())
    summary = batch['summary']
    states = _count_states(results)
    parts = [
        ui.heading(batch['filename'], f"Billing month {_month(batch['period'])}",
                   actions=ui.button_link('/import', 'All imports', kind='secondary'), crumbs=[IMPORT_CRUMB, batch['filename']]),
        ui.figures([('Homeowners found', f"{summary.get('rows', 0):,}"), ('Statements matched', f"{summary.get('linked_blocks', 0):,} of {summary.get('blocks', 0):,}"),
                    ('Things to check', f'{open_total:,}')]),
    ]
    if batch['status'] == 'applied':
        parts.append(ui.notice(f"Applied on {batch['applied_at'][:10]}: {summary.get('created', 0)} homeowners added, {summary.get('updated', 0)} updated, "
                               f"{summary.get('readings', 0)} readings, {summary.get('opening_lines', 0)} old-balance lines, {summary.get('notes', 0)} notes.", 'success',
                               action=ui.button_link(f"/import/{batch['id']}/parallel", 'Compare with Excel')))
    parts.append(ui.section('Things to check', _issue_list(batch, counts), lead='Each one must be fixed or skipped before Apply.'))
    if codes:
        parts.append(ui.section('Type codes found in the workbook', _type_code_form(req, batch, codes),
                                lead='The type column should say R or COMM. These homeowners have something else written there.'))
    if batch['status'] == 'review':
        parts.append(ui.section('Totals compared with Excel', _reconcile_figures(batch, states),
                                lead='Excel TOTAL AMOUNT DUE against old balance + this month\'s water (recalculated) + dues.'))
        parts.append(_apply_section(req, batch, open_total))
    return req.page('Import Excel', ui.join(parts), active='import', dots={'import'} if open_total else None)


def _issue_list(batch, counts):
    if not counts:
        return ui.empty('Nothing to check. Everything in the workbook could be read.')
    rows = []
    for kind, (open_count, total) in counts.items():
        title, _ = batches.ISSUE_KINDS[kind]
        label = ui.join([_link(f"/import/{batch['id']}/issues?kind={kind}", title), ui.dot() if open_count else ''])
        rows.append([label, f'{open_count:,}', f'{total:,}'])
    return ui.table(['What', 'Still to check', 'Found'], rows, numeric_cols=(1, 2))


def _type_code_form(req, batch, codes):
    """One row per code with a Type and a Status choice; one button fixes every homeowner still open."""
    open_codes = [code for code in codes if code['open']] if batch['status'] == 'review' else []
    form_index = {code['code']: index for index, code in enumerate(open_codes)}
    rows = []
    for code in codes:
        label = code['code'] or '(blank)'
        if code['code'] in form_index:
            index = form_index[code['code']]
            cells = [ui.join([ui.dot(), ' ', label, _hidden(f'code_{index}', code['code'])]),
                     ui.select(f'Type for code {label}', f'category_{index}', CATEGORY_OPTIONS, value=code['category'], compact=True),
                     ui.select(f'Status for code {label}', f'status_{index}', STATUS_OPTIONS, value=code['status'], compact=True)]
        else:
            cells = [label, dict(CATEGORY_OPTIONS).get(code['category'], ''), dict(STATUS_OPTIONS).get(code['status'], '')]
        rows.append([cells[0], f"{code['total']:,}", code['guess'], cells[1], cells[2]])
    table = ui.table(['Code', 'Homeowners', 'What it probably means', 'Type', 'Status'], rows, numeric_cols=(1,))
    if not open_codes:
        return table
    waiting = sum(code['open'] for code in open_codes)
    status_line = ui.raw(f'<p>{ui.dot()} {waiting:,} homeowners still need a type and a status.</p>')
    lead = status_line + ui.notice('These are our guesses. Please confirm them with the office before you press the button. '
                     'To treat one homeowner differently, fix that one in "Things to check" first.', 'warn')
    form = ui.form(f"/import/{batch['id']}/type-codes", req.user['csrf'], table, f'Use these for all {waiting:,} homeowners',
                   confirm=f'Yes, use these for all {waiting:,}')
    # .wide lifts the 36rem form cap so the five-column table can use the page width.
    return ui.join([lead, ui.raw('<div class="wide">'), form, ui.raw('</div>')])


def _reconcile_figures(batch, states):
    items = [(label, f'{states.get(key, 0):,}') for key, label in RECONCILE_VIEWS if states.get(key)]
    links = ui.join([ui.button_link(f"/import/{batch['id']}/reconcile?show={key}", f'See: {label}', kind='secondary')
                     for key, label in RECONCILE_VIEWS if states.get(key)])
    return ui.join([ui.figures(items), ui.raw('<div class="form-actions">'), links, ui.raw('</div>')])


def _apply_section(req, batch, open_total):
    if open_total:
        return ui.section('Apply', ui.notice(f'{open_total} things still need checking before you can apply.', 'warn'))
    body = ui.form(f"/import/{batch['id']}/apply", req.user['csrf'], '', 'Apply import', confirm='Yes, apply it now')
    return ui.section('Apply', ui.notice('Applying adds or updates homeowners, readings, old balances and notes. It can only be done once.') + body)


@route('GET', '/import/{id}/issues', roles=ADMIN)
def issues_page(req):
    kind = req.query.get('kind', '')
    if kind not in batches.ISSUE_KINDS:
        raise NotFound('Choose a kind of issue from the import page.')
    with db.connect() as c:
        batch = _load(c, req.params['id'])
        total = c.scalar('SELECT COUNT(*) FROM import_issues WHERE batch_id = ? AND kind = ?', (batch['id'], kind))
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(_int(req.query.get('page'), 1), 1), pages)
        issues = c.all("SELECT * FROM import_issues WHERE batch_id = ? AND kind = ? ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END, message "
                       'LIMIT ? OFFSET ?', (batch['id'], kind, PAGE_SIZE, (page - 1) * PAGE_SIZE))
        open_count = c.scalar("SELECT COUNT(*) FROM import_issues WHERE batch_id = ? AND kind = ? AND state = 'open'", (batch['id'], kind))
    title, skip_label = batches.ISSUE_KINDS[kind]
    editable = batch['status'] == 'review'
    items = [_issue_item(req, batch, issue, skip_label, editable) for issue in issues]
    skip_all = ''
    if editable and open_count > 1:
        skip_all = ui.form(f"/import/{batch['id']}/skip-all", req.user['csrf'], ui.raw(f'<input type="hidden" name="kind" value="{ui.esc(kind)}">'),
                           f'{skip_label}: all {open_count}', danger=True, confirm=f'Yes, {skip_label.lower()} for all {open_count}')
    body = ui.join([
        ui.heading(title, f'{open_count} still to check', actions=ui.button_link(f"/import/{batch['id']}", 'Back to the import', kind='secondary'),
                   crumbs=[IMPORT_CRUMB, _batch_crumb(batch), title]),
        *items, skip_all,
        ui.pagination(page, pages, f"/import/{batch['id']}/issues?kind={quote(kind)}", total=total, noun='item'),
    ])
    return req.page(title, body, active='import', dots={'import'} if open_count else None)


def _issue_item(req, batch, issue, skip_label, editable):
    if issue['state'] != 'open' or not editable:
        state = ui.pill('Fixed', 'success') if issue['state'] == 'fixed' else ui.pill('Skipped', 'neutral') if issue['state'] == 'skipped' else ui.pill('Open', 'warn')
        return ui.raw(f'<div class="block"><p>{ui.esc(issue["message"])} {state}</p></div>')
    action = f"/import/{batch['id']}/issues/{issue['id']}"
    fix = ui.form(action, req.user['csrf'], _hidden('action', 'fix') + _fix_fields(issue), FIX_LABELS[issue['kind']])
    skip = ui.form(action, req.user['csrf'], _hidden('action', 'skip'), skip_label, danger=True)
    return ui.raw(f'<div class="block"><p>{ui.dot()} {ui.esc(issue["message"])}</p>{fix}{skip}</div>')


def _fix_fields(issue):
    kind = issue['kind']
    if kind == 'missing_address':
        return ui.field('Address', 'address')
    if kind == 'unknown_type':
        return ui.select('Type', 'category', CATEGORY_OPTIONS) + ui.select('Status', 'status', STATUS_OPTIONS)
    if kind == 'reading_problem':
        return ui.field('Previous reading', 'previous', attrs='inputmode="decimal"') + ui.field('Present reading', 'present', attrs='inputmode="decimal"')
    if kind == 'unlinked_block':
        return ui.field('Account number', 'account_no', hint='As shown on this import, for example 1D-0042.')
    return ui.raw('')


@route('POST', '/import/{id}/issues/{issue_id}', roles=ADMIN)
def resolve_issue(req, c):
    batch_id = req.params['id']
    action = req.choice('action', ('fix', 'skip'), 'action')
    values = {name: req.text(name, name, limit=200, required=False) for name in ('address', 'category', 'status', 'previous', 'present', 'account_no')}
    kind = c.scalar('SELECT kind FROM import_issues WHERE id = ? AND batch_id = ?', (req.params['issue_id'], batch_id)) or ''
    changed = batches.resolve_issue(c, batch_id, req.params['issue_id'], action, values, req.user)
    target = f'/import/{batch_id}/issues?kind={quote(kind)}' if kind else f'/import/{batch_id}'
    return Redirect(target, ('success', 'Saved.') if changed else ('info', 'That one was already done.'))


@route('POST', '/import/{id}/skip-all', roles=ADMIN)
def skip_all(req, c):
    batch_id = req.params['id']
    kind = req.choice('kind', tuple(batches.ISSUE_KINDS), 'kind of issue')
    count = batches.skip_kind(c, batch_id, kind, req.user)
    return Redirect(f'/import/{batch_id}', ('success', f'{count} skipped.') if count else ('info', 'Nothing was left to skip.'))


@route('POST', '/import/{id}/type-codes', roles=ADMIN)
def map_type_codes(req, c):
    batch_id = req.params['id']
    mapping = {}
    for index in range(MAX_TYPE_CODES + 1):
        if f'code_{index}' not in req.form:
            break
        if index == MAX_TYPE_CODES:
            raise UserError('Too many type codes in one form.')
        code = req.text(f'code_{index}', 'Type code', limit=60, required=False)
        if code in mapping:
            raise UserError('A type code was listed twice. Reload the page and try again.')
        mapping[code] = (req.choice(f'category_{index}', billing.CATEGORIES, 'type'), req.choice(f'status_{index}', batches.STATUSES, 'status'))
    count = batches.map_type_codes(c, batch_id, mapping, req.user)
    return Redirect(f'/import/{batch_id}', ('success', f'Type and status saved for {count:,} homeowners.') if count
                    else ('info', 'Those homeowners were already done.'))


@route('GET', '/import/{id}/reconcile', roles=ADMIN)
def reconcile_page(req):
    show = req.query.get('show', 'differs')
    if show not in dict(RECONCILE_VIEWS):
        show = 'differs'
    with db.connect() as c:
        batch = _load(c, req.params['id'])
        results = [r for r in batches.reconcile(c, batch['id']) if r['state'] == show]
    page, pages, shown = _paginate(req, results)
    rows = [[r['row']['account_no'], r['row']['name'], _money(r['workbook_cents']), _money(r['system_cents']),
             _money(None if r['workbook_cents'] is None or r['system_cents'] is None else r['system_cents'] - r['workbook_cents']),
             f"{r['block']['sheet']} row {r['block']['sheet_row']}" if r['block'] else '—'] for r in shown]
    body = ui.join([
        ui.heading(f'Compared with Excel: {dict(RECONCILE_VIEWS)[show].lower()}', actions=ui.button_link(f"/import/{batch['id']}", 'Back to the import', kind='secondary'),
                   crumbs=[IMPORT_CRUMB, _batch_crumb(batch), 'Compared with Excel']),
        _view_tabs(f"/import/{batch['id']}/reconcile", RECONCILE_VIEWS, show),
        ui.table(['Account', 'Name', 'Excel total', 'System total', 'Difference', 'Statement'], rows, numeric_cols=(2, 3, 4), empty='None.'),
        ui.pagination(page, pages, f"/import/{batch['id']}/reconcile?show={show}", total=len(results), noun='homeowner'),
    ])
    return req.page('Compared with Excel', body, active='import')


@route('POST', '/import/{id}/apply', roles=ADMIN)
def apply_import(req, c):
    batch_id = req.params['id']
    summary = batches.apply(c, batch_id, req.user)
    if summary is None:
        return Redirect(f'/import/{batch_id}', ('info', 'This import was already applied.'))
    return Redirect(f'/import/{batch_id}', ('success', f"Import applied: {summary.get('created', 0)} added, {summary.get('updated', 0)} updated."))


@route('GET', '/import/{id}/parallel', roles=ADMIN)
def parallel_page(req):
    show = req.query.get('show', 'differs')
    if show not in dict(PARALLEL_VIEWS):
        show = 'differs'
    with db.connect() as c:
        batch = _load(c, req.params['id'])
        if batch['status'] != 'applied':
            raise UserError('Apply the import first, then issue this month\'s bills.')
        report = batches.parallel_report(c, batch['id'])
    lines = [line for line in report['lines'] if line['state'] == show]
    page, pages, shown = _paginate(req, lines)
    checklist = ui.table(['Ready to stop using Excel?', ''], [[label, ui.pill('Done', 'success') if done else ui.pill('Not yet', 'warn')]
                                                            for label, done in report['checklist']])
    verdict = (ui.notice('Every check passed. The office can stop keeping the Excel workbook for this phase.', 'success') if report['ready']
               else ui.notice('Keep using Excel side by side until every check below is done.', 'warn'))
    rows = [[line['account_no'], line['name'], _money(line['workbook_cents']), _money(line['system_cents']),
             _money(None if line['workbook_cents'] is None or line['system_cents'] is None else line['system_cents'] - line['workbook_cents'])]
            for line in shown]
    counts = report['counts']
    body = ui.join([
        ui.heading('Side-by-side with Excel', f"Billing month {_month(batch['period'])}", actions=ui.button_link(f"/import/{batch['id']}", 'Back to the import', kind='secondary'),
                   crumbs=[IMPORT_CRUMB, _batch_crumb(batch), 'Side-by-side with Excel']),
        verdict, checklist,
        ui.figures([(label, f'{counts.get(key, 0):,}') for key, label in PARALLEL_VIEWS]),
        _view_tabs(f"/import/{batch['id']}/parallel", PARALLEL_VIEWS, show),
        ui.table(['Account', 'Name', 'Excel total', 'System bill + old balance', 'Difference'], rows, numeric_cols=(2, 3, 4), empty='None.'),
        ui.pagination(page, pages, f"/import/{batch['id']}/parallel?show={show}", total=len(lines), noun='homeowner'),
    ])
    return req.page('Side-by-side with Excel', body, active='import')


# --- helpers --------------------------------------------------------------------------------------------

def _load(c, batch_id):
    batch = batches.load(c, batch_id)
    if batch is None:
        raise NotFound('That import was not found.')
    return batch


def _batch_crumb(batch):
    return (batch['filename'], f"/import/{batch['id']}")


def _count_states(results):
    counts = {}
    for result in results:
        counts[result['state']] = counts.get(result['state'], 0) + 1
    return counts


def _paginate(req, items):
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(max(_int(req.query.get('page'), 1), 1), pages)
    return page, pages, items[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]


def _view_tabs(base, views, show):
    return ui.tabs([{'label': label, 'href': f'{base}?show={key}', 'current': key == show} for key, label in views])


def _hidden(name, value):
    return ui.raw(f'<input type="hidden" name="{ui.esc(name)}" value="{ui.esc(value)}">')


def _money(cents):
    return ui.money(cents) if isinstance(cents, int) else '—'


def _month(period):
    return datetime.strptime(period, '%Y-%m').strftime('%B %Y')


def _link(href, text):
    return ui.raw(f'<a href="{ui.esc(href)}">{ui.esc(text)}</a>')


def _int(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
