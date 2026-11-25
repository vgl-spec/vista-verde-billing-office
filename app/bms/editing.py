"""Several people editing at once, safely: edit locks (who is editing what) and record versions (merge or resolve).

1. Edit lock. Opening a record's edit page takes a short lease on it (LOCK_SECONDS), renewed by the page every
   RENEW_SECONDS while it stays open and released when it closes or saves. Anyone else opening the same record
   sees "Maria is editing this" and a read-only form that unlocks by itself when she is done. An administrator
   may take the record over. Leases expire, so a closed laptop never keeps a record locked.
2. Version. Every editable row has a local `version` that db.update raises on every change (also when a change
   arrives from another office). An edit form carries the version it was drawn from plus the field values it
   started with ("base"). Saving with an older version runs a three-way merge per field:
     only I changed it -> mine; only they changed it -> theirs; both changed it to different values -> conflict.
   Without conflicts the merge is saved (and the person is told what was kept from the other edit). With
   conflicts nothing is saved and the resolver shows each field side by side: they pick which value to keep.

Locks live in `edit_locks` (this database only, never synced: two offices do not see each other's screens).
"""
from datetime import datetime, timedelta, timezone

from . import UserError, db, ui

LOCK_SECONDS = 75           # a lease outlives two missed heartbeats, then frees itself
RENEW_SECONDS = 30
BASE_PREFIX = 'base__'
VERSION_FIELD = 'version'


# --- edit locks --------------------------------------------------------------------------------------------

