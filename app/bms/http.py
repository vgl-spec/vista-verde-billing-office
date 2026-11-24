"""HTTP layer: router, sessions + CSRF, idempotent POST, form/multipart parsing, static files.

Route handlers (registered with `@route`):
    GET   handler(req)      -> Response
    POST  handler(req, c)   -> str redirect target | Redirect(target, flash=(kind, text))
          `c` is the open transaction. The router claims the form's idempotency `key`, runs the
          handler, stores the redirect and commits - handlers never commit. Raise UserError for
          anything the person can fix; the transaction rolls back and they see the message.
    raw=True handlers take (req) and return a Response, owning their own transactions. Only for
          flows the redirect model cannot express (sign in/out set cookies).
Options: roles=(...) limits access (default: any signed-in user), public=True skips sign-in and
CSRF (the handler must protect itself), max_body=N raises the 2 MB body cap for uploads.
"""
import base64
import hmac
import re
import ssl
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date
from email.parser import BytesParser
from email.policy import HTTP as HTTP_POLICY
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import UserError, alerts, auth, db, ui
from .config import APP_DIR
from .money import format_reading, parse_pesos, parse_reading

SESSION_COOKIE = 'bms_session'
FLASH_COOKIE = 'bms_flash'
MAX_BODY_DEFAULT = 2 * 1024 * 1024
MAX_BODY_UPLOAD = 25 * 1024 * 1024
MAX_FORM_FIELDS = 10000
MAX_FIELD_NAME = 100
CONNECTION_TIMEOUT_SECONDS = 60
FLASH_KINDS = ('info', 'success', 'warn', 'danger')
ASSET_ROOT = APP_DIR / 'assets'
ASSET_TYPES = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.woff2': 'font/woff2',
}
SECURITY_HEADERS = (
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'),
    ('Content-Security-Policy',
     "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
)
DATE_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}')
PERIOD_PATTERN = re.compile(r'\d{4}-(0[1-9]|1[0-2])')
PASSWORD_CHANGE_PATH = '/account/password'
PATHS_ALLOWED_BEFORE_PASSWORD_CHANGE = (PASSWORD_CHANGE_PATH, '/logout')


class HttpError(Exception):
    """A request the router refuses; `status` and plain-language `message` reach the browser."""

    def __init__(self, status, message, *, close=False):
        super().__init__(message)
        self.status, self.message, self.close = status, message, close


class NotFound(HttpError):
    def __init__(self, message='That page or record was not found.'):
        super().__init__(404, message)


@dataclass
class Redirect:
    target: str
    flash: tuple = None  # (kind, text) shown once on the next page


@dataclass
class Upload:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Response:
    status: int = 200
    body: bytes = b''
    content_type: str = 'text/html; charset=utf-8'
    headers: list = field(default_factory=list)
    close: bool = False

    @staticmethod
    def redirect(target, flash=None):
        response = Response(303, b'', headers=[('Location', _safe_target(target))])
        if flash:
            response.set_cookie(FLASH_COOKIE, _encode_flash(flash), secure=False, max_age=60)  # not a secret
        return response

    @staticmethod
    def download(data, content_type, filename):
        safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
        return Response(200, data, content_type, [('Content-Disposition', f'attachment; filename="{safe_name}"')])

    def set_cookie(self, name, value, *, secure, max_age):
        flags = f'Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}' + ('; Secure' if secure else '')
        self.headers.append(('Set-Cookie', f'{name}={value}; {flags}'))

    def clear_cookie(self, name):
        self.headers.append(('Set-Cookie', f'{name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'))


@dataclass
class App:
    config: object
    setup_gate: object = field(default_factory=auth.SetupGate)


# --- routing --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    regex: re.Pattern
    handler: object
    roles: tuple
    public: bool
    raw: bool
    max_body: int


_ROUTES = []


