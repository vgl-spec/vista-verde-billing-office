"""Users, password hashing, DB-backed sessions, sign-in rate limit and the first-run setup code."""
import functools
import hashlib
import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import UserError, audit, db

ROLES = ('admin', 'staff', 'cashier')
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
PASSWORD_MIN_LENGTH = 9
PASSWORD_MAX_LENGTH = 256
SESSION_TTL = timedelta(hours=8)
ATTEMPT_LIMIT = 10
ATTEMPT_WINDOW = timedelta(minutes=5)
USERNAME_PATTERN = re.compile(r'[a-z0-9._-]{3,32}')
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'
SETUP_CODE_BYTES = 9


# --- passwords -------------------------------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(SALT_BYTES)
    return f'pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${_derive(password, salt, PBKDF2_ITERATIONS).hex()}'


def verify_password(password, stored):
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split('$')
        if scheme != 'pbkdf2_sha256':
            return False
        expected = bytes.fromhex(hash_hex)
        actual = _derive(password, bytes.fromhex(salt_hex), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _derive(password, salt, iterations):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)


@functools.cache
def _dummy_hash():
    return hash_password(secrets.token_urlsafe(16))


def validate_password(password):
    if len(password) < PASSWORD_MIN_LENGTH:
        raise UserError(f'The password must be at least {PASSWORD_MIN_LENGTH} characters long.')
    if len(password) > PASSWORD_MAX_LENGTH:
        raise UserError(f'The password must be at most {PASSWORD_MAX_LENGTH} characters long.')
    return password


def normalize_username(username):
    cleaned = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(cleaned):
        raise UserError('A username is 3 to 32 letters, digits, dots, dashes or underscores.')
    return cleaned


# --- users ---------------------------------------------------------------------------------------------

def count_users(c):
    return c.scalar('SELECT COUNT(*) FROM users')


def create_user(c, username, display_name, password, role, *, actor=None, must_change_password=False):
    """Create a user and return the new id. Raises UserError for bad input or a taken username."""
    if role not in ROLES:
        raise UserError(f'Role must be one of {", ".join(ROLES)}.')
    cleaned = normalize_username(username)
    name = display_name.strip() or cleaned
    if len(name) > 80:
        raise UserError('The display name must be at most 80 characters.')
    password_hash = hash_password(validate_password(password))
    if c.scalar('SELECT 1 FROM users WHERE username = ?', (cleaned,)):
        raise UserError('That username is already taken.')
    try:
        user_id = c.insert('users', {
            'username': cleaned, 'display_name': name, 'role': role, 'password_hash': password_hash,
            'active': 1, 'must_change_password': int(must_change_password), 'created_at': db.now(),
        })
    except db.IntegrityError:
        raise UserError('That username is already taken.') from None
    audit.log(c, actor, 'user.create', 'user', user_id, None, {'username': cleaned, 'role': role})
    return user_id


def set_password(c, user_id, new_password, *, actor, keep_session_hash=None):
    """Replace a password and end every other session of that user."""
    c.execute('UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?',
              (hash_password(validate_password(new_password)), user_id))
    destroy_user_sessions(c, user_id, except_hash=keep_session_hash)
    audit.log(c, actor, 'user.password_change', 'user', user_id)


def require_password_change(c, user_id, actor):
    """Admin action: the user must choose a new password at their next page view."""
    c.execute('UPDATE users SET must_change_password = 1 WHERE id = ?', (user_id,))
    audit.log(c, actor, 'user.require_password_change', 'user', user_id)


def authenticate(c, username, password):
    """User row for a correct username + password, else None. Same work either way (no timing leak)."""
    user = c.one('SELECT * FROM users WHERE username = ? AND active = 1', (username.strip().lower(),))
    stored = user['password_hash'] if user else _dummy_hash()
    matched = verify_password(password, stored)
    return user if user and matched else None


# --- sign-in rate limit ------------------------------------------------------------------------------------

def is_rate_limited(c, bucket):
    """True after ATTEMPT_LIMIT failures from one bucket (peer address) inside ATTEMPT_WINDOW."""
    cutoff = _timestamp(datetime.now(timezone.utc) - ATTEMPT_WINDOW)
    c.execute('DELETE FROM login_attempts WHERE at < ?', (cutoff,))
    return c.scalar('SELECT COUNT(*) FROM login_attempts WHERE peer = ?', (bucket,)) >= ATTEMPT_LIMIT


def record_failure(c, bucket):
    c.execute('INSERT INTO login_attempts (id, peer, at) VALUES (?, ?, ?)', (db.new_id(), bucket, db.now()))


def _timestamp(moment):
    return moment.strftime(TIMESTAMP_FORMAT)


# --- sessions ---------------------------------------------------------------------------------------------------

def create_session(c, user_id):
    """Store a new session; returns (cookie token, csrf token). Only the token's hash is stored."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    c.execute('DELETE FROM sessions WHERE expires_at < ?', (_timestamp(now),))
    c.execute('INSERT INTO sessions (token_hash, user_id, csrf, expires_at, created_at) VALUES (?, ?, ?, ?, ?)',
              (_hash_token(token), user_id, csrf, _timestamp(now + SESSION_TTL), _timestamp(now)))
    return token, csrf


def find_session_user(c, token):
    """Session user dict (with 'csrf') for a live token of an active user, else None."""
    if not token or len(token) > 200:
        return None
    row = c.one(
        'SELECT u.id, u.username, u.display_name, u.role, u.must_change_password, s.csrf, s.token_hash '
        'FROM sessions s JOIN users u ON u.id = s.user_id '
        'WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1',
        (_hash_token(token), db.now()))
    if row:
        row['must_change_password'] = bool(row['must_change_password'])
    return row


def destroy_session(c, token):
    c.execute('DELETE FROM sessions WHERE token_hash = ?', (_hash_token(token),))


def destroy_user_sessions(c, user_id, *, except_hash=None):
    c.execute('DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?', (user_id, except_hash or ''))


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


# --- first-run setup ---------------------------------------------------------------------------------------------

class SetupGate:
    """One-time code, printed on the server console only, that unlocks creating the first admin."""

    def __init__(self):
        self._code = None
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._code is not None

    def issue(self, c=None):
        """With a connection, the code is kept in this node's local `sync_state` (never replicated) and
        reused on every restart until the first admin exists; a code that changed on each restart sent
        staff to a "code is not right" page after any reboot."""
        with self._lock:
            stored = c.scalar("SELECT value FROM sync_state WHERE key = 'setup_code'") if c is not None else None
            self._code = stored or secrets.token_urlsafe(SETUP_CODE_BYTES)
            if c is not None and not stored:
                c.execute("INSERT INTO sync_state (key, value) VALUES ('setup_code', ?)", (self._code,))
            return self._code

    def matches(self, candidate):
        code = self._code
        return code is not None and hmac.compare_digest(candidate.strip().encode('utf-8'), code.encode('utf-8'))

    def clear(self, c=None):
        with self._lock:
            self._code = None
            if c is not None:
                c.execute("DELETE FROM sync_state WHERE key = 'setup_code'")
