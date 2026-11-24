"""Environment -> immutable Config. The only module that reads os.environ."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import UserError

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = APP_DIR / 'data' / 'bms.sqlite3'
DEFAULT_PORT = 8765
DEFAULT_HOST = '0.0.0.0'  # LAN clients and the online platform proxy both need a non-loopback bind
NODE_SHORT_PATTERN = re.compile(r'[A-Z0-9]{2,6}')
TRUE_VALUES = {'1', 'true', 'yes', 'on'}
FALSE_VALUES = {'0', 'false', 'no', 'off'}


@dataclass(frozen=True)
class Config:
    mode: str                 # 'lan' | 'online'
    database_url: str         # postgres URL (online) or ''
    db_path: Path             # sqlite file (lan)
    host: str
    port: int
    tls_cert: str
    tls_key: str
    node_id: str              # '' -> generated on first start and stored in sync_state
    node_short: str           # '' -> derived from node id
    sync_url: str
    sync_token: str
    cookie_secure: bool
    sync_allow_http: bool = False  # docker/LAN playground only; real hubs are reached over https

    @property
    def uses_postgres(self):
        return bool(self.database_url)

    @property
    def uses_tls(self):
        return bool(self.tls_cert)


def load(env=None):
    """Read and validate configuration; raise UserError so main() can print it and exit."""
    env = os.environ if env is None else env
    mode = env.get('BMS_MODE', 'lan').strip().lower()
    if mode not in ('lan', 'online'):
        raise UserError(f"BMS_MODE must be 'lan' or 'online', got {mode!r}.")
    database_url = env.get('DATABASE_URL', '').strip()
    if mode == 'online' and not database_url:
        raise UserError('BMS_MODE=online needs DATABASE_URL (Postgres connection string).')
    tls_cert, tls_key = env.get('BMS_TLS_CERT', '').strip(), env.get('BMS_TLS_KEY', '').strip()
    if bool(tls_cert) != bool(tls_key):
        raise UserError('Set both BMS_TLS_CERT and BMS_TLS_KEY, or neither.')
    sync_url, sync_token = env.get('BMS_SYNC_URL', '').strip(), env.get('BMS_SYNC_TOKEN', '').strip()
    if sync_url and not sync_token:
        raise UserError('BMS_SYNC_URL is set but BMS_SYNC_TOKEN is missing; refusing to start.')
    node_short = env.get('BMS_NODE_SHORT', '').strip().upper()
    if node_short and not NODE_SHORT_PATTERN.fullmatch(node_short):
        raise UserError('BMS_NODE_SHORT must be 2-6 letters or digits.')
    return Config(
        mode=mode,
        database_url=database_url,
        db_path=Path(env.get('BMS_DB') or DEFAULT_DB_PATH),
        host=env.get('BMS_HOST', DEFAULT_HOST).strip(),
        port=_parse_port(env.get('BMS_PORT') or env.get('PORT') or str(DEFAULT_PORT)),
        tls_cert=tls_cert,
        tls_key=tls_key,
        node_id=env.get('BMS_NODE_ID', '').strip(),
        node_short=node_short,
        sync_url=sync_url,
        sync_token=sync_token,
        cookie_secure=_cookie_secure(env.get('BMS_COOKIE_SECURE'), bool(tls_cert) or mode == 'online'),
        sync_allow_http=_flag(env.get('BMS_SYNC_ALLOW_HTTP'), 'BMS_SYNC_ALLOW_HTTP'),
    )


def _parse_port(text):
    try:
        port = int(text)
    except ValueError:
        raise UserError(f'Port must be a number, got {text!r}.') from None
    if not 0 <= port <= 65535:
        raise UserError(f'Port out of range: {port}.')
    return port


def _cookie_secure(raw, automatic):
    if raw is None or raw.strip() == '':
        return automatic
    return _flag(raw, 'BMS_COOKIE_SECURE')


def _flag(raw, name):
    if raw is None or raw.strip() == '':
        return False
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise UserError(f'{name} must be true or false, got {raw!r}.')
