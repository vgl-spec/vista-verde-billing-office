"""Admin: office settings, users, rate plans, database backup, sync setup; plus the public health
check and the hub's sync API (`/sync/push`, `/sync/pull`, bearer token per office computer)."""
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .. import UserError, alerts, audit, auth, billing, db, editing, exports, receipts, statements, sync, ui
from ..http import HttpError, NotFound, Redirect, Response, ShowPage, _is_uuid, route
from ..money import format_pesos, format_reading, parse_pesos
from ..statements import SETTING_DEFAULTS
from . import cycle
from .payments import receipt_document

ADMIN = ('admin',)
ROLE_OPTIONS = (('staff', 'Office staff'), ('cashier', 'Cashier'), ('admin', 'Administrator'))
YES_NO = (('no', 'Not yet'), ('yes', 'Yes'))
DAY_FIELDS = (('issue_day', 'Day bills are issued'), ('due_day', 'Day bills are due'), ('penalty_day', 'First day of penalty'))
TEXT_SETTINGS = (
    ('association_name', 'Association name', 120, False),
    ('association_address', 'Association address', 200, False),
    ('association_contact', 'Contact number or email', 120, False),
    ('penalty_note', 'Penalty note printed on statements (leave empty for the standard wording)', 400, True),
    ('disconnection_note', 'Disconnection note printed on statements', 400, True),
    ('payment_instructions', 'How to pay (printed on statements)', 400, True),
)
RATE_PLAN_FIELDS = tuple(
    [(f'{category}_min_{index}', category, 'minimums', index, f'{label} (flat amount)')
     for category in billing.CATEGORIES for index, label in enumerate(billing.MINIMUM_LABELS)]
    + [(f'{category}_step_{index}', category, 'steps', index, f'{label} (per m³)')
       for category in billing.CATEGORIES for index, label in enumerate(billing.STEP_LABELS)])
AFTER_PATTERN = re.compile(r'\d{1,18}')
SYNC_FAILURE_PREFIX = 'sync:'
BACKUP_ACTION = 'POST /admin/backup'
NODE_ACTION = 'POST /admin/sync/nodes'


def _tabs(current):
    items = [('settings', '/admin', 'Settings'), ('users', '/admin/users', 'Users'), ('rates', '/admin/rates', 'Water rates'),
             ('maynilad', '/admin/maynilad', 'Maynilad bill'), ('receipts', '/admin/receipts', 'Receipt printing'),
             ('backup', '/admin/backup', 'Backup'), ('sync', '/admin/sync', 'Sync')]
    return ui.tabs([{'label': label, 'href': href, 'current': key == current} for key, href, label in items])


def _page(req, title, body, tab, lead='', parent=None):
    """Admin tab page. Settings is the Admin home; every other page gets a trail back to it.

    `parent` is a (label, href) between Admin and this page, e.g. Water rates for New water rates.
    """
    trail = () if tab == 'settings' else [('Admin', '/admin'), *([parent] if parent else []), title]
    return req.page(title, ui.join([ui.heading(title, lead, crumbs=trail), _tabs(tab), body]), active='admin')


# --- health ------------------------------------------------------------------------------------------

@route('GET', '/healthz', public=True)
def healthz(req):
    try:
        with db.connect() as c:
            c.scalar('SELECT 1')
    except Exception:  # noqa: BLE001 - a health probe reports, it never explains
        return Response(503, b'unavailable', 'text/plain; charset=utf-8')
    return Response(200, b'ok', 'text/plain; charset=utf-8')


# --- settings ------------------------------------------------------------------------------------------

def _settings(c):
    stored = {row['key']: json.loads(row['value']) for row in c.all('SELECT key, value FROM settings')}
    merged = {**SETTING_DEFAULTS, **stored}
    merged.setdefault('policy_confirmed_by', '')
    merged.setdefault('policy_confirmed_at', '')
    return merged


# The settings the Settings form saves, as its fields show them (raw form text): the merge works on these.
SETTINGS_LOCK = ('settings', 'office')


def _settings_text(values):
    text = {key: str(values[key] or '') for key, _, _, _ in TEXT_SETTINGS}
    text.update({
        'dues': format_pesos(int(values['dues_cents'])), 'penalty_rate_percent': str(values['penalty_rate_percent']),
        'show_penalty_note': 'yes' if values['show_penalty_note'] else '',
        'show_disconnection_note': 'yes' if values['show_disconnection_note'] else '',
        'dues_on_water_bill': 'yes' if values['dues_on_water_bill'] else '',
        'maynilad_rate_per_m3': str(values['maynilad_rate_per_m3']),
        'disconnect_after_months': str(values['disconnect_after_months']),
        'policy_confirmed': 'yes' if values['policy_confirmed'] else 'no',
    })
    text.update({key: str(values[key]) for key, _ in DAY_FIELDS})
    return text


SETTINGS_LABELS = {**{key: label for key, label, _, _ in TEXT_SETTINGS}, **dict(DAY_FIELDS),
                   'dues': 'Association dues per month', 'penalty_rate_percent': 'Penalty rate',
                   'show_penalty_note': 'Print the penalty note', 'show_disconnection_note': 'Print the disconnection notice',
                   'dues_on_water_bill': 'Add monthly dues to the water bill', 'maynilad_rate_per_m3': 'Maynilad bulk water rate',
                   'disconnect_after_months': 'Months unpaid before disconnection', 'policy_confirmed': 'Billing rules confirmed'}


# How each number on the form is read, so "150" and "₱150.00" are the same setting during a merge.
SETTINGS_CANONICAL = {'dues': parse_pesos, 'maynilad_rate_per_m3': parse_pesos, 'penalty_rate_percent': int,
                      'disconnect_after_months': int, **{key: int for key, _ in DAY_FIELDS}}


def _settings_version(c):
    """One number for the whole Settings form: versions only rise, so any change to any setting raises the sum."""
    return c.scalar('SELECT COALESCE(SUM(version), 0) FROM settings') or 0


