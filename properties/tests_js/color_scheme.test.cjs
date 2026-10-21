const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const code = fs.readFileSync(path.join(__dirname, '../../static/js/color-scheme.js'), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));

function fixture(preference = 'light', systemDark = false, hasForm = true) {
  const root = { dataset: { colorScheme: preference }, hasAttribute: () => preference !== null };
  const meta = { setAttribute(name, value) { this[name] = value; } };
  const documentListeners = new Map();
  const formListeners = new Map();
  const system = { matches: systemDark, addEventListener(name, fn) { this.changed = fn; } };
  const fieldset = { disabled: false };
  const inputs = ['light', 'dark', 'system'].map((value) => ({ value, checked: value === preference }));
  const status = { textContent: '', classList: { toggle(name, value) { status.error = value; } } };
  const attributes = new Map();
  const form = {
    action: '/profile/appearance/',
    querySelector(selector) {
      if (selector === 'fieldset') return fieldset;
      if (selector === '[data-color-scheme-status]') return status;
      if (selector === '[name="csrfmiddlewaretoken"]') return { value: 'test-csrf' };
    },
    querySelectorAll() { return inputs; },
    addEventListener(name, fn) { formListeners.set(name, fn); },
    setAttribute(name, value) { attributes.set(name, value); },
    removeAttribute(name) { attributes.delete(name); },
  };
  const requests = [];
  const timers = new Set();
  vm.runInNewContext(code, {
    document: {
      documentElement: root,
      addEventListener(name, fn) { documentListeners.set(name, fn); },
      querySelector(selector) { return selector === 'meta[name="color-scheme"]' ? meta : (hasForm ? form : null); },
    },
    window: {
      matchMedia: () => system,
      setTimeout(fn) { timers.add(fn); return fn; },
      clearTimeout(fn) { timers.delete(fn); },
    },
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        requests.push({ url, options, resolve, reject });
        options.signal.addEventListener('abort', () => reject(new Error('Timeout')));
      });
    },
    AbortController, URLSearchParams,
  });
  return {
    root, meta, form, inputs, status, fieldset, requests, timers, attributes,
    ready() { documentListeners.get('DOMContentLoaded')?.(); },
    choose(value) {
      inputs.forEach((input) => { input.checked = input.value === value; });
      formListeners.get('change')({ target: inputs.find((input) => input.checked) });
    },
    systemChange(dark) { system.matches = dark; system.changed(); },
    respond(value, overrides = {}) {
      requests.at(-1).resolve({ ok: true, redirected: false, json: async () => ({ color_scheme: value }), ...overrides });
    },
  };
}

test('system preference resolves before DOMContentLoaded and follows the device without saving', () => {
  const page = fixture('system', true);
  assert.equal(page.root.dataset.bsTheme, 'dark');
  assert.equal(page.meta.content, 'dark');
  page.systemChange(false);
  assert.equal(page.root.dataset.bsTheme, 'light');
  assert.equal(page.meta.content, 'light');
  assert.equal(page.root.dataset.colorScheme, 'system');
  assert.equal(page.requests.length, 0);
});

test('explicit light remains light on a dark device; anonymous pages are untouched', () => {
  const page = fixture('light', true);
  page.systemChange(true);
  assert.equal(page.root.dataset.bsTheme, 'light');
  assert.equal(fixture(null, true).root.dataset.bsTheme, undefined);
});

test('explicit dark ignores device changes; ordinary workspace pages still follow system', () => {
  const darkPage = fixture('dark', false, false);
  darkPage.ready();
  darkPage.systemChange(false);
  assert.equal(darkPage.root.dataset.bsTheme, 'dark');
  const systemPage = fixture('system', false, false);
  systemPage.ready();
  systemPage.systemChange(true);
  assert.equal(systemPage.root.dataset.bsTheme, 'dark');
  assert.equal(systemPage.meta.content, 'dark');
  assert.equal(systemPage.requests.length, 0);
});

test('invalid saved preference falls back to light even on a dark device', () => {
  const page = fixture('unknown', true);
  assert.equal(page.root.dataset.colorScheme, 'light');
  assert.equal(page.root.dataset.bsTheme, 'light');
});

test('choice is immediate and saved independently with CSRF; controls block overlapping writes', async () => {
  const page = fixture();
  page.ready();
  page.choose('dark');
  assert.equal(page.root.dataset.bsTheme, 'dark');
  assert.equal(page.fieldset.disabled, true);
  assert.equal(page.attributes.get('aria-busy'), 'true');
  assert.equal(page.requests.length, 1);
  const { url, options } = page.requests[0];
  assert.equal(url, '/profile/appearance/');
  assert.equal(options.method, 'POST');
  assert.equal(options.headers['X-CSRFToken'], 'test-csrf');
  assert.equal(options.body.toString(), 'color_scheme=dark');
  page.respond('dark');
  await flush();
  assert.equal(page.status.textContent, 'Тема сохранена.');
  assert.equal(page.fieldset.disabled, false);
  assert.equal(page.timers.size, 0);
  assert.equal(page.attributes.has('aria-busy'), false);
  page.choose('dark');
  assert.equal(page.requests.length, 1);
});

test('failed save restores the last confirmed preference and allows a retry', async () => {
  const page = fixture();
  page.ready();
  page.choose('dark');
  page.respond('dark');
  await flush();
  page.choose('light');
  page.requests.at(-1).reject(new Error('Offline'));
  await flush();
  assert.equal(page.root.dataset.bsTheme, 'dark');
  assert.equal(page.inputs.find((input) => input.checked).value, 'dark');
  assert.equal(page.status.error, true);
  assert.match(page.status.textContent, /повторите выбор/);
  assert.equal(page.fieldset.disabled, false);
  page.choose('system');
  page.respond('system');
  await flush();
  assert.equal(page.status.error, false);
  page.systemChange(true);
  assert.equal(page.root.dataset.bsTheme, 'dark');
});

test('HTTP errors, expired sessions and unexpected responses cannot report success', async () => {
  for (const response of [{ ok: false }, { redirected: true }, { json: async () => ({ color_scheme: 'unknown' }) }]) {
    const page = fixture();
    page.ready();
    page.choose('dark');
    page.respond('dark', response);
    await flush();
    assert.equal(page.root.dataset.bsTheme, 'light');
    assert.equal(page.status.error, true);
    assert.equal(page.fieldset.disabled, false);
  }
});

test('timeout unlocks choices and a failed system choice restores explicit preference', async () => {
  const page = fixture('light', true);
  page.ready();
  page.choose('system');
  assert.equal(page.root.dataset.bsTheme, 'dark');
  for (const expire of page.timers) expire();
  await flush();
  assert.equal(page.root.dataset.bsTheme, 'light');
  assert.equal(page.fieldset.disabled, false);
  page.systemChange(true);
  assert.equal(page.root.dataset.bsTheme, 'light');
});

test('rollback to system uses the current device theme after a change during saving', async () => {
  const page = fixture('system', false);
  page.ready();
  page.choose('light');
  page.systemChange(true);
  assert.equal(page.root.dataset.bsTheme, 'light');
  page.requests.at(-1).reject(new Error('Offline'));
  await flush();
  assert.equal(page.root.dataset.colorScheme, 'system');
  assert.equal(page.root.dataset.bsTheme, 'dark');
  assert.equal(page.meta.content, 'dark');
  page.systemChange(false);
  assert.equal(page.root.dataset.bsTheme, 'light');
});
