"""Open-issue alerts shown under the bell. One row per (kind, key); raising again reopens it."""
from . import db

KINDS = frozenset({
    'readings_missing', 'reading_flag', 'bills_draft', 'overdue', 'disconnect_candidate',
    'import_issue', 'sync_conflict', 'sync_error', 'policy_unconfirmed',
})
SEVERITIES = ('info', 'warn', 'urgent')


def raise_alert(c, kind, key, title, link, severity='warn'):
    if kind not in KINDS:
        raise ValueError(f'Unknown alert kind: {kind!r}')
    if severity not in SEVERITIES:
        raise ValueError(f'Unknown alert severity: {severity!r}')
    c.execute(
        'INSERT INTO alerts (id, kind, key, title, link, severity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT (kind, key) DO UPDATE SET title = excluded.title, link = excluded.link, '
        'severity = excluded.severity, '
        # A reopened alert counts as new; one still open keeps its original age.
        'created_at = CASE WHEN alerts.resolved_at IS NULL THEN alerts.created_at ELSE excluded.created_at END, '
        'resolved_at = NULL, resolved_by = NULL',
        (db.new_id(), kind, str(key), title, link, severity, db.now()),
    )


def resolve_alert(c, kind, key, user):
    """Returns True if an open alert was closed."""
    affected = c.execute(
        'UPDATE alerts SET resolved_at = ?, resolved_by = ? WHERE kind = ? AND key = ? AND resolved_at IS NULL',
        (db.now(), user['id'] if user else None, kind, str(key)),
    ).rowcount
    return affected == 1


def open_alerts(c, limit=200):
    return c.all(
        "SELECT * FROM alerts WHERE resolved_at IS NULL "
        "ORDER BY CASE severity WHEN 'urgent' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, created_at DESC LIMIT ?",
        (limit,),
    )


def count_open(c):
    return c.scalar('SELECT COUNT(*) FROM alerts WHERE resolved_at IS NULL')