@route('GET', '/admin', roles=ADMIN)
def settings_page(req):
    with db.connect() as c:
        values = _settings(c)
        version = _settings_version(c)
        confirmer = c.scalar('SELECT display_name FROM users WHERE id = ?', (values['policy_confirmed_by'],)) if values['policy_confirmed_by'] else ''
    holder = editing.acquire(*SETTINGS_LOCK, req.user)
    confirmed = bool(values['policy_confirmed'])
    status = (ui.notice(f"The office confirmed these billing rules ({confirmer or 'unknown user'}, {values['policy_confirmed_at'][:10]}).", 'success')
              if confirmed else ui.notice('The billing rules below still need the office to confirm them.', 'warn'))
    fields = ui.join([
        *[ui.field(label, key, type='textarea' if multiline else 'text', value=values[key], required=False)
          for key, label, _, multiline in TEXT_SETTINGS[:3]],
        ui.field('Association dues per month', 'dues', value=format_pesos(int(values['dues_cents'])), attrs='inputmode="decimal"'),
        ui.field('Penalty rate (% per month)', 'penalty_rate_percent', value=values['penalty_rate_percent'], attrs='inputmode="numeric"'),
        ui.checkbox('Print the penalty note on statements', 'show_penalty_note', checked=bool(values['show_penalty_note']),
                    hint='The usual choice for every new bill. Each draft bill can still tick or untick it.'),
        ui.checkbox('Print the disconnection notice on statements', 'show_disconnection_note',
                    checked=bool(values['show_disconnection_note']),
                    hint='The usual choice for every new bill. Each draft bill can still tick or untick it.'),
        ui.checkbox('Add monthly dues to the water bill', 'dues_on_water_bill', checked=bool(values['dues_on_water_bill']),
                    hint='Leave unticked while monthly dues are billed separately with their own BIR invoice. Applies to bills made from now on.'),
        *[ui.field(label, key, value=values[key], hint='A day of the month, 1 to 28.', attrs='inputmode="numeric"') for key, label in DAY_FIELDS],
        ui.field('Maynilad bulk water rate (per m³)', 'maynilad_rate_per_m3', value=values['maynilad_rate_per_m3'], attrs='inputmode="decimal"'),
        ui.field('Months unpaid before disconnection', 'disconnect_after_months', value=values['disconnect_after_months'], attrs='inputmode="numeric"'),
        *[ui.field(label, key, type='textarea', value=values[key], required=False) for key, label, _, multiline in TEXT_SETTINGS[3:]],
        ui.select('Has the office confirmed these billing rules?', 'policy_confirmed', YES_NO, value='yes' if confirmed else 'no',
                  hint='Penalty, dues, due dates and water rates. Bills show a warning until this is Yes.'),
    ])
    if holder:
        banner = editing.lock_banner(holder, *SETTINGS_LOCK, can_take_over=True, csrf=req.user['csrf'], back='/admin')
        body = status + banner + ui.raw('<fieldset class="locked-fields" disabled>') + fields + ui.raw('</fieldset>')
        return _page(req, 'Settings', body, 'settings', 'Office details and billing rules.')
    fields = ui.join([fields, editing.version_fields(version, _settings_text(values)), editing.lock_marker(*SETTINGS_LOCK)])
    body = status + ui.form('/admin/settings', req.user['csrf'], fields, 'Save settings')
    return _page(req, 'Settings', body, 'settings', 'Office details and billing rules.')


@route('POST', '/admin/settings', roles=ADMIN)
def save_settings(req, c):
    current = _settings(c)
    other = editing.holder_other_than(c, *SETTINGS_LOCK, req.user)
    if other:
        raise UserError(f"{other['user_name']} is changing the settings now, so yours were not saved. Open Settings again when they are done.")
    version = editing.posted_version(req)
    if version is None:
        raise UserError('This page is out of date. Open Settings again, then make your change.')
    kept, who = [], ''
    if version != _settings_version(c):  # someone saved settings since this page was drawn
        conflicts, kept = editing.merge_form(req, _settings_text(current), SETTINGS_LABELS, SETTINGS_CANONICAL)
        who, when = editing.last_change(c, 'settings', 'office')
        if conflicts:
            raise ShowPage('Settings', editing.resolver_page(
                req, title='Settings: choose what to keep', crumbs=[('Admin', '/admin'), 'Choose what to keep'],
                action='/admin/settings', labels=SETTINGS_LABELS, conflicts=conflicts, saved_text=_settings_text(current),
                version=_settings_version(c), who=who, when=when, cancel='/admin'), active='admin')
    editing.release(c, *SETTINGS_LOCK, req.user)
    new = {key: req.text(key, label, limit=limit, required=False, multiline=multiline) for key, label, limit, multiline in TEXT_SETTINGS}
    new['dues_cents'] = req.pesos('dues', 'Association dues')
    new['penalty_rate_percent'] = _whole(req, 'penalty_rate_percent', 'Penalty rate', 0, 100)
    for key in ('show_penalty_note', 'show_disconnection_note', 'dues_on_water_bill'):
        new[key] = req.checkbox(key)
    for key, label in DAY_FIELDS:
        new[key] = _whole(req, key, label, 1, 28)
    new['maynilad_rate_per_m3'] = _rate_text(req.text('maynilad_rate_per_m3', 'Maynilad rate', limit=16))
    new['disconnect_after_months'] = _whole(req, 'disconnect_after_months', 'Months before disconnection', 1, 12)
    confirmed = req.choice('policy_confirmed', [value for value, _ in YES_NO], 'answer') == 'yes'
    new['policy_confirmed'] = confirmed
    changes = {key: value for key, value in new.items() if current.get(key) != value}
    if confirmed and not current['policy_confirmed']:
        changes.update(policy_confirmed_by=req.user['id'], policy_confirmed_at=db.now())
    if not changes:
        return Redirect('/admin', ('info', 'Nothing was changed.' + editing.kept_note(kept, SETTINGS_LABELS, who)))
    for key, value in changes.items():
        billing.set_setting(c, key, value, req.user)
    audit.log(c, req.user, 'settings.update', 'settings', 'office', {key: current.get(key) for key in changes}, changes)
    if confirmed:
        alerts.resolve_alert(c, 'policy_unconfirmed', 'billing-policy', req.user)
    else:
        alerts.raise_alert(c, 'policy_unconfirmed', 'billing-policy',
                           'Billing rules still need the office to confirm them (penalty, dues, rates).', '/admin', 'warn')
    return Redirect('/admin', ('success', 'Settings saved.' + editing.kept_note(kept, SETTINGS_LABELS, who)))


