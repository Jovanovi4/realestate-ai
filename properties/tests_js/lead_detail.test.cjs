const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture({ compact = false, mode = '', bound = false } = {}) {
  let focused = null;
  let sent = 0;
  let destination = null;
  const element = (selectors = [], dataset = {}) => ({
    dataset, hidden: false, attributes: {}, listeners: new Map(), queries: {}, groups: {},
    addEventListener(name, callback) {
      this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
    },
    emit(name, props = {}) {
      const event = { target: this, prevented: false, preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }, ...props };
      for (const listener of this.listeners.get(name) || []) { listener(event); if (event.stopped) break; }
      return event;
    },
    querySelector(selector) { return this.queries[selector] || null; },
    querySelectorAll(selector) { return this.groups[selector] || []; },
    closest(selector) { return selectors.includes(selector) ? this : null; },
    matches() { return false; },
    focus() { focused = this; document.activeElement = this; },
    scrollIntoView() {}, getClientRects() { return this.hidden ? [] : [{}]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    removeAttribute(name) { delete this.attributes[name]; },
    hasAttribute(name) { return selectors.includes(`[${name}]`) || name in this.attributes; },
  });
  const document = element();
  const bodyClasses = new Set();
  document.body = { classList: { toggle(name, active) { if (active) bodyClasses.add(name); else bodyClasses.delete(name); } } };
  document.getElementById = () => null;
  const window = element();
  window.location = { href: 'https://example.test/leads/31/', pathname: '/leads/31/', search: '', assign(url) { destination = url; } };
  const media = Object.assign(element(), { matches: compact });
  window.matchMedia = () => media;
  const workspace = element([], { leadEditorState: mode });
  const panel = element(); panel.hidden = !mode;
  const shell = element();
  const heading = element();
  const contact = element();
  const contactSubmit = Object.assign(element(['[data-lead-contact-submit]']), { value: 'contacted' });
  const planSubmit = Object.assign(element(['[data-lead-plan-submit]']), { value: 'save' });
  const views = ['process', 'complete', 'decline'].map((view) => element([], { leadEditorView: view }));
  const triggers = ['contact', 'plan', 'complete', 'decline'].map((name) => element(['[data-lead-editor-open]'], { leadEditorOpen: name }));
  const back = element(['[data-lead-editor-back]']);
  const backdrop = element();
  const more = element(); more.hidden = true;
  const moreToggle = element(['[data-lead-more-toggle]']);
  const stay = element(['[data-lead-discard-stay]']);
  const leave = element(['[data-lead-discard-leave]']);
  const warningText = element();
  const warning = element(); warning.hidden = true;
  warning.queries = { '#lead-detail-discard-text': warningText, '[data-lead-discard-stay]': stay, '[data-lead-discard-leave]': leave };
  const forms = views.map(() => {
    const form = element();
    form.values = { next_action: 'Позвонить', contact_note: '', contact_method: '' };
    form.querySelector = (selector) => selector === '[name="contact_note"]' ? { value: form.values.contact_note }
      : selector === '[name="contact_method"]:checked' && form.values.contact_method ? {} : null;
    form.requestSubmit = (submitter) => {
      if (!document.emit('submit', { target: form, submitter }).prevented) sent += 1;
    };
    return form;
  });
  if (bound) forms[0].dataset.unsaved = 'true';
  document.queries = { '[data-lead-detail-actions]': workspace, '[data-lead-detail-backdrop]': backdrop };
  workspace.queries = { '#lead-detail-editor': panel, '[data-lead-editor-shell]': shell, '#lead-detail-more': more, '[data-lead-more-toggle]': moreToggle };
  views.forEach((view) => { workspace.queries[`[data-lead-editor-view="${view.dataset.leadEditorView}"]`] = view; });
  workspace.groups = { '[data-lead-editor-open]': triggers };
  panel.queries = { '#lead-detail-discard': warning, '.lead-panel__body': { scrollTop: 0 }, '#lead-panel-editor-title': heading };
  const focusSelector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  panel.groups = { form: forms, [focusSelector]: [back, planSubmit] };
  warning.groups = { [focusSelector]: [stay, leave] };
  shell.queries = { '#lead-panel-editor-title': heading, '[data-lead-contact-fields]': contact };
  shell.groups = { '[data-lead-editor-view]': views, '[data-lead-contact-submit], [data-lead-plan-submit]': [contactSubmit, planSubmit] };
  class FormData {
    constructor(form) { this.items = Object.entries(form.values); }
    [Symbol.iterator]() { return this.items[Symbol.iterator](); }
  }
  const code = ['lead-editor-controls.js', 'lead-detail.js'].map((file) => fs.readFileSync(path.join(__dirname, '../../static/js', file), 'utf8')).join('\n');
  vm.runInNewContext(code, { document, window, FormData, URL, URLSearchParams, WeakMap });
  window.emit('DOMContentLoaded');
  return {
    document, window, workspace, panel, views, forms, triggers, back, backdrop, heading, contact, contactSubmit, planSubmit, more, moreToggle, warning, warningText, leave, stay, bodyClasses, media,
    open(name) { workspace.emit('click', { target: triggers.find((trigger) => trigger.dataset.leadEditorOpen === name) }); },
    click(target) { workspace.emit('click', { target }); },
    key(key, shiftKey = false) { return document.emit('keydown', { key, shiftKey }); },
    link(url) { const link = Object.assign(element(['a[href]']), { href: url }); return document.emit('click', { target: link }); },
    get focused() { return focused; }, get sent() { return sent; }, get destination() { return destination; },
  };
}

