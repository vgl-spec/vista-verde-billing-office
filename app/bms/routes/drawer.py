"""Cash drawer: open the day with a counted opening balance, close it with a counted ending balance, tally the
day's earnings against its cash collections; administrators review the days and void a closing that is not what was
really counted. Arithmetic and the cash payment rule live in `bms.cash_drawer`.

Opening and closing are two steps each: the count form, then a review page drawn by the server from the counted
pieces (nothing saved, `ShowPage`), then Confirm. The review's form carries the counts again, so the server counts
them once more on Confirm and never trusts a total sent by the browser.
"""
import math
from urllib.parse import urlencode

from .. import UserError, audit, cash_drawer, db, statements, ui
from ..cash_drawer import DENOMINATIONS, LOOSE_COINS
from ..http import NotFound, Redirect, Response, ShowPage, route
from ..money import parse_pesos
from ..receipts import METHOD_LABELS, receipt_label

DRAWER_ROLES = ('admin', 'staff', 'cashier')
ADMIN_ONLY = ('admin',)
HOME = '/cash-drawer'
DAYS = '/cash-drawer/days'
CASHIER_CRUMB = ('Cashier', '/payments/cashier')
DRAWER_CRUMB = ('Cash drawer', HOME)
DAYS_CRUMB = ('Drawer days', DAYS)
MAX_NOTE = 300
DAYS_PAGE_SIZE = 50
RECENT_DAYS = 7
REMOVED_FIELD = 'removed'
COUNT_FORM = 'data-cash-count'   # app.js setupCashCount totals these forms as the cashier types


# --- today ---------------------------------------------------------------------------------------------

@route('GET', HOME, roles=DRAWER_ROLES)
def drawer_home(req):
    today = db.today()
    with db.connect() as c:
        blocker = cash_drawer.unclosed_before(c, today)
        drawer = cash_drawer.drawer_on(c, today)
        closing = cash_drawer.posted_closing(c, drawer['id']) if drawer and drawer['status'] == 'closed' else None
        summary = cash_drawer.summarize(c, drawer, closing) if drawer else None
        previous_drawer, previous_closing = cash_drawer.last_closed_before(c, today)
        recent = _day_rows(c, c.all(f'{_DAY_LIST_SQL} ORDER BY d.business_date DESC LIMIT ?', (RECENT_DAYS,)))
    parts = [ui.heading('Cash drawer', 'Count the cash when the day starts and when it ends. Only cash goes in the drawer.',
                        crumbs=[CASHIER_CRUMB, 'Cash drawer'])]
    if blocker:
        parts.append(unclosed_notice(blocker))
    if drawer is None:
        if not blocker:
            parts.append(_start_card(today, previous_drawer, previous_closing))
    elif closing is None:
        parts += [ui.figures(open_figures(summary)), ui.raw('<div class="form-actions">'),
                  ui.button_link(f"{HOME}/{drawer['id']}/close", 'Close the day: count the ending cash'),
                  ui.button_link(f"{HOME}/{drawer['id']}", 'Day report', kind='secondary'), ui.raw('</div>')]
    else:
        parts += [ui.notice('Today is closed. Cash payments dated today are no longer accepted.', 'info'),
                  ui.figures(closed_figures(summary)),
                  ui.raw('<div class="form-actions">'), ui.button_link(f"{HOME}/{drawer['id']}", 'Day report'), ui.raw('</div>')]
    parts.append(ui.section('Last days', _day_table(recent, sort=None),
                            lead='Earned today (ending minus opening) must equal the cash collected that day.'))
    return req.page('Cash drawer', ui.join(parts), active='payments', live=('payments',))


# Today's drawer as the cashier pages see it.
NOT_OPENED, BLOCKED, OPEN, CLOSED = 'not opened', 'earlier day not closed', 'open', 'closed'