def _whole(req, name, label, low, high):
    text = req.text(name, label, limit=4)
    if not text.isdigit() or not low <= int(text) <= high:
        raise UserError(f'{label} must be a whole number from {low} to {high}.')
    return int(text)


def _rate_text(text):
    try:
        cents = parse_pesos(text)
    except UserError:
        raise UserError('Enter the Maynilad rate like 56.68.') from None
    return f'{Decimal(cents) / 100:.2f}'


# --- receipt printing ------------------------------------------------------------------------------------

NUMBER_PATTERN = re.compile(r'-?\d{1,3}(\.\d{1,2})?')


@route('GET', '/admin/receipts', roles=ADMIN)
def receipt_printing_page(req):
    with db.connect() as c:
        values = {key: receipts.calibration(c, key) for key in receipts.FORMS}
    sections = []
    for key in receipts.FORMS:
        form = receipts.FORMS[key]
        fields = ui.join([ui.field(label, name, value=f'{values[key][name]:g}', attrs='inputmode="decimal"')
                          for name, label, _, _ in receipts.CALIBRATION_FIELDS])
        test = ui.button_link(f'/admin/receipts/{key}/test', 'Print a test page', kind='secondary')
        sections.append(ui.raw(f'<section class="section" id="{key}">') + ui.section(
            form.name, ui.form(f'/admin/receipts/{key}', req.user['csrf'], fields, 'Save', secondary=test),
            lead='1. Print a test page on plain paper. 2. Hold it over a real receipt against the light. '
                 '3. If the details sit too far left, put a positive number in "Move everything right"; too high, a positive '
                 'number in "Move everything down". Save and test again.') + ui.raw('</section>'))
    lead = 'Only the details are printed onto the pre-printed Official Receipt. Adjust here once per printer.'
    return _page(req, 'Receipt printing', ui.join(sections), 'receipts', lead)


@route('POST', '/admin/receipts/{type}', roles=ADMIN)
def save_receipt_printing(req, c):
    form_key = req.params['type']
    if form_key not in receipts.FORMS:
        raise NotFound('No such receipt form.')
    before = receipts.calibration(c, form_key)
    new = {}
    for name, label, low, high in receipts.CALIBRATION_FIELDS:
        text = req.text(name, label, limit=7)
        if not NUMBER_PATTERN.fullmatch(text) or not low <= float(text) <= high:
            raise UserError(f'{label} must be a number from {low} to {high}.')
        new[name] = float(text)
    billing.set_setting(c, f'receipt_form_{form_key}', new, req.user)   # a repeat press stores the same values
    audit.log(c, req.user, 'settings.receipt_form', 'setting', f'receipt_form_{form_key}', before, new)
    return Redirect(f'/admin/receipts#{form_key}', ('success', 'Saved. Print a test page to check.'))


@route('GET', '/admin/receipts/{type}/test', roles=ADMIN)
def receipt_test_page(req):
    form_key = req.params['type']
    if form_key not in receipts.FORMS:
        raise NotFound('No such receipt form.')
    with db.connect() as c:
        values = receipts.calibration(c, form_key)
    crumbs = [('Admin', '/admin'), ('Receipt printing', '/admin/receipts'), 'Test page']
    page = receipt_document(req, 'Receipt test page', crumbs, receipts.sample_model(), values, '', test=True)
    return Response(200, page.encode('utf-8'))


# --- users -----------------------------------------------------------------------------------------------

@route('GET', '/admin/users', roles=ADMIN)
def users_page(req):
    with db.connect() as c:
        users = c.all('SELECT id, username, display_name, role, active, must_change_password FROM users ORDER BY active DESC, username')
    rows = [[_link(f"/admin/users/{u['id']}", u['username']), u['display_name'], dict(ROLE_OPTIONS)[u['role']],
             ui.pill('Active', 'success') if u['active'] else ui.pill('Turned off', 'neutral'),
             'Must choose a new password' if u['must_change_password'] else '—'] for u in users]
    fields = ui.join([
        ui.field('Username', 'username', hint='Letters and numbers, for example: maria', attrs='autocomplete="off" autocapitalize="none"'),
        ui.field('Full name', 'display_name', required=False),
        ui.select('Role', 'role', ROLE_OPTIONS, value='staff'),
        ui.field('Temporary password', 'password', type='password', hint=f'At least {auth.PASSWORD_MIN_LENGTH} characters. They must change it when they first sign in.',
                 attrs='autocomplete="new-password"'),
    ])
    body = ui.join([
        ui.section('People who can sign in', ui.table(['Username', 'Name', 'Role', 'Status', 'Password'], rows)),
        ui.section('Add a person', ui.form('/admin/users/new', req.user['csrf'], fields, 'Add person')),
    ])
    return _page(req, 'Users', body, 'users')


@route('POST', '/admin/users/new', roles=ADMIN)
def create_user(req, c):
    role = req.choice('role', [value for value, _ in ROLE_OPTIONS], 'role')
    user_id = auth.create_user(c, req.form.get('username', ''), req.text('display_name', 'Full name', limit=80, required=False),
                               req.form.get('password', ''), role, actor=req.user, must_change_password=True)
    return Redirect(f'/admin/users/{user_id}', ('success', 'Person added. Give them the temporary password in person.'))


