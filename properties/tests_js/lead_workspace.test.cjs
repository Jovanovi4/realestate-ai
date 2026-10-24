const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('clicking a lead row opens its panel without hijacking row controls', () => {
  const listeners = new Map();
  const document = {
    body: { addEventListener() {} },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll() { return []; },
    getElementById() { return null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/leads/' }, addEventListener() {} };
  const code = ["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
  vm.runInNewContext(code, {
    document, window, history, FormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  let opened = 0;
  const opener = { click() { opened += 1; } };
  const row = { querySelector(selector) { return selector === '[data-lead-panel-open], [data-owner-detail-link]' ? opener : null; } };
  const phone = { href: 'tel:+79990000000', closest(selector) { return selector === '#lead-workspace' ? {} : null; } };
  const click = (kind, overrides = {}) => {
    let prevented = false;
    listeners.get('click')({
      target: { closest(selector) {
        if (selector === '[data-lead-open-row], [data-owner-open-row]') return row;
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

test('clicking an owner request row opens its owner workflow', () => {
  const listeners = new Map();
  const document = {
    body: { addEventListener() {} },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll() { return []; },
    getElementById() { return null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/leads/' }, addEventListener() {} };
  const code = ["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
  vm.runInNewContext(code, { document, window, history, FormData, URL, URLSearchParams, WeakMap });
  let opened = 0;
  const row = { querySelector() { return { click() { opened += 1; } }; } };
  const click = (control) => {
    listeners.get('click')({
      target: { closest(selector) {
        if (selector === '[data-lead-open-row], [data-owner-open-row]') return row;
        if (selector.startsWith('a, button,')) return control ? {} : null;
        return null;
      } },
      button: 0, preventDefault() {}, stopImmediatePropagation() {},
    });
  };
  click(false);
  click(true);
  assert.equal(opened, 1);
});

test('unsaved lead form keeps the panel open until the user chooses to leave', () => {
  const listeners = new Map();
  const panel = { querySelector: () => null };
  const form = { dataset: {}, value: 'Исходный шаг' };
  const stay = { focus() {} };
  const warning = { hidden: true, querySelector: () => stay };
  const history = {
    state: null,
    replaceState(state) { this.state = state; },
  };
  const document = {
    body: { addEventListener() {} },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll(selector) { return selector === '#lead-panel form' ? [form] : []; },
    getElementById(id) {
      if (id === 'lead-panel-discard') return warning;
      if (id === 'lead-panel') return panel;
      return null;
    },
  };
  const window = {
    location: { href: 'https://example.test/leads/?lead=1' },
    addEventListener() {},
  };
  class FakeFormData {
    constructor(currentForm) { this.items = [['next_action', currentForm.value]]; }
    [Symbol.iterator]() { return this.items[Symbol.iterator](); }
  }
  const code = ["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
  vm.runInNewContext(code, {
    document, window, history, FormData: FakeFormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  let opened = 0;
  const link = {
    href: 'https://example.test/leads/?lead=2',
    dataset: { leadPanelOpen: '2' },
    closest(selector) { return selector === '#lead-workspace' ? {} : null; },
    hasAttribute(name) { return name === 'hx-get'; },
    getAttribute(name) { return name === 'hx-target' ? '#lead-workspace' : null; },
    click() { opened += 1; },
    focus() {},
  };
  const click = (kind) => {
    let prevented = false;
    const target = {
      closest(selector) {
        if (selector === '[data-lead-discard-stay]') return kind === 'stay' ? stay : null;
        if (selector === '[data-lead-discard-leave]') return kind === 'leave' ? {} : null;
        if (selector === 'a[href]') return kind === 'link' ? link : null;
        return null;
      },
    };
    listeners.get('click')({
      target,
      preventDefault() { prevented = true; },
      stopImmediatePropagation() {},
    });
    return prevented;
  };

  form.value = 'Позвонить завтра';
  assert.equal(click('link'), true);
  assert.equal(warning.hidden, false);
  assert.equal(opened, 0);
  click('stay');
  assert.equal(warning.hidden, true);
  assert.equal(opened, 0);

  click('link');
  click('leave');
  assert.equal(warning.hidden, true);
  assert.equal(opened, 1);
});

test('saved lead shows a result and offers the next visible lead', () => {
  const listeners = new Map();
  const created = [];
  const makeElement = (tag) => ({
    tag,
    dataset: {},
    children: [],
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    append(child) { this.children.push(child); },
    remove() {},
    querySelector(selector) {
      return selector === '[data-lead-panel-open]'
        ? this.children.find((child) => child.dataset.leadPanelOpen)
        : null;
    },
  });
  const body = {
    notice: null,
    querySelector(selector) { return selector === '.lead-panel__notice' ? this.notice : null; },
    prepend(element) { this.notice = element; },
  };
  const workspace = { dataset: { nextLeadId: '2', nextLeadUrl: '?page=1&lead=2' } };
  const document = {
    body: { addEventListener(name, listener) { listeners.set(name, listener); } },
    addEventListener() {},
    querySelectorAll() { return []; },
    querySelector(selector) { return selector === '#lead-panel .lead-panel__body' ? body : null; },
    getElementById(id) { return id === 'lead-workspace' ? workspace : null; },
    createElement(tag) { const element = makeElement(tag); created.push(element); return element; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const requests = [];
  const processed = [];
  const htmx = {
    ajax(method, url, options) { requests.push({ method, url, options }); },
    process(element) { processed.push(element); },
  };
  const window = {
    location: { href: 'https://example.test/leads/?page=1&lead=1&complete=1', pathname: '/leads/', search: '?page=1&lead=1&complete=1' },
    scrollX: 0,
    scrollY: 100,
    addEventListener() {},
  };
  const code = ["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
  vm.runInNewContext(code, {
    document, window, history, FormData, URL, URLSearchParams, WeakMap, htmx,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  listeners.get('leadPanelSaved')({ detail: { message: 'Первый контакт сохранён.', level: 'success' } });
  assert.equal(body.notice.attributes.role, 'status');
  assert.equal(body.notice.textContent, 'Первый контакт сохранён.');
  assert.equal(body.notice.children[0].textContent, 'Следующая заявка');
  assert.equal(body.notice.children[0].dataset.leadPanelOpen, '2');
  assert.equal(processed.length, 1);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/leads/?page=1&lead=1');
});

test('opening a lead focuses its in-page panel and scrolls to it only in a stacked layout', () => {
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
      dataset: {},
      getBoundingClientRect() { return { width }; },
      querySelector(selector) { return selector === '.lead-workspace__list' ? list : null; },
    };
    const document = {
      body: {
        addEventListener(name, listener) { listeners.set(name, listener); },
        classList: { toggle(name, active) { if (active) bodyClasses.add(name); else bodyClasses.delete(name); } },
      },
      addEventListener(name, listener) { listeners.set(name, listener); },
      querySelectorAll() { return []; },
      querySelector(selector) { return selector === '.lead-workspace__list' ? list : null; },
      getElementById(id) {
        if (id === 'lead-workspace') return workspace;
        if (id === 'lead-panel') return panel;
        return null;
      },
    };
    const history = { state: null, replaceState(state) { this.state = state; } };
    const window = { location: { href: 'https://example.test/leads/' }, addEventListener() {} };
    const code = ["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
    vm.runInNewContext(code, { document, window, history, FormData, URL, URLSearchParams, WeakMap });

    listeners.get('htmx:afterSwap')({ detail: { target: { id: 'lead-workspace' } } });
    assert.equal(focused, 1);
    assert.equal(scrolled, width < 704 ? 1 : 0);
    assert.equal(list.inert, false);
    assert.equal(bodyClasses.has('lead-panel-overlay-open'), false);
  }
});

test('lead filters stay expanded while htmx replaces the list after filtering', () => {
  const listeners = new Map();
  const makeElement = () => {
    const classes = new Set();
    return {
      classes, attributes: {},
      classList: {
        contains(name) { return classes.has(name); },
        toggle(name, active) { if (active) classes.add(name); else classes.delete(name); },
      },
      setAttribute(name, value) { this.attributes[name] = value; },
    };
  };
  let panel = makeElement();
  let toggle = makeElement();
  const workspace = { dataset: {} };
  const document = {
    body: { addEventListener(name, listener) { listeners.set(name, listener); } },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll() { return []; },
    querySelector(selector) { return selector === '[data-lead-filter-toggle]' ? toggle : null; },
    getElementById(id) {
      if (id === 'lead-filter-panel') return panel;
      if (id === 'lead-workspace') return workspace;
      return null;
    },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/leads/' }, addEventListener() {} };
  const code = ["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
  vm.runInNewContext(code, { document, window, history, FormData, URL, URLSearchParams, WeakMap });

  listeners.get('show.bs.collapse')({ target: { id: 'lead-filter-panel' } });
  panel = makeElement();
  toggle = makeElement();
  listeners.get('htmx:afterSwap')({ detail: { target: { id: 'lead-workspace' } } });
  assert.equal(panel.classes.has('show'), true);
  assert.equal(toggle.attributes['aria-expanded'], 'true');

  listeners.get('hide.bs.collapse')({ target: { id: 'lead-filter-panel' } });
  panel = makeElement();
  toggle = makeElement();
  listeners.get('htmx:afterSwap')({ detail: { target: { id: 'lead-workspace' } } });
  assert.equal(panel.classes.has('show'), false);
  assert.equal(toggle.attributes['aria-expanded'], 'false');
});

function editorFixture() {
  const listeners = new Map();
  let focused = null;
  let sent = 0;
  let closed = 0;
  const element = (selectors = [], dataset = {}) => ({
    dataset, hidden: false, attributes: {},
    closest(selector) { return selectors.includes(selector) ? this : null; },
    focus() { focused = this; },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    hasAttribute(name) { return selectors.includes(`[${name}]`); },
  });
  const overview = element();
  const body = { scrollTop: 80 };
  const heading = element();
  const contact = element();
  const contactSubmit = Object.assign(element(['[data-lead-contact-submit]']), { value: 'contacted' });
  const planSubmit = Object.assign(element(['[data-lead-plan-submit]']), { value: 'save' });
  const views = ['process', 'complete', 'decline'].map((view) => Object.assign(element([], { leadEditorView: view }), { hidden: true }));
  const forms = views.map(() => ({
    dataset: {}, values: { next_action: 'Позвонить', contact_note: '', contact_method: '' },
    closest(selector) { return selector === '#lead-panel' ? panel : null; },
    querySelector(selector) {
      if (selector === '[name="contact_note"]') return { value: this.values.contact_note };
      if (selector === '[name="contact_method"]:checked') return this.values.contact_method ? {} : null;
      return null;
    },
    requestSubmit(submitter) {
      let prevented = false;
      listeners.get('submit')({ target: this, submitter, preventDefault() { prevented = true; }, stopImmediatePropagation() {} });
      if (!prevented) sent += 1;
    },
  }));
  const shell = Object.assign(element(), {
    hidden: true,
    querySelector(selector) { return selector === '#lead-panel-editor-title' ? heading : selector === '[data-lead-contact-fields]' ? contact : null; },
    querySelectorAll(selector) { return selector === '[data-lead-editor-view]' ? views : [contactSubmit, planSubmit]; },
  });
  const more = Object.assign(element(), { hidden: true });
  const moreToggle = element(['[data-lead-more-toggle]']);
  moreToggle.setAttribute('aria-expanded', 'false');
  const warningText = element();
  const stay = element(['[data-lead-discard-stay]']);
  const leave = element(['[data-lead-discard-leave]']);
  const warning = {
    hidden: true,
    querySelector(selector) { return selector === '#lead-panel-discard-text' ? warningText : selector === '[data-lead-discard-leave]' ? leave : stay; },
  };
  const openers = Object.fromEntries(['contact', 'plan', 'complete', 'decline'].map((mode) => [mode, element(['[data-lead-editor-open]'], { leadEditorOpen: mode })]));
  const panel = {
    dataset: { leadEditorState: '' },
    querySelector(selector) {
      const elements = {
        '[data-lead-editor-shell]': shell, '[data-lead-overview]': overview, '.lead-panel__body': body,
        '#lead-panel-more': more, '[data-lead-more-toggle]': moreToggle,
        '[data-lead-editor-open]': openers.contact, '[data-lead-panel-close]': { click() { closed += 1; } },
      };
      if (selector === '[data-lead-more-toggle][aria-expanded="true"]') return moreToggle.getAttribute('aria-expanded') === 'true' ? moreToggle : null;
      return elements[selector] || views.find((view) => selector === `[data-lead-editor-view="${view.dataset.leadEditorView}"]`) || null;
    },
  };
  moreToggle.closest = (selector) => selector === '#lead-panel' ? panel : selector === '[data-lead-more-toggle]' ? moreToggle : null;
  const document = {
    body: { addEventListener(name, listener) { listeners.set(name, listener); } },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll(selector) { return selector === '#lead-panel form' ? forms : []; },
    getElementById(id) { return id === 'lead-panel' ? panel : id === 'lead-panel-discard' ? warning : null; },
  };
  class PanelFormData {
    constructor(form) { this.items = Object.entries(form.values); }
    [Symbol.iterator]() { return this.items[Symbol.iterator](); }
  }
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/leads/?lead=1' }, addEventListener() {} };
  vm.runInNewContext(["lead-editor-controls.js", "lead-workspace.js"].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n'), {
    document, window, history, FormData: PanelFormData, URL, URLSearchParams, WeakMap,
  });
  return {
    panel, overview, shell, views, forms, body, heading, contact, contactSubmit, planSubmit, more, moreToggle, warning, leave, warningText, openers,
    back: element(['[data-lead-editor-back]']),
    click(target) { listeners.get('click')({ target, preventDefault() {}, stopImmediatePropagation() {} }); },
    escape() { listeners.get('keydown')({ key: 'Escape', preventDefault() {} }); },
    get focused() { return focused; }, get sent() { return sent; }, get closed() { return closed; },
  };
}

test('lead editor opens one screen, keeps the draft on Back and restores the initiating button', () => {
  const ui = editorFixture();
  ui.click(ui.openers.contact);
  assert.equal(ui.overview.hidden, true);
  assert.equal(ui.shell.hidden, false);
  assert.deepEqual(ui.views.map((view) => view.hidden), [false, true, true]);
  assert.equal(ui.contact.hidden, false);
  assert.equal(ui.contactSubmit.disabled, false);
  assert.equal(ui.planSubmit.disabled, true);
  assert.equal(ui.focused, ui.heading);
  ui.forms[0].values.contact_note = 'Попросил позвонить завтра';
  ui.click(ui.back);
  assert.equal(ui.overview.hidden, false);
  assert.equal(ui.shell.hidden, true);
  assert.equal(ui.body.scrollTop, 80);
  assert.equal(ui.focused, ui.openers.contact);
  ui.click(ui.openers.contact);
  assert.equal(ui.forms[0].values.contact_note, 'Попросил позвонить завтра');
  ui.escape();
  assert.equal(ui.closed, 0);
  assert.equal(ui.overview.hidden, false);
  ui.click(ui.openers.plan);
  assert.equal(ui.contact.hidden, true);
  assert.equal(ui.contactSubmit.disabled, true);
  assert.equal(ui.planSubmit.disabled, false);
});

test('closing without a deal is reached from More and Escape returns to the same action', () => {
  const ui = editorFixture();
  ui.click(ui.moreToggle);
  assert.equal(ui.more.hidden, false);
  ui.click(ui.openers.decline);
  assert.deepEqual(ui.views.map((view) => view.hidden), [true, true, false]);
  assert.equal(ui.more.hidden, true);
  ui.escape();
  assert.equal(ui.more.hidden, false);
  assert.equal(ui.focused, ui.openers.decline);
  ui.escape();
  assert.equal(ui.more.hidden, true);
  assert.equal(ui.closed, 0);
});

test('sending a different action warns before discarding another form draft', () => {
  const ui = editorFixture();
  ui.forms[0].values.next_action = 'Новый несохранённый план';
  ui.click(ui.openers.complete);
  ui.forms[1].requestSubmit({ value: '' });
  assert.equal(ui.sent, 0);
  assert.equal(ui.warning.hidden, false);
  assert.match(ui.warningText.textContent, /в другом действии/);
  ui.click(ui.leave);
  assert.equal(ui.sent, 1);
  assert.equal(ui.warning.hidden, true);
});

test('saving only a plan warns when the user also drafted a first contact', () => {
  const ui = editorFixture();
  ui.forms[0].values.contact_method = 'call';
  ui.forms[0].values.contact_note = 'Договорились о просмотре';
  ui.click(ui.openers.plan);
  ui.forms[0].requestSubmit(ui.planSubmit);
  assert.equal(ui.sent, 0);
  assert.match(ui.warningText.textContent, /Способ связи и итог разговора не будут записаны/);
  ui.escape();
  assert.equal(ui.warning.hidden, true);
  assert.equal(ui.panel.dataset.leadEditorState, 'plan');
  assert.equal(ui.forms[0].values.contact_note, 'Договорились о просмотре');
});

test('date shortcuts use CRM timezone, keep precise time and handle the end of a day', () => {
  const listeners = new Map();
  let now = '2026-10-22T21:10:00Z'; // Already 23 October in Moscow.
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
  }
  const input = {
    value: '2026-11-01T15:40', validity: '', events: [],
    setCustomValidity(message) { this.validity = message; }, reportValidity() {}, focus() {},
    dispatchEvent(event) { this.events.push(event.type); },
  };
  const group = { dataset: { timeZone: 'Europe/Moscow' }, querySelector() { return input; } };
  const document = {
    body: { addEventListener() {} }, querySelectorAll() { return []; }, getElementById() { return null; },
    addEventListener(name, listener) { listeners.set(name, listener); },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/leads/' }, addEventListener() {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../static/js/lead-editor-controls.js'), 'utf8'), {
    document, window, history, FormData, URL, URLSearchParams, WeakMap, Date: ClockDate, Intl, Event,
  });
  const pick = (offset) => {
    const button = { dataset: { leadDate: offset }, closest: () => group };
    listeners.get('click')({ target: { closest(selector) { return selector === '[data-lead-date]' ? button : null; } } });
  };
  pick('0');
  assert.equal(input.value, '2026-10-23T15:40');
  pick('1');
  assert.equal(input.value, '2026-10-24T15:40');
  assert.deepEqual(input.events, ['input', 'change', 'input', 'change']);
  now = '2026-10-23T15:10:00Z';
  pick('0');
  assert.equal(input.value, '2026-10-23T19:10');
  now = '2026-10-23T20:59:30Z';
  pick('0');
  assert.match(input.validity, /Сегодня уже нет доступного времени/);
  pick('1');
  assert.equal(input.validity, '');
  assert.equal(input.value, '2026-10-24T19:10');
});
