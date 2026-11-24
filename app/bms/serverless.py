"""Helpers for running the app as a Vercel serverless function (see `api/index.py` and `vercel.json`)."""
from urllib.parse import parse_qsl, urlencode, urlsplit

# vercel.json rewrites every URL to /api/index and Vercel's Python runtime then sees only "/api/index", without the
# original query. The rewrite passes the original path in this parameter; the entry point restores it before
# routing. A visitor setting it by hand can only reach a page they could request directly, still behind its
# normal sign-in and role checks.
ORIGINAL_PATH_PARAM = '__path'


def restore_original_path(raw_path):
    """'/api/index?__path=accounts&q=x' -> '/accounts?q=x'; anything without the parameter is returned unchanged."""
    split = urlsplit(raw_path)
    params = parse_qsl(split.query, keep_blank_values=True)
    original = [value for key, value in params if key == ORIGINAL_PATH_PARAM]
    if not original:
        return raw_path
    rest = [(key, value) for key, value in params if key != ORIGINAL_PATH_PARAM]
    path = '/' + original[0].lstrip('/')
    return path + (f'?{urlencode(rest)}' if rest else '')
