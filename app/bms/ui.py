"""Layout shell and HTML components for the Vista Verde billing screens.

Every component returns `Safe`, a `str` subclass that means "already valid HTML".
Escaping rule, applied everywhere in this module:

* Text parameters (titles, labels, cell values, headers, `text`) are HTML-escaped unless the value
  is `Safe`, so `table(h, [[account['name'], pill('Paid')]])` escapes the name and keeps the pill.
* Markup slots (`body`, `actions`, `secondary`, `action`, `attrs`) take trusted HTML as written.
  Never put user input in them without `esc()`.
* `Safe + str` escapes the `str`; plain f-strings do not. Wrap a hand-written f-string with `raw()`
  only after escaping each user value inside it.
"""

from __future__ import annotations

import hashlib
import html
import itertools
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

ASSET_BASE = "/assets"
BRAND_NAME = "Vista Verde"
BRAND_TAGLINE = "HOA Billing Office"

# Atomic in CPython; gives each rendered control a unique id for label/hint wiring.
_control_ids = itertools.count(1)


class Safe(str):
    """HTML that has already been escaped or written by us."""

    __slots__ = ()

    def __add__(self, other):
        return Safe(str.__add__(self, _text(other)))

    def __radd__(self, other):
        return Safe(_text(other) + str(self))


def _text(value) -> str:
    """Escape `value` for HTML text or a quoted attribute, unless it is already `Safe`."""
    if isinstance(value, Safe):
        return str(value)
    return html.escape(str(value), quote=True)


def esc(value) -> Safe:
    """Escape untrusted text so it can be mixed into hand-written markup."""
    return Safe(_text(value))


def raw(markup: str) -> Safe:
    """Mark hand-written markup as trusted. Escape any user value inside it first."""
    return Safe(markup)


def join(parts: Iterable, sep: str = "") -> Safe:
    """Join text and `Safe` parts, escaping the text ones."""
    return Safe(sep.join(_text(part) for part in parts))


def _href(value: str) -> str:
    """Fail closed on anything that is not a same-site path, fragment, query, http(s) or mailto."""
    target = str(value).strip()
    allowed = target.startswith(("/", "#", "?", "http://", "https://", "mailto:")) and not target.startswith("//")
    return _text(target if allowed else "#")


