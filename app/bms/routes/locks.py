"""Edit-lock endpoints used by app.js on edit pages (see app/bms/editing.py for the rules).

renew / release are raw JSON POSTs (CSRF checked by the router): taking or dropping a lease is the same no matter
how often it is repeated, so they need no idempotency key. status lets a read-only page see when the record frees.
"""
import json
import re

from .. import db, editing
from ..http import HttpError, Redirect, Response, route

# entity name -> who may hold it (the same roles that may open its edit page)
ENTITIES = {
    'accounts': ('admin', 'staff'),
    'readings': ('admin', 'staff'),
    'settings': ('admin',),
}
ENTITY_ID = re.compile(r'[A-Za-z0-9:_.-]{1,80}')


def _target(req, source):
    entity = source.get('entity', '')
    entity_id = source.get('entity_id', '')
    if entity not in ENTITIES or not ENTITY_ID.fullmatch(entity_id):
        raise HttpError(400, 'Unknown record.')
    if req.user['role'] not in ENTITIES[entity]:
        raise HttpError(403, 'Your account is not allowed to edit this.')
    return entity, entity_id


def _json(body):
    return Response(200, json.dumps(body).encode('utf-8'), 'application/json')


@route('POST', '/locks/renew', raw=True)
def renew(req):
    entity, entity_id = _target(req, req.form)
    holder = editing.acquire(entity, entity_id, req.user)
    return _json({'held': holder is None, 'holder': holder['user_name'] if holder else ''})


@route('POST', '/locks/release', raw=True)
def release(req):
    entity, entity_id = _target(req, req.form)
    with db.transaction() as c:
        editing.release(c, entity, entity_id, req.user)
    return _json({'released': True})


@route('GET', '/locks/status')
def status(req):
    entity, entity_id = _target(req, req.query)
    with db.connect() as c:
        holder = editing.holder_other_than(c, entity, entity_id, req.user)
    return _json({'free': holder is None, 'holder': holder['user_name'] if holder else ''})


@route('POST', '/locks/take-over', roles=('admin',))
def take_over(req, c):
    entity, entity_id = _target(req, req.form)
    editing.take_over(c, entity, entity_id, req.user)
    return Redirect(_same_site(req.form.get('back', '')),
                    ('warn', 'You are now editing this record. The other person cannot save over your changes.'))


def _same_site(back):
    """Only a path on this site (never '//host' or '/\\host', which browsers treat as another site)."""
    if back.startswith('/') and '//' not in back and '\\' not in back and len(back) <= 400:
        return back
    return '/'
