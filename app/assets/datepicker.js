/* Themed calendar for every <input type="date"> and <input type="month">, as progressive enhancement.

   The native input stays in the form (visually hidden, still submitted, still validated) and is the
   single source of truth: picking a day sets input.value (YYYY-MM-DD or YYYY-MM) and fires `input` +
   `change`; any change made to the native input (scripts, form reset, Playwright's fill) is shown on
   the button. Without JavaScript the browser's own date field is shown.

   A "From" / "To" pair in the same form (inputs named from and to) limit each other and the days
   between them are shaded. Keyboard follows the WAI-ARIA date picker dialog: arrows move a day or a
   week, Page Up / Page Down a month (with Shift a year), Home / End the start or end of the week,
   Enter picks, Escape closes. Position is set through CSS custom properties (CSP allows CSSOM). */
(() => {
  'use strict';

  const GAP = 4;
  const EDGE = 8;
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const WEEKDAYS = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const CALENDAR_ICON = 'M4 6h16v14H4zM4 10h16M8 3v4M16 3v4';

  let layer = null;
  let open = null;
  let nextId = 1;
  const pickers = [];

  const pad = (n) => String(n).padStart(2, '0');
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const isoMonth = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;

  // Parse without the Date string parser: "2026-11-16" must mean that day here, not UTC midnight.
  function parse(value, monthOnly) {
    const match = monthOnly ? /^(\d{4})-(\d{2})$/.exec(value || '') : /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || '');
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, monthOnly ? 1 : Number(match[3]));
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function addDays(date, days) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
  }

  // Same day of month where it exists, otherwise the month's last day (Jan 31 + 1 month = Feb 28).
  function addMonths(date, months) {
    const target = new Date(date.getFullYear(), date.getMonth() + months, 1);
    const last = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
    return new Date(target.getFullYear(), target.getMonth(), Math.min(date.getDate(), last));
  }

  function label(picker) {
    const date = parse(picker.input.value, picker.monthOnly);
    if (!date) return picker.monthOnly ? 'Choose a month' : 'Choose a date';
    return picker.monthOnly ? `${MONTHS[date.getMonth()]} ${date.getFullYear()}`
      : `${MONTHS[date.getMonth()]} ${date.getDate()}, ${date.getFullYear()}`;
  }

  function icon() {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ico dp-ico');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', CALENDAR_ICON);
    svg.append(path);
    return svg;
  }

  function getLayer() {
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'dd-layer';
      document.body.append(layer);
    }
    return layer;
  }

  // The other end of a From / To pair, so each limits the other and the range can be shaded.
  function partner(input) {
    if (!input.form || !['from', 'to'].includes(input.name)) return null;
    const other = input.form.elements.namedItem(input.name === 'from' ? 'to' : 'from');
    return other instanceof HTMLInputElement && other.type === input.type ? other : null;
  }

  function limits(picker) {
    const { input, monthOnly } = picker;
    let min = parse(input.min, monthOnly);
    let max = parse(input.max, monthOnly);
    const other = partner(input);
    const otherDate = other ? parse(other.value, monthOnly) : null;
    if (otherDate && input.name === 'to' && (!min || otherDate > min)) min = otherDate;
    if (otherDate && input.name === 'from' && (!max || otherDate < max)) max = otherDate;
    return { min, max };
  }

  function allowed(picker, date, unit) {
    const { min, max } = limits(picker);
    if (unit === 'month') {
      const first = new Date(date.getFullYear(), date.getMonth(), 1);
      const last = new Date(date.getFullYear(), date.getMonth() + 1, 0);
      return !(min && last < min) && !(max && first > max);
    }
    return !(min && date < min) && !(max && date > max);
  }

  function enhance(input) {
    if (input.dataset.enhanced === 'true') return;
    input.dataset.enhanced = 'true';
    const id = `dp${nextId++}`;
    const monthOnly = input.type === 'month';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'dd dp';
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', `${id}-pop`);
    const value = document.createElement('span');
    value.className = 'dp-value';
    button.append(value, icon());

    const fieldLabel = input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null;
    const name = fieldLabel ? fieldLabel.firstChild.textContent.trim() : (input.getAttribute('aria-label') || 'Date');
    if (fieldLabel) {
      if (!fieldLabel.id) fieldLabel.id = `${id}-label`;
      button.setAttribute('aria-labelledby', `${fieldLabel.id} ${id}-value`);
      value.id = `${id}-value`;
      fieldLabel.addEventListener('click', (event) => {
        event.preventDefault();
        button.focus();
      });
    }
    if (input.getAttribute('aria-describedby')) button.setAttribute('aria-describedby', input.getAttribute('aria-describedby'));

    const pop = document.createElement('div');
    pop.className = 'dp-pop';
    pop.id = `${id}-pop`;
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', `Choose ${name.toLowerCase()}`);

    input.classList.add('dd-native');
    input.tabIndex = -1;
    input.setAttribute('aria-hidden', 'true');
    input.after(button);

    const picker = { input, button, value, pop, monthOnly, view: monthOnly ? 'months' : 'days', focus: null };
    pickers.push(picker);
    sync(picker);

    button.addEventListener('click', () => (open === picker ? close(true) : show(picker)));
    button.addEventListener('keydown', (event) => {
      if (['ArrowDown', 'Enter', ' '].includes(event.key) && open !== picker) {
        event.preventDefault();
        show(picker);
      }
    });
    pop.addEventListener('keydown', (event) => onKey(picker, event));
    input.addEventListener('change', () => sync(picker));
    input.addEventListener('invalid', () => {
      button.setAttribute('aria-invalid', 'true');
      button.focus();
    });
    if (input.disabled) button.disabled = true;
  }

  function sync(picker) {
    picker.value.textContent = label(picker);
    picker.button.classList.toggle('is-empty', !picker.input.value);
    picker.button.disabled = picker.input.disabled;
    if (picker.input.value) picker.button.removeAttribute('aria-invalid');
  }

  function choose(picker, date) {
    const { input } = picker;
    const next = picker.monthOnly ? isoMonth(date) : isoDay(date);
    if (input.value !== next) {
      input.value = next;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    sync(picker);
    close(true);
  }

  function clear(picker) {
    if (picker.input.value) {
      picker.input.value = '';
      picker.input.dispatchEvent(new Event('input', { bubbles: true }));
      picker.input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    sync(picker);
    close(true);
  }

  function show(picker) {
    if (open && open !== picker) close(false);
    open = picker;
    const today = new Date();
    const chosen = parse(picker.input.value, picker.monthOnly);
    picker.focus = chosen || (picker.monthOnly ? new Date(today.getFullYear(), today.getMonth(), 1) : today);
    picker.view = picker.monthOnly ? 'months' : 'days';
    getLayer().append(picker.pop);
    picker.button.setAttribute('aria-expanded', 'true');
    render(picker);
    place(picker);
  }

  function close(refocus) {
    if (!open) return;
    const { button, pop } = open;
    pop.remove();
    button.setAttribute('aria-expanded', 'false');
    open = null;
    if (refocus) button.focus();
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function navButton(text, aria, onClick) {
    const button = make('button', 'dp-nav', text);
    button.type = 'button';
    button.setAttribute('aria-label', aria);
    button.addEventListener('click', onClick);
    return button;
  }

  function render(picker) {
    const { pop, focus } = picker;
    const head = make('div', 'dp-head');
    const body = make('div', 'dp-body');
    const foot = make('div', 'dp-foot');

    if (picker.view === 'days') {
      const title = make('button', 'dp-title', `${MONTHS[focus.getMonth()]} ${focus.getFullYear()}`);
      title.type = 'button';
      title.setAttribute('aria-label', `${MONTHS[focus.getMonth()]} ${focus.getFullYear()}. Pick a different month`);
      title.setAttribute('aria-live', 'polite');
      title.addEventListener('click', () => { picker.view = 'months'; render(picker); });
      head.append(
        navButton('‹', 'Previous month', () => move(picker, addMonths(picker.focus, -1))),
        title,
        navButton('›', 'Next month', () => move(picker, addMonths(picker.focus, 1))),
      );
      body.append(dayGrid(picker));
    } else {
      const title = make('span', 'dp-title', String(focus.getFullYear()));
      title.setAttribute('aria-live', 'polite');
      head.append(
        navButton('‹', 'Previous year', () => move(picker, addMonths(picker.focus, -12))),
        title,
        navButton('›', 'Next year', () => move(picker, addMonths(picker.focus, 12))),
      );
      body.append(monthGrid(picker));
    }

    const today = new Date();
    const todayButton = make('button', 'btn btn-secondary dp-today', picker.monthOnly ? 'This month' : 'Today');
    todayButton.type = 'button';
    todayButton.disabled = !allowed(picker, today, picker.monthOnly ? 'month' : 'day');
    todayButton.addEventListener('click', () => choose(picker, picker.monthOnly ? new Date(today.getFullYear(), today.getMonth(), 1) : today));
    foot.append(todayButton);
    if (!picker.input.required) {
      const clearButton = make('button', 'btn btn-quiet dp-clear', 'Clear');
      clearButton.type = 'button';
      clearButton.addEventListener('click', () => clear(picker));
      foot.append(clearButton);
    }

    pop.replaceChildren(head, body, foot);
    const target = pop.querySelector('[data-focus]');
    if (target) target.focus();
  }

  function dayGrid(picker) {
    const { focus, input } = picker;
    const grid = make('table', 'dp-grid');
    grid.setAttribute('role', 'grid');
    const headRow = make('tr');
    WEEKDAYS.forEach((day) => {
      const th = make('th', '', day);
      th.scope = 'col';
      headRow.append(th);
    });
    const thead = make('thead');
    thead.append(headRow);
    grid.append(thead);
    const tbody = make('tbody');
    const first = new Date(focus.getFullYear(), focus.getMonth(), 1);
    const start = addDays(first, -first.getDay());
    const chosen = input.value;
    const todayIso = isoDay(new Date());
    const other = partner(input);
    const otherIso = other ? other.value : '';
    const [low, high] = [chosen, otherIso].filter(Boolean).sort();

    for (let week = 0; week < 6; week++) {
      const row = make('tr');
      for (let day = 0; day < 7; day++) {
        const date = addDays(start, week * 7 + day);
        const iso = isoDay(date);
        const cell = make('td');
        const button = make('button', 'dp-day', String(date.getDate()));
        button.type = 'button';
        button.dataset.date = iso;
        button.setAttribute('aria-label', `${MONTHS[date.getMonth()]} ${date.getDate()}, ${date.getFullYear()}`);
        button.tabIndex = -1;
        if (date.getMonth() !== focus.getMonth()) button.classList.add('is-outside');
        if (iso === todayIso) {
          button.classList.add('is-today');
          button.setAttribute('aria-current', 'date');
        }
        if (iso === chosen) button.setAttribute('aria-pressed', 'true');
        if (iso === otherIso) button.classList.add('is-partner');
        if (low && high && low !== high && iso > low && iso < high) button.classList.add('is-in-range');
        if (!allowed(picker, date, 'day')) button.disabled = true;
        if (iso === isoDay(focus)) {
          button.tabIndex = 0;
          button.dataset.focus = '';
        }
        button.addEventListener('click', () => choose(picker, date));
        cell.append(button);
        row.append(cell);
      }
      tbody.append(row);
    }
    grid.append(tbody);
    return grid;
  }

  function monthGrid(picker) {
    const { focus, input, monthOnly } = picker;
    const grid = make('div', 'dp-months');
    const chosen = parse(input.value, monthOnly);
    const now = new Date();
    MONTHS.forEach((name, index) => {
      const date = new Date(focus.getFullYear(), index, monthOnly ? 1 : Math.min(focus.getDate(), 28));
      const button = make('button', 'dp-month', name.slice(0, 3));
      button.type = 'button';
      button.setAttribute('aria-label', `${name} ${focus.getFullYear()}`);
      button.tabIndex = -1;
      if (chosen && chosen.getFullYear() === focus.getFullYear() && chosen.getMonth() === index) button.setAttribute('aria-pressed', 'true');
      if (now.getFullYear() === focus.getFullYear() && now.getMonth() === index) button.classList.add('is-today');
      if (!allowed(picker, date, 'month')) button.disabled = true;
      if (index === focus.getMonth()) {
        button.tabIndex = 0;
        button.dataset.focus = '';
      }
      button.addEventListener('click', () => {
        if (monthOnly) choose(picker, date);
        else { picker.focus = date; picker.view = 'days'; render(picker); }
      });
      grid.append(button);
    });
    return grid;
  }

  function move(picker, date) {
    picker.focus = date;
    render(picker);
  }

  function onKey(picker, event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation(); // do not also close the menu sheet behind it
      close(true);
      return;
    }
    if (event.key === 'Tab') {
      // Keep Tab inside the calendar while it is open (dialog pattern).
      const focusable = Array.from(picker.pop.querySelectorAll('button:not([disabled])')).filter((b) => b.tabIndex >= 0);
      const index = focusable.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) { event.preventDefault(); focusable[focusable.length - 1].focus(); }
      else if (!event.shiftKey && index === focusable.length - 1) { event.preventDefault(); focusable[0].focus(); }
      return;
    }
    const onGrid = document.activeElement instanceof HTMLElement && document.activeElement.matches('.dp-day, .dp-month');
    if (!onGrid) return;
    const step = picker.view === 'days'
      ? { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }
      : { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -3, ArrowDown: 3 };
    const { focus } = picker;
    let next = null;
    if (event.key in step) next = picker.view === 'days' ? addDays(focus, step[event.key]) : addMonths(focus, step[event.key]);
    else if (event.key === 'PageUp' || event.key === 'PageDown') {
      const sign = event.key === 'PageUp' ? -1 : 1;
      next = addMonths(focus, sign * (event.shiftKey || picker.view === 'months' ? 12 : 1));
    } else if (event.key === 'Home' && picker.view === 'days') next = addDays(focus, -focus.getDay());
    else if (event.key === 'End' && picker.view === 'days') next = addDays(focus, 6 - focus.getDay());
    if (next) {
      event.preventDefault();
      move(picker, next);
    }
  }

  function place(picker) {
    const { button, pop } = picker;
    const box = button.getBoundingClientRect();
    const viewWidth = document.documentElement.clientWidth;
    const tabbar = document.querySelector('.tabbar');
    const tabTop = tabbar ? tabbar.getBoundingClientRect() : null;
    const viewHeight = tabTop && tabTop.height > 0 ? Math.min(tabTop.top, window.innerHeight) : window.innerHeight;
    const width = pop.offsetWidth;
    const height = pop.offsetHeight;
    const left = Math.min(Math.max(box.left, EDGE), viewWidth - width - EDGE);
    const below = viewHeight - box.bottom - GAP - EDGE;
    const goUp = below < height && box.top - GAP - EDGE > below;
    const top = goUp ? box.top - GAP - height : box.bottom + GAP;
    pop.classList.toggle('is-up', goUp);
    pop.style.setProperty('--dp-left', `${Math.max(left, EDGE)}px`);
    pop.style.setProperty('--dp-top', `${Math.max(top, EDGE)}px`);
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('input[type="date"], input[type="month"]').forEach(enhance);
    document.addEventListener('bms:content', (event) => {
      if (event.target instanceof Element) event.target.querySelectorAll('input[type="date"], input[type="month"]').forEach(enhance);
    });

    document.addEventListener('pointerdown', (event) => {
      if (!open || !(event.target instanceof Node)) return;
      if (open.button.contains(event.target) || open.pop.contains(event.target)) return;
      close(false);
    });

    let frame = 0;
    const follow = (event) => {
      if (!open || (event && event.target instanceof Node && open.pop.contains(event.target))) return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!open) return;
        const box = open.button.getBoundingClientRect();
        if (box.bottom < 0 || box.top > window.innerHeight) close(false);
        else place(open);
      });
    };
    window.addEventListener('scroll', follow, true);
    window.addEventListener('resize', follow);
    document.addEventListener('reset', () => window.setTimeout(() => pickers.forEach(sync), 0));
  });

  window.addEventListener('pageshow', () => pickers.forEach(sync));
})();