def route(method, pattern, *, roles=None, public=False, raw=False, max_body=MAX_BODY_DEFAULT):
    if method not in ('GET', 'POST'):
        raise ValueError(f'Unsupported method {method!r}')
    if roles is not None and not set(roles) <= set(auth.ROLES):
        raise ValueError(f'Unknown role in {roles!r}')

    def register(handler):
        regex = re.compile(re.sub(r'\{(\w+)\}', r'(?P<\1>[^/]+)', pattern))
        _ROUTES.append(Route(method, pattern, regex, handler, tuple(roles) if roles else (), public, raw, max_body))
        # Fewest placeholders first, so '/accounts/new' wins over '/accounts/{id}'.
        _ROUTES.sort(key=lambda r: (r.pattern.count('{'), -len(r.pattern)))
        return handler
    return register


def find_route(method, path):
    for candidate in _ROUTES:
        if candidate.method == method:
            match = candidate.regex.fullmatch(path)
            if match:
                return candidate, match.groupdict()
    return None


def path_exists_for_other_method(method, path):
    return any(r.method != method and r.regex.fullmatch(path) for r in _ROUTES)


# --- request ----------------------------------------------------------------------------------------

class Request:
    def __init__(self, app, method, path, query, headers, peer):
        self.app, self.method, self.path, self.headers, self.peer = app, method, path, headers, peer
        self.query = {name: values[0] for name, values in query.items()}
        self.query_lists = query  # every value of a repeated parameter (?a=1&a=2)
        self.params = {}
        self.form_lists = {}  # every POST field: name -> list of values
        self.form = {}        # first value of each field
        self.files = {}
        self.cookies = {}
        self.user = None      # session user dict (includes 'csrf') or None
        self.flash = None     # (kind, text) from the previous request's redirect
        self.flash_cookie_seen = False

    def load_form(self, form_lists, files):
        self.form_lists, self.files = form_lists, files
        self.form = {name: values[0] for name, values in form_lists.items()}

    @property
    def secure_cookies(self):
        return self.app.config.cookie_secure

    def page(self, title, body, *, active='', dots=None, status=200, flash=None, live=True):
        """A full page. `live`: the tables whose changes update it in place (True = any record; False = never)."""
        alert_count, live_seq = 0, None
        if self.user:
            with db.connect() as c:
                alert_count = alerts.count_open(c)
                live_seq = db.latest_change(c) if live else None
        markup = ui.shell(title, body, user=self.user, active=active, alert_count=alert_count,
                          flash=flash or self.flash, dots=dots, path=self.path, live=live, live_seq=live_seq)
        return Response(status, markup.encode('utf-8'))

    # Input accessors: every one raises UserError with words the person can act on.

    def text(self, name, label=None, *, limit=160, required=True, multiline=False):
        value = self.form.get(name, '').strip()
        label = label or name
        if required and not value:
            raise UserError(f'{label} is required.')
        if len(value) > limit:
            raise UserError(f'{label} must be at most {limit} characters.')
        allowed = '\t\n\r' if multiline else ''
        if any(ord(ch) < 32 and ch not in allowed for ch in value):
            raise UserError(f'{label} contains characters that are not allowed.')
        return value

    def choice(self, name, allowed, label=None):
        value = self.form.get(name, '').strip()
        if value not in allowed:
            raise UserError(f'Choose a valid {label or name}.')
        return value

    def list_filter(self, name, allowed, default='all'):
        """A list filter from the query string. An unknown value is a 404, never silently ignored (fail closed)."""
        value = self.query.get(name, default)
        if value not in allowed:
            raise NotFound(f'That {name} filter is not valid.')
        return value

    def checkbox(self, name):
        """A ticked box sends 'yes'; an unticked box sends nothing. Anything else is refused (fail closed)."""
        value = self.form.get(name, '')
        if value not in ('', 'yes'):
            raise UserError('That tick box sent an unexpected value. Reload the page and try again.')
        return value == 'yes'

    def date(self, name, label=None):
        value = self.text(name, label or 'Date', limit=10)
        try:
            if not DATE_PATTERN.fullmatch(value):
                raise ValueError
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise UserError(f'{label or "Date"} must be a real date.') from None

    def period(self, name, label=None):
        value = self.text(name, label or 'Billing month', limit=7)
        if not PERIOD_PATTERN.fullmatch(value):
            raise UserError('Use a billing month in YYYY-MM format.')
        return value

    def pesos(self, name, label=None):
        return parse_pesos(self.text(name, label or 'Amount', limit=32))

    def reading(self, name, label=None):
        return format_reading(parse_reading(self.text(name, label or 'Reading', limit=16)))


