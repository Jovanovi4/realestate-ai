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
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/lead-workspace.js'), 'utf8');
  vm.runInNewContext(code, {
    document, window, history, FormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  let opened = 0;
  const opener = { click() { opened += 1; } };
  const row = { querySelector(selector) { return selector === '[data-lead-panel-open]' ? opener : null; } };
  const phone = { href: 'tel:+79990000000', closest(selector) { return selector === '#lead-workspace' ? {} : null; } };
  const click = (kind, overrides = {}) => {
    let prevented = false;
    listeners.get('click')({
      target: { closest(selector) {
        if (selector === '[data-lead-open-row]') return row;
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

test('unsaved lead form keeps the panel open until the user chooses to leave', () => {
  const listeners = new Map();
  const panel = { querySelector: () => null };
  const form = { dataset: {}, value: 'Исходный шаг' };
  const stay = { focus() {} };
  const warning = { hidden: true, querySelector: () => stay };
  let planFocuses = 0;
  const planEditor = { open: false, querySelector: () => ({ focus() { planFocuses += 1; } }) };
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
      if (id === 'lead-panel-process') return planEditor;
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
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/lead-workspace.js'), 'utf8');
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
        if (selector === '[data-lead-panel-plan]') return kind === 'plan' ? {} : null;
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

  click('plan');
  assert.equal(planEditor.open, true);
  assert.equal(planFocuses, 1);

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
    location: { href: 'https://example.test/leads/?page=1&lead=1', pathname: '/leads/', search: '?page=1&lead=1' },
    scrollX: 0,
    scrollY: 100,
    addEventListener() {},
  };
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/lead-workspace.js'), 'utf8');
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

test('wide lead layout falls back to overlay when the compact table would scroll horizontally', () => {
  const listeners = new Map();
  const classes = new Set();
  const table = { scrollWidth: 700, clientWidth: 600 };
  const workspace = {
    parentElement: { children: [] },
    getBoundingClientRect() { return { width: 1200 }; },
    querySelector(selector) { return selector === '.lead-workspace__list .table-responsive' ? table : null; },
    classList: {
      toggle(name, active) { if (active) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
      remove(name) { classes.delete(name); },
    },
  };
  const document = {
    body: { addEventListener() {}, classList: { remove() {} } },
    documentElement: { scrollWidth: 1200, clientWidth: 1200 },
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    getElementById(id) { return id === 'lead-workspace' ? workspace : null; },
  };
  const history = { state: null, replaceState(state) { this.state = state; } };
  const window = { location: { href: 'https://example.test/leads/' }, addEventListener() {} };
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/lead-workspace.js'), 'utf8');
  vm.runInNewContext(code, {
    document, window, history, FormData, URL, URLSearchParams, WeakMap,
    ResizeObserver: class { observe() {} unobserve() {} },
  });

  listeners.get('DOMContentLoaded')();
  assert.equal(classes.has('lead-workspace--roomy'), false);

  table.scrollWidth = 600;
  listeners.get('DOMContentLoaded')();
  assert.equal(classes.has('lead-workspace--roomy'), true);
});
