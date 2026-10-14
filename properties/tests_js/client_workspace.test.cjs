const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const code = fs.readFileSync(path.join(__dirname, '../../static/js/client-workspace.js'), 'utf8');

test('client filters stay open across list refresh and close with Escape', () => {
  const documentListeners = new Map();
  const bodyListeners = new Map();
  const trigger = {
    attributes: {},
    classList: { toggle() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    focus() { this.focused = true; },
  };
  const search = { focus() { this.focused = true; } };
  const makeWorkspace = () => {
    const classes = new Set();
    return {
      dataset: { activeFilters: 'true' },
      classList: {
        toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
      querySelector(selector) { return selector === '#client-q' ? search : null; },
    };
  };
  let workspace = makeWorkspace();
  const document = {
    body: { addEventListener(name, listener) { bodyListeners.set(name, listener); } },
    addEventListener(name, listener) { documentListeners.set(name, listener); },
    querySelectorAll() { return []; },
    querySelector(selector) { return selector === '[data-client-filter-toggle]' ? trigger : null; },
    getElementById(id) { return id === 'client-workspace' ? workspace : null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/clients/' }, addEventListener() {} };
  vm.runInNewContext(code, { document, window, history, FormData, URL, URLSearchParams, WeakMap });

  assert.equal(trigger.attributes['aria-expanded'], 'false');
  documentListeners.get('click')({
    target: { closest(selector) { return selector === '[data-client-filter-toggle]' ? trigger : null; } },
    preventDefault() {},
  });
  assert.equal(trigger.attributes['aria-expanded'], 'true');
  assert.equal(search.focused, true);
  workspace = makeWorkspace();
  bodyListeners.get('htmx:afterSwap')({ detail: { target: { id: 'client-workspace' } } });
  assert.equal(workspace.classList.contains('client-workspace--filters-open'), true);

  documentListeners.get('keydown')({
    key: 'Escape', target: { closest(selector) { return selector === '#client-toolbar' ? {} : null; } },
    preventDefault() {},
  });
  assert.equal(trigger.attributes['aria-expanded'], 'false');
  assert.equal(workspace.classList.contains('client-workspace--filters-open'), false);
  assert.equal(trigger.focused, true);
});

test('client relationship save appears only after selecting another status', () => {
  const listeners = new Map();
  const select = { value: 'new' };
  const save = { hidden: false };
  const form = {
    dataset: { initialStatus: 'new' },
    querySelector(selector) {
      if (selector === 'select[name="status"]') return select;
      if (selector === '[data-panel-status-save]') return save;
      return null;
    },
  };
  const document = {
    body: { addEventListener() {} },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll(selector) { return selector === '#client-panel form' ? [form] : []; },
    querySelector(selector) { return selector === '#client-panel [data-client-panel-status-form]' ? form : null; },
    getElementById() { return null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/clients/?client=1' }, addEventListener() {} };
  class FakeFormData {
    constructor() { this.values = [['status', select.value]]; }
    [Symbol.iterator]() { return this.values[Symbol.iterator](); }
  }
  vm.runInNewContext(code, {
    document, window, history, FormData: FakeFormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  assert.equal(save.hidden, true);
  const change = () => listeners.get('change')({
    target: { matches: (selector) => selector === '#client-panel [data-client-panel-status-form] select[name="status"]' },
  });
  select.value = 'in_progress';
  change();
  assert.equal(save.hidden, false);
  select.value = 'new';
  change();
  assert.equal(save.hidden, true);
  form.dataset.unsaved = 'true';
  change();
  assert.equal(save.hidden, false);
});

test('clicking a client row opens its panel without hijacking row controls', () => {
  const listeners = new Map();
  const document = {
    body: { addEventListener() {} },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    getElementById() { return null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/clients/' }, addEventListener() {} };
  vm.runInNewContext(code, {
    document, window, history, FormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  let opened = 0;
  const opener = { click() { opened += 1; } };
  const row = { querySelector(selector) { return selector === '[data-client-panel-open]' ? opener : null; } };
  const phone = { href: 'tel:+79990000000', closest(selector) { return selector === '#client-workspace' ? {} : null; } };
  const click = (kind, overrides = {}) => {
    let prevented = false;
    listeners.get('click')({
      target: { closest(selector) {
        if (selector === '[data-client-open-row]') return row;
        if (selector.startsWith('a, button,')) return kind === 'row' ? null : {};
        if (selector === 'a[href]') return kind === 'phone' ? phone : null;
        return null;
      } },
      button: 0,
      ...overrides,
      preventDefault() { prevented = true; },
      stopImmediatePropagation() {},
    });
    return prevented;
  };

  assert.equal(click('row'), true);
  assert.equal(opened, 1);
  for (const kind of ['checkbox', 'phone', 'details']) assert.equal(click(kind), false);
  assert.equal(click('row', { ctrlKey: true }), false);
  assert.equal(click('row', { button: 1 }), false);
  assert.equal(opened, 1);
});

test('unsaved client task prevents panel navigation until discard is chosen', () => {
  const listeners = new Map();
  const form = { dataset: {}, value: 'Позвонить завтра' };
  const stay = { focus() {} };
  const warning = { hidden: true, querySelector: () => stay };
  const document = {
    body: { addEventListener() {} },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll(selector) { return selector === '#client-panel form' ? [form] : []; },
    querySelector() { return null; },
    getElementById(id) { return id === 'client-panel-discard' ? warning : null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/clients/?client=1' }, addEventListener() {} };
  class FakeFormData {
    constructor(currentForm) { this.items = [['text', currentForm.value]]; }
    [Symbol.iterator]() { return this.items[Symbol.iterator](); }
  }
  vm.runInNewContext(code, {
    document, window, history, FormData: FakeFormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });
  let opened = 0;
  const link = {
    href: 'https://example.test/clients/?client=2',
    closest(selector) { return selector === '#client-workspace' ? {} : null; },
    hasAttribute(name) { return name === 'hx-get'; },
    getAttribute(name) { return name === 'hx-target' ? '#client-workspace' : null; },
    click() { opened += 1; }, focus() {},
  };
  const click = (kind) => {
    let prevented = false;
    listeners.get('click')({
      target: { closest(selector) {
        if (selector === '[data-client-discard-stay]') return kind === 'stay' ? stay : null;
        if (selector === '[data-client-discard-leave]') return kind === 'leave' ? {} : null;
        if (selector === 'a[href]') return kind === 'link' ? link : null;
        return null;
      } },
      preventDefault() { prevented = true; }, stopImmediatePropagation() {},
    });
    return prevented;
  };
  form.value = 'Позвонить в пятницу';
  assert.equal(click('link'), true);
  assert.equal(warning.hidden, false);
  assert.equal(opened, 0);
  click('stay');
  assert.equal(warning.hidden, true);
  click('link');
  click('leave');
  assert.equal(opened, 1);
});

test('saving a client action refreshes the list and offers the next client', () => {
  const listeners = new Map();
  const body = { notice: null, querySelector(selector) { return selector === '.lead-panel__notice' ? this.notice : null; }, prepend(element) { this.notice = element; } };
  const workspace = {
    dataset: { nextClientId: '2', nextClientUrl: '?page=1&client=2' },
    getBoundingClientRect() { return { width: 900 }; }, querySelector() { return null; },
    classList: { toggle() {}, contains() { return false; } },
  };
  const document = {
    body: { addEventListener(name, listener) { listeners.set(name, listener); }, classList: { remove() {} } },
    addEventListener() {}, querySelectorAll() { return []; },
    documentElement: { scrollWidth: 900, clientWidth: 900 },
    querySelector(selector) { return selector === '#client-panel .lead-panel__body' ? body : null; },
    getElementById(id) { return id === 'client-workspace' ? workspace : null; },
    createElement(tag) { return {
      tag, dataset: {}, children: [], attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      append(child) { this.children.push(child); }, remove() {},
    }; },
  };
  const requests = [];
  const processed = [];
  const htmx = { ajax(method, url, options) { requests.push({ method, url, options }); }, process(element) { processed.push(element); } };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/clients/?page=1&client=1', pathname: '/clients/', search: '?page=1&client=1' }, scrollX: 0, scrollY: 100, scrollTo() {}, addEventListener() {} };
  vm.runInNewContext(code, {
    document, window, history, FormData, URL, URLSearchParams, WeakMap, htmx,
    ResizeObserver: class { observe() {} unobserve() {} },
  });
  listeners.get('clientPanelSaved')({ detail: { message: 'Задача обновлена.' } });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/clients/?page=1&client=1');
  listeners.get('htmx:afterSwap')({ detail: { target: { id: 'client-workspace' } } });
  assert.equal(body.notice.textContent, 'Задача обновлена.');
  assert.equal(body.notice.attributes.role, 'status');
  assert.equal(body.notice.children[0].dataset.clientPanelOpen, '2');
  assert.equal(processed.length, 1);
});

test('opening another client focuses the in-page panel without blocking the list', () => {
  for (const width of [700, 1100]) {
    const listeners = new Map();
    let focused = 0;
    let scrolled = 0;
    const list = { inert: false };
    const bodyClasses = new Set();
    const panel = {
      focus() { focused += 1; },
      scrollIntoView() { scrolled += 1; },
    };
    const workspace = {
      getBoundingClientRect() { return { width }; },
      querySelector(selector) { return selector === '.client-workspace__list' ? list : null; },
    };
    const document = {
      body: {
        addEventListener(name, listener) { listeners.set(name, listener); },
        classList: { toggle(name, active) { if (active) bodyClasses.add(name); else bodyClasses.delete(name); } },
      },
      addEventListener(name, listener) { listeners.set(name, listener); },
      querySelectorAll() { return []; },
      querySelector(selector) { return selector === '.client-workspace__list' ? list : null; },
      getElementById(id) {
        if (id === 'client-workspace') return workspace;
        if (id === 'client-panel') return panel;
        return null;
      },
    };
    const history = { state: null, replaceState(state) { this.state = state; } };
    const window = { location: { href: 'https://example.test/clients/?client=2' }, addEventListener() {} };
    vm.runInNewContext(code, { document, window, history, FormData, URL, URLSearchParams, WeakMap });

    listeners.get('htmx:afterSwap')({ detail: { target: { id: 'client-workspace' } } });
    assert.equal(focused, 1);
    assert.equal(scrolled, width < 704 ? 1 : 0);
    assert.equal(list.inert, false);
    assert.equal(bodyClasses.has('lead-panel-overlay-open'), false);
  }
});
