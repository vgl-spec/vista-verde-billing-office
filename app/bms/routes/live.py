"""Live updates: every open page learns within a moment that records changed, without reloading.

GET /live?since=<change no.> answers {"seq": newest change no., "tables": [tables changed since], "retry_ms": n}.
On the office server it waits (long poll, up to LONG_POLL_SECONDS) and answers the moment a save commits, so
other computers see it in under a second. Online (serverless, several instances) it answers at once and the page
asks again after ONLINE_POLL_MS. The page then fetches its own current URL again and app.js slides the changed
rows into place (no full reload). Read-only: it only reads the `events` change log.
"""
import json
import time

from .. import db
from ..http import HttpError, Response, route

LONG_POLL_SECONDS = 25        # below the server's 60 s socket timeout and most proxies' idle limits
RECHECK_SECONDS = 2           # also catches changes committed by another process (a sync pull)
ONLINE_POLL_MS = 15000        # serverless: each poll is a paid function call, so ask rarely
MAX_TABLE_SCAN = 5000         # after a large import, just say "everything" instead of listing tables
MAX_SEQ = 10 ** 15


@route('GET', '/live')
def live_changes(req):
    text = req.query.get('since', '')
    if not text.isdigit() or int(text) > MAX_SEQ:
        raise HttpError(400, 'since must be a change number.')
    since = int(text)
    online = req.app.config.uses_postgres
    deadline = time.monotonic() + (0 if online else LONG_POLL_SECONDS)
    while True:
        with db.connect() as c:  # never hold a connection while waiting
            current = db.latest_change(c)
            if since and current > since:
                tables = _changed_tables(c, since, current)
                break
        remaining = deadline - time.monotonic()
        if since == 0 or remaining <= 0:
            tables = []
            break
        db.wait_for_change(min(RECHECK_SECONDS, remaining))
    body = {'seq': current, 'tables': tables, 'retry_ms': ONLINE_POLL_MS if online else 0}
    return Response(200, json.dumps(body).encode('utf-8'), 'application/json')


def _changed_tables(c, since, current):
    if current - since > MAX_TABLE_SCAN:
        return ['*']
    # A range on the primary key: reads only the new change rows.
    return sorted(row['tbl'] for row in c.all('SELECT DISTINCT tbl FROM events WHERE seq > ? AND seq <= ?', (since, current)))
