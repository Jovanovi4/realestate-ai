const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const code = fs.readFileSync(path.join(__dirname, '../../static/js/deal-panel.js'), 'utf8');

function setup({ open = false, fetchReply, initialStage = 'new' } = {}) {
  const listeners = new Map();
  const dispatched = [];
  const requests = [];
  const classes = new Set();
  let focused = 0;
  let currentPanel = null;
  class FakeElement {}
  const field = { hidden: true, querySelector: () => form.outcome };
  const button = { disabled: false, hidden: false };
  const error = { hidden: true, textContent: '', focus() {} };
  const form = {
    dataset: { dealId: '1' }, action: '/deals/1/stage/', isConnected: true,
    elements: { stage: { value: initialStage }, expected_stage: { value: initialStage } },
    outcome: { value: '', required: false },
    matches(selector) { return selector === '[data-deal-panel-stage-form]'; },
    reportValidity() { return !this.outcome.required || !!this.outcome.value.trim(); },
    querySelector(selector) {
      if (selector === '[data-deal-panel-loss-field]') return field;
      if (selector === '[type="submit"]') return button;
      if (selector === '[data-panel-status-save]') return button;
      if (selector === '[data-deal-panel-error]') return error;
      if (selector === '[name="stage"]') return this.elements.stage;
      return null;
    },
  };
  const panel = {
    querySelector(selector) { return selector === '[data-deal-panel-stage-form]' ? form : null; },
    setAttribute() {}, focus() { focused += 1; },
  };
  const slot = {
    dataset: open ? { dealId: '1' } : {},
    set outerHTML(html) {
      currentPanel = html === 'OPEN' ? panel : null;
      this.dataset = currentPanel ? { dealId: '1' } : {};
    },
  };
  currentPanel = open ? panel : null;
  const link = { attributes: {}, setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; }, focus() { focused += 1; } };
  const card = { dataset: { dealId: '1', dealStage: 'new' }, classList: { toggle(name, active) { if (active) classes.add(name); else classes.delete(name); } }, querySelector() { return link; } };
  const board = { querySelectorAll() { return [card]; }, querySelector() { return link; } };
  const list = { inert: false };
  const workspace = {
    getBoundingClientRect() { return { width: 1400 }; },
    classList: { toggle() {} }, querySelector() { return list; },
  };
  const warning = { hidden: true, querySelector: () => ({ focus() {} }) };
  const document = {
    body: { classList: { toggle() {} } },
    getElementById(id) {
      return { 'deal-workspace': workspace, 'deal-panel-slot': slot, 'deal-panel': currentPanel, 'deal-panel-discard': warning }[id] || null;
    },
    querySelector(selector) { return selector === '[data-deal-board]' ? board : null; },
    querySelectorAll() { return []; },
    addEventListener(name, listener) { listeners.set(name, listener); },
    dispatchEvent(event) { dispatched.push(event); },
  };
  const location = { href: 'https://example.test/deals/', assign() {}, reload() {} };
  const history = {
    state: null,
    replaceState(state, _, url) { this.state = state; location.href = new URL(url, location.href).href; },
    pushState(state, _, url) { this.state = state; location.href = new URL(url, location.href).href; },
  };
  const window = { addEventListener() {}, confirm: () => true };
  class FakeFormData {
    constructor(currentForm) { this.values = [
      ['stage', currentForm.elements.stage.value],
      ['expected_stage', currentForm.elements.expected_stage.value],
      ['outcome_note', currentForm.outcome.value],
    ]; }
    [Symbol.iterator]() { return this.values[Symbol.iterator](); }
  }
  const fetch = async (url, options) => {
    requests.push({ url, options });
    return fetchReply ? fetchReply(url, options) : { ok: true, text: async () => String(url).includes('deal=') ? 'OPEN' : 'CLOSED' };
  };
  vm.runInNewContext(code, {
    document, window, location, history, fetch, FormData: FakeFormData,
    URL, URLSearchParams, WeakMap, Element: FakeElement,
    ResizeObserver: class { observe() {} },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
  });
  const makeLink = (href, attribute) => Object.assign(new FakeElement(), {
    href, closest(selector) { return selector === 'a[href]' ? this : null; },
    matches(selector) { return selector.includes(attribute); },
  });
  const click = (target) => {
    let prevented = false;
    listeners.get('click')({ target, button: 0, preventDefault() { prevented = true; } });
    return prevented;
  };
  return { listeners, dispatched, requests, classes, card, form, field, button, error, warning, location, click, makeLink, FakeElement, focused: () => focused };
}