def cashier_banner(c):
    """The drawer at the top of the cashier pages: (one of NOT_OPENED / BLOCKED / OPEN / CLOSED, markup)."""
    today = db.today()
    blocker = cash_drawer.unclosed_before(c, today)
    drawer = cash_drawer.drawer_on(c, today)
    parts = [unclosed_notice(blocker)] if blocker else []
    if drawer is None:
        if blocker:
            return BLOCKED, ui.join(parts)
        parts.append(ui.notice('Today’s cash drawer is not open yet. Cash payments can be saved after the opening cash '
                               'is counted.', 'warn', action=ui.button_link(HOME, 'Start the day')))
        return NOT_OPENED, ui.join(parts)
    if drawer['status'] == 'closed':
        parts.append(ui.notice('Today’s cash drawer is closed. Cash payments dated today are no longer accepted.', 'info',
                               action=ui.button_link(f"{HOME}/{drawer['id']}", 'Day report', kind='secondary')))
        return CLOSED, ui.join(parts)
    summary = cash_drawer.summarize(c, drawer)
    parts += [ui.figures(open_figures(summary)), ui.raw('<div class="form-actions">'),
              ui.button_link(f"{HOME}/{drawer['id']}/close", 'Close the day', kind='secondary'), ui.raw('</div>')]
    return OPEN, ui.join(parts)


def start_card_for_cashier(c):
    """The first thing the cashier sees before the day is opened: today's date to confirm."""
    today = db.today()
    previous_drawer, previous_closing = cash_drawer.last_closed_before(c, today)
    return _start_card(today, previous_drawer, previous_closing)


def unclosed_notice(blocker):
    return ui.notice(f"The cash drawer for {_day_text(blocker['business_date'])} is not closed. Count it and close it; "
                     'no new day can be opened until it is.', 'danger',
                     action=ui.button_link(f"{HOME}/{blocker['id']}/close", 'Close that day'))


def _start_card(today, previous_drawer, previous_closing):
    figures = []
    if previous_closing:
        when = _day_text(previous_drawer['business_date'])
        left = previous_closing['closing_cents'] - previous_closing['removed_cents']
        figures = [(f'Ending on {when}', str(ui.money(previous_closing['closing_cents'])), '', 'closing'),
                   ('Taken out for the bank', str(ui.money(previous_closing['removed_cents']))),
                   ('Should be in the drawer now', str(ui.money(left)), 'Becomes today’s opening', 'opening')]
    return ui.well(ui.join([
        ui.raw('<p class="muted">Step 1 of 3: check today’s date</p>'),
        ui.raw(f'<p class="drawer-date">{ui.esc(_day_text(today))}</p>'),
        ui.raw('<p>Is this today’s date? If not, the computer’s clock is wrong: tell the administrator before taking '
               'any payment.</p>'),
        ui.figures(figures) if figures else ui.raw(''),
        ui.raw('<div class="form-actions">'),
        ui.button_link(f'{HOME}/open', 'Yes, this is today. Count the opening cash'),
        ui.raw('</div>'),
    ]))


def open_figures(summary):
    return [('Opening balance', str(ui.money(summary.opening_cents)), '', 'opening'),
            ('Cash taken today', str(ui.money(summary.cash_collected_cents))),
            ('Should be in the drawer', str(ui.money(summary.expected_in_drawer_cents)), 'Opening + cash taken today'),
            ('Check, GCash, bank today', str(ui.money(summary.other_collected_cents)), 'Not in the drawer')]


def closed_figures(summary):
    return [('Opening balance', str(ui.money(summary.opening_cents)), '', 'opening'),
            ('Ending balance', str(ui.money(summary.closing_cents)), '', 'closing'),
            ('Earned today', str(ui.money(summary.earned_cents)), 'Ending minus opening'),
            ('Cash collected today', str(ui.money(summary.cash_collected_cents)), 'Collection for the day, cash'),
            _result_figure(summary.difference_cents)]


# --- opening -------------------------------------------------------------------------------------------

@route('GET', f'{HOME}/open', roles=DRAWER_ROLES)
def open_form(req):
    today = db.today()
    with db.connect() as c:
        if cash_drawer.drawer_on(c, today) or cash_drawer.unclosed_before(c, today):
            return Response.redirect(HOME)
        _, previous_closing = cash_drawer.last_closed_before(c, today)
    if any(name in req.query for name in _count_fields()):
        values = {name: req.query.get(name, '') for name in _count_fields()}   # "Count again" from the review page
    elif previous_closing and not previous_closing['removed_cents']:
        # Nothing was taken out, so yesterday's notes and coins are still in the drawer: show them to be checked.
        values = cash_drawer.count_form_values(cash_drawer.load_counts(previous_closing['closing_counts']))
    else:
        values = {}
    expected = (previous_closing['closing_cents'] - previous_closing['removed_cents']) if previous_closing else None
    lead = ('Count every note and coin in the drawer now. ' +
            (f'Yesterday left {ui.money(expected)} in the drawer.' if expected is not None else 'This is the first day.'))
    form = ui.form(f'{HOME}/open', req.user['csrf'], ui.join([
        _hidden({'business_date': today}),
        count_grid(values),
    ]), 'Check the total', secondary=ui.button_link(HOME, 'Cancel', kind='secondary'), attrs=COUNT_FORM)
    body = ui.join([
        ui.heading('Count the opening cash', lead, crumbs=[CASHIER_CRUMB, DRAWER_CRUMB, 'Opening']),
        ui.raw(f'<p class="muted">Step 2 of 3 · {ui.esc(_day_text(today))}</p>'),
        ui.well(form),
    ])
    return req.page('Opening cash', body, active='payments', live=False)