@route('GET', '/admin/users/{id}', roles=ADMIN)
def user_page(req):
    with db.connect() as c:
        user = _load_user(c, req.params['id'])
    base = f"/admin/users/{user['id']}"
    toggle_label = 'Turn off this account' if user['active'] else 'Turn this account back on'
    body = ui.join([
        ui.facts([('Username', user['username']), ('Name', user['display_name']), ('Role', dict(ROLE_OPTIONS)[user['role']]),
                  ('Status', 'Active' if user['active'] else 'Turned off')]),
        ui.section('Change role', ui.form(f'{base}/role', req.user['csrf'], ui.select('Role', 'role', ROLE_OPTIONS, value=user['role']), 'Save role')),
        ui.section('Reset password', ui.form(f'{base}/reset', req.user['csrf'], ui.field(
            'New temporary password', 'password', type='password', hint=f'At least {auth.PASSWORD_MIN_LENGTH} characters. They must change it at their next sign in.',
            attrs='autocomplete="new-password"'), 'Reset password')),
        ui.section(toggle_label, ui.form(f'{base}/active', req.user['csrf'], '', toggle_label, danger=bool(user['active']),
                                        confirm='Yes, turn it off' if user['active'] else '')),
    ])
    name = user['display_name'] or user['username']
    crumbs = [('Admin', '/admin'), ('Users', '/admin/users'), name]
    heading = ui.heading(name, actions=ui.button_link('/admin/users', 'All users', kind='secondary'), crumbs=crumbs)
    return req.page(user['username'], ui.join([heading, body]), active='admin')


@route('POST', '/admin/users/{id}/role', roles=ADMIN)
def change_role(req, c):
    user = _load_user(c, req.params['id'])
    role = req.choice('role', [value for value, _ in ROLE_OPTIONS], 'role')
    target = f"/admin/users/{user['id']}"
    if role == user['role']:
        return Redirect(target, ('info', 'Nothing was changed.'))
    if user['role'] == 'admin':
        _require_another_admin(c, user['id'])
    c.execute('UPDATE users SET role = ? WHERE id = ?', (role, user['id']))
    audit.log(c, req.user, 'user.role', 'user', user['id'], {'role': user['role']}, {'role': role})
    return Redirect(target, ('success', 'Role changed.'))


@route('POST', '/admin/users/{id}/active', roles=ADMIN)
def toggle_active(req, c):
    user = _load_user(c, req.params['id'])
    target = f"/admin/users/{user['id']}"
    if user['active']:
        if user['role'] == 'admin':
            _require_another_admin(c, user['id'])
        c.execute('UPDATE users SET active = 0 WHERE id = ? AND active = 1', (user['id'],))
        auth.destroy_user_sessions(c, user['id'])
        audit.log(c, req.user, 'user.deactivate', 'user', user['id'])
        return Redirect(target, ('success', 'Account turned off. That person is signed out everywhere.'))
    c.execute('UPDATE users SET active = 1 WHERE id = ? AND active = 0', (user['id'],))
    audit.log(c, req.user, 'user.activate', 'user', user['id'])
    return Redirect(target, ('success', 'Account turned back on.'))


@route('POST', '/admin/users/{id}/reset', roles=ADMIN)
def reset_password(req, c):
    user = _load_user(c, req.params['id'])
    auth.set_password(c, user['id'], req.form.get('password', ''), actor=req.user)
    auth.require_password_change(c, user['id'], req.user)
    return Redirect(f"/admin/users/{user['id']}", ('success', 'Password reset. They must choose a new one when they sign in.'))


def _require_another_admin(c, user_id):
    # Postgres runs READ COMMITTED: lock the admin rows so two admins demoting each other at the
    # same moment cannot both pass the count. SQLite's write lock already serializes this.
    lock = ' FOR UPDATE' if c.dialect == 'postgres' else ''
    admins = c.all(f"SELECT id FROM users WHERE role = 'admin' AND active = 1{lock}")
    if not any(row['id'] != user_id for row in admins):
        raise UserError('This is the last administrator. Make someone else an administrator first.')


def _load_user(c, user_id):
    user = c.one('SELECT id, username, display_name, role, active, must_change_password FROM users WHERE id = ?', (user_id,))
    if user is None:
        raise NotFound('That user was not found.')
    return user


# --- rate plans ----------------------------------------------------------------------------------------------

@route('GET', '/admin/rates', roles=ADMIN)
def rates_page(req):
    with db.connect() as c:
        plans = c.all('SELECT id, label, config, effective_from, created_at, note FROM rate_plans '
                      'ORDER BY effective_from DESC, created_at DESC')
        current = billing.current_plan(c)
    current_id = current['id'] if current else ''
    today = db.today()

    def state(plan):
        if plan['id'] == current_id:
            return ui.pill('In use today', 'success')
        return ui.pill('Starts later', 'info') if plan['effective_from'] > today else ui.pill('Past', 'neutral')

    # One version at a time, picked from a drop-down (the office did not want one long Excel-like list).
    chosen_id = req.list_filter('version', tuple(p['id'] for p in plans) + ('',), default=current_id or (plans[0]['id'] if plans else ''))
    chosen = next((p for p in plans if p['id'] == chosen_id), None)
    shown = ''
    if chosen:
        config = json.loads(chosen['config'])
        rows = [[label, *[_peso_text(config[category][kind][index]) for category in billing.CATEGORIES]]
                for kind, labels in (('minimums', billing.MINIMUM_LABELS), ('steps', billing.STEP_LABELS))
                for index, label in enumerate(labels)]
        picker = ui.raw('<form class="form filters" method="get" action="/admin/rates">') + ui.select(
            'Rate version', 'version', [(p['id'], f"{p['label']} · starts {p['effective_from']}") for p in plans], value=chosen_id
        ) + ui.raw('</form>')
        shown = ui.join([picker, ui.section(chosen['label'], ui.table(['Water used', 'Residential', 'Commercial'], rows, numeric_cols=(1, 2)),
                                            lead=f"Starts {chosen['effective_from']}" + (f" · {chosen['note']}" if chosen['note'] else ''))])
    history = ui.table(['Version', 'Starts on', 'Why it changed', ''],
                       [[ui.raw(f'<a href="/admin/rates?version={ui.esc(p["id"])}">{ui.esc(p["label"])}</a>'), p['effective_from'],
                         p['note'] or '—', state(p)] for p in plans], empty='No rate versions yet.')
    actions = ui.button_link('/admin/rates/new', 'New rate version')
    body = ui.join([ui.notice('Rates are never edited. A change is a new version with its own start date; old bills keep the rates they were made with.'),
                    actions, shown, ui.section('All versions', history)])
    return _page(req, 'Water rates', body, 'rates')


