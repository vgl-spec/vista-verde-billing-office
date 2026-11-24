"""Sign in, sign out, first-run setup and the change-password page.

Sign in and setup are `public` routes, so they carry their own CSRF protection: a random token set
as a cookie when the form is shown and echoed back in the form (it must match). Without it a
hostile page could sign a visitor in to an account of the attacker's choosing.
"""
import hmac
import secrets

from .. import UserError, auth, db, ui
from ..http import (FLASH_COOKIE, PASSWORD_CHANGE_PATH, SESSION_COOKIE, HttpError, Redirect, Response,
                    route)

PRE_SESSION_COOKIE = 'bms_pre'
PRE_SESSION_TTL_SECONDS = 3600
SIGN_IN_FAILED = 'That username or password is not right.'
TOO_MANY_ATTEMPTS = 'Too many tries. Please wait a few minutes and try again.'
GATE_BUCKET_PREFIX = 'setup:'
PASSWORD_BUCKET_PREFIX = 'password:'


# --- pre-session CSRF ---------------------------------------------------------------------------------

def _pre_session_token(req):
    return req.cookies.get(PRE_SESSION_COOKIE) or secrets.token_urlsafe(24)


def _require_pre_session_token(req):
    cookie = req.cookies.get(PRE_SESSION_COOKIE, '')
    if not cookie or not hmac.compare_digest(req.form.get('csrf', '').encode(), cookie.encode()):
        raise HttpError(403, 'This page was open too long. Please reload it and try again.')


def _public_page(req, title, body, token, status=200):
    response = req.page(title, body, status=status)
    response.set_cookie(PRE_SESSION_COOKIE, token, secure=req.secure_cookies, max_age=PRE_SESSION_TTL_SECONDS)
    return response


# --- sign in / out --------------------------------------------------------------------------------------------

def _login_body(token, message=''):
    fields = (
        ui.field('Username', 'username', attrs='autocomplete="username" autocapitalize="none" autofocus')
        + ui.field('Password', 'password', type='password', attrs='autocomplete="current-password"')
    )
    notice = ui.notice(message, 'danger') if message else ''
    return ui.heading('Sign in', 'Use the username and password the office gave you.') + notice + ui.form('/login', token, fields, 'Sign in')


@route('GET', '/login', public=True)
def login_page(req):
    if req.user:
        return Response.redirect('/')
    token = _pre_session_token(req)
    return _public_page(req, 'Sign in', _login_body(token), token)


@route('POST', '/login', public=True)
def login(req):
    _require_pre_session_token(req)
    token = req.form['csrf']
    username, password = req.form.get('username', ''), req.form.get('password', '')
    with db.transaction() as c:
        limited = auth.is_rate_limited(c, req.peer)
    if limited:
        return _public_page(req, 'Sign in', _login_body(token, TOO_MANY_ATTEMPTS), token, status=429)
    # Hash outside any transaction: it takes ~0.3 s and must not hold the database write lock.
    user = None
    if len(password) <= auth.PASSWORD_MAX_LENGTH:
        with db.connect() as c:
            user = auth.authenticate(c, username, password)
    with db.transaction() as c:
        if user is None:
            auth.record_failure(c, req.peer)
        else:
            session_token, _ = auth.create_session(c, user['id'])
    if user is None:
        return _public_page(req, 'Sign in', _login_body(token, SIGN_IN_FAILED), token, status=403)
    response = Response.redirect('/')
    response.set_cookie(SESSION_COOKIE, session_token, secure=req.secure_cookies, max_age=int(auth.SESSION_TTL.total_seconds()))
    return response


@route('POST', '/logout', raw=True)
def logout(req):
    with db.transaction() as c:
        auth.destroy_session(c, req.cookies.get(SESSION_COOKIE, ''))
    response = Response.redirect('/login')
    response.clear_cookie(SESSION_COOKIE)
    response.clear_cookie(FLASH_COOKIE)
    return response


# --- first-run setup ----------------------------------------------------------------------------------------------

def _setup_body(token):
    fields = (
        ui.field('Setup code', 'code', hint='Shown in the black window where the server is running.', attrs='autocomplete="off"')
        + ui.field('Username', 'username', hint='Letters and numbers, for example: maria', attrs='autocomplete="username" autocapitalize="none"')
        + ui.field('Your name', 'display_name', required=False)
        + ui.field('Password', 'password', type='password', hint=f'At least {auth.PASSWORD_MIN_LENGTH} characters.', attrs='autocomplete="new-password"')
        + ui.field('Type the password again', 'password_again', type='password', attrs='autocomplete="new-password"')
    )
    return ui.heading('First-time setup', 'Create the administrator account.') + ui.form('/setup', token, fields, 'Create administrator')


