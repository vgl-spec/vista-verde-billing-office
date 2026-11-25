/* Progressive enhancement only: every page works without this file except the readings-grid
   keyboard helper. Loaded synchronously in <head> so the theme is set before first paint. */
(() => {
  'use strict';

  const root = document.documentElement;
  const THEME_KEY = 'bms-theme';
  const prefersDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  root.classList.add('js');

  /* ---- Theme: stored choice wins, otherwise follow the OS. Storage can throw (private mode). ---- */

  function storedTheme() {
    try {
      const value = localStorage.getItem(THEME_KEY);
      return value === 'light' || value === 'dark' ? value : null;
    } catch (error) {
      return null;
    }
  }

  function resolveTheme() {
    return storedTheme() || (prefersDark && prefersDark.matches ? 'dark' : 'light');
  }

  function applyTheme() {
    root.dataset.theme = resolveTheme();
  }

  function toggleTheme() {
    const next = resolveTheme() === 'dark' ? 'light' : 'dark';
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (error) {
      /* Choice still applies for this page view; it just will not be remembered. */
    }
    root.dataset.theme = next;
  }

  applyTheme();
  if (prefersDark && prefersDark.addEventListener) prefersDark.addEventListener('change', applyTheme);

  /* ---- Double-submit guard: one press on a POST form is one request. ---- */

  const submitButtons = (form) => form.querySelectorAll('button[type="submit"], button:not([type])');

  function lockForm(form) {
    form.dataset.pending = 'true';
    // Deferred so the browser has already serialised the clicked button's name/value.
    window.setTimeout(() => {
      submitButtons(form).forEach((button) => {
        button.dataset.saving = 'true';
        button.savedChildren = Array.from(button.childNodes);
        button.replaceChildren(document.createTextNode('Saving…'));
        button.disabled = true;
      });
    }, 0);
  }

  function unlockForm(form) {
    delete form.dataset.pending;
    submitButtons(form).forEach((button) => {
      if (button.dataset.saving !== 'true') return;
      delete button.dataset.saving;
      button.disabled = false;
      if (button.savedChildren) button.replaceChildren(...button.savedChildren);
    });
  }

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || (form.getAttribute('method') || '').toLowerCase() !== 'post') return;
    if (form.dataset.pending === 'true') {
      event.preventDefault();
      return;
    }
    lockForm(form);
  });

  // Back/forward cache restores the page with the form still locked.
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form[data-pending]').forEach(unlockForm);
  });

  /* ---- In-page confirmation (never window.confirm / alert). ---- */

  let confirmPanel = null;

  function buildConfirmPanel() {
    const panel = document.createElement('dialog');
    panel.className = 'confirm-panel';
    panel.setAttribute('aria-labelledby', 'confirm-text');

    const text = document.createElement('p');
    text.className = 'confirm-text';
    text.id = 'confirm-text';

    const actions = document.createElement('div');
    actions.className = 'confirm-actions';

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn-secondary';
    cancel.textContent = 'Go back';
    cancel.addEventListener('click', () => panel.close());

    const accept = document.createElement('button');
    accept.type = 'button';
    accept.className = 'btn btn-danger';

    actions.append(cancel, accept);
    panel.append(text, actions);
    document.body.append(panel);
    return { panel, text, cancel, accept };
  }

  function askToConfirm(message, acceptLabel, onAccept) {
    if (!confirmPanel) confirmPanel = buildConfirmPanel();
    const { panel, text, cancel, accept } = confirmPanel;
    if (panel.open) return;

    text.textContent = message;
    accept.textContent = acceptLabel;
    accept.disabled = false;
    accept.onclick = () => {
      accept.disabled = true; // a second click on Confirm must not re-run the action
      panel.close();
      onAccept();
    };

    if (typeof panel.showModal === 'function') panel.showModal();
    else panel.setAttribute('open', '');
    cancel.focus(); // the safe choice is the default one
  }

  document.addEventListener('click', (event) => {
    const trigger = event.target instanceof Element ? event.target.closest('[data-confirm]') : null;
    if (!trigger) return;
    if (trigger.dataset.confirmed === 'true') {
      delete trigger.dataset.confirmed; // second pass: let the real click through
      return;
    }
    event.preventDefault();
    const label = trigger.textContent.trim() || 'Confirm';
    askToConfirm(trigger.dataset.confirm, label, () => {
      trigger.dataset.confirmed = 'true';
      trigger.click();
    });
  });

  /* ---- Everything below needs the page to be parsed. ---- */

  function setupMenuSheet() {
    const sheet = document.getElementById('menu-sheet');
    if (!sheet) return;
    let opener = null;

    function close() {
      sheet.classList.remove('is-open');
      root.classList.remove('sheet-open');
      if (opener) opener.focus();
    }

    function open(trigger) {
      opener = trigger;
      sheet.classList.add('is-open');
      root.classList.add('sheet-open');
      const first = sheet.querySelector('a[href]:not([data-sheet-close]), button');
      if (first) first.focus();
    }

    document.querySelectorAll('[data-sheet-open]').forEach((trigger) => {
      trigger.addEventListener('click', (event) => {
        event.preventDefault();
        open(trigger);
      });
    });

    sheet.querySelectorAll('[data-sheet-close]').forEach((control) => {
      control.addEventListener('click', (event) => {
        event.preventDefault();
        close();
      });
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && sheet.classList.contains('is-open')) close();
    });

    // Rotating a tablet past the breakpoint hides the sheet; do not leave scroll locked.
    window.matchMedia('(min-width: 768px)').addEventListener('change', (query) => {
      if (query.matches) {
        sheet.classList.remove('is-open');
        root.classList.remove('sheet-open');
      }
    });
  }

  /* ---- Navigation highlight moves on click, not when the next page arrives. ----
     Without this the old item stayed yellow until the server answered, so two items looked selected. */
  function setupInstantNav() {
    document.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target instanceof Element ? event.target.closest('a.nav-link, a.tab, a.sheet-link, a.nav-sublink, a.subnav-link') : null;
      if (!link || link.target === '_blank' || link.origin !== window.location.origin) return;
      // A section page only moves the mark among its own group; the section itself stays marked.
      const group = link.closest('.nav-sub, .subnav') || link.closest('nav') || document;
      group.querySelectorAll(':scope > [aria-current="page"]').forEach((item) => item.removeAttribute('aria-current'));
      link.setAttribute('aria-current', 'page');
    });
  }

  /* ---- Loading bar: from the press until the next page arrives (see app.css html.is-loading). ---- */
  const LOADING_GIVE_UP_MS = 15000;  // a download (Excel export) never replaces the page: do not leave the bar running
  let loadingTimer = 0;

  function showLoading() {
    root.classList.add('is-loading');
    window.clearTimeout(loadingTimer);
    loadingTimer = window.setTimeout(() => root.classList.remove('is-loading'), LOADING_GIVE_UP_MS);
  }

  function setupLoadingBar() {
    document.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (!link || link.target || link.hasAttribute('download') || link.origin !== window.location.origin) return;
      if (link.pathname === window.location.pathname && link.search === window.location.search && link.hash) return;  // same-page jump
      showLoading();
    });
    // Registered after the double-submit guard, so a blocked second press shows nothing new.
    document.addEventListener('submit', (event) => { if (!event.defaultPrevented) showLoading(); });
    window.addEventListener('pageshow', () => root.classList.remove('is-loading'));
  }

  // Enter moves down a readings column; Shift+Enter moves up. On the last input it lands on Save.
  function setupGridKeys() {
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.isComposing) return;
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.hasAttribute('data-grid')) return;

      event.preventDefault();
      const column = Array.from(document.querySelectorAll('input[data-grid]'))
        .filter((candidate) => candidate.dataset.grid === input.dataset.grid && !candidate.disabled);
      const index = column.indexOf(input) + (event.shiftKey ? -1 : 1);

      if (column[index]) {
        column[index].focus();
        column[index].select();
      } else if (!event.shiftKey && input.form) {
        const save = input.form.querySelector('button[type="submit"], button:not([type])');
        if (save) save.focus();
      }
    });
  }

  // Readings grid: show consumption as the person types. The server recalculates on save.
  function setupLiveConsumption() {
    const pattern = /^\d{1,9}(\.\d{1,2})?$/;
    document.addEventListener('input', (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || input.dataset.grid !== 'present') return;
      const row = input.closest('tr');
      const output = row ? row.querySelector('[data-use]') : null;
      if (!output) return;
      const typedPrevious = row.querySelector('[data-prev-input]');
      const previousText = (typedPrevious ? typedPrevious.value : input.dataset.prev || '').trim();
      const presentText = input.value.trim();
      output.classList.remove('is-bad');
      if (!pattern.test(presentText) || !pattern.test(previousText)) {
        output.textContent = '';
        return;
      }
      const used = (Math.round(parseFloat(presentText) * 100) - Math.round(parseFloat(previousText) * 100)) / 100;
      if (used < 0) {
        output.textContent = 'Lower than before';
        output.classList.add('is-bad');
      } else {
        output.textContent = String(used);
      }
    });
  }

  // Cash drawer count: each line's amount and the total as the cashier types. A guide only: the server counts again
  // on save and shows the confirmed total on the review page.
  function setupCashCount() {
    const piecesPattern = /^\d{1,5}$/;
    const pesosPattern = /^(\d+|\d{1,3}(,\d{3})+)(\.\d{1,2})?$/;
    const peso = (cents) => '₱' + (cents / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    document.addEventListener('input', (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.form || !input.form.hasAttribute('data-cash-count')) return;
      let total = 0;
      let valid = true;
      input.form.querySelectorAll('input[data-cash-pieces]').forEach((box) => {
        const text = box.value.trim().replace(/,/g, '');
        const pieces = text === '' ? 0 : piecesPattern.test(text) ? parseInt(text, 10) : NaN;
        const cents = pieces * parseInt(box.dataset.cashPieces, 10);
        const row = box.closest('tr');
        const line = row ? row.querySelector('[data-cash-line]') : null;
        if (line) line.textContent = Number.isNaN(cents) ? 'Check this number' : peso(cents);
        if (Number.isNaN(cents)) valid = false; else total += cents;
      });
      input.form.querySelectorAll('input[data-cash-coins]').forEach((box) => {
        const text = box.value.trim().replace(/^₱\s*/, '');
        if (text === '') return;
        if (pesosPattern.test(text)) total += Math.round(parseFloat(text.replace(/,/g, '')) * 100); else valid = false;
      });
      const output = input.form.querySelector('[data-cash-total]');
      if (output) output.textContent = valid ? peso(total) : 'Check the boxes marked above';
    });
  }

  // Ticking clock showing the SERVER's time in Manila (UTC+8, no daylight saving), never the laptop's: the server
  // stamps every payment, so its clock is the one staff must check. The laptop only measures time passing since load.
  function setupLiveClock() {
    const clock = document.querySelector('[data-server-now]');
    const serverMs = clock ? Date.parse(clock.dataset.serverNow) : NaN;
    if (Number.isNaN(serverMs)) return;
    const offsetMs = serverMs - Date.now();
    const manilaDay = (moment) => moment.toISOString().slice(0, 10);
    const startDay = manilaDay(new Date(serverMs + 8 * 3600000));
    const tick = () => {
      // Shifting by 8 hours and reading the UTC fields gives Manila time whatever zone the laptop is set to.
      const manila = new Date(Date.now() + offsetMs + 8 * 3600000);
      // The date printed above the clock came from the server; after midnight it is stale, so fetch the new day.
      if (manilaDay(manila) !== startDay) { window.location.reload(); return; }
      const hours = manila.getUTCHours();
      const pad = (value) => String(value).padStart(2, '0');
      clock.textContent = `${hours % 12 || 12}:${pad(manila.getUTCMinutes())}:${pad(manila.getUTCSeconds())} ${hours < 12 ? 'AM' : 'PM'}`;
    };
    tick();
    window.setInterval(tick, 1000);
  }

  /* ---- Tables: sort and search unpaged tables in the page; copy buttons on key values. ----
     Paged tables are sorted by the server (their headers are links), because sorting one page here
     would hide rows that sit on the other pages. */

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const ICONS = {
    sort: 'M8 10l4-4 4 4M8 14l4 4 4-4',
    asc: 'M8 14l4-4 4 4',
    desc: 'M8 10l4 4 4-4',
    copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
    check: 'M5 12.5l4.5 4.5L19 7',
    search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ico');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', ICONS[name]);
    svg.append(path);
    return svg;
  }

  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  const NUMBER = /^-?\d+(\.\d+)?%?$/;
  const ISO_DATE = /^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2})?$/;
  const WORD_DATE = /^[A-Z][a-z]{2,8}\.?( \d{1,2},)? \d{4}( \d{1,2}:\d{2} [AP]M)?$/; // "Nov 2026", "November 11, 2026 2:06 PM"

  const cellText = (cell) => (cell ? cell.textContent.replace(/\s+/g, ' ').trim() : '');
  const isBlank = (text) => text === '' || text === '—';
  const toNumber = (text) => text.replace(/[₱,\s]|m³/g, '').replace(/^−/, '-');

  // One type per column, so "10" never sorts between "1" and "2" and money sorts by value.
  function columnKind(texts) {
    const filled = texts.filter((text) => !isBlank(text));
    if (filled.length && filled.every((text) => NUMBER.test(toNumber(text)))) return 'number';
    if (filled.length && filled.every((text) => ISO_DATE.test(text) || (WORD_DATE.test(text) && !Number.isNaN(Date.parse(text))))) return 'date';
    return 'text';
  }

  function sortKey(text, kind) {
    if (kind === 'number') return parseFloat(toNumber(text));
    if (kind === 'date') return ISO_DATE.test(text) ? text : new Date(Date.parse(text)).toISOString();
    return text;
  }

  function sortTable(table, column, button, keepDirection = false) {
    const body = table.tBodies[0];
    const rows = Array.from(body.rows).filter((row) => !row.hasAttribute('data-no-match'));
    const header = button.closest('th');
    const current = header.getAttribute('aria-sort');
    const descending = keepDirection ? current === 'descending' : current === 'ascending';
    table.liveSort = () => sortTable(table, column, button, true);
    const texts = rows.map((row) => cellText(row.cells[column]));
    const kind = columnKind(texts);
    const keyed = rows.map((row, index) => ({ row, blank: isBlank(texts[index]), key: sortKey(texts[index], kind) }));

    keyed.sort((a, b) => {
      if (a.blank !== b.blank) return a.blank ? 1 : -1; // empty cells stay at the bottom both ways
      const order = kind === 'text' ? collator.compare(a.key, b.key) : a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
      return descending ? -order : order;
    });
    body.append(...keyed.map((item) => item.row));

    table.querySelectorAll('thead th').forEach((th) => {
      th.removeAttribute('aria-sort');
      const ico = th.querySelector('.sort-ico');
      if (ico) ico.replaceChildren(icon('sort'));
    });
    header.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
    button.querySelector('.sort-ico').replaceChildren(icon(descending ? 'desc' : 'asc'));
  }

  function makeSortable(table) {
    const headers = table.tHead ? Array.from(table.tHead.rows[0].cells) : [];
    headers.forEach((th, column) => {
      const label = th.textContent.trim();
      // Columns of buttons or tick boxes have no header text and nothing to sort by.
      if (!label || th.querySelector('a, button')) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'sort-link';
      const ico = document.createElement('span');
      ico.className = 'sort-ico';
      ico.setAttribute('aria-hidden', 'true');
      ico.append(icon('sort'));
      button.append(document.createTextNode(label), ico);
      button.addEventListener('click', () => sortTable(table, column, button));
      th.replaceChildren(button);
    });
  }

  function makeSearchable(table, index) {
    const body = table.tBodies[0];
    const rows = Array.from(body.rows);
    const wrap = table.closest('.table-wrap') || table;

    const tools = document.createElement('div');
    tools.className = 'table-tools';
    const label = document.createElement('label');
    label.className = 'table-search';
    const input = document.createElement('input');
    input.type = 'search';
    input.autocomplete = 'off';
    input.placeholder = 'Search this table';
    input.id = `table-search-${index}`;
    input.setAttribute('aria-label', 'Search this table');
    label.append(icon('search'), input);
    const count = document.createElement('span');
    count.className = 'table-count';
    count.setAttribute('aria-live', 'polite');
    tools.append(label, count);
    wrap.before(tools);

    const noMatch = document.createElement('tr');
    noMatch.setAttribute('data-no-match', '');
    noMatch.hidden = true;
    const noMatchCell = document.createElement('td');
    noMatchCell.colSpan = table.tHead.rows[0].cells.length;
    noMatchCell.className = 'table-empty';
    noMatchCell.textContent = 'No rows match this search.';
    noMatch.append(noMatchCell);
    body.append(noMatch);

    // Rows are read each time, so rows a live update added are searched too.
    const currentRows = () => Array.from(body.rows).filter((row) => !row.hasAttribute('data-no-match') && !row.hasAttribute('data-live-gone'));
    const showCount = (shown, total) => {
      count.textContent = shown === total ? `${total.toLocaleString()} rows` : `${shown.toLocaleString()} of ${total.toLocaleString()} rows`;
    };

    const filter = () => {
      const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      const present = currentRows();
      let shown = 0;
      present.forEach((row) => {
        const match = terms.every((term) => cellText(row).toLowerCase().includes(term));
        row.hidden = !match;
        if (match) shown += 1;
      });
      noMatch.hidden = shown !== 0;
      body.append(noMatch); // keep the message last after a re-sort
      showCount(shown, present.length);
    };
    input.addEventListener('input', filter);
    table.liveFilter = filter;
    // Enter in this box must never submit a form the table sits in.
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') event.preventDefault();
    });
    showCount(rows.length, rows.length);
  }

  function setupTables() {
    document.querySelectorAll('table[data-sortable]').forEach(makeSortable);
    document.querySelectorAll('table[data-searchable]').forEach(makeSearchable);
    document.querySelectorAll('table.ledger').forEach((table) => {
      table.liveRefresh = () => {
        if (table.liveSort) table.liveSort();
        if (table.liveFilter) table.liveFilter();
      };
    });
  }

  // navigator.clipboard exists only on https or localhost; the office opens the system over plain http on the LAN.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise((resolve, reject) => {
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.setAttribute('readonly', '');
      scratch.className = 'copy-scratch';
      document.body.append(scratch);
      scratch.select();
      const copied = document.execCommand('copy');
      scratch.remove();
      if (copied) resolve();
      else reject(new Error('copy refused'));
    });
  }

  function setupCopy() {
    let status = null;
    document.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('[data-copy]') : null;
      if (!button) return;
      event.preventDefault();
      if (!status) {
        status = document.createElement('p');
        status.className = 'sr';
        status.setAttribute('aria-live', 'polite');
        document.body.append(status);
      }
      const value = button.dataset.copy;
      copyText(value).then(() => {
        button.classList.add('is-copied');
        button.title = 'Copied';
        button.replaceChildren(icon('check'));
        status.textContent = `Copied ${value}`;
        window.clearTimeout(button.copyTimer);
        button.copyTimer = window.setTimeout(() => {
          button.classList.remove('is-copied');
          button.title = 'Copy';
          button.replaceChildren(icon('copy'));
        }, 1500);
      }).catch(() => {
        status.textContent = 'Could not copy. Select the text and press Ctrl+C.';
      });
    });
  }

  // A filter drop-down applies as soon as a choice is made; dates and the search box wait for the button,
  // because a date range needs both ends before it means anything.
  function setupFilterAutoApply() {
    document.addEventListener('change', (event) => {
      const select = event.target;
      if (!(select instanceof HTMLSelectElement)) return;
      const form = select.closest('form.filters');
      if (!form || (form.getAttribute('method') || 'get').toLowerCase() !== 'get') return;
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.submit();
    });
  }

  /* ---- Review window (readings): every saved reading of the month, plus what is typed but not saved. ---- */

  const READING = /^\d{1,9}(\.\d{1,2})?$/;

  // Rows on this page whose present reading was typed or changed since the page loaded.
  function unsavedReadings() {
    return Array.from(document.querySelectorAll('#readings-grid input[data-grid="present"]'))
      .filter((input) => !input.disabled && input.value.trim() !== input.defaultValue.trim() && input.value.trim() !== '')
      .map((input) => {
        const row = input.closest('tr');
        const typedPrevious = row ? row.querySelector('[data-prev-input]') : null;
        const previous = (typedPrevious ? typedPrevious.value : input.dataset.prev || '').trim();
        const present = input.value.trim();
        let problem = '';
        let used = '';
        if (!READING.test(present)) problem = 'Not a number';
        else if (!READING.test(previous)) problem = 'Previous reading missing';
        else {
          const value = (Math.round(parseFloat(present) * 100) - Math.round(parseFloat(previous) * 100)) / 100;
          if (value < 0) problem = 'Lower than before';
          else if (value === 0) problem = 'Zero use';
          used = String(value);
        }
        const name = row ? (row.querySelector('.grid-name') || {}).textContent || '' : '';
        const account = row ? cellText(row.cells[0]) : '';
        return { account, name: name.trim(), previous, present, used, problem, input };
      });
  }

  function fillUnsaved(dialog) {
    const section = dialog.querySelector('[data-review-unsaved]');
    const list = dialog.querySelector('[data-review-unsaved-list]');
    if (!section || !list) return;
    const rows = unsavedReadings();
    section.hidden = rows.length === 0;
    const table = document.createElement('table');
    table.className = 'ledger review-unsaved-table';
    const head = document.createElement('tr');
    ['Account', 'Name', 'Previous', 'Typed', 'Used (m³)', 'Check'].forEach((label, index) => {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = label;
      if (index >= 2 && index <= 4) th.className = 'num';
      head.append(th);
    });
    const thead = document.createElement('thead');
    thead.append(head);
    const body = document.createElement('tbody');
    rows.forEach((item) => {
      const tr = document.createElement('tr');
      [item.account, item.name, item.previous, item.present, item.used, item.problem || 'Looks fine'].forEach((text, index) => {
        const td = document.createElement('td');
        td.textContent = text;
        if (index >= 2 && index <= 4) td.className = 'num';
        if (index === 5) td.className = item.problem ? 'review-problem' : 'review-ok';
        tr.append(td);
      });
      // Clicking a row takes the person back to that input to fix it.
      tr.addEventListener('click', () => {
        dialog.close();
        item.input.focus();
        item.input.select();
      });
      body.append(tr);
    });
    table.append(thead, body);
    list.replaceChildren(table);
  }

  function setupDialogs() {
    document.addEventListener('click', (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) return;
      const opener = target.closest('[data-dialog-open]');
      if (opener) {
        const dialog = document.getElementById(opener.dataset.dialogOpen);
        if (!(dialog instanceof HTMLDialogElement)) return;
        fillUnsaved(dialog);
        if (!dialog.open) dialog.showModal();
        return;
      }
      if (target.closest('[data-dialog-close]')) {
        const dialog = target.closest('dialog');
        if (dialog) dialog.close();
        return;
      }
      if (target.closest('[data-review-save]')) {
        const form = document.querySelector('#readings-grid form[method="post"]');
        const dialog = target.closest('dialog');
        if (dialog) dialog.close();
        if (form && typeof form.requestSubmit === 'function') form.requestSubmit(); // the double-submit guard applies
        return;
      }
      // A click on the dim area around the window (the dialog itself, not its content) closes it.
      if (target instanceof HTMLDialogElement && target.classList.contains('review-panel')) target.close();
    });
  }

  /* ---- Live updates: what another computer saves appears here without reloading. ----
     The server says when records changed (/live, routes/live.py). This page then fetches its own address again
     (same filters, same page of the table: the server runs only that page's query) and moves the differences in:
     new rows slide in, changed rows glow, removed rows fade out, figures update. Rows someone is typing in are
     never touched. One request at a time; paused while the tab is hidden; slower after errors. */
  const LIVE_MIN_GAP_MS = 400;        // between two long polls on the office server
  const LIVE_DEBOUNCE_MS = 250;       // a burst of saves becomes one refresh
  const LIVE_MAX_BACKOFF_MS = 30000;
  const LIVE_REGIONS = '.table-wrap, .empty, .figures, dl.facts, .pager';
  const liveBaseline = new WeakMap(); // region -> a copy of it as the server last drew it
  const adopt = (node) => Array.from(node.childNodes).map((child) => document.importNode(child, true));

  function rowIsBusy(row) {
    if (row.contains(document.activeElement) && document.activeElement !== document.body) return true;
    return Array.from(row.querySelectorAll('input, select, textarea')).some((control) => {
      if (control.type === 'checkbox' || control.type === 'radio') return control.checked !== control.defaultChecked;
      if (control.tagName === 'SELECT') return Array.from(control.options).some((option) => option.selected !== option.defaultSelected);
      return control.value !== control.defaultValue;
    });
  }

  function announceContent(node) {
    // Lets select.js and datepicker.js dress any dropdown or date box that just arrived.
    node.dispatchEvent(new CustomEvent('bms:content', { bubbles: true }));
  }

  function glow(node, className) {
    node.classList.remove('live-new', 'live-changed');
    void node.offsetWidth; // restart the animation if it is already running
    node.classList.add(className);
    window.setTimeout(() => node.classList.remove(className), 2600);
  }

  function fadeOut(row) {
    row.classList.add('live-gone');
    row.setAttribute('data-live-gone', '');
    window.setTimeout(() => row.remove(), 320);
  }

  // Returns false when the change cannot be shown safely in place (the caller then offers a refresh).
  function reconcileTable(current, next) {
    const body = current.tBodies[0];
    const nextBody = next.tBodies[0];
    if (!body || !nextBody) return false;
    if (body.dataset.v === nextBody.dataset.v) return true;
    const rows = Array.from(body.rows).filter((row) => !row.hasAttribute('data-no-match') && !row.hasAttribute('data-live-gone'));
    const nextRows = Array.from(nextBody.rows);
    const keyed = rows.every((row) => row.dataset.key) && nextRows.every((row) => row.dataset.key)
      && new Set(nextRows.map((row) => row.dataset.key)).size === nextRows.length;
    if (!keyed) {
      if (rows.some(rowIsBusy)) return false;
      body.replaceChildren(...nextRows.map((row) => document.importNode(row, true)));
      body.dataset.v = nextBody.dataset.v;
      Array.from(body.rows).forEach((row) => glow(row, 'live-changed'));
      announceContent(body);
    } else {
      const byKey = new Map(rows.map((row) => [row.dataset.key, row]));
      const wanted = new Set(nextRows.map((row) => row.dataset.key));
      rows.forEach((row) => { if (!wanted.has(row.dataset.key) && !rowIsBusy(row)) fadeOut(row); });
      let previous = null;
      nextRows.forEach((nextRow) => {
        let row = byKey.get(nextRow.dataset.key);
        if (!row) {
          row = document.importNode(nextRow, true);
          glow(row, 'live-new');
          if (previous) previous.after(row); else body.prepend(row);
          announceContent(row);
        } else {
          if (row.dataset.v !== nextRow.dataset.v && !rowIsBusy(row)) {
            row.replaceChildren(...adopt(nextRow));
            row.dataset.v = nextRow.dataset.v;
            glow(row, 'live-changed');
            announceContent(row);
          }
          const inPlace = previous ? previous.nextElementSibling === row : body.firstElementChild === row;
          if (!inPlace && !rowIsBusy(row)) { if (previous) previous.after(row); else body.prepend(row); }
        }
        previous = row;
      });
      body.dataset.v = nextBody.dataset.v;
    }
    const foot = current.tFoot;
    if (foot && next.tFoot && !foot.isEqualNode(next.tFoot)) {
      foot.replaceChildren(...adopt(next.tFoot));
      glow(foot.rows[0], 'live-changed');
    }
    if (current.liveRefresh) current.liveRefresh(); // keep the table's own search and sort as the person set them
    return true;
  }

  function sameKind(a, b) {
    return a.classList.contains('table-wrap') === b.classList.contains('table-wrap') && a.tagName === b.tagName
      && a.className.split(' ')[0] === b.className.split(' ')[0];
  }

  // Returns false when the page changed shape (a table appeared, a section was added): the caller offers a refresh.
  function applyLive(main, nextMain) {
    const regions = Array.from(main.querySelectorAll(LIVE_REGIONS));
    const nextRegions = Array.from(nextMain.querySelectorAll(LIVE_REGIONS));
    if (regions.length !== nextRegions.length) return false;
    let shown = true;
    regions.forEach((region, index) => {
      const next = nextRegions[index];
      if (!sameKind(region, next)) {
        if (Array.from(region.querySelectorAll('tr')).some(rowIsBusy)) { shown = false; return; }
        const fresh = document.importNode(next, true);
        region.replaceWith(fresh);
        glow(fresh, 'live-new');
        fresh.querySelectorAll('table[data-sortable]').forEach(makeSortable);
        fresh.querySelectorAll('table[data-searchable]').forEach((table, n) => makeSearchable(table, `live-${Date.now()}-${n}`));
        announceContent(fresh);
        return;
      }
      if (region.classList.contains('table-wrap')) {
        const table = region.querySelector('table');
        const nextTable = next.querySelector('table');
        if (!table || !nextTable || !reconcileTable(table, nextTable)) shown = false;
        return;
      }
      const before = liveBaseline.has(region) ? liveBaseline.get(region) : region;
      if (!before.isEqualNode(next)) {
        region.replaceChildren(...adopt(next));
        glow(region, 'live-changed');
        announceContent(region);
      }
      liveBaseline.set(region, next.cloneNode(true));
    });
    return shown;
  }

  function offerRefresh() {
    if (document.querySelector('.live-pill')) return;
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'live-pill';
    pill.textContent = 'New changes from another computer. Show them';
    pill.addEventListener('click', () => window.location.reload());
    document.body.append(pill);
  }

  function setupLive() {
    const main = document.querySelector('main[data-live]');
    if (!main || !window.fetch || !window.DOMParser) return;
    const watched = main.dataset.live === '*' ? null : new Set(main.dataset.live.split(','));
    let seq = Number(main.dataset.liveSeq) || 0;
    let controller = null;
    let pollTimer = 0;
    let refreshTimer = 0;
    let failures = 0;
    let refreshing = false;
    let refreshAgain = false;
    let stopped = false;

    const matters = (tables) => tables.length > 0 && (!watched || tables.includes('*') || tables.some((t) => watched.has(t)));

    async function refresh() {
      if (refreshing) { refreshAgain = true; return; }
      // A save is on its way: the page is about to be replaced anyway.
      if (document.querySelector('form[data-pending]')) return;
      refreshing = true;
      try {
        const response = await fetch(window.location.href, { cache: 'no-store', redirect: 'manual', headers: { 'X-Live': '1' } });
        if (!response.ok) return;
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        const nextMain = doc.querySelector('main');
        if (!nextMain) return;
        if (!applyLive(main, nextMain)) offerRefresh();
        const bell = document.querySelector('.topbar .bell');
        const nextBell = doc.querySelector('.topbar .bell');
        if (bell && nextBell && bell.outerHTML !== nextBell.outerHTML) bell.replaceWith(document.importNode(nextBell, true));
      } catch (error) {
        /* The next change tries again. */
      } finally {
        refreshing = false;
        if (refreshAgain) { refreshAgain = false; refresh(); }
      }
    }

    function scheduleRefresh() {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(refresh, LIVE_DEBOUNCE_MS);
    }

    function schedulePoll(delay) {
      window.clearTimeout(pollTimer);
      pollTimer = window.setTimeout(poll, delay);
    }

    async function poll() {
      if (stopped || document.hidden || controller) return; // never two requests at once
      controller = new AbortController();
      try {
        const response = await fetch(`/live?since=${seq}`, { cache: 'no-store', redirect: 'manual', signal: controller.signal, headers: { 'X-Live': '1' } });
        if (response.type === 'opaqueredirect' || response.status === 401 || response.status === 403) { stopped = true; return; } // signed out
        if (!response.ok) throw new Error(`live ${response.status}`);
        const data = await response.json();
        failures = 0;
        if (data.seq !== seq) {
          const relevant = data.seq > seq ? matters(data.tables) : true; // a smaller number: the database was restored
          seq = data.seq;
          if (relevant) scheduleRefresh();
        }
        controller = null;
        schedulePoll(Math.max(data.retry_ms || 0, LIVE_MIN_GAP_MS));
      } catch (error) {
        const aborted = error && error.name === 'AbortError';
        controller = null;
        if (!aborted) {
          failures += 1;
          schedulePoll(Math.min(LIVE_MAX_BACKOFF_MS, 1000 * 2 ** failures));
        }
      }
    }

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        window.clearTimeout(pollTimer);
        if (controller) controller.abort();
      } else {
        schedulePoll(0); // catch up at once: the server answers straight away if anything changed meanwhile
      }
    });
    window.addEventListener('pagehide', () => { stopped = true; if (controller) controller.abort(); });
    schedulePoll(LIVE_MIN_GAP_MS);
  }

  /* ---- Edit locks (app/bms/editing.py): one person edits a record at a time. ----
     The editing page renews its short lease every few seconds and gives it back when it closes; a page that
     found the record busy checks until it frees, then opens for editing by itself. */
  const LOCK_WAIT_POLL_MS = 5000;

  function lockRequestBody(entity, entityId, csrf) {
    const data = new FormData();
    data.append('csrf', csrf);
    data.append('entity', entity);
    data.append('entity_id', entityId);
    return data;
  }

  function setupEditLocks() {
    const hold = document.querySelector('input[data-lock-hold]');
    if (hold) {
      const form = hold.closest('form');
      const csrf = form ? (form.querySelector('input[name="csrf"]') || {}).value : '';
      const { entity, entityId } = hold.dataset;
      const every = (Number(hold.dataset.renewSeconds) || 30) * 1000;
      let saving = false;
      let renewing = false;
      if (form) form.addEventListener('submit', () => { saving = true; }); // the save itself gives the lease back
      const renew = async () => {
        if (renewing || saving || document.hidden) return;
        renewing = true;
        try {
          const response = await fetch('/locks/renew', { method: 'POST', body: lockRequestBody(entity, entityId, csrf), redirect: 'manual' });
          if (!response.ok) return;
          const data = await response.json();
          if (!data.held && form && !form.dataset.lockLost) {
            form.dataset.lockLost = 'true';
            const lost = document.createElement('p');
            lost.className = 'notice notice-warn';
            lost.setAttribute('role', 'alert');
            lost.textContent = `${data.holder} took over editing this record. Your changes cannot be saved here; copy anything you need, then open it again.`;
            form.before(lost);
            form.querySelectorAll('input, select, textarea, button').forEach((control) => { control.disabled = true; });
          }
        } catch (error) {
          /* Offline for a moment: the next renewal tries again before the lease runs out. */
        } finally {
          renewing = false;
        }
      };
      window.setInterval(renew, every);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) renew(); });
      window.addEventListener('pagehide', () => {
        if (!saving && navigator.sendBeacon) navigator.sendBeacon('/locks/release', lockRequestBody(entity, entityId, csrf));
      });
    }

    const wait = document.querySelector('[data-lock-wait]');
    if (wait) {
      const { entity, entityId } = wait.dataset;
      const check = async () => {
        if (document.hidden) return;
        try {
          const response = await fetch(`/locks/status?entity=${encodeURIComponent(entity)}&entity_id=${encodeURIComponent(entityId)}`,
            { cache: 'no-store', redirect: 'manual' });
          if (!response.ok) return;
          const data = await response.json();
          if (data.free) window.location.reload(); // free now: reopening takes the lease for this person
        } catch (error) {
          /* Try again on the next tick. */
        }
      };
      window.setInterval(check, LOCK_WAIT_POLL_MS);
    }
  }

  // Typed readings live only in the page until Save; a sort link or Next would throw them away.
  function setupUnsavedGuard() {
    let dirty = false;
    document.addEventListener('input', (event) => {
      if (event.target instanceof HTMLInputElement && event.target.closest('form[method="post"]') && event.target.hasAttribute('data-grid')) dirty = true;
    });
    document.addEventListener('submit', () => { dirty = false; });
    window.addEventListener('beforeunload', (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    setupTables();
    setupCopy();
    setupFilterAutoApply();
    setupDialogs();
    setupUnsavedGuard();
    document.querySelectorAll('[data-print]').forEach((button) => {
      button.addEventListener('click', () => window.print());
    });
    setupLiveConsumption();
    setupCashCount();
    setupLiveClock();
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      button.addEventListener('click', toggleTheme);
    });
    document.querySelectorAll('[data-go-back]').forEach((button) => {
      button.addEventListener('click', () => window.history.back());
    });
    setupMenuSheet();
    setupInstantNav();
    setupLoadingBar();
    setupLive();
    setupEditLocks();
    setupGridKeys();
  });
})();
