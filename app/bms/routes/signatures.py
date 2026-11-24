"""Each person's own signature image, printed above "Authorized Signature" on the receipts they record."""
import base64

from .. import UserError, audit, db, ui
from ..http import NotFound, Redirect, Response, route

MAX_SIGNATURE_BYTES = 300 * 1024
# Judged by the file's first bytes, never by its name or the browser's claim: SVG and anything else is refused.
IMAGE_SIGNATURES = ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'))
PAGE = '/account/signature'


def signature_url(c, user_id):
    """The image address with a version, or '' when that person has no signature."""
    updated = c.scalar('SELECT updated_at FROM signatures WHERE user_id = ?', (user_id,)) if user_id else None
    return f"/signatures/{user_id}?v={updated.replace(':', '').replace('.', '')}" if updated else ''


@route('GET', PAGE)
def signature_page(req):
    with db.connect() as c:
        image = signature_url(c, req.user['id'])
    current = (ui.raw(f'<p class="sign-preview"><img src="{ui.esc(image)}" alt="Your signature as it prints"></p>') if image
               else ui.notice('No signature yet. Receipts you record print with a blank signature line.'))
    upload = ui.form(PAGE, req.user['csrf'], ui.field(
        'Signature picture', 'signature', type='file',
        hint='Sign in black ink on white paper, take a clear photo or scan, crop it close. PNG or JPG, up to 300 KB.',
        attrs='accept="image/png,image/jpeg"'), 'Save signature', multipart=True)
    parts = [ui.heading('My signature', 'Printed above "Authorized Signature" on the receipts you record.',
                        crumbs=[('Cashier', '/payments/cashier'), 'My signature']), current, upload]
    if image:
        parts.append(ui.section('Remove it', ui.form(f'{PAGE}/remove', req.user['csrf'], '', 'Remove my signature',
                                                     danger=True, confirm='Remove your signature from future receipt printouts?')))
    return req.page('My signature', ui.join(parts), active='payments')


@route('POST', PAGE, max_body=MAX_SIGNATURE_BYTES + 64 * 1024)
def save_signature(req, c):
    upload = req.files.get('signature')
    if upload is None or not upload.data:
        raise UserError('Choose the signature picture first.')
    if len(upload.data) > MAX_SIGNATURE_BYTES:
        raise UserError('That picture is larger than 300 KB. Crop it closer or save it smaller.')
    content_type = next((kind for magic, kind in IMAGE_SIGNATURES if upload.data.startswith(magic)), None)
    if content_type is None:
        raise UserError('Use a PNG or JPG picture.')
    # Saving the same person twice simply replaces the picture, so a repeated press changes nothing more.
    c.execute('INSERT INTO signatures (user_id, content_type, data, updated_at) VALUES (?, ?, ?, ?) '
              'ON CONFLICT (user_id) DO UPDATE SET content_type = excluded.content_type, data = excluded.data, '
              'updated_at = excluded.updated_at',
              (req.user['id'], content_type, base64.b64encode(upload.data).decode('ascii'), db.now()))
    audit.log(c, req.user, 'signature.save', 'user', req.user['id'], None, {'bytes': len(upload.data), 'type': content_type})
    return Redirect(PAGE, ('success', 'Signature saved. It prints on the receipts you record.'))


@route('POST', f'{PAGE}/remove')
def remove_signature(req, c):
    c.execute('DELETE FROM signatures WHERE user_id = ?', (req.user['id'],))
    audit.log(c, req.user, 'signature.remove', 'user', req.user['id'], None, None)
    return Redirect(PAGE, ('success', 'Signature removed.'))


@route('GET', '/signatures/{id}')
def signature_image(req):
    with db.connect() as c:
        row = c.one('SELECT content_type, data FROM signatures WHERE user_id = ?', (req.params['id'],))
    if row is None:
        raise NotFound('No signature.')
    return Response(200, base64.b64decode(row['data']), row['content_type'], [('Cache-Control', 'private, max-age=86400')])