@route('GET', '/setup', public=True)
def setup_page(req):
    with db.connect() as c:
        has_users = auth.count_users(c) > 0
    if has_users:
        return Response.redirect('/login')
    if not req.app.setup_gate.active:
        body = ui.heading('Setup is closed') + ui.notice(
            'On the computer that runs the server, run: python app/server.py create-admin <username>', 'warn')
        return req.page('Setup is closed', body)
    token = _pre_session_token(req)
    return _public_page(req, 'First-time setup', _setup_body(token), token)


@route('POST', '/setup', public=True)
def setup(req):
    _require_pre_session_token(req)
    gate, bucket = req.app.setup_gate, GATE_BUCKET_PREFIX + req.peer
    with db.transaction() as c:
        limited = auth.is_rate_limited(c, bucket)
        code_ok = not limited and gate.matches(req.form.get('code', ''))
        if not limited and not code_ok:
            auth.record_failure(c, bucket)
    if limited:
        raise HttpError(429, TOO_MANY_ATTEMPTS)
    if not code_ok:
        return _setup_retry(req, 'The setup code is not right. Copy it exactly from the black server window and try again.', 403)
    password = req.form.get('password', '')
    if password != req.form.get('password_again', ''):
        return _setup_retry(req, 'The two passwords are not the same. Please type them again.', 400)
    try:
        with db.transaction() as c:
            if auth.count_users(c) > 0:
                raise UserError('Setup was already completed. Please sign in.')
            auth.create_user(c, req.form.get('username', ''), req.form.get('display_name', ''), password, 'admin')
            gate.clear(c)
    except UserError as problem:
        return _setup_retry(req, str(problem), 400)
    return Response.redirect('/login', ('success', 'Administrator created. Please sign in.'))


def _setup_retry(req, message, status):
    """Show the form again with the problem on top: a dead-end error page made staff start over."""
    token = _pre_session_token(req)
    body = ui.notice(message, 'danger') + _setup_body(token)
    return _public_page(req, 'First-time setup', body, token, status)


# --- change password -----------------------------------------------------------------------------------------------

@route('GET', PASSWORD_CHANGE_PATH)
def password_page(req):
    fields = (
        ui.field('Current password', 'current', type='password', attrs='autocomplete="current-password"')
        + ui.field('New password', 'new', type='password', hint=f'At least {auth.PASSWORD_MIN_LENGTH} characters.', attrs='autocomplete="new-password"')
        + ui.field('Type the new password again', 'new_again', type='password', attrs='autocomplete="new-password"')
    )
    forced = bool(req.user['must_change_password'])
    lead = 'You must choose a new password before you continue.' if forced else ''
    # A forced change has nowhere to go back to: every other page sends them here until it is done.
    crumbs = () if forced else [('Home', '/'), 'Change password']
    body = ui.heading('Change password', lead, crumbs=crumbs) + ui.form(PASSWORD_CHANGE_PATH, req.user['csrf'], fields, 'Change password')
    return req.page('Change password', body)


@route('POST', PASSWORD_CHANGE_PATH)
def change_password(req, c):
    bucket = PASSWORD_BUCKET_PREFIX + req.user['id']
    if auth.is_rate_limited(c, bucket):
        return Redirect(PASSWORD_CHANGE_PATH, ('danger', TOO_MANY_ATTEMPTS))
    stored = c.scalar('SELECT password_hash FROM users WHERE id = ?', (req.user['id'],))
    current = req.form.get('current', '')
    if len(current) > auth.PASSWORD_MAX_LENGTH or not auth.verify_password(current, stored):
        # Returned, not raised: the failure record must commit or the rate limit would never trip.
        auth.record_failure(c, bucket)
        return Redirect(PASSWORD_CHANGE_PATH, ('danger', 'The current password is not right.'))
    new_password = req.form.get('new', '')
    if new_password != req.form.get('new_again', ''):
        raise UserError('The two new passwords are not the same.')
    auth.set_password(c, req.user['id'], new_password, actor=req.user, keep_session_hash=req.user['token_hash'])
    return Redirect('/', ('success', 'Your password was changed.'))
