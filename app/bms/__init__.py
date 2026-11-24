"""Vista Verde HOA billing. `main()` is the single entry point (see app/server.py).

Imports inside `main` are deliberate: the package must import cleanly for tools and tests without
dragging in the web layer or optional dependencies.
"""
import sys
from datetime import datetime, timedelta, timezone


class UserError(ValueError):
    """A problem the person can fix; the message is plain language and safe to show them."""


REQUEST_KEY_RETENTION = timedelta(days=30)
USAGE = 'Usage: python app/server.py [create-admin <username>]'


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    from . import auth, db
    try:
        settings = bootstrap()
    except UserError as error:
        raise SystemExit(f'Cannot start: {error}') from None
    if argv[:1] == ['create-admin'] and len(argv) == 2:
        return _create_admin(argv[1], auth, db)
    if argv:
        raise SystemExit(USAGE)
    _serve(settings)


def bootstrap(env=None):
    """Shared start-up for the office server and the serverless (Vercel) entry: config, migrations,
    node identity, housekeeping. Raises UserError on bad configuration."""
    from . import config, db, migrate
    settings = config.load(env)
    db.configure(settings)
    migrate.run()
    with db.transaction() as c:
        db.ensure_node(c)
        _run_startup_maintenance(c)
    return settings


def create_app(settings):
    """Register every route and open first-run setup when no user exists yet. The setup code is printed
    to the console (office) or the function log (Vercel) only, never shown in a page."""
    from . import auth, db, http, routes  # noqa: F401 - importing routes registers every handler
    app = http.App(settings)
    with db.transaction() as c:
        needs_setup = auth.count_users(c) == 0
        code = app.setup_gate.issue(c) if needs_setup else None
    if needs_setup:
        print('No users exist yet. First-run setup:', flush=True)
        print(f'  open /setup in a browser and enter this one-time code: {code}', flush=True)
        print('  (or run: python app/server.py create-admin <username>)', flush=True)
    return app


def _serve(settings):
    from . import http, sync
    app = create_app(settings)
    sync.start_worker(settings)  # idles until an admin sets up and enables sync
    http.serve(app)


def _create_admin(username, auth, db):
    import getpass
    password = getpass.getpass(f'Password (at least {auth.PASSWORD_MIN_LENGTH} characters): ')
    if password != getpass.getpass('Repeat password: '):
        raise SystemExit('The passwords did not match; no user was created.')
    try:
        with db.transaction() as c:
            auth.create_user(c, username, username, password, 'admin')
    except UserError as error:
        raise SystemExit(f'No user created: {error}') from None
    print(f'Administrator {username.strip().lower()} created. Start the server and sign in.')


def _run_startup_maintenance(c):
    from . import alerts, billing
    cutoff = (datetime.now(timezone.utc) - REQUEST_KEY_RETENTION).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    c.execute('DELETE FROM requests WHERE created_at < ?', (cutoff,))
    if billing.get_setting(c, 'policy_confirmed'):
        alerts.resolve_alert(c, 'policy_unconfirmed', 'billing-policy', None)
    else:
        alerts.raise_alert(c, 'policy_unconfirmed', 'billing-policy',
                           'Billing rules still need the office to confirm them (penalty, dues, rates).',
                           '/admin', 'warn')
