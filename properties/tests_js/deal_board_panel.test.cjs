const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('saving a stage in the side panel moves the existing board card immediately', async () => {
  const listeners = new Map();
  const events = [];
  const makeBadge = (count) => ({
    textContent: String(count),
    setAttribute() {},
  });
  const makeItems = () => ({
    cards: [],
    querySelector(selector) {
      if (selector === '[data-deal-card]') return this.cards[0] || null;
      return null;
    },
    prepend(card) {
      if (card.column) card.column.items.cards = card.column.items.cards.filter((item) => item !== card);
      this.cards.unshift(card);
      card.column = this.column;
    },
    append() {},
  });
  const makeColumn = (stage, count) => {
    const items = makeItems();
    const column = {
      dataset: { dealStage: stage, activeStage: 'true' },
      items, badge: makeBadge(count),
      querySelector(selector) {
        if (selector === '.client-board__items') return items;
        if (selector === '.client-board__count') return this.badge;
        return null;
      },
    };
    items.column = column;
    return column;
  };
  const source = makeColumn('new', 1);
  const destination = makeColumn('viewing', 0);
  const card = {
    dataset: { dealId: '1', dealStage: 'new' }, column: source,
    closest(selector) { return selector === '.deal-board__column' ? this.column : null; },
  };
  source.items.cards.push(card);
  destination.items.querySelector = function (selector) {
    if (selector === '.client-board__empty') return null;
    if (selector === '[data-deal-card]') return this.cards[0] || null;
    return null;
  };
  const board = {
    addEventListener() {},
    classList: { contains() { return false; }, toggle() {} },
    querySelectorAll(selector) {
      if (selector === '.deal-board__column') return [source, destination];
      if (selector === '[data-deal-card]') return [card];
      return [];
    },
  };
  const element = () => ({ classList: { add() {}, remove() {} }, addEventListener() {} });
  const modalElement = {
    querySelector() { return element(); },
    addEventListener() {},
  };
  const activeCount = { dataset: { count: '1' }, textContent: 'Активных сделок: 1' };
  const status = { textContent: '' };
  const document = {
    querySelector(selector) {
      if (selector === '[data-deal-board]') return board;
      if (selector === '[data-deal-board-error]') return element();
      if (selector === '[data-deal-board-status]') return status;
      if (selector === '[data-deal-active-count]') return activeCount;
      if (selector === '[data-deal-board-csrf] [name=csrfmiddlewaretoken]') return { value: 'csrf' };
      return null;
    },
    getElementById(id) { return id === 'deal-loss-modal' ? modalElement : null; },
    addEventListener(name, listener) { listeners.set(name, listener); },
    dispatchEvent(event) { events.push(event.type); },
    createElement() { return { className: '', textContent: '' }; },
  };
  const window = {
    bootstrap: {}, location: { href: 'https://example.test/deals/?deal=1' },
    history: { state: { reaicrmDealIndex: 1 }, replaceState() {} },
    matchMedia: () => ({ matches: false }),
  };
  const bootstrap = { Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) } };
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/deal-board.js'), 'utf8');
  vm.runInNewContext(code, {
    document, window, bootstrap, URL, URLSearchParams,
    Event: class { constructor(type) { this.type = type; } },
    fetch: async () => ({ ok: false }),
  });

  listeners.get('deal-panel-stage-saved')({ detail: { id: '1', stage: 'viewing' } });
  await new Promise(setImmediate);

  assert.equal(card.dataset.dealStage, 'viewing');
  assert.equal(card.column, destination);
  assert.equal(source.badge.textContent, '0');
  assert.equal(destination.badge.textContent, '1');
  assert.equal(activeCount.textContent, 'Активных сделок: 1');
  assert.equal(status.textContent, 'Этап сделки сохранён.');
  assert.deepEqual(events, ['board-move-saved']);
});