@route('POST', f'{HOME}/open', roles=DRAWER_ROLES)
def open_day(req, c):
    day = req.date('business_date', 'Business date')
    if day != db.today():
        raise UserError('The date changed since this page was opened (the clock passed midnight). '
                        'Go back to Cash drawer and start again.')
    if cash_drawer.drawer_on(c, day):
        return Redirect('/payments/cashier', ('info', 'Today’s cash drawer is already open. Nothing new was saved.'))
    blocker = cash_drawer.unclosed_before(c, day)
    if blocker:
        raise UserError(f"Close the cash drawer for {_day_text(blocker['business_date'])} first.")
    counts = cash_drawer.parse_counts(req.form)
    opening = cash_drawer.count_total(counts)
    previous_drawer, previous_closing = cash_drawer.last_closed_before(c, day)
    expected = (previous_closing['closing_cents'] - previous_closing['removed_cents']) if previous_closing else None
    note = req.text('note', 'Note', limit=MAX_NOTE, required=False)
    needs_note = expected is not None and opening != expected
    if req.form.get('confirmed') != 'yes' or (needs_note and not note):
        raise ShowPage('Check the opening', _open_review(req, day, counts, opening, expected, previous_drawer, note,
                                                         missing_note=req.form.get('confirmed') == 'yes'),
                       active='payments', status=200)
    drawer_id = c.insert('cash_drawers', {
        'business_date': day, 'opening_cents': opening, 'opening_counts': cash_drawer.dump_counts(counts),
        'expected_opening_cents': expected, 'previous_drawer_id': previous_drawer['id'] if previous_drawer else None,
        'opening_note': note, 'opened_by': req.user['id'], 'opened_at': db.now()})
    audit.log(c, req.user, 'cash_drawer.open', 'cash_drawer', drawer_id, None,
              {'business_date': day, 'opening_cents': opening, 'expected_opening_cents': expected,
               'difference_cents': None if expected is None else opening - expected, 'note': note, 'counts': counts})
    return Redirect('/payments/cashier', ('success', f'Cash drawer opened for {_day_text(day)} with {ui.money(opening)}. '
                                                     'You can take payments now.'))


def _open_review(req, day, counts, opening, expected, previous_drawer, note, *, missing_note):
    figures = [('Opening balance (counted)', str(ui.money(opening)), '', 'opening')]
    if expected is not None:
        figures += [(f"Left in the drawer on {_day_text(previous_drawer['business_date'])}", str(ui.money(expected)), '', 'closing'),
                    _difference_figure(opening - expected, 'Counted minus what was left')]
    parts = [ui.heading('Check the opening cash', 'Nothing is saved until you press Confirm.',
                        crumbs=[CASHIER_CRUMB, DRAWER_CRUMB, 'Opening']),
             ui.raw(f'<p class="muted">Step 3 of 3 · {ui.esc(_day_text(day))}</p>')]
    if missing_note:
        parts.append(ui.notice('The count is not what was left in the drawer. Write what happened in the note, then confirm.',
                               'danger'))
    needs_note = expected is not None and opening != expected
    hint = ('Required: the count differs from what was left in the drawer. Say why (for example: change fund added).'
            if needs_note else 'Optional.')
    form = ui.form(f'{HOME}/open', req.user['csrf'], ui.join([
        _hidden({'business_date': day, 'confirmed': 'yes', **cash_drawer.count_form_values(counts)}),
        ui.field('Note', 'note', value=note, required=needs_note, hint=hint, attrs=f'maxlength="{MAX_NOTE}" autocomplete="off"'),
    ]), 'Confirm and open the day', secondary=ui.button_link(f'{HOME}/open?' + urlencode(_count_query(counts)), 'Count again',
                                                             kind='secondary'))
    parts += [ui.figures(figures), counts_table(counts), ui.well(form)]
    return ui.join(parts)


