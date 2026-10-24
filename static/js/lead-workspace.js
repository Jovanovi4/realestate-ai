(() => {
  if (window.reaicrmLeadPanelInitialized) return;
  window.reaicrmLeadPanelInitialized = true;
  let lastOpenedLead = null;
  let hadPanel = false;
  let restoreScroll = null;
  let listReturnScroll = null;
  let savedMessage = null;
  let pendingNavigation = null;
  let allowNavigation = false;
  let allowUnload = false;
  let refreshingAfterSave = false;
  let filtersOpen = document.getElementById('lead-filter-panel')?.classList.contains('show') || false;
  let historyIndex = Number.isInteger(history.state?.reaicrmLeadIndex) ? history.state.reaicrmLeadIndex : 0;
  let restoringCancelledHistory = false;
  const formSnapshots = new WeakMap();
  history.replaceState({ ...history.state, reaicrmLeadIndex: historyIndex }, '', window.location.href);
  const formValues = (form) => new URLSearchParams(new FormData(form)).toString();
  const syncFilterPanel = () => {
    const panel = document.getElementById('lead-filter-panel');
    const toggle = document.querySelector('[data-lead-filter-toggle]');
    if (!panel || !toggle) return;
    panel.classList.toggle('show', filtersOpen);
    toggle.setAttribute('aria-expanded', String(filtersOpen));
  };
  const rememberForms = (clearUnsaved = false) => {
    document.querySelectorAll('#lead-panel form').forEach((form) => {
      formSnapshots.set(form, formValues(form));
      if (clearUnsaved) delete form.dataset.unsaved;
    });
  };
  const formHasChanges = (form) => form.dataset.unsaved === 'true' || formValues(form) !== formSnapshots.get(form);
  const hasUnsavedChanges = () => [...document.querySelectorAll('#lead-panel form')].some(formHasChanges);
  const editorReturn = new WeakMap();
  const toggleMoreActions = (panel, open) => {
    const actions = panel.querySelector('#lead-panel-more');
    if (actions) actions.hidden = !open;
    panel.querySelector('[data-lead-more-toggle]')?.setAttribute('aria-expanded', String(open));
  };
  const setEditor = (mode, trigger) => {
    const panel = document.getElementById('lead-panel');
    const shell = panel?.querySelector('[data-lead-editor-shell]');
    if (!shell) return;
    const body = panel.querySelector('.lead-panel__body');
    if (mode && !panel.dataset.leadEditorState) editorReturn.set(panel, { trigger, scroll: body.scrollTop });
    if (!window.reaicrmLeadEditor.setMode(panel, mode)) return;
    panel.querySelector('[data-lead-overview]').hidden = Boolean(mode);
    const heading = shell.querySelector('#lead-panel-editor-title');
    if (mode) {
      toggleMoreActions(panel, false);
      panel.querySelector('.lead-panel__notice')?.remove();
      body.scrollTop = 0;
      heading.focus({ preventScroll: true });
    } else {
      const previous = editorReturn.get(panel);
      body.scrollTop = previous?.scroll || 0;
      const opener = previous?.trigger;
      if (opener?.dataset.leadEditorOpen === 'decline') toggleMoreActions(panel, true);
      (opener || panel.querySelector('[data-lead-editor-open]') || panel).focus({ preventScroll: true });
    }
  };
  const hideDiscardWarning = () => {
    const warning = document.getElementById('lead-panel-discard');
    if (warning) warning.hidden = true;
  };
  const warnBeforeLeaving = (action, hardNavigation, trigger, submitWarning = '') => {
    const warning = document.getElementById('lead-panel-discard');
    if (!warning) return;
    pendingNavigation = { action, hardNavigation, trigger };
    warning.querySelector('#lead-panel-discard-text').textContent = submitWarning || 'Сохраните запись или выйдите без изменений.';
    warning.querySelector('[data-lead-discard-leave]').textContent = submitWarning ? 'Продолжить отправку' : 'Выйти без сохранения';
    warning.hidden = false;
    warning.querySelector('[data-lead-discard-stay]')?.focus({ preventScroll: true });
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
  const updateSidebarCounters = (workspace) => {
    const counts = [
      { element: document.querySelector('[aria-controls="sidebar-crm-links"]'), count: Number(workspace.dataset.newLeadsCount) || 0, label: workspace.dataset.newLeadsLabel, base: 'CRM', ariaHidden: false },
      { element: document.querySelector('#sidebar-crm-links a[href="' + workspace.dataset.leadsUrl + '"]'), count: Number(workspace.dataset.newLeadsCount) || 0, label: workspace.dataset.newLeadsLabel, base: 'Заявки', ariaHidden: true },
      { element: document.querySelector('.sidebar-main-nav a[href="' + workspace.dataset.tasksUrl + '"]'), count: Number(workspace.dataset.overdueCount) || 0, label: workspace.dataset.overdueLabel, base: 'Задачи', ariaHidden: false, suffix: 'просроченных дел' },
    ];
    counts.forEach(({ element, count, label, base, ariaHidden, suffix = 'заявок требуют ответа' }) => {
      if (!element) return;
      let badge = element.querySelector('.sidebar-counter');
      if (!count) {
        badge?.remove();
        if (base === 'Заявки') element.removeAttribute('aria-label');
        else element.setAttribute('aria-label', base);
        return;
      }
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'sidebar-counter';
        element.insertBefore(badge, element.querySelector('.sidebar-group-chevron'));
      }
      badge.textContent = label;
      badge.title = `${count} ${suffix}`;
      if (ariaHidden) badge.setAttribute('aria-hidden', 'true');
      else badge.setAttribute('aria-label', `${count} ${suffix}`);
      element.setAttribute('aria-label', `${base}, ${count} ${suffix}`);
    });
  };
  const showSavedNotice = (body, workspace, details) => {
    if (!body || !details) return;
    body.querySelector('.lead-panel__notice')?.remove();
    const notice = document.createElement('div');
    notice.className = `alert alert-${details.level === 'info' ? 'info' : 'success'} lead-panel__notice`;
    notice.setAttribute('role', 'status');
    notice.textContent = details.message;
    if (workspace?.dataset.nextLeadUrl) {
      const next = document.createElement('a');
      next.className = 'btn btn-neutral btn-sm';
      next.href = workspace.dataset.nextLeadUrl;
      next.setAttribute('hx-get', workspace.dataset.nextLeadUrl);
      next.setAttribute('hx-target', '#lead-workspace');
      next.setAttribute('hx-swap', 'outerHTML');
      next.setAttribute('hx-push-url', 'true');
      next.dataset.leadPanelOpen = workspace.dataset.nextLeadId;
      next.textContent = 'Следующая заявка';
      notice.append(next);
    }
    body.prepend(notice);
    const next = notice.querySelector('[data-lead-panel-open]');
    if (next) htmx.process(next);
  };
  rememberForms();
  const syncPanel = ({ moveFocus = false } = {}) => {
    const workspace = document.getElementById('lead-workspace');
    const panel = document.getElementById('lead-panel');
    const selected = document.querySelector('[data-lead-panel-open][aria-current="true"]');
    const selectedId = selected?.dataset.leadPanelOpen || null;

    if (!panel) {
      if (lastOpenedLead && hadPanel) {
        document.querySelector(`[data-lead-panel-open="${lastOpenedLead}"]`)?.focus({ preventScroll: true });
      }
      hadPanel = false;
      lastOpenedLead = null;
      return;
    }

    hadPanel = true;
    if (selectedId) lastOpenedLead = selectedId;
    if (moveFocus) {
      panel.focus({ preventScroll: true });
      if (workspace?.getBoundingClientRect().width < 704) panel.scrollIntoView?.({ block: 'start' });
    }
  };

  document.addEventListener('DOMContentLoaded', () => {
    rememberForms();
    syncFilterPanel();
    syncPanel();
  });
  document.addEventListener('show.bs.collapse', (event) => {
    if (event.target.id === 'lead-filter-panel') filtersOpen = true;
  });
  document.addEventListener('hide.bs.collapse', (event) => {
    if (event.target.id === 'lead-filter-panel') filtersOpen = false;
  });
  document.addEventListener('hidden.bs.modal', (event) => {
    if (event.target.id === 'action-confirmation-modal') allowUnload = false;
  });
  document.addEventListener('click', (event) => {
    const stay = event.target.closest('[data-lead-discard-stay]');
    if (stay) {
      event.preventDefault();
      const trigger = pendingNavigation?.trigger;
      pendingNavigation = null;
      hideDiscardWarning();
      trigger?.focus({ preventScroll: true });
      return;
    }
    if (event.target.closest('[data-lead-discard-leave]')) {
      event.preventDefault();
      resumeNavigation();
      return;
    }
    const editorOpener = event.target.closest('[data-lead-editor-open]');
    if (editorOpener) {
      event.preventDefault();
      setEditor(editorOpener.dataset.leadEditorOpen, editorOpener);
      return;
    }
    if (event.target.closest('[data-lead-editor-back]')) {
      event.preventDefault();
      setEditor('');
      return;
    }
    const moreToggle = event.target.closest('[data-lead-more-toggle]');
    if (moreToggle) {
      toggleMoreActions(moreToggle.closest('#lead-panel'), moreToggle.getAttribute('aria-expanded') !== 'true');
      return;
    }
    const row = event.target.closest('[data-lead-open-row], [data-owner-open-row]');
    if (row && !event.target.closest('a, button, input, select, textarea, label, summary, details, [role="button"]')
        && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !(event.button > 0)) {
      const opener = row.querySelector('[data-lead-panel-open], [data-owner-detail-link]');
      if (opener) {
        event.preventDefault();
        opener.click();
        return;
      }
    }
    const link = event.target.closest('a[href]');
    if (!link || allowNavigation || !link.closest('#lead-workspace') || !hasUnsavedChanges()) return;
    const destination = new URL(link.href, window.location.href);
    if (!['http:', 'https:'].includes(destination.protocol)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const isSoft = link.hasAttribute('hx-get') && link.getAttribute('hx-target') === '#lead-workspace';
    warnBeforeLeaving(() => isSoft ? link.click() : window.location.assign(destination.href), !isSoft, link);
  }, true);
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (form.closest('#lead-panel')) {
      const otherDraft = [...document.querySelectorAll('#lead-panel form')].some((other) => other !== form && formHasChanges(other));
      const unsavedContact = event.submitter?.value === 'save' && (
        form.querySelector('[name="contact_method"]:checked') || form.querySelector('[name="contact_note"]')?.value.trim()
      );
      if (!allowNavigation && (otherDraft || unsavedContact)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        warnBeforeLeaving(() => form.requestSubmit(event.submitter || undefined), false, event.submitter || form,
          otherDraft ? 'После отправки этой формы несохранённые изменения в другом действии будут потеряны.' : 'Сохранится только план. Способ связи и итог разговора не будут записаны. Чтобы записать контакт, вернитесь к первому контакту.');
        return;
      }
      allowUnload = true;
      return;
    }
    if (allowNavigation || !form.closest('#lead-workspace') || !hasUnsavedChanges()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const submitter = event.submitter;
    warnBeforeLeaving(() => form.requestSubmit(submitter || undefined), false, submitter || form);
  }, true);
  document.body.addEventListener('htmx:beforeRequest', (event) => {
    if (event.detail.target?.id !== 'lead-workspace') return;
    const control = event.detail.elt;
    if (!allowNavigation && !refreshingAfterSave && hasUnsavedChanges()) {
      event.preventDefault();
      warnBeforeLeaving(() => control?.tagName === 'FORM' ? control.requestSubmit() : control?.click(), false, control);
      return;
    }
    if (control?.matches('[data-lead-panel-open], [data-lead-panel-close]')) {
      const currentScroll = { x: window.scrollX, y: window.scrollY };
      if (control.dataset.leadPanelOpen) {
        listReturnScroll = currentScroll;
        lastOpenedLead = control.dataset.leadPanelOpen;
      }
      restoreScroll = control.dataset.leadPanelOpen ? currentScroll : (listReturnScroll || currentScroll);
    }
  });
  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target?.id === 'lead-panel') {
      allowUnload = false;
      rememberForms();
      syncPanel();
      const panel = document.getElementById('lead-panel');
      const error = panel?.querySelector('.errorlist, .alert-danger');
      const field = panel?.querySelector('[aria-invalid="true"]') || error?.closest('div')?.querySelector('input:not([type="hidden"]), select, textarea');
      if (!field) error?.setAttribute('tabindex', '-1');
      (field || error || panel)?.focus({ preventScroll: true });
    }
    if (event.detail.target?.id === 'lead-workspace') {
      refreshingAfterSave = false;
      rememberForms();
      updateSidebarCounters(document.getElementById('lead-workspace'));
      if (restoreScroll) {
        window.scrollTo(restoreScroll.x, restoreScroll.y);
        restoreScroll = null;
      }
      syncFilterPanel();
      syncPanel({ moveFocus: !!document.getElementById('lead-panel') });
      if (!document.getElementById('lead-panel')) listReturnScroll = null;
      if (savedMessage) {
        showSavedNotice(document.querySelector('#lead-panel .lead-panel__body'), document.getElementById('lead-workspace'), savedMessage);
        savedMessage = null;
      }
    }
  });
  document.body.addEventListener('leadPanelSaved', (event) => {
    savedMessage = event.detail?.message ? event.detail : null;
    rememberForms(true);
    allowUnload = false;
    refreshingAfterSave = true;
    showSavedNotice(document.querySelector('#lead-panel .lead-panel__body'), document.getElementById('lead-workspace'), savedMessage);
    restoreScroll = { x: window.scrollX, y: window.scrollY };
    const overviewUrl = new URL(window.location.href);
    overviewUrl.searchParams.delete('contact');
    overviewUrl.searchParams.delete('complete');
    history.replaceState(history.state, '', overviewUrl.pathname + overviewUrl.search);
    htmx.ajax('GET', overviewUrl.pathname + overviewUrl.search, { target: '#lead-workspace', swap: 'outerHTML' });
  });
  document.body.addEventListener('htmx:afterRequest', (event) => {
    if (event.detail.elt?.closest?.('#lead-panel')) allowUnload = false;
    if (event.detail.target?.id === 'lead-workspace' && !event.detail.successful) refreshingAfterSave = false;
  });
  document.body.addEventListener('htmx:historyRestore', () => {
    rememberForms();
    syncFilterPanel();
    syncPanel();
    if (!document.getElementById('lead-panel') && listReturnScroll) {
      window.scrollTo(listReturnScroll.x, listReturnScroll.y);
      listReturnScroll = null;
    }
  });
  document.body.addEventListener('htmx:pushedIntoHistory', () => {
    historyIndex += 1;
    history.replaceState({ ...history.state, reaicrmLeadIndex: historyIndex }, '', window.location.href);
  });
  window.addEventListener('popstate', (event) => {
    if (restoringCancelledHistory) {
      restoringCancelledHistory = false;
      event.stopImmediatePropagation();
      return;
    }
    const destinationIndex = event.state?.reaicrmLeadIndex;
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
  window.addEventListener('beforeunload', (event) => {
    if (allowUnload || !hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('keydown', (event) => {
    const panel = document.getElementById('lead-panel');
    if (!panel) return;
    if (event.key === 'Escape') {
      const warning = document.getElementById('lead-panel-discard');
      if (warning && !warning.hidden) {
        event.preventDefault();
        const trigger = pendingNavigation?.trigger;
        pendingNavigation = null;
        hideDiscardWarning();
        trigger?.focus({ preventScroll: true });
        return;
      }
      if (panel.dataset.leadEditorState) {
        event.preventDefault();
        setEditor('');
        return;
      }
      if (panel.querySelector('[data-lead-more-toggle][aria-expanded="true"]')) {
        event.preventDefault();
        toggleMoreActions(panel, false);
        panel.querySelector('[data-lead-more-toggle]').focus({ preventScroll: true });
        return;
      }
      const close = panel.querySelector('[data-lead-panel-close]');
      if (close) { event.preventDefault(); close.click(); }
      return;
    }
  });
})();