# --- form parsing ---------------------------------------------------------------------------------------

def parse_form(content_type, body):
    """Return ({name: [values]}, {name: Upload}). Names ending in [] may repeat; others may not."""
    kind = (content_type or '').split(';')[0].strip().lower()
    try:
        if kind == 'application/x-www-form-urlencoded':
            pairs = [(n, v) for n, values in parse_qs(
                body.decode('utf-8'), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS).items() for v in values]
            files = {}
        elif kind == 'multipart/form-data':
            pairs, files = _parse_multipart(content_type, body)
        else:
            raise HttpError(415, 'This form type is not supported.')
    except (UnicodeDecodeError, ValueError):
        raise HttpError(400, 'The form data could not be read.') from None
    fields = {}
    for name, value in pairs:
        if len(name) > MAX_FIELD_NAME:
            raise HttpError(400, 'A form field name is too long.')
        if name in fields and not name.endswith('[]'):
            raise HttpError(400, 'The form repeated a field. Reload the page and try again.')
        fields.setdefault(name, []).append(value)
    return fields, files


def _parse_multipart(content_type, body):
    message = BytesParser(policy=HTTP_POLICY).parsebytes(
        b'Content-Type: ' + content_type.encode('latin-1') + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    if not message.is_multipart() or message.defects:
        raise ValueError('malformed multipart body')
    pairs, files = [], {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if not name:
            raise ValueError('unnamed part')
        data = part.get_payload(decode=True) or b''
        filename = part.get_filename()
        if filename is None:
            pairs.append((name, data.decode('utf-8')))
        elif name in files:
            raise ValueError('duplicate upload field')
        elif filename:
            files[name] = Upload(filename, part.get_content_type(), data)
    return pairs, files


def _read_body(handler, limit):
    if 'Transfer-Encoding' in handler.headers:
        raise HttpError(411, 'Chunked uploads are not supported.', close=True)
    try:
        length = int(handler.headers.get('Content-Length', ''))
    except ValueError:
        raise HttpError(411, 'The request needs a Content-Length.', close=True) from None
    if length < 0:
        raise HttpError(400, 'Invalid request size.', close=True)
    if length > limit:
        raise HttpError(413, f'That upload is too large (limit {limit // (1024 * 1024)} MB).', close=True)
    return handler.rfile.read(length)


# --- the request pipeline -------------------------------------------------------------------------------

def handle(app, handler):
    """Turn one raw HTTP request into a Response. Never raises."""
    split = urlsplit(handler.path)
    path = unquote(split.path)
    if handler.command == 'GET' and path.startswith('/assets/'):
        return _static(path)
    req = Request(app, handler.command, path, parse_qs(split.query, keep_blank_values=True),
                  handler.headers, _peer(app.config, handler))
    try:
        response = _dispatch(req, handler)
    except HttpError as error:
        response = _error_page(req, error.status, error.message)
        response.close = error.close
    except UserError as error:
        response = _error_page(req, 400, str(error))
    except ShowPage as page:
        response = req.page(page.title, page.body, active=page.active, status=page.status, live=False)
    except Exception as error:  # noqa: BLE001 - last line of defence; logged without the message (may hold PII)
        _log_failure(req, error)
        response = _error_page(req, 500, 'Something went wrong on our side. Nothing was saved. Please try again, and tell the office if it keeps happening.')
    if req.flash_cookie_seen and response.status < 300:
        response.clear_cookie(FLASH_COOKIE)
    return response


class ShowPage(Exception):
    """Raised by a POST handler to answer with a page instead of a redirect, saving nothing (the transaction
    rolls back and the form key stays unused): e.g. the edit-conflict resolver."""

    def __init__(self, title, body, *, active='', status=409):
        super().__init__(title)
        self.title, self.body, self.active, self.status = title, body, active, status


def _dispatch(req, handler):
    _load_cookies(req)
    found = find_route(req.method, req.path)
    if found is None:
        if path_exists_for_other_method(req.method, req.path):
            raise HttpError(405, 'That action is not allowed here.')
        raise NotFound()
    selected, req.params = found
    if not selected.public:
        if req.user is None:
            return Response.redirect('/login')
        if req.user['must_change_password'] and req.path not in PATHS_ALLOWED_BEFORE_PASSWORD_CHANGE:
            return Response.redirect(PASSWORD_CHANGE_PATH)
        if selected.roles and req.user['role'] not in selected.roles:
            raise HttpError(403, 'Your account is not allowed to open this page.')
    if req.method == 'GET':
        return selected.handler(req)
    req.load_form(*parse_form(handler.headers.get('Content-Type'), _read_body(handler, selected.max_body)))
    if not selected.public:
        _check_csrf(req)
    if selected.raw or selected.public:
        return selected.handler(req)
    return _run_idempotent(req, selected)


def _load_cookies(req):
    jar = cookies.SimpleCookie()
    try:
        jar.load(req.headers.get('Cookie', ''))
    except cookies.CookieError:
        return
    req.cookies = {name: morsel.value for name, morsel in jar.items()}
    if SESSION_COOKIE in req.cookies:
        with db.connect() as c:
            req.user = auth.find_session_user(c, req.cookies[SESSION_COOKIE])
    # A live refresh (app.js, header X-Live) runs in the background: it must not use up the one-time message that the
    # page the person is moving to should show.
    if FLASH_COOKIE in req.cookies and req.headers.get('X-Live') != '1':
        req.flash_cookie_seen = True
        req.flash = _decode_flash(req.cookies[FLASH_COOKIE])


def _check_csrf(req):
    sent = req.form.get('csrf', '')
    if not hmac.compare_digest(sent.encode(), req.user['csrf'].encode()):
        raise HttpError(403, 'Your session expired or the form was out of date. Sign in again and retry.')


def _run_idempotent(req, selected):
    """One transaction: claim the form key, run the handler, store the redirect.

    The key is claimed first, so a concurrent duplicate blocks on the unique key (Postgres) or the
    write lock (SQLite) and then replays the winner's redirect instead of repeating its effects.
    """
    key = req.form.get('key', '')
    if not _is_uuid(key):
        raise HttpError(400, 'The form is missing its request key. Reload the page and try again.')
    action = f'{selected.method} {selected.pattern}'
    try:
        with db.transaction() as c:
            c.execute(
                "INSERT INTO requests (key, user_id, action, target, created_at) VALUES (?, ?, ?, '', ?)",
                (key, req.user['id'], action, db.now()))
            result = selected.handler(req, c)
            outcome = result if isinstance(result, Redirect) else Redirect(result)
            c.execute('UPDATE requests SET target = ?, flash = ? WHERE key = ?',
                      (_safe_target(outcome.target), _encode_flash(outcome.flash) if outcome.flash else '', key))
    except db.IntegrityError:
        # Either the key was already used (replay) or the handler hit a UNIQUE constraint. Decide
        # outside the aborted transaction, on a fresh connection.
        outcome = _replay(key, action, req.user['id'])
    return Response.redirect(outcome.target, outcome.flash)


def _replay(key, action, user_id):
    with db.connect() as c:
        stored = c.one('SELECT user_id, action, target, flash FROM requests WHERE key = ?', (key,))
    if stored is None:
        raise UserError('That record already exists, or a value clashes with one already saved.')
    if stored['user_id'] != user_id or stored['action'] != action or not stored['target']:
        raise UserError('This form was already used. Reload the page and start again.')
    return Redirect(stored['target'], _decode_flash(stored['flash']) if stored['flash'] else None)


def _is_uuid(text):
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def _safe_target(target):
    """Only same-site absolute paths: a handler bug must not become an open redirect."""
    if not isinstance(target, str) or not target.startswith('/') or target.startswith('//') or '\\' in target or '\n' in target or '\r' in target:
        raise ValueError(f'Unsafe redirect target: {target!r}')
    return target


def _encode_flash(flash):
    kind, text = flash
    if kind not in FLASH_KINDS:
        raise ValueError(f'Unknown flash kind {kind!r}')
    return base64.urlsafe_b64encode(f'{kind}:{text}'.encode()).decode().rstrip('=')


def _decode_flash(value):
    try:
        kind, _, text = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode().partition(':')
    except (ValueError, UnicodeDecodeError):
        return None
    if not text:
        return None
    return (kind if kind in FLASH_KINDS else 'info', text)


def _peer(config, handler):
    """Client address. Online behind the platform proxy, the proxy appends the real client last."""
    if config.mode == 'online':
        forwarded = handler.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[-1].strip()[:64]
    return handler.client_address[0]


# --- responses ----------------------------------------------------------------------------------------------

def _error_page(req, status, message):
    titles = {400: 'Could not save', 403: 'Not allowed', 404: 'Not found', 405: 'Not allowed'}
    title = titles.get(status, 'Something went wrong')
    back = _back_target(req)
    body = ui.heading(title) + ui.notice(message, 'danger') + ui.button_link(back, 'Go back' if back != '/' else 'Go to the home page')
    return req.page(title, body, status=status)


def _back_target(req):
    referrer = req.headers.get('Referer', '')
    parts = urlsplit(referrer)
    if parts.path.startswith('/') and not parts.path.startswith('//') and parts.path != req.path:
        return parts.path + (f'?{parts.query}' if parts.query else '')
    return '/'


def _static(path):
    missing = Response(404, b'Not found', 'text/plain; charset=utf-8')
    try:
        target = (ASSET_ROOT / path[len('/assets/'):]).resolve()
        content_type = ASSET_TYPES.get(target.suffix.lower())
        if content_type is None or not target.is_relative_to(ASSET_ROOT.resolve()) or not target.is_file():
            return missing
    except (OSError, ValueError):  # e.g. an embedded NUL byte in the path
        return missing
    return Response(200, target.read_bytes(), content_type, [('Cache-Control', 'no-cache')])


def _log_failure(req, error):
    frames = traceback.extract_tb(error.__traceback__)
    where = ' <- '.join(f'{f.name}:{f.lineno}' for f in reversed(frames[-4:]))
    print(f'ERROR {req.method} {req.path} {type(error).__name__} at {where}', file=sys.stderr, flush=True)


# --- server --------------------------------------------------------------------------------------------------------

def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        timeout = CONNECTION_TIMEOUT_SECONDS
        server_version = 'BMS'
        sys_version = ''

        def log_message(self, *args):
            return  # access logs would carry resident names in URLs

        def do_GET(self):
            self._respond(handle(app, self))

        def do_POST(self):
            self._respond(handle(app, self))

        def do_PUT(self):
            self._respond(Response(405, b'Method not allowed', 'text/plain; charset=utf-8', close=True))

        do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_PUT

        def _respond(self, response):
            try:
                self.send_response(response.status)
                self.send_header('Content-Type', response.content_type)
                self.send_header('Content-Length', str(len(response.body)))
                if 'Cache-Control' not in dict(response.headers):
                    self.send_header('Cache-Control', 'no-store')
                for name, value in SECURITY_HEADERS + tuple(response.headers):
                    self.send_header(name, value)
                if app.config.cookie_secure:
                    self.send_header('Strict-Transport-Security', 'max-age=31536000')
                if response.close:
                    self.send_header('Connection', 'close')
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True

    return Handler


def serve(app):
    server = ThreadingHTTPServer((app.config.host, app.config.port), make_handler(app))
    server.daemon_threads = True
    if app.config.uses_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(app.config.tls_cert, app.config.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    scheme = 'https' if app.config.uses_tls else 'http'
    print(f'Vista Verde billing is running at {scheme}://{app.config.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