# --- closing -------------------------------------------------------------------------------------------

@route('GET', f'{HOME}/{{id}}/close', roles=DRAWER_ROLES)
def close_form(req):
    with db.connect() as c:
        drawer = _drawer_or_404(c, req.params['id'])
    if drawer['status'] == 'closed':
        return Response.redirect(f"{HOME}/{drawer['id']}")
    values = {name: req.query.get(name, '') for name in _count_fields() + [REMOVED_FIELD]}
    # Blind count: the expected amount is shown only after counting, so the ending is what is really in the drawer.
    form = ui.form(f"{HOME}/{drawer['id']}/close", req.user['csrf'], ui.join([
        count_grid(values),
        ui.field('Cash taken out after counting', REMOVED_FIELD, value=values[REMOVED_FIELD], required=False,
                 hint='For the bank or the treasurer. Leave empty if everything stays in the drawer for tomorrow.',
                 attrs='inputmode="decimal" autocomplete="off"'),
    ]), 'Check the total', secondary=ui.button_link(HOME, 'Cancel', kind='secondary'), attrs=COUNT_FORM)
    body = ui.join([
        ui.heading('Count the ending cash', 'Count every note and coin in the drawer now, before looking at the totals.',
                   crumbs=[CASHIER_CRUMB, DRAWER_CRUMB, 'Closing']),
        ui.raw(f"<p class=\"muted\">Closing {ui.esc(_day_text(drawer['business_date']))}</p>"),
        ui.well(form),
    ])
    return req.page('Ending cash', body, active='payments', live=False)


@route('POST', f'{HOME}/{{id}}/close', roles=DRAWER_ROLES)
def close_day(req, c):
    drawer = _drawer_or_404(c, req.params['id'])
    report = f"{HOME}/{drawer['id']}"
    already_closed = Redirect(report, ('info', 'This day is already closed. Nothing new was saved.'))
    if drawer['status'] == 'closed':
        return already_closed
    counts = cash_drawer.parse_counts(req.form)
    closing = cash_drawer.count_total(counts)
    removed_text = req.form.get(REMOVED_FIELD, '').strip()
    removed = parse_pesos(removed_text) if removed_text else 0
    if removed > closing:
        raise UserError(f'Cash taken out ({ui.money(removed)}) cannot be more than the ending count ({ui.money(closing)}).')
    collected, other = cash_drawer.day_collections(c, drawer['business_date'])
    summary = cash_drawer.DaySummary(drawer['opening_cents'], collected, other, closing, removed, None)
    note = req.text('note', 'Note', limit=MAX_NOTE, required=False)
    needs_note = summary.difference_cents != 0
    if req.form.get('confirmed') != 'yes' or (needs_note and not note):
        raise ShowPage('Check the ending', _close_review(req, drawer, counts, removed, summary, note,
                                                         missing_note=req.form.get('confirmed') == 'yes'),
                       active='payments', status=200)
    # The conditional flip is the guard: a second person closing the same day at the same moment matches nothing here.
    if not c.update('cash_drawers', drawer['id'], {'status': 'closed'}, only_if={'status': 'open'}):
        return already_closed
    closing_id = c.insert('cash_drawer_closings', {
        'drawer_id': drawer['id'], 'status': 'posted', 'closing_cents': closing, 'closing_counts': cash_drawer.dump_counts(counts),
        'cash_collected_cents': collected, 'removed_cents': removed, 'note': note, 'closed_by': req.user['id'],
        'closed_at': db.now()})
    audit.log(c, req.user, 'cash_drawer.close', 'cash_drawer', drawer['id'], None,
              {'closing_id': closing_id, 'business_date': drawer['business_date'], 'opening_cents': drawer['opening_cents'],
               'closing_cents': closing, 'earned_cents': summary.earned_cents, 'cash_collected_cents': collected,
               'difference_cents': summary.difference_cents, 'removed_cents': removed, 'note': note, 'counts': counts})
    return Redirect(report, ('success', f"{_day_text(drawer['business_date'])} closed with {ui.money(closing)}: "
                                        f"{_result_words(summary.difference_cents)}."))