@route('GET', '/admin/rates/new', roles=ADMIN)
def new_rates_page(req):
    with db.connect() as c:
        config = billing.plan_config(billing.current_plan(c) or billing.get_plan(c, '6f1d0000-0000-4000-8000-0000000000a1'))
    groups = []
    for category in billing.CATEGORIES:
        group = [ui.field(label, name, value=_peso_text(config[category][kind][index]), attrs='inputmode="decimal"')
                 for name, cat, kind, index, label in RATE_PLAN_FIELDS if cat == category]
        groups.append(ui.section(category.title(), ui.join(group)))
    fields = ui.join([
        ui.field('Name of this version', 'label', hint='For example: November 2026 brackets'),
        ui.field('Starts on', 'effective_from', type='date', hint='Bills for months from this date use the new rates.'),
        ui.field('Why are the rates changing?', 'note', type='textarea', attrs='maxlength="300"',
                 hint='For example: board resolution of Nov 2026; temporary until March. Shown in the rates history.'),
        *groups,
    ])
    body = ui.form('/admin/rates/new', req.user['csrf'], fields, 'Save new rates', secondary=ui.button_link('/admin/rates', 'Cancel', kind='secondary'))
    return _page(req, 'New water rates', body, 'rates', parent=('Water rates', '/admin/rates'))


@route('POST', '/admin/rates/new', roles=ADMIN)
def create_rates(req, c):
    label = req.text('label', 'Name of this version', limit=120)
    effective_from = req.date('effective_from', 'Start date')
    note = req.text('note', 'Why the rates are changing', limit=300, multiline=True)
    if c.scalar('SELECT 1 FROM rate_plans WHERE label = ? AND effective_from = ?', (label, effective_from)):
        raise UserError('A rate version with this name and start date already exists.')
    config = {category: {'minimums': [None, None], 'steps': [None] * len(billing.STEP_LABELS)} for category in billing.CATEGORIES}
    for name, category, kind, index, field_label in RATE_PLAN_FIELDS:
        cents = req.pesos(name, f'{category.title()} {field_label}')
        config[category][kind][index] = _cents_text(cents)
    billing.validate_plan_config(config)
    plan_id = c.insert('rate_plans', {'label': label, 'config': json.dumps(config, sort_keys=True), 'effective_from': effective_from,
                                      'note': note, 'created_at': db.now(), 'created_by': req.user['id']})
    audit.log(c, req.user, 'rate_plan.create', 'rate_plan', plan_id, None,
              {'label': label, 'effective_from': effective_from, 'note': note, 'config': config})
    return Redirect('/admin/rates', ('success', f'New rates saved. They apply from {effective_from}.'))


