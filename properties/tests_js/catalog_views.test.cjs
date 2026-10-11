const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('object and landing cards open on their body while their own actions stay independent', () => {
  const listeners = new Map();
  class FakeElement {}
  const document = {
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener(name, listener) { listeners.set(name, listener); },
  };
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/catalog-views.js'), 'utf8');
  vm.runInNewContext(code, {
    document, window: { addEventListener() {} }, Element: FakeElement,
    localStorage: { getItem() { return null; }, setItem() {} },
    ResizeObserver: class { observe() {} },
  });

  const opened = [];
  const makeCard = (destination) => ({
    querySelector(selector) {
      assert.equal(selector, '.property-card__title a[href]');
      return { click() { opened.push(destination); } };
    },
  });
  const property = makeCard('/properties/1/');
  const landing = makeCard('/properties/2/edit/#landing-pane');
  const click = (card, kind = 'body', overrides = {}) => {
    let prevented = false;
    const target = Object.assign(new FakeElement(), {
      closest(selector) {
        if (selector === '[data-catalog-card-open]') return card;
        if (selector.startsWith('a, button,')) return kind === 'body' ? null : {};
        return null;
      },
    });
    listeners.get('click')({
      target, button: 0, ...overrides,
      preventDefault() { prevented = true; },
    });
    return prevented;
  };

  assert.equal(click(property), true);
  assert.equal(click(landing), true);
  assert.deepEqual(opened, ['/properties/1/', '/properties/2/edit/#landing-pane']);
  for (const kind of ['name link', 'edit icon', 'public link', 'action menu']) {
    assert.equal(click(landing, kind), false);
  }
  assert.equal(click(property, 'body', { metaKey: true }), false);
  assert.equal(click(property, 'body', { button: 1 }), false);
  assert.equal(opened.length, 2);
});

test('catalog controls follow the last visible card and return to the list edge', () => {
  const listeners = new Map();
  let resizeObserver;
  class FakeElement {}
  const makeCatalog = (name, right, cardRights) => {
    const values = new Map();
    const catalog = {
      dataset: { catalogView: name, view: 'cards' },
      right,
      cardRights,
      buttons: [],
      style: {
        setProperty(key, value) { values.set(key, value); },
        removeProperty(key) { values.delete(key); },
        getPropertyValue(key) { return values.get(key) || ''; },
      },
      getBoundingClientRect() { return { right: this.right }; },
      querySelector() { return null; },
      querySelectorAll(selector) {
        if (selector === '[data-catalog-option]') return this.buttons;
        if (selector === '.catalog-card-grid__item > .property-card') {
          return this.cardRights.map((card) => ({ getBoundingClientRect: () => ({ right: card }) }));
        }
        return [];
      },
    };
    catalog.buttons = ['cards', 'compact', 'list'].map((view) => Object.assign(new FakeElement(), {
      dataset: { catalogOption: view },
      classList: { toggle() {} },
      setAttribute() {},
      closest(selector) {
        if (selector === '[data-catalog-option]') return this;
        return selector === '[data-catalog-view]' ? catalog : null;
      },
    }));
    return catalog;
  };

  const property = makeCatalog('properties', 1000, [400]);
  const landing = makeCatalog('landings', 1000, [400, 700]);
  const document = {
    querySelectorAll(selector) { return selector === '[data-catalog-view]' ? [property, landing] : []; },
    querySelector(selector) { return selector === '[data-catalog-view="properties"]' ? property : null; },
    addEventListener(name, listener) { listeners.set(name, listener); },
  };
  const code = fs.readFileSync(path.join(__dirname, '../../static/js/catalog-views.js'), 'utf8');
  vm.runInNewContext(code, {
    document, window: { addEventListener() {} }, Element: FakeElement,
    localStorage: { getItem() { return null; }, setItem() {} },
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; resizeObserver = this; }
      observe() {}
    },
  });

  const inset = (catalog) => catalog.style.getPropertyValue('--catalog-control-inset');
  assert.equal(inset(property), '600.0px');
  assert.equal(inset(landing), '300.0px');

  property.cardRights = [400, 700];
  listeners.get('click')({ target: property.buttons[1] });
  assert.equal(inset(property), '300.0px');

  property.right = 900;
  property.cardRights = [350, 580];
  resizeObserver.callback([{ target: property }]);
  assert.equal(inset(property), '320.0px');

  listeners.get('click')({ target: property.buttons[2] });
  assert.equal(inset(property), '');

  listeners.get('click')({ target: property.buttons[0] });
  property.cardRights = [];
  listeners.get('htmx:afterSwap')({ detail: { target: { closest: () => property } } });
  assert.equal(inset(property), '');
});