def _close_review(req, drawer, counts, removed, summary, note, *, missing_note):
    needs_note = summary.difference_cents != 0
    parts = [ui.heading('Check the ending cash', 'Nothing is saved until you press Confirm.',
                        crumbs=[CASHIER_CRUMB, DRAWER_CRUMB, 'Closing']),
             ui.raw(f"<p class=\"muted\">Closing {ui.esc(_day_text(drawer['business_date']))}</p>")]
    if missing_note:
        parts.append(ui.notice('Earned today does not match the cash collected. Write what happened in the note, then confirm.',
                               'danger'))
    hint = ('Required: the drawer is short or over. Say why; an administrator reviews every day that does not tally.'
            if needs_note else 'Optional.')
    form = ui.form(f"{HOME}/{drawer['id']}/close", req.user['csrf'], ui.join([
        _hidden({'confirmed': 'yes', REMOVED_FIELD: _pesos_value(removed), **cash_drawer.count_form_values(counts)}),
        ui.field('Note', 'note', value=note, required=needs_note, hint=hint, attrs=f'maxlength="{MAX_NOTE}" autocomplete="off"'),
    ]), 'Confirm and close the day',
        secondary=ui.button_link(f"{HOME}/{drawer['id']}/close?" + urlencode({**_count_query(counts),
                                                                              REMOVED_FIELD: _pesos_value(removed)}),
                                 'Count again', kind='secondary'))
    parts += [ui.figures(closed_figures(summary)),
              ui.facts([('Check, GCash, bank today (not in the drawer)', ui.money(summary.other_collected_cents)),
                        ('Taken out for the bank', ui.money(removed)),
                        ('Left in the drawer for tomorrow', ui.money(summary.left_in_drawer_cents))]),
              counts_table(counts), ui.well(form)]
    return ui.join(parts)


# --- administrator: the days and voiding a closing -------------------------------------------------------

_DAY_LIST_SQL = ('SELECT d.id, d.business_date, d.opening_cents, d.opened_by, k.closing_cents, k.cash_collected_cents, '
                 'k.closed_by, k.id AS closing_id FROM cash_drawers d '
                 "LEFT JOIN cash_drawer_closings k ON k.drawer_id = d.id AND k.status = 'posted'")


@route('GET', DAYS, roles=ADMIN_ONLY)
def drawer_days(req):
    with db.connect() as c:
        total = c.scalar('SELECT COUNT(*) FROM cash_drawers')
        pages = max(1, math.ceil(total / DAYS_PAGE_SIZE))
        page = min(max(_int_or(req.query.get('page'), 1), 1), pages)
        rows = _day_rows(c, c.all(f'{_DAY_LIST_SQL} ORDER BY d.business_date DESC LIMIT ? OFFSET ?',
                                  (DAYS_PAGE_SIZE, (page - 1) * DAYS_PAGE_SIZE)))
    body = ui.join([
        ui.heading('Drawer days', 'Every day’s opening and ending cash, newest first. Open a day to see who counted it '
                                  'and to void a closing that is not what was really counted.',
                   crumbs=[CASHIER_CRUMB, 'Drawer days']),
        _day_table(rows, sort=None),
        ui.pagination(page, pages, f'{DAYS}?', total=total, noun='day'),
    ])
    return req.page('Drawer days', body, active='payments', live=False)