def _cents_text(cents):
    return str(cents // 100) if cents % 100 == 0 else f'{Decimal(cents) / 100:.2f}'


def _peso_text(value):
    return _cents_text(parse_pesos(value))


# --- Maynilad bill and system loss ------------------------------------------------------------------------------

@route('GET', '/admin/maynilad', roles=ADMIN)
def maynilad_page(req):
    with db.connect() as c:
        period = req.query.get('period') or billing.current_period(c)
        if not billing.PERIOD_PATTERN.fullmatch(period):
            raise NotFound('That billing month is not valid.')
        figures = billing.get_bulk_water(c, period) or {}
        saved = c.all('SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key DESC LIMIT 24',
                      (billing.BULK_KEY_PREFIX + '%',))
    month = statements.month_label(period)
    rates = billing.bulk_rates(figures) if figures else None
    result = ''
    if rates:
        result = ui.section(f'Rates for {month}', ui.facts([
            ('Maynilad rate', f"{format_pesos(figures['maynilad_bill_cents'])} ÷ {figures['maynilad_m3']} m³ = ₱{rates.maynilad_rate} per m³"),
            ('System loss', f"{format_pesos(figures['loss_cents'])} ÷ {figures['loss_m3']} m³ = ₱{rates.loss_rate} per m³"),
            ('HOA subsidy', f'{rates.subsidy_percent}%'),
            ('Charged to homeowners', f'₱{rates.charged_loss_rate} per m³ of water used, as a separate "System loss" line'),
        ]))
    fields = ui.join([
        ui.raw(f'<input type="hidden" name="period" value="{ui.esc(period)}">'),
        ui.field('Total Maynilad bill (₱)', 'maynilad_bill', value=_money_text(figures.get('maynilad_bill_cents')),
                 attrs='inputmode="decimal"', hint='Both mother meters together, for example 3,215,356.84'),
        ui.field('Total Maynilad cubic metres', 'maynilad_m3', value=figures.get('maynilad_m3', ''), attrs='inputmode="decimal"',
                 hint='For example 55,619'),
        ui.field('System loss amount (₱)', 'loss_amount', value=_money_text(figures.get('loss_cents')), attrs='inputmode="decimal"',
                 hint='For example 439,773.24'),
        ui.field('System loss cubic metres', 'loss_m3', value=figures.get('loss_m3', ''), attrs='inputmode="decimal"',
                 hint='For example 21,692'),
        ui.field('HOA subsidy (% of system loss the HOA pays)', 'subsidy_percent', value=figures.get('subsidy_percent', 0),
                 attrs='inputmode="numeric"', hint='0 if homeowners pay all of it. June 2026 was 50.'),
    ])
    pick = ui.join([ui.raw('<form class="form filters" method="get" action="/admin/maynilad">'),
                    ui.field('Billing month', 'period', type='month', value=period),
                    ui.raw('<div class="form-actions"><button class="btn btn-secondary" type="submit">Show</button></div></form>')])
    history = [[statements.month_label(row['key'][len(billing.BULK_KEY_PREFIX):]),
                *_rate_cells(billing.bulk_rates(json.loads(row['value'])))] for row in saved]
    body = ui.join([
        ui.notice('From the Maynilad bill each month. The system works out the rate per m³ the way the office does: '
                  'total bill ÷ cubic metres, and system loss amount ÷ its cubic metres.'),
        pick, result,
        ui.section(f'Figures for {month}', ui.form('/admin/maynilad', req.user['csrf'], fields, 'Save and update draft bills'),
                   lead="Saving also recalculates this month's draft bills. Issued bills never change."),
        ui.section('Saved months', ui.table(['Billing month', 'Maynilad rate', 'System loss', 'Subsidy', 'Charged per m³'], history,
                                            empty='Nothing saved yet.', numeric_cols=(1, 2, 3, 4))),
    ])
    return _page(req, 'Maynilad bill and system loss', body, 'maynilad')


@route('POST', '/admin/maynilad', roles=ADMIN)
def save_maynilad(req, c):
    period = req.period('period')
    figures = {
        'maynilad_bill_cents': req.pesos('maynilad_bill', 'Total Maynilad bill'),
        'maynilad_m3': format_reading(billing.parse_cubic_meters(req.text('maynilad_m3', 'Maynilad cubic metres', limit=20),
                                                                 'Maynilad cubic metres')),
        'loss_cents': req.pesos('loss_amount', 'System loss amount'),
        'loss_m3': format_reading(billing.parse_cubic_meters(req.text('loss_m3', 'System loss cubic metres', limit=20),
                                                             'System loss cubic metres')),
        'subsidy_percent': _whole(req, 'subsidy_percent', 'HOA subsidy', 0, 100),
    }
    if figures['maynilad_bill_cents'] <= 0:
        raise UserError('The Maynilad bill must be more than zero.')
    before = billing.get_bulk_water(c, period)
    billing.set_setting(c, billing.bulk_key(period), figures, req.user)
    audit.log(c, req.user, 'maynilad.save', 'settings', billing.bulk_key(period), before, figures)
    # Drafts follow the new rate; refresh_draft is draft-only, so issued bills are never touched.
    drafts = [row['id'] for row in c.all("SELECT id FROM bills WHERE period = ? AND status = 'draft'", (period,))]
    if drafts:
        plan, settings = cycle._plan_for(c, period), statements.office_settings(c)
        for bill_id in drafts:
            cycle.refresh_draft(c, bill_id, plan, settings)
    rates = billing.bulk_rates(figures)
    return Redirect(f'/admin/maynilad?period={period}', ('success', (
        f'Saved. Maynilad rate ₱{rates.maynilad_rate}, system loss ₱{rates.loss_rate}, charged ₱{rates.charged_loss_rate} per m³. '
        f'{len(drafts)} draft bill{"s" if len(drafts) != 1 else ""} updated.')))


def _money_text(cents):
    return '' if cents is None else f'{Decimal(cents) / 100:,.2f}'


def _rate_cells(rates):
    return [f'₱{rates.maynilad_rate}', f'₱{rates.loss_rate}', f'{rates.subsidy_percent}%', f'₱{rates.charged_loss_rate}']


# --- backup ----------------------------------------------------------------------------------------------------

@route('GET', '/admin/backup', roles=ADMIN)
def backup_page(req):
    if req.app.config.uses_postgres:
        body = ui.notice('This server keeps its records in the online database, which makes its own daily backups. '
                         'Download a copy from the database provider\'s dashboard.')
        return _page(req, 'Backup', body, 'backup')
    with db.connect() as c:
        last = c.one("SELECT at FROM audit_log WHERE action = 'backup.download' ORDER BY at DESC LIMIT 1")
    lead = f"Last backup downloaded on {last['at'][:10]}." if last else 'No backup has been downloaded yet.'
    body = ui.join([
        ui.notice('The backup file holds every homeowner\'s records. Keep it on a USB drive locked in the office, never in email or chat.', 'warn'),
        ui.section('Full backup (to restore the system)', ui.form('/admin/backup', req.user['csrf'], '', 'Download backup'),
                   lead='One file with everything. The server also keeps a copy of its own every time it starts '
                        '(newest 30, in app\\data\\backups on the server computer).'),
        ui.section('All lists as Excel files', ui.form('/admin/backup/lists', req.user['csrf'], '', 'Download all lists'),
                   lead='A zip with homeowners, readings and bills of every month, payments, unpaid balances and collections, '
                        'to open in Excel or hand over to the next board.'),
    ])
    return _page(req, 'Backup', body, 'backup', lead)


@route('POST', '/admin/backup/lists', roles=ADMIN, raw=True)
def download_all_lists(req):
    """Every list in one zip of Excel files (site visit: manual "download all data" for turnover between boards)."""
    key = _form_key(req)
    with db.connect() as c:
        everything = {'from': '2000-01-01', 'to': db.today()}
        files = []
        for name in ('homeowners', 'aging'):
            files.append(exports.DATASETS[name].loader(c, everything))
        for table, name in (('readings', 'readings'), ('bills', 'bills')):
            for row in c.all(f'SELECT DISTINCT period FROM {table} ORDER BY period'):  # table names are literals above
                files.append(exports.DATASETS[name].loader(c, {'period': row['period']}))
        for name in ('payments', 'collections'):
            files.append(exports.DATASETS[name].loader(c, everything))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file_name, headers, rows in files:
            archive.writestr(f'{file_name}.xlsx', exports.to_xlsx(file_name[:31], headers, rows))
    data = buffer.getvalue()
    with db.transaction() as c:
        if _claim_key(c, key, req.user, 'POST /admin/backup/lists'):
            audit.log(c, req.user, 'backup.lists', 'database', 'lists', None, {'files': len(files), 'bytes': len(data)})
    return Response.download(data, 'application/zip', f'vistaverde-all-lists-{db.today()}.zip')


@route('POST', '/admin/backup', roles=ADMIN, raw=True)
def download_backup(req):
    if req.app.config.uses_postgres:
        raise HttpError(400, 'Backups of the online database are made by the database provider.')
    key = _form_key(req)
    data = _sqlite_backup(req.app.config.db_path)
    with db.transaction() as c:
        # The key makes a double click download twice but audit once.
        if _claim_key(c, key, req.user, BACKUP_ACTION):
            audit.log(c, req.user, 'backup.download', 'database', 'backup', None, {'bytes': len(data)})
    return Response.download(data, 'application/vnd.sqlite3', f'vistaverde-backup-{db.today()}.sqlite3')


def _sqlite_backup(path):
    """Consistent copy while the office keeps working (SQLite online backup API)."""
    handle, temp_name = tempfile.mkstemp(suffix='.sqlite3')
    os.close(handle)
    try:
        source = sqlite3.connect(str(path))
        target = sqlite3.connect(temp_name)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return Path(temp_name).read_bytes()
    finally:
        os.unlink(temp_name)


def _form_key(req):
    key = req.form.get('key', '')
    if not _is_uuid(key):
        raise HttpError(400, 'The form is missing its request key. Reload the page and try again.')
    return key


def _claim_key(c, key, user, action):
    """True the first time a form key is used for `action`; a raw route's own idempotency."""
    claimed = c.execute(
        "INSERT INTO requests (key, user_id, action, target, created_at) VALUES (?, ?, ?, '/admin', ?) ON CONFLICT (key) DO NOTHING",
        (key, user['id'], action, db.now())).rowcount
    return claimed == 1


# --- sync: admin screens -------------------------------------------------------------------------------------------

@route('GET', '/admin/sync', roles=ADMIN)
def sync_page(req):
    config = req.app.config
    with db.connect() as c:
        state = sync.status(c, config)
        problems = sync.consent_problems(c)
        nodes = c.all('SELECT node_id, name, created_at, last_seen, revoked_at FROM sync_nodes ORDER BY created_at')
        consent_name = c.scalar('SELECT display_name FROM users WHERE id = ?', (state['consent_by'],)) if state['consent_by'] else ''
    parts = [_sync_status(state, consent_name)]
    if state['conflicts']:
        parts.append(ui.notice(f"{state['conflicts']} records from another office need a decision.", 'danger',
                               action=ui.button_link('/admin/sync/conflicts', 'Decide now', dot=True)))
    parts.append(_consent_section(req, state, problems))
    if config.mode == 'lan':
        parts.append(_hub_section(req, state))
    if not state['hub_url']:
        parts.append(_nodes_section(req, nodes))
    return _page(req, 'Sync', ui.join(parts), 'sync', 'Keep the office computer and the online server in step.')


def _sync_status(state, consent_name):
    facts = [('Sync', 'On' if state['enabled'] else 'Off')]
    if state['enabled']:
        facts.append(('Switched on by', f"{consent_name or 'unknown'} on {state['consent_at'][:10]}"))
    if state['hub_url']:
        facts += [('Online server', state['hub_url']), ('Changes waiting to send', f"{state['unpushed']:,}"),
                  ('Last received', state['last_pull_at'][:16].replace('T', ' ') or 'never')]
    if state['dead']:
        facts.append(('Changes refused by the server', f"{state['dead']:,}"))
    if state['waiting']:
        facts.append(('Changes waiting for related records', f"{state['waiting']:,}"))
    return ui.facts(facts)


def _consent_section(req, state, problems):
    if state['enabled']:
        return ui.section('Switch sync off', ui.form('/admin/sync/disable', req.user['csrf'], '', 'Switch sync off', danger=True,
                                                     confirm='Yes, switch sync off'))
    notes = ui.join([ui.notice(problem, 'warn') for problem in problems])
    fields = ui.select('Has the HOA board agreed, in writing, to keep homeowners\' records on the online server (Data Privacy Act)?',
                       'consent', YES_NO, value='no')
    return ui.section('Switch sync on', notes + ui.form('/admin/sync/enable', req.user['csrf'], fields, 'Switch sync on'),
                      lead='Sync copies resident records to the online server. It stays off until the HOA agrees.')


def _hub_section(req, state):
    if state['from_env']:
        return ui.section('Online server', ui.notice('The online server address is set on this computer\'s start-up settings.'))
    fields = ui.join([
        ui.field('Online server address', 'hub_url', value=state['hub_url'], hint='Starts with https://', attrs='autocomplete="off"'),
        ui.field('Sync key', 'hub_token', type='password', required=not state['has_token'],
                 hint='Shown once on the online server when this office was added.' + (' A key is saved; leave empty to keep it.' if state['has_token'] else ''),
                 attrs='autocomplete="off"'),
    ])
    return ui.section('Connect to the online server', ui.form('/admin/sync/hub', req.user['csrf'], fields, 'Save connection'))


def _nodes_section(req, nodes):
    rows = []
    for node in nodes:
        action = ui.pill('Removed', 'neutral') if node['revoked_at'] else ui.form(
            f"/admin/sync/nodes/{node['node_id']}/revoke", req.user['csrf'], '', 'Remove', danger=True, confirm='Yes, remove')
        rows.append([node['name'], node['created_at'][:10], (node['last_seen'] or '')[:16].replace('T', ' ') or 'never', action])
    form = ui.form('/admin/sync/nodes', req.user['csrf'], ui.field('Office computer name', 'name', hint='For example: Phase 1D office'), 'Add office computer')
    return ui.section('Office computers that sync here', ui.table(['Name', 'Added', 'Last contact', ''], rows, empty='No office computers yet.') + form)


@route('POST', '/admin/sync/enable', roles=ADMIN)
def enable_sync(req, c):
    consent = req.choice('consent', [value for value, _ in YES_NO], 'answer') == 'yes'
    changed = sync.enable(c, req.user, consent)
    return Redirect('/admin/sync', ('success', 'Sync is on.') if changed else ('info', 'Sync was already on.'))


@route('POST', '/admin/sync/disable', roles=ADMIN)
def disable_sync(req, c):
    changed = sync.disable(c, req.user)
    return Redirect('/admin/sync', ('success', 'Sync is off.') if changed else ('info', 'Sync was already off.'))


@route('POST', '/admin/sync/hub', roles=ADMIN)
def save_hub(req, c):
    if req.app.config.sync_url:
        raise UserError('The online server is set in this computer\'s start-up settings.')
    url = req.text('hub_url', 'Online server address', limit=200)
    token = req.form.get('hub_token', '').strip()
    if not token and not sync.get_state(c, 'hub_token'):
        raise UserError('Paste the sync key from the online server.')
    sync.save_hub(c, url, token, req.user)
    return Redirect('/admin/sync', ('success', 'Connection saved. Sync runs every 15 seconds while switched on.'))


@route('POST', '/admin/sync/nodes', roles=ADMIN, raw=True)
def add_node(req):
    """Raw: the new key is shown in this response only (never stored, never in a redirect)."""
    key = _form_key(req)
    name = req.text('name', 'Office computer name', limit=sync.NODE_NAME_LIMIT)
    with db.transaction() as c:
        if not _claim_key(c, key, req.user, NODE_ACTION):
            token = None
        else:
            _, token = sync.create_node(c, name, req.user)
    if token is None:
        body = ui.notice('This form was already used, so its key was shown once already. If it was lost, remove that office computer and add it again.', 'warn')
    else:
        body = ui.join([
            ui.notice('Copy this sync key now. It is shown only once and cannot be seen again.', 'warn'),
            ui.raw(f'<p><code>{ui.esc(token)}</code></p>'),
            ui.notice('On the office computer: Admin → Sync → paste this server\'s address and this key.'),
        ])
    crumbs = [('Admin', '/admin'), ('Sync', '/admin/sync'), 'Office computer added']
    return req.page('Office computer added', ui.join([ui.heading('Office computer added', crumbs=crumbs), body,
                                                     ui.button_link('/admin/sync', 'Back to Sync')]), active='admin')


@route('POST', '/admin/sync/nodes/{id}/revoke', roles=ADMIN)
def revoke_node(req, c):
    changed = sync.revoke_node(c, req.params['id'], req.user)
    return Redirect('/admin/sync', ('success', 'Office computer removed. Its key no longer works.') if changed else ('info', 'Already removed.'))


@route('GET', '/admin/sync/conflicts', roles=ADMIN)
def conflicts_page(req):
    with db.connect() as c:
        items = [(item, _summary(c, item['tbl'], c.one(f"SELECT * FROM {item['tbl']} WHERE {db.PRIMARY_KEY.get(item['tbl'], 'id')} = ?", (item['local_id'],))),
                  _summary(c, item['tbl'], json.loads(item['payload']))) for item in sync.open_conflicts(c)]
    if not items:
        body = ui.empty('Nothing needs a decision.', ui.button_link('/admin/sync', 'Back to Sync', kind='secondary'))
        return _page(req, 'Sync conflicts', body, 'sync', parent=('Sync', '/admin/sync'))
    sections = []
    for item, mine, theirs in items:
        base = f"/admin/sync/conflicts/{item['id']}"
        forms = [ui.form(base, req.user['csrf'], ui.raw('<input type="hidden" name="choice" value="keep_mine">'), 'Keep this office\'s record', secondary=''),
                 ui.form(base, req.user['csrf'], ui.raw('<input type="hidden" name="choice" value="keep_theirs">'), 'Keep the other office\'s record')]
        if item['tbl'] == 'payments':
            forms.append(ui.form(base, req.user['csrf'], ui.raw('<input type="hidden" name="choice" value="keep_both">')
                                 + ui.field('New receipt number for the other office\'s payment', 'new_receipt'), 'Keep both'))
        table = ui.table(['', 'This office', 'Other office'], [[label, mine.get(label, '—'), theirs.get(label, '—')] for label in theirs])
        sections.append(ui.section(sync.CONFLICT_LABELS.get(item['reason'], 'Two offices disagree.'), table + ui.join(forms)))
    return _page(req, 'Sync conflicts', ui.join(sections), 'sync', 'Choose which record is right. The other offices follow your choice.',
                 parent=('Sync', '/admin/sync'))


@route('POST', '/admin/sync/conflicts/{id}', roles=ADMIN)
def resolve_conflict(req, c):
    choice = req.choice('choice', sync.RESOLUTIONS, 'decision')
    sync.resolve(c, req.params['id'], choice, req.user, req.form.get('new_receipt', ''))
    return Redirect('/admin/sync/conflicts', ('success', 'Decision saved.'))


def _summary(c, table, row):
    if not row:
        return {}
    labels = {'receipt_no': 'Receipt', 'amount_cents': 'Amount', 'paid_on': 'Paid on', 'method': 'Method', 'status': 'Status',
              'account_no': 'Account no.', 'name': 'Name', 'address': 'Address', 'period': 'Month', 'previous': 'Previous reading',
              'present': 'Present reading', 'total_cents': 'Total', 'statement_no': 'Statement no.', 'memo': 'Description'}
    summary = {}
    account_id = row.get('account_id')
    if account_id:
        summary['Homeowner'] = c.scalar('SELECT name FROM accounts WHERE id = ?', (sync._local_id(c, 'accounts', account_id),)) or 'not on this computer'
    for column, label in labels.items():
        if column in row and row[column] is not None:
            value = row[column]
            summary[label] = format_pesos(value) if column.endswith('_cents') and isinstance(value, int) else str(value)
    return summary


def _link(href, text):
    return ui.raw(f'<a href="{ui.esc(href)}">{ui.esc(text)}</a>')


# --- sync: hub API (public routes, bearer token) ------------------------------------------------------------------------

def _json(status, data):
    return Response(status, json.dumps(data).encode('utf-8'), 'application/json')


def _authorize_node(req):
    """(node, None) or (None, error Response). One generic answer for every auth failure."""
    bucket = SYNC_FAILURE_PREFIX + req.peer
    with db.transaction() as c:
        if auth.is_rate_limited(c, bucket):
            return None, _json(429, {'error': 'too many attempts'})
        node = sync.authenticate_node(c, req.headers.get('Authorization', ''))
        if node is None:
            auth.record_failure(c, bucket)
            return None, _json(401, {'error': 'unauthorized'})
        if not sync.is_enabled(c):
            return None, _json(403, {'error': 'sync is not enabled on this server'})
    return node, None


@route('GET', '/sync/pull', public=True)
def sync_pull(req):
    node, error = _authorize_node(req)
    if error:
        return error
    after = req.query.get('after', '0')
    if not AFTER_PATTERN.fullmatch(after):
        return _json(400, {'error': 'bad cursor'})
    with db.transaction() as c:
        page = sync.serve_pull(c, node, int(after))
    return _json(200, page)


@route('POST', '/sync/push', public=True, max_body=sync.MAX_PUSH_BODY)
def sync_push(req):
    node, error = _authorize_node(req)
    if error:
        return error
    try:
        batch = json.loads(req.form.get('batch', ''))
        with db.transaction() as c:
            accepted = sync.receive_push(c, node, batch)
    except (ValueError, InvalidOperation):
        return _json(400, {'error': 'invalid batch'})
    except db.IntegrityError:
        return _json(409, {'error': 'batch clashes with stored records'})
    return _json(200, {'accepted': accepted})