def _one_of(value: str, allowed: Iterable[str], what: str) -> str:
    if value not in allowed:
        raise ValueError(f"{what} must be one of {sorted(allowed)}, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Icons (inline SVG paths, 24x24, stroke only)
# ---------------------------------------------------------------------------

_ICON_PATHS = {
    "home": "M3 11l9-8 9 8M5 10v10h14V10",
    "accounts": "M16 8a4 4 0 1 0-8 0 4 4 0 0 0 8 0zM4 21c0-4 3.6-6 8-6s8 2 8 6",
    "cycle": "M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z",
    "payments": "M3 7h18v10H3zM12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
    "reports": "M5 20V10M12 20V4M19 20v-7",
    "import": "M12 16V4m0 0L7 9m5-5l5 5M4 20h16",
    "admin": "M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M6 15v4",
    "menu": "M4 6h16M4 12h16M4 18h16",
    "bell": "M6 17v-6a6 6 0 1 1 12 0v6l2 2H4zM10 21h4",
    "sun": "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5",
    "moon": "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z",
    "copy": "M9 9h11v11H9zM5 15H4V4h11v1",
    "check": "M5 12.5l4.5 4.5L19 7",
    "sort": "M8 10l4-4 4 4M8 14l4 4 4-4",
    "sort-asc": "M8 14l4-4 4 4",
    "sort-desc": "M8 10l4 4 4-4",
}


def _icon(name: str) -> str:
    return f'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="{_ICON_PATHS[name]}"/></svg>'


# ---------------------------------------------------------------------------
# Navigation model: feeds the sidebar, the phone tab bar and the Menu sheet.
# ---------------------------------------------------------------------------

EVERYONE = ("admin", "staff", "cashier")
OFFICE = ("admin", "staff")
ADMIN_ONLY = ("admin",)

# (key, href, label, roles, shown_in_phone_tab_bar, pages). `pages` are the section's own screens, listed under it
# in the sidebar while the section is open (and as a row of links on phones), so no screen hides behind a button.
# Roles mirror each route's own `roles=`; tests/test_ui.py opens every listed page as every role to keep them equal.
_NAV = (
    ("home", "/", "Home", EVERYONE, True, ()),
    ("accounts", "/accounts", "Homeowners", EVERYONE, True, (
        ("/accounts", "All homeowners", EVERYONE), ("/accounts/new", "Add a homeowner", OFFICE))),
    ("cycle", "/cycle", "Reading & SOA", EVERYONE, True, (
        ("/cycle", "Monthly steps", EVERYONE), ("/cycle/routes", "Reading sheets", OFFICE),
        ("/cycle/readings", "Enter readings", OFFICE), ("/cycle/bills", "Bills and statements", OFFICE),
        ("/cycle/penalties", "Penalties", OFFICE))),
    ("payments", "/payments/cashier", "Cashier", EVERYONE, True, (
        ("/payments/cashier", "Take a payment", EVERYONE), ("/payments", "Payments", EVERYONE),
        ("/account/signature", "My signature", EVERYONE), ("/payments/audit", "Audit trail", ADMIN_ONLY))),
    ("reports", "/reports", "Reports", OFFICE, False, (
        ("/reports", "All reports", OFFICE), ("/reports/collections", "Collections", OFFICE),
        ("/reports/aging", "Unpaid balances", OFFICE), ("/reports/disconnections", "Disconnection list", OFFICE),
        ("/reports/consumption", "Water use and system loss", OFFICE), ("/reports/usage", "3-month comparison", OFFICE))),
    ("import", "/import", "Import Excel", ADMIN_ONLY, False, ()),
    ("admin", "/admin", "Admin", ADMIN_ONLY, False, (
        ("/admin", "Settings", ADMIN_ONLY), ("/admin/users", "Users", ADMIN_ONLY), ("/admin/rates", "Water rates", ADMIN_ONLY),
        ("/admin/maynilad", "Maynilad bill", ADMIN_ONLY), ("/admin/receipts", "Receipt printing", ADMIN_ONLY),
        ("/admin/backup", "Backup", ADMIN_ONLY), ("/admin/sync", "Sync", ADMIN_ONLY))),
)

_ROLE_LABELS = {"admin": "Administrator", "staff": "Office staff", "cashier": "Cashier"}


def _nav_for(role: str):
    """The sections this role may open, each with only the pages this role may open."""
    return [(key, href, label, roles, in_tabs, tuple(page for page in pages if role in page[2]))
            for key, href, label, roles, in_tabs, pages in _NAV if role in roles]


def nav_pages(role: str):
    """Every page the navigation offers this role (for tests and the Menu sheet)."""
    return [page[0] for item in _nav_for(role) for page in item[5]] + [item[1] for item in _nav_for(role)]


def _current_page(pages, path: str) -> str:
    """The listed page this path belongs to: the longest href that is the path or a parent of it."""
    matches = [href for href, _, _ in pages if path == href or path.startswith(href.rstrip("/") + "/")]
    return max(matches, key=len, default="")


# ---------------------------------------------------------------------------
# Small components
# ---------------------------------------------------------------------------

def dot(label: str = "Needs attention") -> Safe:
    """Red guide dot: the next action waits here. The label is read by screen readers."""
    return Safe(_dot_markup(label))


def _dot_markup(label: str = "Needs attention") -> str:
    return f'<span class="dot"><span class="sr">{_text(label)}</span></span>'


def money(cents: int) -> Safe:
    """Format integer centavos as pesos, e.g. 105000 -> ₱1,050.00 and -250 -> -₱2.50."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"money() takes integer centavos, got {cents!r}")
    whole, fraction = divmod(abs(cents), 100)
    return Safe(f"{'-' if cents < 0 else ''}₱{whole:,}.{fraction:02d}")


def pill(text: str, kind: str = "info") -> Safe:
    """Status label: a coloured dot plus words, so meaning never rests on colour alone."""
    _one_of(kind, ("info", "success", "warn", "danger", "neutral"), "pill kind")
    return Safe(f'<span class="status status-{kind}">{_text(text)}</span>')


def notice(text: str, kind: str = "info", *, action: str = "") -> Safe:
    """Inline message. `action` is trusted markup, typically a `button_link`."""
    _one_of(kind, ("info", "success", "warn", "danger"), "notice kind")
    label = {"info": "Note", "success": "Done", "warn": "Please check", "danger": "Problem"}[kind]
    role = "alert" if kind in ("warn", "danger") else "status"
    action_html = f'<span class="notice-action">{action}</span>' if action else ""
    return Safe(
        f'<div class="notice notice-{kind}" role="{role}"><strong class="notice-label">{label}:</strong>'
        f"{_text(text)}{action_html}</div>"
    )


def button_link(href: str, label: str, *, kind: str = "primary", dot: bool = False) -> Safe:
    """A link that looks like a button. `dot=True` hangs a red guide dot off its corner."""
    _one_of(kind, ("primary", "secondary"), "button kind")
    dot_html = _dot_markup() if dot else ""
    return Safe(f'<a class="btn btn-{kind}" href="{_href(href)}">{_text(label)}{dot_html}</a>')


def empty(text: str, action: str = "") -> Safe:
    """Empty state. `action` is trusted markup, typically a `button_link`."""
    return Safe(_empty_markup(text, action))


# Per-render form keys (one fresh idempotency key per form) change on every draw without the row changing.
_VOLATILE = re.compile(r'name="key" value="[^"]*"')


def _version(markup: str) -> str:
    """Short fingerprint of a row's content: a live update replaces (and flashes) only rows whose content changed."""
    return hashlib.blake2s(_VOLATILE.sub("", markup).encode("utf-8"), digest_size=6).hexdigest()


def _empty_markup(text: str, action: str = "") -> str:
    return f'<div class="empty"><p>{_text(text)}</p>{action}</div>'


def well(body: str) -> Safe:
    """The one boxed area of a page, for its primary task."""
    return Safe(f'<div class="well">{body}</div>')


def facts(pairs: Iterable[Sequence]) -> Safe:
    """Label/value details separated by hairlines. Each pair is (label, value)."""
    rows = "".join(f"<div><dt>{_text(label)}</dt><dd>{_text(value)}</dd></div>" for label, value in pairs)
    return Safe(f'<dl class="facts">{rows}</dl>')


def figures(items: Iterable[Sequence]) -> Safe:
    """Row of headline numbers. Each item is (label, value) or (label, value, note)."""
    cells = []
    for item in items:
        label, value, *rest = item
        note = f'<span class="figure-note">{_text(rest[0])}</span>' if rest and rest[0] else ""
        cells.append(
            f'<div class="figure"><span class="figure-label">{_text(label)}</span>'
            f'<strong class="figure-value">{_text(value)}</strong>{note}</div>'
        )
    return Safe(f'<div class="figures-wrap"><div class="figures">{"".join(cells)}</div></div>')


def tabs(items: Iterable[Mapping]) -> Safe:
    """Sub-navigation. Each item: {'label', 'href', 'current': bool, 'dot': bool}."""
    links = []
    for item in items:
        current = ' aria-current="page"' if item.get("current") else ""
        marker = dot() if item.get("dot") else ""
        links.append(f'<a href="{_href(item["href"])}"{current}>{_text(item["label"])}{marker}</a>')
    return Safe(f'<nav class="tabs" aria-label="Sections">{"".join(links)}</nav>')


_STEP_STATES = {"done": "Done", "current": "Do this now", "todo": "Not started"}


def steps(items: Iterable[Mapping]) -> Safe:
    """Numbered monthly-cycle stepper.

    Each item: {'title', 'status', 'href', 'state': 'done'|'current'|'todo', 'dot': bool,
    'number': optional int, defaults to its 1-based position}. Unknown states render as 'todo'.
    """
    entries = []
    for position, item in enumerate(items, start=1):
        state = item.get("state") if item.get("state") in _STEP_STATES else "todo"
        number = item.get("number", position)
        mark = "✓" if state == "done" else _text(number)
        current = ' aria-current="step"' if state == "current" else ""
        marker = dot() if item.get("dot") else ""
        entries.append(
            f'<li class="step step-{state}"><a class="step-link" href="{_href(item["href"])}"{current}>'
            f'<span class="step-no" aria-hidden="true">{mark}</span>'
            f'<span><span class="step-title"><span class="sr">Step {_text(number)}: </span>{_text(item["title"])}</span>'
            f'<span class="step-status"><span class="sr">{_STEP_STATES[state]}. </span>{_text(item.get("status", ""))}</span>'
            f"</span></a>{marker}</li>"
        )
    return Safe(f'<ol class="steps">{"".join(entries)}</ol>')


def pagination(page: int, pages: int, base: str, *, total: int | None = None, noun: str = "record") -> Safe:
    """Previous / Next with a plain-words position. `base` is a path, optionally with a query."""
    if pages <= 1:
        return Safe("")
    page = min(max(int(page), 1), pages)
    joiner = "&" if "?" in base else "?"

    def link(target: int, label: str) -> str:
        return f'<a class="btn btn-secondary" href="{_href(f"{base}{joiner}page={target}")}">{label}</a>'

    previous = link(page - 1, "← Previous") if page > 1 else ""
    following = link(page + 1, "Next →") if page < pages else ""
    count = f" · {total:,} {noun}{'' if total == 1 else 's'}" if total is not None else ""
    return Safe(
        f'<nav class="pager" aria-label="Pages">{previous}'
        f'<span class="pager-info">Page {page} of {pages}{_text(count)}</span>{following}</nav>'
    )


# ---------------------------------------------------------------------------
# Page structure
# ---------------------------------------------------------------------------

def crumbs(items: Sequence) -> Safe:
    """Breadcrumb trail for a nested page: `[('Homeowners', '/accounts'), (name, href), 'Edit details']`.

    Every item but the last is a (label, href) pair; the last is the current page's label (plain
    text, not a link). Phones show only "‹ Back to <parent>", the one link they need.
    """
    return Safe(_crumbs_markup(items))


def _crumbs_markup(items: Sequence) -> str:
    *parents, current = items or [None]
    if not parents or not isinstance(current, str):
        raise ValueError(f"crumbs need at least one (label, href) parent and a text label last, got {list(items)!r}")
    links = []
    for parent in parents:
        if isinstance(parent, str) or len(parent) != 2:
            raise ValueError(f"crumb parents must be (label, href) pairs, got {parent!r}")
        label, href = parent
        links.append(f'<li><a href="{_href(href)}">{_text(label)}</a></li>')
    back_label, back_href = parents[-1]
    return (
        '<nav class="crumbs" aria-label="Breadcrumb">'
        f'<ol class="crumbs-trail">{"".join(links)}<li aria-current="page">{_text(current)}</li></ol>'
        f'<a class="crumbs-back" href="{_href(back_href)}"><span aria-hidden="true">‹</span>Back to {_text(back_label)}</a></nav>'
    )


def heading(title: str, lead: str = "", *, actions: str = "", crumbs: Sequence = ()) -> Safe:
    """Page header with the one h1. `actions` is trusted markup; keep one primary button in it.

    `crumbs` (nested pages only) is the trail given to `crumbs()`; top-level pages pass nothing.
    """
    crumbs_html = _crumbs_markup(crumbs) if crumbs else ""
    lead_html = f'<p class="lead">{_text(lead)}</p>' if lead else ""
    actions_html = f'<div class="page-actions">{actions}</div>' if actions else ""
    return Safe(f'<header class="page-head"><div>{crumbs_html}<h1>{_text(title)}</h1>{lead_html}</div>{actions_html}</header>')


def section(title: str, body: str, *, lead: str = "") -> Safe:
    """Hairline-separated section with an h2."""
    heading_id = f"sec{next(_control_ids)}"
    lead_html = f'<p class="lead">{_text(lead)}</p>' if lead else ""
    return Safe(
        f'<section class="block" aria-labelledby="{heading_id}"><div class="block-head">'
        f'<h2 id="{heading_id}">{_text(title)}</h2>{lead_html}</div>{body}</section>'
    )


SHORT_TOKEN_MAX = 20
# Desktop keeps the first column on one line (names, account nos.); a sentence longer than this may wrap,
# otherwise a long charge description pushed the amount column out of view.
FIRST_COLUMN_WRAP_OVER = 40
# Unpaged tables are sorted and searched in the browser (app.js); below this many rows a search box is noise.
CLIENT_SEARCH_MIN_ROWS = 8
SHORT_HEADER_MAX = 16


def _is_short_token(cell) -> bool:
    return isinstance(cell, str) and not isinstance(cell, Safe) and 0 < len(cell) <= SHORT_TOKEN_MAX and " " not in cell


def _is_long_text(cell) -> bool:
    return isinstance(cell, str) and not isinstance(cell, Safe) and len(cell) > FIRST_COLUMN_WRAP_OVER


def copyable(value, display=None) -> Safe:
    """Cell content with a copy button, for values staff paste elsewhere (account no., name, OR no.).

    `display` (text or `Safe`, e.g. a link) is what is shown; `value` is what is copied. The button is
    type="button", so a table inside a form never submits through it.
    """
    text = str(value or "")
    shown = _text(display if display is not None else text)
    if not text.strip():
        return Safe(shown)
    # Meter numbers like "979189 - 23" must not break across lines beside the button; names may wrap.
    text_class = "copy-text nowrap" if len(text) <= SHORT_TOKEN_MAX else "copy-text"
    return Safe(
        f'<span class="copy-cell"><span class="{text_class}">{shown}</span>'
        f'<button class="copy-btn js-only" type="button" data-copy="{_text(text)}" aria-label="Copy {_text(text)}" '
        f'title="Copy">{_icon("copy")}</button></span>'
    )


@dataclass(frozen=True)
class Sort:
    """Server-side column order for a paged table: the order must cover every page, not just one.

    `columns` maps a table column index to a public sort key. The route maps the same keys to SQL (or a
    Python key) in its own whitelist, so nothing from the query string ever reaches a query.
    `base` is the page URL with its filters but without sort, dir or page.
    """

    base: str
    columns: Mapping[int, str]
    key: str = ""
    descending: bool = False

    @classmethod
    def from_query(cls, query: Mapping, base: str, columns: Mapping[int, str]) -> "Sort":
        key = query.get("sort", "")
        if key not in columns.values():
            return cls(base, columns)  # missing or unknown key: the page's own default order
        return cls(base, columns, key, query.get("dir") == "desc")

    @property
    def params(self) -> dict:
        """Query pairs that keep this order on the pager and the search form."""
        return {"sort": self.key, "dir": "desc" if self.descending else "asc"} if self.key else {}

    def order(self, expressions: Mapping[str, object], default: str, tiebreak: str = "") -> tuple[str, tuple]:
        """(ORDER BY body, its parameters) from the route's whitelist, or the page's default order.

        An expression is SQL text, or (SQL, params) when it needs a value such as the billing month.
        Empty values go last in both directions (SQLite and Postgres disagree on where NULLs sort).
        """
        if not self.key:
            return default, ()
        expression = expressions[self.key]
        sql, params = expression if isinstance(expression, tuple) else (expression, ())
        clause = f'({sql}) IS NULL, ({sql}) {"DESC" if self.descending else "ASC"}'
        return (f"{clause}, {tiebreak}" if tiebreak else clause), tuple(params) * 2

    def apply(self, rows: list, keys: Mapping[str, object]) -> list:
        """Same as `order` for rows already in memory (reports computed in Python)."""
        if not self.key:
            return rows
        return sorted(rows, key=keys[self.key], reverse=self.descending)

    def header(self, index: int, label: str) -> str:
        key = self.columns.get(index)
        if not key or not label:
            return _text(label)
        active = key == self.key
        next_dir = "desc" if active and not self.descending else "asc"
        joiner = "&" if "?" in self.base else "?"
        href = f"{self.base}{joiner}{urlencode({'sort': key, 'dir': next_dir})}"
        icon = ("sort-desc" if self.descending else "sort-asc") if active else "sort"
        return (f'<a class="sort-link" href="{_href(href)}">{_text(label)}'
                f'<span class="sort-ico" aria-hidden="true">{_icon(icon)}</span></a>')

    def aria(self, index: int) -> str:
        key = self.columns.get(index)
        if key and key == self.key:
            return f' aria-sort="{"descending" if self.descending else "ascending"}"'
        return ""


def table(
    headers: Sequence,
    rows: Sequence[Sequence],
    *,
    empty: str = "Nothing to show yet.",
    numeric_cols: Iterable[int] = (),
    sort: Sort | None = None,
    foot: Sequence | None = None,
    keys: Sequence | None = None,
) -> Safe:
    """Ledger table; on phones each row becomes a stacked label/value list.

    `keys` (one stable id per row, e.g. the record id) lets a live update find each row again, so a new row
    slides in and a changed row flashes instead of the whole table being redrawn.

    Cells are text (escaped) or `Safe` markup such as `pill()` / `button_link()`. A row whose
    length differs from `headers` raises, because the mismatch would shift every later column.

    Order and search: a paged table passes `sort`, and its headers become links the server answers,
    because sorting one page in the browser would hide rows on the other pages. Every other table is
    sorted (and, from CLIENT_SEARCH_MIN_ROWS rows, searched) in the browser by app.js.
    `foot` is a totals row that stays below the rows whatever the order.
    """
    if not rows:
        return Safe(_empty_markup(empty))

    numeric = set(numeric_cols)

    def cell_class(index: int, cell) -> str:
        # A short single token (account no. "1D-0630", dates, meter nos.) must never wrap: browsers break
        # after hyphens, so narrow columns showed "1D-" on one line and "0630" on the next.
        classes = (["num"] if index in numeric else []) + (["nowrap"] if _is_short_token(cell) else [])
        if index == 0 and _is_long_text(cell):
            classes.append("wrap")
        return f' class="{" ".join(classes)}"' if classes else ""

    def head_cell(index: int, header) -> str:
        # Short headers stay on one line so every header in the row lines up; long ones may wrap.
        classes = (["num"] if index in numeric else []) + (["nowrap"] if len(str(header)) <= SHORT_HEADER_MAX else [])
        css = f' class="{" ".join(classes)}"' if classes else ""
        if sort is not None:
            return f'<th scope="col" role="columnheader"{css}{sort.aria(index)}>{sort.header(index, header)}</th>'
        return f'<th scope="col" role="columnheader"{css}>{_text(header)}</th>'

    def cells(row) -> str:
        if len(row) != len(headers):
            raise ValueError(f"table row has {len(row)} cells but {len(headers)} headers: {list(row)!r}")
        return "".join(
            f'<td role="cell" data-label="{_text(headers[index])}"{cell_class(index, cell)}>{_text(cell)}</td>'
            for index, cell in enumerate(row)
        )

    head = "".join(head_cell(index, header) for index, header in enumerate(headers))
    if keys is not None and len(keys) != len(rows):
        raise ValueError(f"table has {len(rows)} rows but {len(keys)} keys")
    row_keys = [f' data-key="{_text(key)}"' for key in keys] if keys is not None else [""] * len(rows)
    row_cells = [cells(row) for row in rows]
    body = "".join(f'<tr role="row"{key} data-v="{_version(markup)}">{markup}</tr>' for markup, key in zip(row_cells, row_keys))
    foot_html = f'<tfoot><tr role="row">{cells(foot)}</tr></tfoot>' if foot else ""
    behaviour = ""
    if sort is None and len(rows) > 1:
        behaviour = " data-sortable" + (" data-searchable" if len(rows) >= CLIENT_SEARCH_MIN_ROWS else "")
    # The wrapper scrolls a wide table inside itself on desktop so the page never scrolls sideways.
    return Safe(
        f'<div class="table-wrap"><table class="ledger" role="table"{behaviour}><thead><tr role="row">{head}</tr></thead>'
        f'<tbody data-v="{_version(body)}">{body}</tbody>{foot_html}</table></div>'
    )


def search_form(action: str, value: str, *, label: str, hint: str = "", hidden: Mapping | None = None,
                extra: str = "", filtered: bool = False, placeholder: str = "") -> Safe:
    """GET search bar for a paged table. `hidden` keeps the page's other choices, such as its sort order.

    `extra` is trusted markup for more filter fields shown before the search box (phase, status, dates).
    `filtered` says one of those filters is narrowing the list, so "Clear filters" is offered.
    """
    hidden_inputs = "".join(
        f'<input type="hidden" name="{_text(k)}" value="{_text(v)}">' for k, v in (hidden or {}).items() if v not in ("", None)
    )
    # A short label keeps every filter label on one line, so the inputs in the bar line up; the detail is the placeholder.
    attrs = 'autocomplete="off"' + (f' placeholder="{_text(placeholder)}"' if placeholder else "")
    search = field(label, "q", type="search", value=value, required=False, hint=hint, attrs=attrs)
    clear = f'<a class="btn btn-quiet" href="{_href(action)}">Clear filters</a>' if value or filtered else ""
    return Safe(
        f'<form class="form filters" method="get" action="{_href(action)}" role="search">{hidden_inputs}{extra}{search}'
        f'<div class="form-actions"><button class="btn btn-primary" type="submit">Search</button>{clear}</div></form>'
    )


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------

_FIELD_TYPES = ("text", "number", "date", "month", "password", "email", "tel", "search", "file", "textarea")


def field(
    label: str,
    name: str,
    *,
    type: str = "text",
    value="",
    required: bool = True,
    hint: str = "",
    attrs: str = "",
) -> Safe:
    """Labelled input. Optional fields say "optional" so required is the quiet default.

    `attrs` is trusted markup placed inside the tag (e.g. `inputmode="decimal" data-grid="reading"`).
    A password value is never written back into the page.
    """
    _one_of(type, _FIELD_TYPES, "field type")
    control_id = f"f{next(_control_ids)}"
    hint_id = f"{control_id}-hint"
    described = f' aria-describedby="{hint_id}"' if hint else ""
    required_attr = " required" if required else ""
    shown = "" if type in ("password", "file") else _text(value)

    if type == "textarea":
        control = f'<textarea id="{control_id}" name="{_text(name)}"{required_attr}{described} {attrs}>{shown}</textarea>'
    else:
        control = (
            f'<input id="{control_id}" type="{type}" name="{_text(name)}" value="{shown}"'
            f"{required_attr}{described} {attrs}>"
        )
    optional = "" if required else '<span class="opt">optional</span>'
    hint_html = f'<span class="hint" id="{hint_id}">{_text(hint)}</span>' if hint else ""
    # Older staff mistype long passwords; js-only keeps the button out of no-JS pages where it would do nothing.
    reveal = (
        f'<button class="btn btn-quiet js-only" type="button" data-reveal="{control_id}" aria-controls="{control_id}" '
        'aria-pressed="false">Show password</button>'
        if type == "password" else ""
    )
    return Safe(f'<div class="field"><label for="{control_id}">{_text(label)}{optional}</label>{control}{reveal}{hint_html}</div>')


def checkbox(label: str, name: str, *, checked: bool = False, hint: str = "") -> Safe:
    """A tick box with its label beside it; ticked submits 'yes', unticked submits nothing."""
    control_id = f"f{next(_control_ids)}"
    hint_id = f"{control_id}-hint"
    described = f' aria-describedby="{hint_id}"' if hint else ""
    hint_html = f'<span class="hint" id="{hint_id}">{_text(hint)}</span>' if hint else ""
    ticked = " checked" if checked else ""
    return Safe(
        f'<div class="field field-check"><label for="{control_id}"><input class="tick" id="{control_id}" type="checkbox" '
        f'name="{_text(name)}" value="yes"{ticked}{described}><span>{_text(label)}</span></label>{hint_html}</div>'
    )


def select(
    label: str,
    name: str,
    options: Iterable,
    *,
    value="",
    required: bool = True,
    hint: str = "",
    attrs: str = "",
    compact: bool = False,
) -> Safe:
    """Labelled drop-down. Each option is a (value, label) pair or a plain string used for both.

    `compact=True` is for a select inside a table cell whose column header already says what it is:
    no visible label (the label becomes `aria-label`, e.g. "Type for code NC"), sized to its longest
    option. app.js replaces the look of every select with a themed list; the native one still submits.
    """
    if compact:
        return _compact_select(label, name, options, value=value, required=required, attrs=attrs)
    control_id = f"f{next(_control_ids)}"
    hint_id = f"{control_id}-hint"
    described = f' aria-describedby="{hint_id}"' if hint else ""
    required_attr = " required" if required else ""
    optional = "" if required else '<span class="opt">optional</span>'
    hint_html = f'<span class="hint" id="{hint_id}">{_text(hint)}</span>' if hint else ""
    return Safe(
        f'<div class="field"><label for="{control_id}">{_text(label)}{optional}</label>'
        f'<select id="{control_id}" name="{_text(name)}"{required_attr}{described} {attrs}>{_options(options, value)}</select>{hint_html}</div>'
    )


def _compact_select(label: str, name: str, options: Iterable, *, value, required: bool, attrs: str) -> Safe:
    required_attr = " required" if required else ""
    return Safe(
        f'<select class="select-compact" name="{_text(name)}" aria-label="{_text(label)}"{required_attr} {attrs}>'
        f"{_options(options, value)}</select>"
    )


def _options(options: Iterable, value) -> str:
    chosen = str(value)
    items = []
    for option in options:
        option_value, option_label = (option, option) if isinstance(option, str) else option
        selected = " selected" if str(option_value) == chosen else ""
        items.append(f'<option value="{_text(option_value)}"{selected}>{_text(option_label)}</option>')
    return "".join(items)


def form(
    action: str,
    csrf: str,
    body: str,
    submit_label: str,
    *,
    secondary: str = "",
    danger: bool = False,
    confirm: str = "",
    multipart: bool = False,
) -> Safe:
    """POST form with a CSRF token and a fresh idempotency `key` (one per render, so retries replay).

    `confirm` asks in-page before submitting (the submit button label becomes the confirm label).
    `secondary` is trusted markup shown beside the submit button, e.g. a Cancel link.
    """
    enctype = ' enctype="multipart/form-data"' if multipart else ""
    kind = "danger" if danger else "primary"
    confirm_attr = f' data-confirm="{_text(confirm)}"' if confirm else ""
    return Safe(
        f'<form class="form" method="post" action="{_href(action)}"{enctype}>'
        f'<input type="hidden" name="csrf" value="{_text(csrf)}">'
        f'<input type="hidden" name="key" value="{uuid.uuid4()}">'
        f"{body}"
        f'<div class="form-actions"><button class="btn btn-{kind}" type="submit"{confirm_attr}>{_text(submit_label)}</button>'
        f"{secondary}</div></form>"
    )


# ---------------------------------------------------------------------------
# Page frames
# ---------------------------------------------------------------------------

def _head(title: str) -> str:
    return (
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{_text(title)} · {BRAND_NAME}</title>"
        f'<link rel="icon" type="image/png" href="{ASSET_BASE}/vistaverde-mark.png">'
        # Preloaded so the first paint already uses Atkinson; a late font swap made text jump on every page.
        f'<link rel="preload" href="{ASSET_BASE}/atkinson-regular.woff2" as="font" type="font/woff2" crossorigin>'
        f'<link rel="preload" href="{ASSET_BASE}/atkinson-bold.woff2" as="font" type="font/woff2" crossorigin>'
        f'<link rel="stylesheet" href="{ASSET_BASE}/app.css">'
        # Synchronous on purpose: it sets the theme before first paint (no flash).
        f'<script src="{ASSET_BASE}/app.js"></script>'
        f'<script src="{ASSET_BASE}/reveal.js" defer></script>'
        f'<script src="{ASSET_BASE}/select.js" defer></script>'
        f'<script src="{ASSET_BASE}/datepicker.js" defer></script>'
    )


PRINT_STYLESHEETS = ("print-landscape.css",)


_LAYOUT_HREF = re.compile(r"/(?!/)[A-Za-z0-9/_.-]+\.css(\?v=[0-9a-f]{1,32})?")  # same site: never "//host/..."


def document(title: str, body: str, *, body_class: str = "doc-body", stylesheet: str = "", layout_href: str = "") -> Safe:
    """Bare page (no navigation) for print documents; put each page inside `<div class="doc doc-page">`.

    `stylesheet` adds one extra file from PRINT_STYLESHEETS, e.g. the A4 landscape page rule for statements.
    `layout_href` adds a stylesheet the app generates itself (receipt positions): a same-site .css path only.
    """
    extra = ""
    if stylesheet:
        _one_of(stylesheet, PRINT_STYLESHEETS, "print stylesheet")
        extra = f'<link rel="stylesheet" href="{ASSET_BASE}/{stylesheet}">'
    if layout_href:
        if not _LAYOUT_HREF.fullmatch(layout_href):
            raise ValueError(f"layout stylesheet must be a same-site .css path, got {layout_href!r}")
        extra += f'<link rel="stylesheet" href="{layout_href}">'
    return Safe(
        f'<!doctype html><html lang="en"><head>{_head(title)}{extra}</head>'
        f'<body class="{_text(body_class)}">{body}</body></html>'
    )


def _mark(size_class: str = "brand-mark") -> str:
    return f'<img class="{size_class}" src="{ASSET_BASE}/vistaverde-mark.png" alt="" width="64" height="64">'


def _theme_toggle() -> str:
    return (
        '<button class="btn btn-quiet theme-toggle js-only" type="button" data-theme-toggle>'
        f'<span class="to-dark">{_icon("moon")}<span class="lbl">Dark mode</span><span class="sr">Switch to dark theme</span></span>'
        f'<span class="to-light">{_icon("sun")}<span class="lbl">Light mode</span><span class="sr">Switch to light theme</span></span>'
        "</button>"
    )


def _signout(csrf: str) -> str:
    return (
        '<form class="signout" method="post" action="/logout">'
        f'<input type="hidden" name="csrf" value="{_text(csrf)}"><input type="hidden" name="key" value="{uuid.uuid4()}">'
        '<button class="btn btn-secondary" type="submit">Sign out</button></form>'
    )


def _get(row, key: str, default: str = "") -> str:
    """Read a column from a dict or a sqlite3.Row-like object, which may lack `.get`."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else str(value)


def _who(user: Mapping) -> str:
    role = _get(user, "role")
    role_label = _ROLE_LABELS.get(role, role.title())
    name = _get(user, "display_name") or _get(user, "username")
    return f'<div class="who"><span class="who-name">{_text(name)}</span><span class="who-role">{_text(role_label)}</span></div>'


def _flash(flash) -> str:
    if not flash:
        return ""
    kind, text = flash if isinstance(flash, tuple) else ("info", flash)
    return f'<div class="flash">{notice(text, kind)}</div>'


def _bell(alert_count: int) -> str:
    count = max(int(alert_count), 0)
    label = f"Alerts: {count} open" if count else "Alerts: none open"
    badge = f'<span class="bell-count" aria-hidden="true">{"99+" if count > 99 else count}</span>' if count else ""
    return f'<a class="bell" href="/alerts" aria-label="{label}">{_icon("bell")}{badge}</a>'


def shell(
    title: str,
    body: str,
    *,
    user: Mapping | None,
    active: str = "",
    alert_count: int = 0,
    flash=None,
    dots: Iterable[str] | None = None,
    csrf: str = "",
    path: str = "",
    live=False,
    live_seq: int | None = None,
) -> Safe:
    """Full page. `active` is a nav key; `dots` is the set of nav keys that need attention; `path` picks the
    section page to mark as current. `live` (True, or the table names it shows) and `live_seq` (the change
    number the page was drawn at) let app.js update the page in place when another computer saves something.

    `user=None` renders the public frame (sign in, first-run setup): no navigation. `flash` is a
    message or a (kind, message) tuple. `csrf` feeds the Sign out form; if omitted it is read from
    `user['csrf']`, and without either the Sign out button posts an empty token and is rejected.
    """
    if user is None:
        return _public_page(title, body, flash)

    pending = set(dots or ())
    token = csrf or _get(user, "csrf")
    items = _nav_for(_get(user, "role"))
    active_label = next((label for key, _, label, _, _, _ in items if key == active), title)
    active_pages = next((pages for key, _, _, _, _, pages in items if key == active), ())
    current_page = _current_page(active_pages, path)

    def nav_link(key: str, href: str, label: str, css: str) -> str:
        current = ' aria-current="page"' if key == active else ""
        marker = _dot_markup() if key in pending else ""
        return f'<a class="{css}" href="{href}"{current}>{_icon(key)}<span>{_text(label)}</span>{marker}</a>'

    def page_links(pages, css: str) -> str:
        links = []
        for href, label, _ in pages:
            current = ' aria-current="page"' if href == current_page else ""
            links.append(f'<a class="{css}" href="{href}"{current}>{_text(label)}</a>')
        return "".join(links)

    def side_section(key: str, href: str, label: str, pages) -> str:
        link = nav_link(key, href, label, "nav-link")
        if key != active or len(pages) < 2:
            return link
        return f'{link}<div class="nav-sub" role="group" aria-label="{_text(label)} pages">{page_links(pages, "nav-sublink")}</div>'

    side_links = "".join(side_section(key, href, label, pages) for key, href, label, _, _, pages in items)
    sheet_links = "".join(nav_link(key, href, label, "sheet-link") for key, href, label, _, in_tabs, _ in items if not in_tabs)
    subnav = (f'<nav class="subnav" aria-label="{_text(active_label)} pages">{page_links(active_pages, "subnav-link")}</nav>'
              if len(active_pages) > 1 else "")

    def tab_link(key: str, href: str, label: str) -> str:
        current = ' aria-current="page"' if key == active else ""
        marker = _dot_markup() if key in pending else ""
        return f'<a class="tab" href="{href}"{current}><span class="pill-ico">{_icon(key)}</span><span>{_text(label)}</span>{marker}</a>'

    tab_links = "".join(tab_link(key, href, label) for key, href, label, _, in_tabs, _ in items if in_tabs)
    menu_needs_attention = any(key in pending for key, _, _, _, in_tabs, _ in items if not in_tabs)
    menu_tab = (
        '<a class="tab" href="#menu-sheet" data-sheet-open aria-haspopup="dialog">'
        f'<span class="pill-ico">{_icon("menu")}</span><span>Menu</span>{_dot_markup() if menu_needs_attention else ""}</a>'
    )

    page = f"""<div class="shell">
<aside class="side"><a class="brand" href="/">{_mark()}<span class="brand-text"><strong>{BRAND_NAME}</strong><span>{BRAND_TAGLINE}</span></span></a>
<nav class="side-nav" aria-label="Main">{side_links}</nav></aside>
<div class="stage"><header class="topbar"><a class="topbar-brand" href="/" aria-label="{BRAND_NAME} home">{_mark()}</a>
<p class="topbar-ctx">{_text(active_label)}</p>
<div class="topbar-tools">{_bell(alert_count)}{_theme_toggle()}{_who(user)}{_signout(token)}</div></header>
<main id="main" tabindex="-1"{_live_attrs(live, live_seq)}>{subnav}{_flash(flash)}{body}</main></div></div>
<nav class="tabbar" aria-label="Main">{tab_links}{menu_tab}</nav>
<div class="sheet" id="menu-sheet" role="dialog" aria-modal="true" aria-label="Menu">
<a class="sheet-backdrop" href="#main" data-sheet-close tabindex="-1" aria-hidden="true"></a>
<div class="sheet-panel"><div class="sheet-handle" aria-hidden="true"></div><p class="sheet-title">{BRAND_NAME}</p>
<nav class="sheet-nav" aria-label="More pages">{sheet_links}</nav>
<div class="sheet-me">{_who(user)}<div class="sheet-actions">{_theme_toggle()}</div>{_signout(token)}
<a class="btn btn-quiet" href="#main" data-sheet-close>Close menu</a></div></div></div>"""
    return Safe(f'<!doctype html><html lang="en"><head>{_head(title)}</head><body class="app"><a class="skip" href="#main">Skip to content</a>{page}</body></html>')


def _live_attrs(live, live_seq) -> str:
    if not live or live_seq is None:
        return ""
    tables = "*" if live is True else ",".join(sorted(live))
    if not re.fullmatch(r"\*|[a-z_]+(,[a-z_]+)*", tables):
        raise ValueError(f"live tables must be table names, got {live!r}")
    return f' data-live="{tables}" data-live-seq="{int(live_seq)}"'


def _public_page(title: str, body: str, flash) -> Safe:
    page = f"""<div class="public-wrap"><div class="public-tools">{_theme_toggle()}</div>
<div class="public-brand">{_mark()}<div><strong>{BRAND_NAME}</strong><span>{BRAND_TAGLINE}</span></div></div>
<main id="main" tabindex="-1">{_flash(flash)}{body}</main></div>"""
    return Safe(f'<!doctype html><html lang="en"><head>{_head(title)}</head><body class="public"><a class="skip" href="#main">Skip to content</a>{page}</body></html>')
