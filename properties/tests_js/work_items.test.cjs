const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(require('node:path').join(__dirname, '../../static/js/work-items.js'), 'utf8');

function setup() {
  const documentListeners = new Map(), bodyListeners = new Map();
  const element = (attrs = {}) => ({ ...attrs, classes: new Set(),
    classList: { toggle(name, enabled) { if (enabled) this.owner.classes.add(name); else this.owner.classes.delete(name); } },
    focus() { this.focused = true; }, setAttribute(name, value) { this[name] = value; },
  });
  const make = (attrs) => { const el = element(attrs); el.classList.owner = el; return el; };
  const keys = ['task:1', 'lead:1', 'task:1', 'lead:1']; // desktop and mobile copies, IDs overlap across types
  const inputs = keys.map(value => make({ value, checked: false, matches: s => s === '[data-work-select]', closest: () => root }));
  const rows = keys.map(value => make({ dataset: { workRow: value } }));
  const all = [0, 1].map(() => make({ checked: false, matches: s => s === '[data-work-select-all]', closest: () => root }));
  const bar = make({ hidden: true }), count = make({ textContent: '' });
  const filter = make({ hidden: true }), toggle = make(), kind = make();
  let panel = null;
  const open = make({ dataset: { workPanelOpen: 'task:1' }, getClientRects: () => [1], click() { this.clicked = true; } });
  const close = make({ click() { this.clicked = true; } });
  const root = make({ dataset: { workScope: 'period=all&page=1' },
    querySelectorAll(s) { return ({ '[data-work-select]': inputs, '[data-work-row]': rows, '[data-work-select-all]': all, '[data-work-panel-open]': [open] })[s] || []; },
    querySelector(s) { return ({ '[data-work-selection]': bar, '[data-work-selected-count]': count, '[data-work-panel-open][aria-current="true"]': open })[s]; },
    getBoundingClientRect: () => ({ width: 900 }),
  });
  const document = {
    getElementById(id) { return ({ 'work-workspace': root, 'work-filter-panel': filter, 'work-kind': kind, 'work-panel': panel })[id] || null; },
    querySelector(s) { return ({ '[data-work-filter-toggle]': toggle, '[data-work-panel-close]': close })[s] || null; },
    addEventListener: (name, handler) => documentListeners.set(name, handler),
    body: { addEventListener: (name, handler) => bodyListeners.set(name, handler) },
  };
  vm.runInNewContext(code, { document, window: { getSelection: () => ({ toString: () => '' }), scrollTo() {} }, Set });
  return { inputs, rows, all, bar, count, root, open, close, filter, toggle, kind,
    event: (name, e) => documentListeners.get(name)(e),
    swap: () => bodyListeners.get('htmx:afterSwap')({ detail: { target: { id: 'work-workspace' } } }),
    setPanel() { panel = make({ scrollIntoView() { this.scrolled = true; } }); return panel; },
    closePanel() { panel = null; },
  };
}

test('checkboxes synchronize desktop and mobile copies without mixing task and lead IDs', () => {
  const ui = setup();
  ui.inputs[0].checked = true;
  ui.event('change', { target: ui.inputs[0] });
  assert.deepEqual(ui.inputs.map(el => el.checked), [true, false, true, false]);
  assert.equal(ui.count.textContent, 'Выбрано: 1');
  assert.equal(ui.bar.hidden, false);
  assert.equal(ui.all[0].indeterminate, true);
  assert.equal(ui.rows[0].classes.has('is-selected'), true);
});

test('select all counts distinct rows; selection survives panel refresh and resets on filter or page change', () => {
  const ui = setup();
  ui.all[0].checked = true;
  ui.event('change', { target: ui.all[0] });
  assert.equal(ui.count.textContent, 'Выбрано: 2');
  assert.equal(ui.all[1].checked, true);
  assert.equal(ui.all[1].indeterminate, false);
  ui.swap();
  assert.equal(ui.inputs.every(el => el.checked), true);
  ui.root.dataset.workScope = 'period=all&page=2';
  ui.swap();
  assert.equal(ui.inputs.some(el => el.checked), false);
  assert.equal(ui.bar.hidden, true);
});

test('filter panel stays expanded across refresh and Escape returns focus to toggle', () => {
  const ui = setup();
  ui.event('click', { target: { closest: s => s === '[data-work-filter-toggle]' ? ui.toggle : null } });
  assert.equal(ui.filter.hidden, false);
  assert.equal(ui.kind.focused, true);
  ui.swap();
  assert.equal(ui.toggle['aria-expanded'], 'true');
  ui.event('keydown', { key: 'Escape', target: { closest: () => ui.filter }, preventDefault() {} });
  assert.equal(ui.filter.hidden, true);
  assert.equal(ui.toggle.focused, true);
});

test('row opens preview while checkbox, action button and modified clicks keep their own behavior', () => {
  const ui = setup();
  const row = { querySelector: () => ui.open };
  const target = { closest: s => s === '[data-work-row]' ? row : null };
  ui.event('click', { target });
  assert.equal(ui.open.clicked, true);
  ui.open.clicked = false;
  ui.event('click', { target, ctrlKey: true });
  assert.equal(ui.open.clicked, false);
  ui.event('click', { target: { closest: s => s === '[data-work-row]' ? row : s.startsWith('a, button') ? {} : null } });
  assert.equal(ui.open.clicked, false);
});

test('panel receives focus, Escape closes it, and closing returns focus to its row', () => {
  const ui = setup();
  const panel = ui.setPanel();
  ui.swap();
  assert.equal(panel.focused, true);
  ui.event('keydown', { key: 'Escape', target: { closest: () => null }, preventDefault() {} });
  assert.equal(ui.close.clicked, true);
  ui.closePanel(); ui.swap();
  assert.equal(ui.open.focused, true);
});