@route('GET', f'{HOME}/{{id}}', roles=DRAWER_ROLES)
def day_report(req):
    with db.connect() as c:
        drawer = _drawer_or_404(c, req.params['id'])
        day = drawer['business_date']
        closings = c.all('SELECT * FROM cash_drawer_closings WHERE drawer_id = ? ORDER BY closed_at', (drawer['id'],))
        standing = next((k for k in closings if k['status'] == 'posted'), None)
        summary = cash_drawer.summarize(c, drawer, standing)
        cash_rows = c.all("SELECT p.id, p.receipt_type, p.receipt_no, p.amount_cents, p.created_at, a.name, "
                          "COALESCE(u.display_name, '') AS cashier FROM payments p JOIN accounts a ON a.id = p.account_id "
                          "LEFT JOIN users u ON u.id = p.created_by WHERE p.status = 'posted' AND p.method = 'cash' "
                          'AND p.paid_on = ? ORDER BY p.created_at', (day,))
        other_rows = c.all("SELECT method, COUNT(*) AS n, SUM(amount_cents) AS total FROM payments WHERE status = 'posted' "
                           "AND method <> 'cash' AND paid_on = ? GROUP BY method ORDER BY method", (day,))
        names = _user_names(c, [drawer['opened_by']] + [k['closed_by'] for k in closings] + [k['voided_by'] for k in closings])
    is_admin = req.user['role'] == 'admin'
    crumbs = [CASHIER_CRUMB, DAYS_CRUMB if is_admin else DRAWER_CRUMB, _day_text(day)]
    parts = [ui.heading(f'Cash drawer, {_day_text(day)}', 'Opening and ending cash, and who counted them.', crumbs=crumbs)]
    if standing is None:
        parts.append(ui.notice('This day is not closed yet.' + (' Its closing was voided: count the drawer and close it again.'
                                                                 if closings else ''), 'warn',
                               action=ui.button_link(f"{HOME}/{drawer['id']}/close", 'Close the day')))
        parts.append(ui.figures(open_figures(summary)))
    else:
        if summary.changed_after_closing_cents:
            parts.append(ui.notice(f'Cash collections for this day changed by {ui.money(summary.changed_after_closing_cents)} '
                                   'after it was closed (a payment voided or recorded later). The figures below use '
                                   'today’s records.', 'warn'))
        parts.append(ui.figures(closed_figures(summary)))
    opening_facts = [('Opened by', f"{names.get(drawer['opened_by'], 'Unknown')}, {_when(drawer['opened_at'])}"),
                     ('Opening balance', ui.money(drawer['opening_cents']))]
    if drawer['expected_opening_cents'] is not None:
        opening_facts += [('Left in the drawer the day before', ui.money(drawer['expected_opening_cents'])),
                          ('Opening difference', _result_words(drawer['opening_cents'] - drawer['expected_opening_cents']))]
    if drawer['opening_note']:
        opening_facts.append(('Opening note', drawer['opening_note']))
    parts.append(ui.section('Opening', ui.facts(opening_facts)))
    if standing:
        parts.append(ui.section('Closing', ui.facts([
            ('Closed by', f"{names.get(standing['closed_by'], 'Unknown')}, {_when(standing['closed_at'])}"),
            ('Ending balance', ui.money(standing['closing_cents'])),
            ('Cash collected when closed', ui.money(standing['cash_collected_cents'])),
            ('Taken out for the bank', ui.money(standing['removed_cents'])),
            ('Left in the drawer for the next day', ui.money(summary.left_in_drawer_cents)),
            ('Closing note', standing['note'] or '—')])))
        if is_admin:
            parts.append(ui.section('Void this closing', ui.form(
                f"{HOME}/closings/{standing['id']}/void", req.user['csrf'],
                ui.field('Reason', 'reason', hint='Required. The day opens again and must be counted and closed again. '
                                                  'This closing stays in the history below.',
                         attrs=f'maxlength="{MAX_NOTE}" autocomplete="off"'),
                'Void closing', danger=True,
                confirm='Void this closing? The day opens again, and the drawer must be counted and closed again.')))
    parts.append(ui.section('Notes and coins', counts_table(
        cash_drawer.load_counts(drawer['opening_counts']),
        cash_drawer.load_counts(standing['closing_counts']) if standing else None)))
    parts.append(ui.section('Cash collected this day', ui.table(
        ['Time (UTC)', 'Receipt', 'Homeowner', 'Recorded by', 'Amount'],
        [[_when(r['created_at']), ui.raw(f'<a href="/payments/{ui.esc(r["id"])}">{ui.esc(receipt_label(r))}</a>'), r['name'],
          r['cashier'] or '—', ui.money(r['amount_cents'])] for r in cash_rows],
        empty='No cash payments on this day.', numeric_cols=(4,),
        foot=['Total', '', '', '', ui.money(summary.cash_collected_cents)] if cash_rows else None),
        lead='Collection for the day, cash only: this is what Earned today must equal.'))
    if other_rows:
        parts.append(ui.section('Not in the drawer', ui.table(
            ['Paid by', 'Payments', 'Amount'],
            [[METHOD_LABELS.get(r['method'], r['method']), str(r['n']), ui.money(r['total'])] for r in other_rows],
            numeric_cols=(1, 2)), lead='Check, GCash and bank payments are collections too, but they never enter the drawer.'))
    if closings:
        parts.append(ui.section('Closing history', ui.table(
            ['Closed', 'By', 'Ending', 'Result then', 'Status', 'Voided by', 'Reason'],
            [[_when(k['closed_at']), names.get(k['closed_by'], 'Unknown'), ui.money(k['closing_cents']),
              _result_words(k['closing_cents'] - drawer['opening_cents'] - k['cash_collected_cents']),
              ui.pill('Voided', 'neutral') if k['status'] == 'voided' else ui.pill('Standing', 'success'),
              f"{names.get(k['voided_by'], 'Unknown')}, {_when(k['voided_at'])}" if k['status'] == 'voided' else '—',
              k['void_reason'] or '—'] for k in closings], numeric_cols=(2,)),
            lead='Every closing ever saved for this day, including voided ones.'))
    return req.page('Cash drawer day', ui.join(parts), active='payments', live=('payments',))


