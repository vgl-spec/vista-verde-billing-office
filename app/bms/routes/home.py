"""Home: the monthly billing cycle as four steps, with the next action marked by a red dot."""
from datetime import datetime

from .. import alerts, billing, db, ui
from ..http import route

STEP_PRINT, STEP_READINGS, STEP_BILLS, STEP_PAYMENTS = 1, 2, 3, 4


@route('GET', '/')
def dashboard(req):
    with db.connect() as c:
        period = billing.current_period(c)
        counts = _load_counts(c, period)
        counts['alerts'] = alerts.count_open(c)
    if counts['accounts'] == 0:
        return _getting_started(req, period)
    current = current_step(counts)
    body = ui.join([
        ui.heading('Home', f'Billing month: {_month_label(period)}'),
        ui.section('This month', ui.steps(_step_items(counts, current))),
        ui.section('At a glance', ui.figures([
            ('Homeowners', f"{counts['accounts']:,}"),
            ('Readings entered', f"{counts['readings']:,} of {counts['accounts']:,}"),
            ('Bills waiting to print', f"{counts['drafts']:,}"),
            ('Open alerts', f"{counts['alerts']:,}"),
        ])),
    ])
    return req.page('Home', body, active='home', dots={'payments'} if current == STEP_PAYMENTS else {'cycle'})


def _getting_started(req, period):
    """With no homeowners the monthly steps are meaningless (a dot on "Print route sheets" for 0 people),
    so point at the one thing to do first: bring the records in."""
    if req.user['role'] == 'admin':
        actions = ui.join([ui.button_link('/import', 'Import the Excel workbook', dot=True),
                           ui.button_link('/accounts/new', 'Add one homeowner by hand', kind='secondary')])
        lead = 'Start by importing the Phase 1D Excel workbook. Homeowners, meter readings and old balances come in together.'
        dots = {'import'}
    else:
        actions = ''
        lead = 'An administrator needs to import the Excel workbook first. Ask them to open Import Excel.'
        dots = None
    body = ui.join([
        ui.heading('Home', f'Billing month: {_month_label(period)}'),
        ui.section('No homeowners yet', ui.join([ui.notice(lead), actions])),
    ])
    return req.page('Home', body, active='home', dots=dots)


def current_step(counts):
    """The step that needs doing now, judged from what already exists for the billing month."""
    if counts['readings'] == 0:
        return STEP_PRINT
    if counts['readings'] < counts['accounts']:
        return STEP_READINGS
    if counts['bills'] < counts['readings'] or counts['drafts'] > 0:
        return STEP_BILLS
    return STEP_PAYMENTS


def _load_counts(c, period):
    return c.one(
        "SELECT (SELECT COUNT(*) FROM accounts WHERE status = 'active') AS accounts, "
        '(SELECT COUNT(*) FROM readings WHERE period = ?) AS readings, '
        "(SELECT COUNT(*) FROM bills WHERE period = ? AND status <> 'cancelled') AS bills, "
        "(SELECT COUNT(*) FROM bills WHERE status = 'draft') AS drafts",
        (period, period))


def _step_items(counts, current):
    descriptions = {
        STEP_PRINT: ('Print route sheets', f"{counts['accounts']:,} homeowners to read", '/cycle/routes'),
        STEP_READINGS: ('Enter readings', f"{counts['readings']:,} of {counts['accounts']:,} entered", '/cycle/readings'),
        STEP_BILLS: ('Make and print bills', f"{counts['bills']:,} made, {counts['drafts']:,} waiting to print", '/cycle/bills'),
        STEP_PAYMENTS: ('Record payments', 'Open the cashier screen', '/payments/cashier'),
    }
    return [{
        'title': title, 'status': status, 'href': href,
        'state': 'done' if number < current else 'current' if number == current else 'todo',
        'dot': number == current,
    } for number, (title, status, href) in descriptions.items()]


def _month_label(period):
    return datetime.strptime(period, '%Y-%m').strftime('%B %Y')