test('deal panel opens from the board and warns before discarding a changed stage', async () => {
  const ui = setup();
  const open = ui.makeLink('https://example.test/deals/?deal=1', '[data-deal-panel-open]');
  const close = ui.makeLink('https://example.test/deals/', '[data-deal-panel-close]');

  assert.equal(ui.click(open), true);
  await new Promise(setImmediate);
  assert.equal(ui.location.href, open.href);
  assert.equal(ui.classes.has('is-open'), true);
  assert.equal(ui.focused(), 1);

  ui.form.elements.stage.value = 'viewing';
  assert.equal(ui.click(close), true);
  assert.equal(ui.warning.hidden, false);
  assert.equal(ui.requests.length, 1);
  ui.listeners.get('click')({ target: Object.assign(new ui.FakeElement(), {
    closest(selector) { return selector === '[data-deal-discard-leave]' ? {} : null; },
  }), preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(ui.location.href, close.href);
  assert.equal(ui.classes.has('is-open'), false);
  assert.equal(ui.requests.length, 2);
});

test('stage form requires a refusal reason and reports a saved stage to the board', async () => {
  const ui = setup({
    open: true,
    fetchReply: async (url) => String(url).includes('/stage/')
      ? { ok: true, json: async () => ({ stage: 'lost', changed: true }) }
      : { ok: true, text: async () => 'OPEN' },
  });
  ui.form.elements.stage.value = 'lost';
  ui.listeners.get('change')({ target: { matches: (selector) => selector === '#deal-panel-stage' } });
  assert.equal(ui.button.hidden, false);
  assert.equal(ui.field.hidden, false);
  assert.equal(ui.form.outcome.required, true);

  await ui.listeners.get('submit')({ target: ui.form, preventDefault() {} });
  assert.equal(ui.requests.length, 0);
  ui.form.outcome.value = 'Клиент выбрал другой объект';
  await ui.listeners.get('submit')({ target: ui.form, preventDefault() {} });
  assert.equal(ui.requests.length, 2);
  assert.equal(ui.dispatched[0].type, 'deal-panel-stage-saved');
  assert.equal(ui.dispatched[0].detail.stage, 'lost');
  assert.equal(ui.error.hidden, true);
});

test('stage save appears only after the stage or refusal note changes', () => {
  const ui = setup({ open: true, initialStage: 'lost' });
  assert.equal(ui.button.hidden, true);
  ui.form.outcome.value = 'Клиент отказался';
  ui.listeners.get('input')({ target: { matches: (selector) => selector === '#deal-panel-outcome' } });
  assert.equal(ui.button.hidden, false);
  ui.form.outcome.value = '';
  ui.listeners.get('input')({ target: { matches: (selector) => selector === '#deal-panel-outcome' } });
  assert.equal(ui.button.hidden, true);
});

test('stage conflict stays in the open panel as a visible form error', async () => {
  const ui = setup({
    open: true,
    fetchReply: async () => ({ ok: false, json: async () => ({ error: 'Этап сделки уже изменился.' }) }),
  });
  ui.form.elements.stage.value = 'viewing';

  await ui.listeners.get('submit')({ target: ui.form, preventDefault() {} });

  assert.equal(ui.error.hidden, false);
  assert.equal(ui.error.textContent, 'Этап сделки уже изменился.');
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.dispatched.length, 0);
});