@route('POST', f'{HOME}/closings/{{id}}/void', roles=ADMIN_ONLY)
def void_closing(req, c):
    closing = c.one('SELECT k.*, d.business_date FROM cash_drawer_closings k JOIN cash_drawers d ON d.id = k.drawer_id '
                    'WHERE k.id = ?', (req.params['id'],))
    if closing is None:
        raise NotFound('That closing was not found.')
    reason = req.text('reason', 'Reason', limit=MAX_NOTE)
    report = f"{HOME}/{closing['drawer_id']}"
    if not c.transition('cash_drawer_closings', closing['id'], 'status', 'posted', 'voided',
                        {'void_reason': reason, 'voided_by': req.user['id'], 'voided_at': db.now()}):
        return Redirect(report, ('info', 'This closing was already voided.'))
    c.update('cash_drawers', closing['drawer_id'], {'status': 'open'}, only_if={'status': 'closed'})
    audit.log(c, req.user, 'cash_drawer.void_closing', 'cash_drawer', closing['drawer_id'],
              {'closing_id': closing['id'], 'status': 'posted', 'closing_cents': closing['closing_cents']},
              {'closing_id': closing['id'], 'status': 'voided', 'reason': reason})
    return Redirect(report, ('success', f"The closing of {_day_text(closing['business_date'])} was voided. The day is open "
                                        'again: the cashier counts the drawer and closes it again.'))


# --- shared pieces ---------------------------------------------------------------------------------------

def count_grid(values):
    """The count form's grid: pieces per note and coin, the line amount, the running total (app.js setupCashCount)."""
    rows, total, valid = [], 0, True
    for denomination in DENOMINATIONS:
        name = cash_drawer.field_name(denomination)
        text = str(values.get(name, '')).strip()
        pieces = int(text) if text.isdigit() and int(text) <= cash_drawer.MAX_PIECES else (0 if text == '' else None)
        valid = valid and pieces is not None
        total += (pieces or 0) * denomination
        label = cash_drawer.denomination_label(denomination)
        rows.append([label, ui.raw(f'<input class="grid-input" type="text" inputmode="numeric" name="{name}" '
                                   f'value="{ui.esc(text)}" maxlength="6" autocomplete="off" data-grid="cash" '
                                   f'data-cash-pieces="{denomination}" aria-label="{ui.esc(label)} pieces">'),
                     ui.raw(f'<span data-cash-line>{ui.money((pieces or 0) * denomination)}</span>')])
    coins_text = str(values.get(LOOSE_COINS, '')).strip()
    rows.append(['Coins below ₱1 (total amount)', ui.raw(
        f'<input class="grid-input" type="text" inputmode="decimal" name="{LOOSE_COINS}" value="{ui.esc(coins_text)}" '
        'maxlength="10" autocomplete="off" data-grid="cash" data-cash-coins aria-label="Coins below one peso, total amount">'),
        ''])
    shown_total = str(ui.money(total)) if valid and not coins_text else 'Shown after Check the total'
    return ui.table(['Note or coin', 'How many', 'Amount'], rows, numeric_cols=(2,), fixed_order=True,
                    foot=['Total', '', ui.raw(f'<output class="cash-total" data-cash-total>{ui.esc(shown_total)}</output>')])


def counts_table(opening, closing=None):
    """Pieces per note and coin: one count, or the opening beside the ending."""
    headers = ['Note or coin', 'Pieces', 'Amount'] if closing is None else ['Note or coin', 'Opening', 'Ending']
    rows = []
    for denomination in DENOMINATIONS:
        key = str(denomination)
        if closing is None:
            rows.append([cash_drawer.denomination_label(denomination), str(opening[key]), ui.money(opening[key] * denomination)])
        else:
            rows.append([cash_drawer.denomination_label(denomination), f'{opening[key]} pcs · {ui.money(opening[key] * denomination)}',
                         f'{closing[key]} pcs · {ui.money(closing[key] * denomination)}'])
    if closing is None:
        rows.append(['Coins below ₱1', '', ui.money(opening[LOOSE_COINS])])
        foot = ['Total', '', ui.money(cash_drawer.count_total(opening))]
    else:
        rows.append(['Coins below ₱1', ui.money(opening[LOOSE_COINS]), ui.money(closing[LOOSE_COINS])])
        foot = ['Total', ui.money(cash_drawer.count_total(opening)), ui.money(cash_drawer.count_total(closing))]
    return ui.table(headers, rows, numeric_cols=(1, 2), fixed_order=True, foot=foot)