def _stamp(seconds_from_now=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def acquire(entity, entity_id, user, c=None):
    """Take or renew the lease for `user`; returns the other person's lock row when someone else holds it.

    Pass `c` when already inside a transaction (a SQLite writer must not open a second one).
    """
    if c is None:
        with db.transaction() as own:
            return acquire(entity, entity_id, user, own)
    now = _stamp()
    # One statement: the lease is taken only when it is free, expired, or already this person's.
    c.execute('INSERT INTO edit_locks (entity, entity_id, user_id, user_name, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) '
              'ON CONFLICT (entity, entity_id) DO UPDATE SET user_id = excluded.user_id, user_name = excluded.user_name, '
              'acquired_at = CASE WHEN edit_locks.user_id = excluded.user_id THEN edit_locks.acquired_at ELSE excluded.acquired_at END, '
              'expires_at = excluded.expires_at WHERE edit_locks.user_id = excluded.user_id OR edit_locks.expires_at < ?',
              (entity, entity_id, user['id'], _display(user), now, _stamp(LOCK_SECONDS), now))
    holder = c.one('SELECT user_id, user_name, acquired_at FROM edit_locks WHERE entity = ? AND entity_id = ?', (entity, entity_id))
    return holder if holder and holder['user_id'] != user['id'] else None


def holder_other_than(c, entity, entity_id, user):
    """The active lock on this record held by someone else, or None (read inside a save's transaction)."""
    row = c.one('SELECT user_id, user_name, acquired_at FROM edit_locks WHERE entity = ? AND entity_id = ? AND expires_at >= ?',
                (entity, entity_id, _stamp()))
    return row if row and row['user_id'] != user['id'] else None


def release(c, entity, entity_id, user):
    c.execute('DELETE FROM edit_locks WHERE entity = ? AND entity_id = ? AND user_id = ?', (entity, entity_id, user['id']))


def take_over(c, entity, entity_id, user):
    """Administrator only: the lease moves to them. The other person's save is then refused, never lost silently."""
    c.execute('DELETE FROM edit_locks WHERE entity = ? AND entity_id = ?', (entity, entity_id))
    return acquire(entity, entity_id, user, c)


def lock_banner(holder, entity, entity_id, *, can_take_over, csrf, back):
    """The notice above a read-only edit form; app.js reloads the page when the lock frees."""
    since = db.office_clock(holder['acquired_at'])
    take = ''
    if can_take_over:
        take = ui.form('/locks/take-over', csrf, ui.raw(
            f'<input type="hidden" name="entity" value="{ui.esc(entity)}"><input type="hidden" name="entity_id" value="{ui.esc(entity_id)}">'
            f'<input type="hidden" name="back" value="{ui.esc(back)}">'),
            'Take over editing', danger=True,
            confirm=f"{holder['user_name']} may lose what they typed. Take over editing?")
    return ui.raw(f'<div class="lock-banner" data-lock-wait data-entity="{ui.esc(entity)}" data-entity-id="{ui.esc(entity_id)}">'
                  f'<p><strong>{ui.esc(holder["user_name"])} is editing this</strong> on another computer (since {ui.esc(since)}). '
                  'You can look, but editing opens here by itself when they are done.</p></div>') + take


def lock_marker(entity, entity_id):
    """Put inside an edit form this person holds: app.js renews the lease and lets it go when the page closes."""
    return ui.raw(f'<input type="hidden" data-lock-hold data-entity="{ui.esc(entity)}" data-entity-id="{ui.esc(entity_id)}" '
                  f'data-renew-seconds="{RENEW_SECONDS}">')


def _display(user):
    return user.get('display_name') or user.get('username') or 'Someone'


# --- versions and the three-way merge ---------------------------------------------------------------------
# The merge works on the form's raw text, before the form's own checks run. The merged text then goes through
# the same validation and save code as any other save, so a merge can never slip in a value a person could not type.

SPECIAL_FIELDS = ('csrf', 'key', VERSION_FIELD)


def version_fields(version, base):
    """Hidden inputs: the version the form was drawn from and each field's starting text."""
    inputs = [f'<input type="hidden" name="{VERSION_FIELD}" value="{int(version)}">']
    inputs += [f'<input type="hidden" name="{BASE_PREFIX}{ui.esc(name)}" value="{ui.esc(_as_text(value))}">' for name, value in base.items()]
    return ui.raw(''.join(inputs))


def posted_version(req):
    text = req.form.get(VERSION_FIELD, '')
    return int(text) if text.isdigit() else None


def merge_form(req, saved_text, fields, canonical=None):
    """Three-way merge of the posted form onto the record as saved now, field by field (raw form text).

    Fields only the other person changed take their value (written back into req.form); fields only this person
    changed keep theirs. Returns (conflicts, kept): conflicts = [(field, saved text, typed text)] both changed
    differently; kept = fields taken from the other person's save.

    `canonical` maps a field to a function giving the value's meaning (e.g. money text to centavos), so "150" typed
    and "₱150.00" saved count as the same value instead of a false conflict. Text it cannot read compares as text.
    """
    canonical = canonical or {}
    conflicts, kept = [], []
    for field in fields:
        base = req.form.get(BASE_PREFIX + field)
        if base is None:
            raise UserError('This page is out of date. Open it again, then make your change.')
        theirs, mine, base = _norm(saved_text.get(field, '')), _norm(req.form.get(field, '')), _norm(base)
        meaning = _meaning_reader(canonical.get(field))
        if meaning(mine) == meaning(theirs) or meaning(theirs) == meaning(base):
            continue
        if meaning(mine) == meaning(base):
            req.form[field] = theirs
            kept.append(field)
        else:
            conflicts.append((field, theirs, mine))
    return conflicts, kept


def _meaning_reader(parse):
    if parse is None:
        return lambda text: text

    def meaning(text):
        try:
            return ('value', parse(text))
        except (UserError, ValueError, ArithmeticError):
            return ('text', text)
    return meaning


def resolver_page(req, *, title, crumbs, action, labels, conflicts, saved_text, version, who, when, cancel):
    """Nothing was saved: each conflicting field side by side; the person picks what to keep and saves again."""
    e = ui.esc
    conflicted = {field for field, _, _ in conflicts}
    rows = ''.join(
        f'<tr><th scope="row">{e(labels.get(field, field))}</th>'
        f'<td><label class="pick"><input type="radio" name="{e(field)}" value="{e(theirs)}"> <span>{e(theirs) or "(empty)"}</span></label></td>'
        f'<td><label class="pick"><input type="radio" name="{e(field)}" value="{e(mine)}" checked> <span>{e(mine) or "(empty)"}</span></label></td></tr>'
        for field, theirs, mine in conflicts)
    # Every other field goes back as merged, and the base becomes "as saved now": saving again is a normal save.
    carried = ''.join(f'<input type="hidden" name="{e(name)}" value="{e(value)}">' for name, value in req.form.items()
                      if name not in conflicted and name not in SPECIAL_FIELDS and not name.startswith(BASE_PREFIX))
    bases = ''.join(f'<input type="hidden" name="{BASE_PREFIX}{e(name)}" value="{e(_norm(value))}">' for name, value in saved_text.items())
    hidden = ui.raw(carried + bases + f'<input type="hidden" name="{VERSION_FIELD}" value="{int(version)}">')
    table = ui.raw('<div class="table-wrap"><table class="ledger resolver"><thead><tr><th scope="col">Field</th>'
                   f'<th scope="col">Saved by {e(who)}</th><th scope="col">What you typed</th></tr></thead><tbody>{rows}</tbody></table></div>')
    lead = (f'{who} saved changes to this record ({when}) while you were editing it. Nothing of yours was saved yet. '
            'Their other changes are kept. For each line below, tick the value to keep, then save.')
    return ui.join([
        ui.heading(title, crumbs=crumbs),
        ui.notice(lead, 'warn'),
        ui.form(action, req.user['csrf'], ui.join([hidden, table]), 'Save my choices',
                secondary=ui.button_link(cancel, 'Cancel', kind='secondary')),
    ])


def last_change(c, entity, entity_id):
    """(who, when) of the newest audit entry for this record, for the resolver's explanation."""
    row = c.one('SELECT l.at, u.display_name FROM audit_log l LEFT JOIN users u ON u.id = l.user_id '
                'WHERE l.entity = ? AND l.entity_id = ? ORDER BY l.at DESC LIMIT 1', (entity, entity_id))
    if row is None:
        return 'Someone else', 'just now'
    return row['display_name'] or 'Someone else', db.office_minute(row['at'])


def kept_note(kept, labels, who):
    if not kept:
        return ''
    names = ', '.join(labels.get(field, field) for field in kept)
    return f' {who} had also changed {names} meanwhile; that was kept.'


def _norm(text):
    return str(text or '').replace('\r\n', '\n').strip()


def _as_text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'yes' if value else ''
    return str(value)
