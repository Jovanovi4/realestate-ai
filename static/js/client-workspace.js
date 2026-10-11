(() => {
  if (window.reaicrmClientPanelInitialized) return;
  window.reaicrmClientPanelInitialized = true;

  let lastOpenedClient = null;
  let hadPanel = false;
  let observedWorkspace = null;
  let restoreScroll = null;
  let listReturnScroll = null;
  let savedMessage = null;
  let pendingNavigation = null;
  let allowNavigation = false;
  let allowUnload = false;
  let refreshingAfterSave = false;
  let historyIndex = Number.isInteger(history.state?.reaicrmClientIndex) ? history.state.reaicrmClientIndex : 0;
  let restoringCancelledHistory = false;
  const formSnapshots = new WeakMap();
  const observer = new ResizeObserver(() => syncPanel());
  history.replaceState({ ...history.state, reaicrmClientIndex: historyIndex }, '', window.location.href);

  const formValues = (form) => new URLSearchParams(new FormData(form)).toString();
  const rememberForms = (clearUnsaved = false) => {
    document.querySelectorAll('#client-panel form').forEach((form) => {
      formSnapshots.set(form, formValues(form));
      if (clearUnsaved) delete form.dataset.unsaved;
    });
  };
  const hasUnsavedChanges = () => [...document.querySelectorAll('#client-panel form')].some(
    (form) => form.dataset.unsaved === 'true' || formValues(form) !== formSnapshots.get(form),
  );
  const syncStatusSave = () => {
    const form = document.querySelector?.('#client-panel [data-client-panel-status-form]');
    const select = form?.querySelector('select[name="status"]');
    const button = form?.querySelector('[data-panel-status-save]');
    if (select && button) button.hidden = form.dataset.unsaved !== 'true' && select.value === form.dataset.initialStatus;
  };
  const hideDiscardWarning = () => {
    const warning = document.getElementById('client-panel-discard');
    if (warning) warning.hidden = true;
  };
  const warnBeforeLeaving = (action, hardNavigation, trigger) => {
    const warning = document.getElementById('client-panel-discard');
    if (!warning) return;
    pendingNavigation = { action, hardNavigation, trigger };
    warning.hidden = false;
    warning.querySelector('[data-client-discard-stay]')?.focus({ preventScroll: true });
  };
  const resumeNavigation = () => {
    if (!pendingNavigation) return;
    const { action, hardNavigation } = pendingNavigation;
    pendingNavigation = null;
    hideDiscardWarning();
    allowNavigation = true;
    allowUnload = hardNavigation;
    action();
    allowNavigation = false;
    if (!hardNavigation) allowUnload = false;
  };
  const backgroundElements = (workspace) => [
    ...document.querySelectorAll('.app-sidebar, .mobile-header'),
    ...[...(workspace?.parentElement?.children || [])].filter((element) => element !== workspace && element.tagName !== 'SCRIPT'),
  ];
  const syncPanel = ({ moveFocus = false } = {}) => {
    const workspace = document.getElementById('client-workspace');
    if (workspace !== observedWorkspace) {
      if (observedWorkspace) observer.unobserve(observedWorkspace);
      if (workspace) observer.observe(workspace);
      observedWorkspace = workspace;
    }
    if (workspace) {
      workspace.classList.toggle('client-workspace--roomy', workspace.getBoundingClientRect().width >= 1120);
      const list = workspace.querySelector('.client-workspace__list');
      const table = workspace.querySelector('.client-workspace__list .table-responsive');
      if (workspace.classList.contains('client-workspace--roomy')
          && ((list && list.scrollWidth > list.clientWidth + 1)
            || (table && table.scrollWidth > table.clientWidth + 1)
            || document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)) {
        workspace.classList.remove('client-workspace--roomy');
      }
    }
    const panel = document.getElementById('client-panel');
    const list = document.querySelector('.client-workspace__list');
    const selected = document.querySelector('[data-client-panel-open][aria-current="true"]');
    if (!panel) {
      document.body.classList.remove('lead-panel-overlay-open');
      if (list) list.inert = false;
      backgroundElements(workspace).forEach((element) => { element.inert = false; });
      if (lastOpenedClient && hadPanel) {
        const candidates = [...document.querySelectorAll(`[data-client-panel-open="${lastOpenedClient}"]`)];
        (candidates.find((element) => element.getClientRects().length) || candidates[0])?.focus({ preventScroll: true });
      }
      lastOpenedClient = null;
      hadPanel = false;
      return;
    }
    const isOverlay = getComputedStyle(panel).position === 'fixed';
    if (list) list.inert = isOverlay;
    backgroundElements(workspace).forEach((element) => { element.inert = isOverlay; });
    panel.setAttribute('aria-modal', String(isOverlay));
    document.body.classList.toggle('lead-panel-overlay-open', isOverlay);
    hadPanel = true;
    if (selected?.dataset.clientPanelOpen) lastOpenedClient = selected.dataset.clientPanelOpen;
    if (moveFocus) panel.focus({ preventScroll: true });
  };
  const showSavedNotice = (details) => {
    const body = document.querySelector('#client-panel .lead-panel__body');
    const workspace = document.getElementById('client-workspace');
    if (!body || !details) return;
    body.querySelector('.lead-panel__notice')?.remove();
    const notice = document.createElement('div');
    notice.className = 'alert alert-success lead-panel__notice';
    notice.setAttribute('role', 'status');
    notice.textContent = details.message;
    if (workspace?.dataset.nextClientUrl) {
      const next = document.createElement('a');
      next.className = 'btn btn-neutral btn-sm';
      next.href = workspace.dataset.nextClientUrl;
      next.setAttribute('hx-get', workspace.dataset.nextClientUrl);
      next.setAttribute('hx-target', '#client-workspace');
      next.setAttribute('hx-swap', 'outerHTML');
      next.setAttribute('hx-push-url', 'true');
      next.dataset.clientPanelOpen = workspace.dataset.nextClientId;
      next.textContent = 'Следующий контакт';
      notice.append(next);
      htmx.process(next);
    }
    body.prepend(notice);
  };

  rememberForms();
  syncStatusSave();
  document.addEventListener('DOMContentLoaded', () => { rememberForms(); syncStatusSave(); syncPanel(); });
  document.addEventListener('change', (event) => {
    if (event.target?.matches?.('#client-panel [data-client-panel-status-form] select[name="status"]')) syncStatusSave();
  });
  document.addEventListener('click', (event) => {
    const stay = event.target.closest('[data-client-discard-stay]');
    if (stay) {
      event.preventDefault();
      const trigger = pendingNavigation?.trigger;
      pendingNavigation = null;
      hideDiscardWarning();
      trigger?.focus({ preventScroll: true });
      return;
    }
    if (event.target.closest('[data-client-discard-leave]')) {
      event.preventDefault();
      resumeNavigation();
      return;
    }
    const row = event.target.closest('[data-client-open-row]');
    if (row && !event.target.closest('a, button, input, select, textarea, label, summary, details, [role="button"]')
        && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !(event.button > 0)) {
      const opener = row.querySelector('[data-client-panel-open]');
      if (opener) {
        event.preventDefault();
        opener.click();
        return;
      }
    }
    const link = event.target.closest('a[href]');
    if (!link || allowNavigation || !link.closest('#client-workspace') || !hasUnsavedChanges()) return;
    const destination = new URL(link.href, window.location.href);
    if (!['http:', 'https:'].includes(destination.protocol)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const isSoft = link.hasAttribute('hx-get') && link.getAttribute('hx-target') === '#client-workspace';
    warnBeforeLeaving(() => isSoft ? link.click() : window.location.assign(destination.href), !isSoft, link);
  }, true);
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (form.closest('#client-panel')) { allowUnload = true; return; }
    if (allowNavigation || !form.closest('#client-workspace') || !hasUnsavedChanges()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    warnBeforeLeaving(() => form.requestSubmit(event.submitter || undefined), false, event.submitter || form);
  }, true);
  document.body.addEventListener('htmx:beforeRequest', (event) => {
    if (event.detail.target?.id !== 'client-workspace') return;
    const control = event.detail.elt;
    if (!allowNavigation && !refreshingAfterSave && hasUnsavedChanges()) {
      event.preventDefault();
      warnBeforeLeaving(() => control?.tagName === 'FORM' ? control.requestSubmit() : control?.click(), false, control);
      return;
    }
    if (control?.matches('[data-client-panel-open], [data-client-panel-close], .client-workspace__backdrop')) {
      const currentScroll = { x: window.scrollX, y: window.scrollY };
      if (control.dataset.clientPanelOpen) {
        listReturnScroll = currentScroll;
        lastOpenedClient = control.dataset.clientPanelOpen;
      }
      restoreScroll = control.dataset.clientPanelOpen ? currentScroll : (listReturnScroll || currentScroll);
    }
  });
  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target?.id === 'client-panel') {
      allowUnload = false;
      rememberForms();
      syncStatusSave();
      syncPanel();
      const panel = document.getElementById('client-panel');
      const error = panel?.querySelector('.errorlist, .client-panel__error');
      (error || panel)?.focus({ preventScroll: true });
    }
    if (event.detail.target?.id === 'client-workspace') {
      refreshingAfterSave = false;
      rememberForms();
      syncStatusSave();
      syncPanel({ moveFocus: !!document.getElementById('client-panel') });
      if (restoreScroll) {
        window.scrollTo(restoreScroll.x, restoreScroll.y);
        restoreScroll = null;
      }
      if (!document.getElementById('client-panel')) listReturnScroll = null;
      if (savedMessage) { showSavedNotice(savedMessage); savedMessage = null; }
    }
  });
  document.body.addEventListener('clientPanelSaved', (event) => {
    savedMessage = event.detail?.message ? event.detail : null;
    rememberForms(true);
    allowUnload = false;
    refreshingAfterSave = true;
    restoreScroll = { x: window.scrollX, y: window.scrollY };
    htmx.ajax('GET', window.location.pathname + window.location.search, { target: '#client-workspace', swap: 'outerHTML' });
  });
  document.body.addEventListener('htmx:afterRequest', (event) => {
    if (event.detail.elt?.closest?.('#client-panel')) allowUnload = false;
    if (event.detail.target?.id === 'client-workspace' && !event.detail.successful) refreshingAfterSave = false;
  });
  document.body.addEventListener('htmx:historyRestore', () => {
    rememberForms();
    syncStatusSave();
    syncPanel();
    if (!document.getElementById('client-panel') && listReturnScroll) {
      window.scrollTo(listReturnScroll.x, listReturnScroll.y);
      listReturnScroll = null;
    }
  });
  document.body.addEventListener('htmx:pushedIntoHistory', () => {
    historyIndex += 1;
    history.replaceState({ ...history.state, reaicrmClientIndex: historyIndex }, '', window.location.href);
  });
  window.addEventListener('popstate', (event) => {
    if (restoringCancelledHistory) {
      restoringCancelledHistory = false;
      event.stopImmediatePropagation();
      return;
    }
    const destinationIndex = event.state?.reaicrmClientIndex;
    if (!Number.isInteger(destinationIndex)) return;
    if (destinationIndex !== historyIndex && hasUnsavedChanges()
        && !window.confirm('Есть несохранённые изменения. Выйти без сохранения?')) {
      event.stopImmediatePropagation();
      restoringCancelledHistory = true;
      history.go(historyIndex - destinationIndex);
      return;
    }
    historyIndex = destinationIndex;
  }, true);
  window.addEventListener('resize', () => syncPanel());
  window.addEventListener('beforeunload', (event) => {
    if (allowUnload || !hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('keydown', (event) => {
    const panel = document.getElementById('client-panel');
    if (!panel) return;
    if (event.key === 'Escape') {
      const warning = document.getElementById('client-panel-discard');
      if (warning && !warning.hidden) {
        event.preventDefault();
        const trigger = pendingNavigation?.trigger;
        pendingNavigation = null;
        hideDiscardWarning();
        trigger?.focus({ preventScroll: true });
        return;
      }
      const close = panel.querySelector('[data-client-panel-close]');
      if (close) { event.preventDefault(); close.click(); }
      return;
    }
    if (event.key !== 'Tab' || panel.getAttribute('aria-modal') !== 'true') return;
    const focusable = [...panel.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary')]
      .filter((element) => element.getClientRects().length);
    if (!focusable.length) { event.preventDefault(); panel.focus(); return; }
    if (event.shiftKey && (document.activeElement === focusable[0] || document.activeElement === panel)) { event.preventDefault(); focusable.at(-1).focus(); }
    else if (!event.shiftKey && (document.activeElement === focusable.at(-1) || document.activeElement === panel)) { event.preventDefault(); focusable[0].focus(); }
  });
})();
