/* Themed drop-down for every <select>, as progressive enhancement.

   The native <select> stays in the form (visually hidden, still submitted, still validated) and
   remains the single source of truth: choosing in the themed list sets select.value and fires
   `input` + `change` on it, and any change made to the native select (scripts, form reset,
   Playwright's selectOption) is mirrored back. Without JavaScript the plain select is shown.

   Keyboard follows the WAI-ARIA "select-only combobox" pattern: focus stays on the button and
   aria-activedescendant points at the highlighted option. The list lives in one body-level layer
   with fixed positioning, so table wrappers that scroll sideways (overflow) never clip it.
   Position is set through CSS custom properties with style.setProperty (CSSOM), which the
   `style-src 'self'` CSP allows; inline style attributes in markup would be blocked. */
(() => {
  'use strict';

  const GAP = 4;          // px between the button and its list
  const EDGE = 8;         // px kept free at the viewport edges
  const MAX_LIST = 352;   // px; about seven 46px rows before the list scrolls
  const PAGE_STEP = 5;
  const TYPEAHEAD_MS = 600;
  const SEARCH_FROM = 10; // a list this long gets a filter box even without data-search

  let layer = null;
  let open = null;        // the enhanced control whose list is showing
  let nextId = 1;
  const controls = [];

  function getLayer() {
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'dd-layer';
      document.body.append(layer);
    }
    return layer;
  }

  function enhance(select) {
    if (select.multiple || select.size > 1 || select.dataset.enhanced === 'true') return;
    select.dataset.enhanced = 'true';
    const id = `dd${nextId++}`;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'dd';
    if (select.classList.contains('select-compact')) button.classList.add('dd-compact');
    button.setAttribute('role', 'combobox');
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', `${id}-list`);

    const label = select.id ? document.querySelector(`label[for="${CSS.escape(select.id)}"]`) : null;
    if (label) {
      if (!label.id) label.id = `${id}-label`;
      button.setAttribute('aria-labelledby', label.id);
      // A click on the label would focus the hidden native select; send it to the button instead.
      label.addEventListener('click', (event) => {
        event.preventDefault();
        button.focus();
      });
    } else if (select.getAttribute('aria-label')) {
      button.setAttribute('aria-label', select.getAttribute('aria-label'));
    }
    if (select.getAttribute('aria-describedby')) button.setAttribute('aria-describedby', select.getAttribute('aria-describedby'));

    // Every option label stacked invisibly in one grid cell: the button is as wide as the longest
    // option, so a compact select in a table never cuts its text and never changes width on choice.
    const face = document.createElement('span');
    face.className = 'dd-face';
    const value = document.createElement('span');
    value.className = 'dd-value';
    face.append(value);
    for (const option of select.options) {
      const sizer = document.createElement('span');
      sizer.className = 'dd-sizer';
      sizer.setAttribute('aria-hidden', 'true');
      sizer.textContent = option.text;
      face.append(sizer);
    }
    const chevron = document.createElement('span');
    chevron.className = 'dd-chevron';
    chevron.setAttribute('aria-hidden', 'true');
    button.append(face, chevron);

    // data-search (or a long list) adds a filter box above the options: typing narrows them live and
    // Enter picks the highlighted one. `pop` is what gets positioned; without a filter box it is the list itself.
    const searchable = select.hasAttribute('data-search') || select.options.length > SEARCH_FROM;
    const list = document.createElement('ul');
    list.className = searchable ? 'dd-options' : 'dd-list';
    list.id = `${id}-list`;
    list.setAttribute('role', 'listbox');
    list.tabIndex = -1;
    if (label) list.setAttribute('aria-labelledby', label.id);
    else if (select.getAttribute('aria-label')) list.setAttribute('aria-label', select.getAttribute('aria-label'));
    Array.from(select.options).forEach((option, index) => {
      const item = document.createElement('li');
      item.className = 'dd-option';
      item.id = `${id}-o${index}`;
      item.setAttribute('role', 'option');
      item.textContent = option.text;
      if (option.disabled) item.setAttribute('aria-disabled', 'true');
      list.append(item);
    });

    let pop = list;
    let search = null;
    let none = null;
    if (searchable) {
      pop = document.createElement('div');
      pop.className = 'dd-list dd-pop';
      search = document.createElement('input');
      search.type = 'text';
      search.className = 'dd-search';
      search.autocomplete = 'off';
      search.spellcheck = false;
      search.placeholder = 'Type to filter…';
      search.setAttribute('role', 'combobox');
      search.setAttribute('aria-expanded', 'true');
      search.setAttribute('aria-controls', list.id);
      search.setAttribute('aria-autocomplete', 'list');
      search.setAttribute('aria-label', `Filter ${(label ? label.textContent : select.getAttribute('aria-label') || 'options').trim()}`);
      none = document.createElement('p');
      none.className = 'dd-none';
      none.textContent = 'Nothing matches.';
      none.hidden = true;
      pop.append(search, list, none);
    }

    select.classList.add('dd-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    select.after(button);

    const control = { select, button, list, pop, search, none, value, active: -1, typed: '', typedAt: 0 };
    controls.push(control);
    sync(control);

    button.addEventListener('click', () => (open === control ? close(true) : show(control, select.selectedIndex)));
    button.addEventListener('keydown', (event) => onKey(control, event));
    // Keep focus on the button while the pointer picks an option.
    list.addEventListener('mousedown', (event) => event.preventDefault());
    list.addEventListener('click', (event) => {
      const item = event.target instanceof Element ? event.target.closest('.dd-option') : null;
      if (!item || item.getAttribute('aria-disabled') === 'true') return;
      choose(control, Array.prototype.indexOf.call(list.children, item));
      close(true);
    });
    list.addEventListener('mousemove', (event) => {
      const item = event.target instanceof Element ? event.target.closest('.dd-option') : null;
      if (item) highlight(control, Array.prototype.indexOf.call(list.children, item), false);
    });
    if (search) {
      search.addEventListener('input', () => filter(control));
      search.addEventListener('keydown', (event) => onSearchKey(control, event));
    }
    select.addEventListener('change', () => sync(control));
    // A required select left empty: show the problem on the control the person can see.
    select.addEventListener('invalid', () => {
      button.setAttribute('aria-invalid', 'true');
      button.focus();
    });
    select.addEventListener('focus', () => button.focus());
    if (select.disabled) button.disabled = true;
  }

  function sync(control) {
    const { select, button, list, value } = control;
    const chosen = select.selectedIndex;
    value.textContent = chosen >= 0 ? select.options[chosen].text : '';
    Array.from(list.children).forEach((item, index) => item.setAttribute('aria-selected', String(index === chosen)));
    button.disabled = select.disabled;
    if (chosen >= 0 && select.value !== '') button.removeAttribute('aria-invalid');
  }

  function choose(control, index) {
    const { select } = control;
    const option = select.options[index];
    if (!option || option.disabled) return;
    if (select.selectedIndex !== index) {
      select.selectedIndex = index;
      select.dispatchEvent(new Event('input', { bubbles: true }));
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    sync(control);
  }

  function enabledIndexes(control) {
    const items = control.list.children;
    return Array.from(control.select.options)
      .map((option, index) => (option.disabled || items[index].hidden ? -1 : index))
      .filter((index) => index >= 0);
  }

  // Live filter: every word typed must appear in the option's text (any order, any case).
  function filter(control) {
    const words = control.search.value.toLowerCase().split(/\s+/).filter(Boolean);
    Array.from(control.list.children).forEach((item) => {
      item.hidden = !words.every((word) => item.textContent.toLowerCase().includes(word));
    });
    const usable = enabledIndexes(control);
    control.none.hidden = usable.length > 0;
    if (control.active >= 0 && control.list.children[control.active]) control.list.children[control.active].classList.remove('is-active');
    control.active = -1;
    control.search.removeAttribute('aria-activedescendant');
    if (usable.length) highlight(control, usable.includes(control.select.selectedIndex) && !words.length ? control.select.selectedIndex : usable[0]);
    place(control);
  }

  function onSearchKey(control, event) {
    const moves = { ArrowDown: 1, ArrowUp: -1, PageDown: PAGE_STEP, PageUp: -PAGE_STEP };
    if (event.key in moves) {
      event.preventDefault();
      highlight(control, step(control, control.active, moves[event.key]));
    } else if (event.key === 'Enter') {
      event.preventDefault(); // never submit the form from the filter box; Enter means "pick this one"
      if (control.active >= 0 && !control.list.children[control.active].hidden) {
        choose(control, control.active);
        close(true);
      }
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'Tab') {
      close(false);
    }
  }

  function highlight(control, index, scroll = true) {
    const { button, list } = control;
    if (index < 0 || index >= list.children.length) return;
    if (control.active >= 0 && list.children[control.active]) list.children[control.active].classList.remove('is-active');
    control.active = index;
    const item = list.children[index];
    item.classList.add('is-active');
    (control.search || button).setAttribute('aria-activedescendant', item.id);
    if (scroll) {
      // Scroll only the list; scrollIntoView could also move the page.
      if (item.offsetTop < list.scrollTop) list.scrollTop = item.offsetTop;
      else if (item.offsetTop + item.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = item.offsetTop + item.offsetHeight - list.clientHeight;
    }
  }

  function show(control, index) {
    if (open && open !== control) close(false);
    open = control;
    getLayer().append(control.pop);
    control.pop.classList.add('is-open');
    control.button.setAttribute('aria-expanded', 'true');
    if (control.search) {
      control.search.value = '';
      filter(control);
    }
    place(control);
    const usable = enabledIndexes(control);
    highlight(control, usable.includes(index) ? index : (usable[0] ?? -1));
    if (control.search) control.search.focus();
  }

  function close(refocus) {
    if (!open) return;
    const { button, pop } = open;
    pop.classList.remove('is-open');
    pop.remove();
    button.setAttribute('aria-expanded', 'false');
    button.removeAttribute('aria-activedescendant');
    open = null;
    if (refocus) button.focus();
  }

  // Below the button when it fits, otherwise above (near the bottom of the screen); always inside the viewport.
  function place(control) {
    const { button, pop: list } = control;
    const box = button.getBoundingClientRect();
    const viewWidth = document.documentElement.clientWidth;
    const viewHeight = visibleBottom();
    // At least as wide as the button, wider when an option needs it (CSS caps it at the viewport).
    list.style.setProperty('--dd-min', `${box.width}px`);
    list.style.setProperty('--dd-max', `${MAX_LIST}px`);
    const width = list.offsetWidth;
    const natural = list.offsetHeight;
    const left = Math.min(Math.max(box.left, EDGE), viewWidth - width - EDGE);
    const below = viewHeight - box.bottom - GAP - EDGE;
    const above = box.top - GAP - EDGE;
    const goUp = below < natural && above > below;
    const height = Math.min(natural, goUp ? above : below);
    const top = goUp ? box.top - GAP - height : box.bottom + GAP;
    list.classList.toggle('is-up', goUp);
    list.style.setProperty('--dd-left', `${left}px`);
    list.style.setProperty('--dd-top', `${Math.max(top, EDGE)}px`);
    list.style.setProperty('--dd-max', `${Math.max(height, 0)}px`);
  }

  // The phone tab bar is fixed over the bottom of the screen; a list must not open underneath it.
  function visibleBottom() {
    const tabbar = document.querySelector('.tabbar');
    if (!tabbar) return window.innerHeight;
    const box = tabbar.getBoundingClientRect();
    return box.height > 0 ? Math.min(box.top, window.innerHeight) : window.innerHeight;
  }

  function step(control, from, delta) {
    const usable = enabledIndexes(control);
    if (!usable.length) return -1;
    const position = usable.indexOf(from);
    if (position < 0) return delta > 0 ? usable[0] : usable[usable.length - 1];
    return usable[Math.min(Math.max(position + delta, 0), usable.length - 1)];
  }

  // Type-ahead: typed letters within TYPEAHEAD_MS build a prefix; repeating one letter cycles matches.
  function typeahead(control, key) {
    const now = Date.now();
    control.typed = now - control.typedAt > TYPEAHEAD_MS ? key : control.typed + key;
    control.typedAt = now;
    const options = Array.from(control.select.options);
    const sameLetter = control.typed.split('').every((ch) => ch === control.typed[0]);
    const prefix = sameLetter ? control.typed[0] : control.typed;
    const start = control.active >= 0 ? control.active : control.select.selectedIndex;
    const order = options.map((_, offset) => (start + (sameLetter ? 1 : 0) + offset) % options.length);
    const match = order.find((index) => !options[index].disabled && options[index].text.trim().toLowerCase().startsWith(prefix.toLowerCase()));
    return match ?? -1;
  }

  function onKey(control, event) {
    const isOpen = open === control;
    const { key } = event;
    if (event.ctrlKey || event.metaKey) return;

    if (!isOpen) {
      // A filterable list: the first letter typed on the closed button starts the filter.
      if (control.search && key.length === 1 && key !== ' ' && !event.altKey) {
        event.preventDefault();
        show(control, control.select.selectedIndex);
        control.search.value = key;
        filter(control);
        return;
      }
      if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(key)) {
        event.preventDefault();
        show(control, control.select.selectedIndex);
      } else if (key === 'Home' || key === 'End') {
        event.preventDefault();
        const usable = enabledIndexes(control);
        show(control, key === 'Home' ? usable[0] : usable[usable.length - 1]);
      } else if (key.length === 1 && key !== ' ' && !event.altKey) {
        const match = typeahead(control, key);
        if (match >= 0) {
          event.preventDefault();
          show(control, match);
        }
      }
      return;
    }

    const moves = { ArrowDown: 1, ArrowUp: -1, PageDown: PAGE_STEP, PageUp: -PAGE_STEP };
    if (key in moves || key === 'Home' || key === 'End') control.typedAt = 0; // after moving, Space means "choose"
    if (key in moves) {
      event.preventDefault();
      if (event.altKey && key === 'ArrowUp') {
        choose(control, control.active);
        close(true);
        return;
      }
      highlight(control, step(control, control.active, moves[key]));
    } else if (key === 'Home' || key === 'End') {
      event.preventDefault();
      const usable = enabledIndexes(control);
      highlight(control, key === 'Home' ? usable[0] : usable[usable.length - 1]);
    } else if (key === 'Enter' || (key === ' ' && Date.now() - control.typedAt > TYPEAHEAD_MS)) {
      event.preventDefault();
      choose(control, control.active);
      close(true);
    } else if (key === 'Escape') {
      event.preventDefault();
      event.stopPropagation(); // do not also close the menu sheet / dialog behind it
      close(true);
    } else if (key === 'Tab') {
      choose(control, control.active); // APG: Tab accepts the highlighted option and moves on
      close(false);
    } else if (key.length === 1 && !event.altKey) {
      event.preventDefault();
      const match = typeahead(control, key);
      if (match >= 0) highlight(control, match);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select').forEach(enhance);
    // Rows and sections that a live update brings in (app.js) are dressed the same way.
    document.addEventListener('bms:content', (event) => {
      if (event.target instanceof Element) event.target.querySelectorAll('select').forEach(enhance);
    });

    document.addEventListener('pointerdown', (event) => {
      if (!open || !(event.target instanceof Node)) return;
      if (open.button.contains(event.target) || open.pop.contains(event.target)) return;
      close(false);
    });

    // Keep the list attached to its button while the page or a table wrapper scrolls; close it if
    // the button has scrolled out of sight.
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

    // A form reset changes the native values without a change event.
    document.addEventListener('reset', () => window.setTimeout(() => controls.forEach(sync), 0));
  });

  // Back/forward restores form values after scripts ran; show the restored choice.
  window.addEventListener('pageshow', () => controls.forEach(sync));
})();
