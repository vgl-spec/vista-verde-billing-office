"""Audit trail: who changed what, with before/after snapshots (synced, append-only)."""
import json

from . import db


def actor_id(user):
    """`user` is a session user dict, or None for system actions (startup, importer, sync)."""
    return user['id'] if user else None


def log(c, user, action, entity, entity_id, before=None, after=None):
    c.insert('audit_log', {
        'at': db.now(),
        'user_id': actor_id(user),
        'action': action,
        'entity': entity,
        'entity_id': str(entity_id),
        'before': _dump(before),
        'after': _dump(after),
    })


def _dump(snapshot):
    return None if snapshot is None else json.dumps(snapshot, sort_keys=True, default=str)