test('full card switches one side editor and keeps its draft after Back', () => {
  const ui = fixture();
  ui.open('contact');
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.panel.getAttribute('role'), 'region');
  assert.equal(ui.backdrop.hidden, true);
  assert.deepEqual(ui.views.map((view) => view.hidden), [false, true, true]);
  assert.equal(ui.contact.hidden, false);
  assert.equal(ui.contactSubmit.disabled, false);
  ui.forms[0].values.contact_note = 'Подготовить подборку';
  ui.click(ui.back);
  assert.equal(ui.panel.hidden, true);
  assert.equal(ui.focused, ui.triggers[0]);
  ui.open('plan');
  assert.equal(ui.contact.hidden, true);
  assert.equal(ui.planSubmit.disabled, false);
  ui.open('contact');
  assert.equal(ui.forms[0].values.contact_note, 'Подготовить подборку');
  ui.open('complete');
  assert.deepEqual(ui.views.map((view) => view.hidden), [true, false, true]);
});

test('compact editor traps focus and Escape returns to the action without dropping its draft', () => {
  const ui = fixture({ compact: true });
  ui.open('plan');
  assert.equal(ui.panel.getAttribute('role'), 'dialog');
  assert.equal(ui.panel.getAttribute('aria-modal'), 'true');
  assert.equal(ui.backdrop.hidden, false);
  assert.equal(ui.bodyClasses.has('lead-next-panel-open'), true);
  ui.forms[0].values.next_action = 'Перезвонить завтра';
  ui.planSubmit.focus();
  assert.equal(ui.key('Tab').prevented, true);
  assert.equal(ui.focused, ui.back);
  ui.key('Tab', true);
  assert.equal(ui.focused, ui.planSubmit);
  ui.key('Escape');
  assert.equal(ui.panel.hidden, true);
  assert.equal(ui.backdrop.hidden, true);
  assert.equal(ui.bodyClasses.has('lead-next-panel-open'), false);
  assert.equal(ui.forms[0].values.next_action, 'Перезвонить завтра');
});

test('server-open editor stays on the right form and protects a bound draft', () => {
  const ui = fixture({ mode: 'contact', bound: true });
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.contact.hidden, false);
  assert.equal(ui.focused, ui.heading);
  assert.equal(ui.window.emit('beforeunload').prevented, true);
  ui.forms[0].requestSubmit(ui.contactSubmit);
  assert.equal(ui.sent, 1);
  assert.equal(ui.window.emit('beforeunload').prevented, false);
});

test('full card warns before another action replaces a draft and submits only after confirmation', () => {
  const ui = fixture();
  ui.forms[0].values.next_action = 'Черновик плана';
  ui.open('complete');
  const submitter = { ...ui.contactSubmit, value: '' };
  ui.forms[1].requestSubmit(submitter);
  assert.equal(ui.sent, 0);
  assert.equal(ui.warning.hidden, false);
  ui.click(ui.stay);
  assert.equal(ui.warning.hidden, true);
  ui.forms[1].requestSubmit(submitter);
  ui.click(ui.leave);
  assert.equal(ui.sent, 1);
});

test('saving only a plan warns about the unrecorded contact', () => {
  const ui = fixture();
  ui.forms[0].values.contact_note = 'Согласовали просмотр';
  ui.open('plan');
  ui.forms[0].requestSubmit(ui.planSubmit);
  assert.equal(ui.sent, 0);
  assert.match(ui.warningText.textContent, /Способ связи и итог разговора не будут записаны/);
  ui.key('Escape');
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.warning.hidden, true);
  assert.equal(ui.forms[0].values.contact_note, 'Согласовали просмотр');
});

test('leaving after Back reveals the draft warning and respects the user choice', () => {
  const ui = fixture();
  ui.open('contact');
  ui.forms[0].values.contact_note = 'Не потерять эту заметку';
  ui.click(ui.back);
  assert.equal(ui.link('https://example.test/clients/').prevented, true);
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.warning.hidden, false);
  assert.equal(ui.destination, null);
  ui.click(ui.leave);
  assert.equal(ui.destination, 'https://example.test/clients/');
  assert.equal(ui.window.emit('beforeunload').prevented, false);
});

test('decline remains in More and closes back to its trigger', () => {
  const ui = fixture();
  ui.click(ui.moreToggle);
  assert.equal(ui.more.hidden, false);
  ui.open('decline');
  assert.equal(ui.more.hidden, true);
  assert.deepEqual(ui.views.map((view) => view.hidden), [true, true, false]);
  ui.key('Escape');
  assert.equal(ui.more.hidden, false);
  assert.equal(ui.focused, ui.triggers[3]);
});