def _day_rows(c, drawers):
    """The list rows for these days, with each day's live cash collections in one query (no query per day)."""
    days = [d['business_date'] for d in drawers]
    collected = {}
    if days:
        marks = ', '.join('?' for _ in days)
        collected = {r['paid_on']: r['total'] for r in c.all(
            "SELECT paid_on, SUM(amount_cents) AS total FROM payments WHERE status = 'posted' AND method = 'cash' "
            f'AND paid_on IN ({marks}) GROUP BY paid_on', tuple(days))}
    names = _user_names(c, [d['opened_by'] for d in drawers] + [d['closed_by'] for d in drawers])
    return [{**d, 'cash_now': collected.get(d['business_date'], 0), 'opened_name': names.get(d['opened_by'], 'Unknown'),
             'closed_name': names.get(d['closed_by'], '')} for d in drawers]


def _day_table(rows, sort):
    table_rows = []
    for r in rows:
        closed = r['closing_cents'] is not None
        earned = r['closing_cents'] - r['opening_cents'] if closed else None
        result = (ui.pill(_result_words(earned - r['cash_now']), _result_kind(earned - r['cash_now'])) if closed
                  else ui.pill('Open', 'info'))
        table_rows.append([ui.raw(f'<a href="{HOME}/{ui.esc(r["id"])}">{ui.esc(_day_text(r["business_date"]))}</a>'),
                           ui.money(r['opening_cents']), ui.money(r['closing_cents']) if closed else '—',
                           ui.money(earned) if closed else '—', ui.money(r['cash_now']), result,
                           r['opened_name'], r['closed_name'] or '—'])
    return ui.table(['Day', 'Opening', 'Ending', 'Earned', 'Cash collected', 'Result', 'Opened by', 'Closed by'], table_rows,
                    empty='No day has been opened yet.', numeric_cols=(1, 2, 3, 4), sort=sort,
                    keys=[r['id'] for r in rows])


def _result_figure(difference):
    return ('Result', _result_words(difference), 'Earned today minus cash collected', _result_kind(difference, figure=True))


def _difference_figure(difference, note):
    return ('Difference', _result_words(difference), note, _result_kind(difference, figure=True))


def _result_words(difference):
    if difference == 0:
        return 'Tally'
    return f'Short {ui.money(-difference)}' if difference < 0 else f'Over {ui.money(difference)}'


def _result_kind(difference, *, figure=False):
    if difference == 0:
        return 'good' if figure else 'success'
    if difference < 0:
        return 'bad' if figure else 'danger'
    return 'warn'


def _drawer_or_404(c, drawer_id):
    drawer = cash_drawer.load_drawer(c, drawer_id)
    if drawer is None:
        raise NotFound('That cash drawer day was not found.')
    return drawer


def _user_names(c, user_ids):
    ids = sorted({i for i in user_ids if i})
    if not ids:
        return {}
    marks = ', '.join('?' for _ in ids)
    return {r['id']: r['display_name'] for r in c.all(f'SELECT id, display_name FROM users WHERE id IN ({marks})', tuple(ids))}


def _hidden(values):
    return ui.raw(''.join(f'<input type="hidden" name="{ui.esc(k)}" value="{ui.esc(v)}">' for k, v in values.items()))


def _count_fields():
    return [cash_drawer.field_name(d) for d in DENOMINATIONS] + [LOOSE_COINS]


def _count_query(counts):
    return {k: v for k, v in cash_drawer.count_form_values(counts).items() if v}


def _pesos_value(cents):
    return f'{cents // 100}.{cents % 100:02d}' if cents else ''


def _day_text(iso_day):
    return statements.weekday_date(iso_day)


def _when(timestamp):
    return (timestamp or '')[:16].replace('T', ' ') + ' UTC' if timestamp else '—'


def _int_or(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
