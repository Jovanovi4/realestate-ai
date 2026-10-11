(() => {
  const workspace = document.getElementById('deal-workspace');
  const board = document.querySelector('[data-deal-board]');
  if (!workspace || !board) return;

  let lastOpenedId = null;
  let pendingNavigation = null;
  let requestNumber = 0;
  let historyIndex = Number.isInteger(history.state?.reaicrmDealIndex) ? history.state.reaicrmDealIndex : 0;
  let restoringCancelledHistory = false;
  let currentPanelUrl = location.href;
  const snapshots = new WeakMap();
  history.replaceState({ ...history.state, reaicrmDealIndex: historyIndex }, '', location.href);

  const slot = () => document.getElementById('deal-panel-slot');
  const panel = () => document.getElementById('deal-panel');
  const stageForm = () => panel()?.querySelector('[data-deal-panel-stage-form]');
  const formValues = (form) => new URLSearchParams(new FormData(form)).toString();
  const rememberForm = () => {
    const form = stageForm();
    if (form && !snapshots.has(form)) snapshots.set(form, formValues(form));
  };
  const syncStageSave = () => {
    const form = stageForm();
    const button = form?.querySelector('[data-panel-status-save]');
    if (button) button.hidden = formValues(form) === snapshots.get(form);
  };
  const hasUnsavedChanges = () => { const form = stageForm(); return !!form && formValues(form) !== snapshots.get(form); };
  const hideWarning = () => { const warning = document.getElementById('deal-panel-discard'); if (warning) warning.hidden = true; };
  const warnBeforeLeaving = (action) => {
    const warning = document.getElementById('deal-panel-discard');
    if (!warning) return;
    pendingNavigation = action;
    warning.hidden = false;
    warning.querySelector('[data-deal-discard-stay]')?.focus({ preventScroll: true });
  };
  const syncReason = () => {
    const form = stageForm();
    const field = form?.querySelector('[data-deal-panel-loss-field]');
    if (!field) return;
    const required = form.elements.stage.value === 'lost';
    field.hidden = !required;
    field.querySelector('textarea').required = required;
  };
  const syncPanel = ({ moveFocus = false } = {}) => {
    const openPanel = panel();
    const selectedId = slot()?.dataset.dealId || null;
    const roomy = workspace.getBoundingClientRect().width >= 1280;
    workspace.classList.toggle('deal-workspace--roomy', roomy);
    workspace.classList.toggle('deal-workspace--open', !!openPanel);
    const overlay = !!openPanel && !roomy;
    workspace.querySelector('.deal-workspace__list').inert = overlay;
    document.querySelectorAll('.app-sidebar, .mobile-header, .deal-board-page-header').forEach((item) => { item.inert = overlay; });
    document.body.classList.toggle('lead-panel-overlay-open', overlay);
    board.querySelectorAll('[data-deal-card]').forEach((card) => {
      const selected = card.dataset.dealId === selectedId;
      card.classList.toggle('is-open', selected);
      const link = card.querySelector('[data-deal-panel-open]');
      if (selected) link?.setAttribute('aria-current', 'true');
      else link?.removeAttribute('aria-current');
    });
    if (openPanel) {
      lastOpenedId = selectedId;
      openPanel.setAttribute('aria-modal', String(overlay));
      syncReason();
      rememberForm();
      syncStageSave();
      if (moveFocus) openPanel.focus({ preventScroll: true });
    } else if (moveFocus && lastOpenedId) {
      board.querySelector(`[data-deal-card][data-deal-id="${lastOpenedId}"] [data-deal-panel-open]`)?.focus({ preventScroll: true });
    }
  };

  const loadPanel = async (url, { push = false, moveFocus = true } = {}) => {
    const currentRequest = ++requestNumber;
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Deal-Panel': '1' } });
    if (!response.ok) throw new Error('Карточка сделки недоступна.');
    const html = await response.text();
    if (currentRequest !== requestNumber) return;
    slot().outerHTML = html;
    if (push) {
      historyIndex += 1;
      history.pushState({ ...history.state, reaicrmDealIndex: historyIndex }, '', url);
    }
    currentPanelUrl = location.href;
    syncPanel({ moveFocus });
  };
  const navigate = (url, { push = true, moveFocus = true } = {}) => {
    const action = () => loadPanel(url, { push, moveFocus }).catch(() => location.assign(url));
    if (hasUnsavedChanges()) warnBeforeLeaving(action);
    else action();
  };

  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    if (target.closest('[data-deal-discard-stay]')) {
      event.preventDefault();
      pendingNavigation = null;
      hideWarning();
      stageForm()?.querySelector('[name="stage"]')?.focus({ preventScroll: true });
      return;
    }
    if (target.closest('[data-deal-discard-leave]')) {
      event.preventDefault();
      const action = pendingNavigation;
      pendingNavigation = null;
      hideWarning();
      action?.();
      return;
    }
    const link = target.closest('a[href]');
    if (!link) return;
    if (link.matches('[data-deal-panel-open], [data-deal-panel-close]')) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
      event.preventDefault();
      navigate(link.href);
      return;
    }
    if (link.closest('#deal-panel') && hasUnsavedChanges()) {
      event.preventDefault();
      warnBeforeLeaving(() => location.assign(link.href));
    }
  });
  document.addEventListener('change', (event) => {
    if (event.target.matches('#deal-panel-stage')) { syncReason(); syncStageSave(); }
  });
  document.addEventListener('input', (event) => {
    if (event.target.matches('#deal-panel-outcome')) syncStageSave();
  });
  document.addEventListener('submit', async (event) => {
    const form = event.target;
    if (!form.matches('[data-deal-panel-stage-form]')) return;
    event.preventDefault();
    syncReason();
    if (!form.reportValidity()) return;
    const button = form.querySelector('[type="submit"]');
    const error = form.querySelector('[data-deal-panel-error]');
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: new FormData(form),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'Не удалось сохранить этап. Попробуйте снова.');
      if (result.changed) document.dispatchEvent(new CustomEvent('deal-panel-stage-saved', {
        detail: { id: form.dataset.dealId, stage: result.stage },
      }));
      await loadPanel(location.href, { moveFocus: true });
    } catch (failure) {
      if (!form.isConnected) return;
      error.textContent = failure.message;
      error.hidden = false;
      error.focus?.({ preventScroll: true });
    } finally {
      if (form.isConnected) button.disabled = false;
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !panel() || !document.getElementById('deal-panel-discard')?.hidden) return;
    event.preventDefault();
    const query = new URL(location.href);
    query.searchParams.delete('deal');
    navigate(query.href);
  });
  window.addEventListener('popstate', (event) => {
    const destinationIndex = event.state?.reaicrmDealIndex;
    if (restoringCancelledHistory) { restoringCancelledHistory = false; return; }
    if (hasUnsavedChanges() && !window.confirm('Есть несохранённые изменения. Выйти без сохранения?')) {
      restoringCancelledHistory = true;
      if (Number.isInteger(destinationIndex) && destinationIndex !== historyIndex) {
        history.go(historyIndex - destinationIndex);
      } else {
        restoringCancelledHistory = false;
        history.replaceState({ ...history.state, reaicrmDealIndex: historyIndex }, '', currentPanelUrl);
      }
      return;
    }
    if (Number.isInteger(destinationIndex)) historyIndex = destinationIndex;
    loadPanel(location.href, { moveFocus: false }).catch(() => location.reload());
  });
  window.addEventListener('beforeunload', (event) => {
    if (!hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('board-move-saved', () => {
    syncPanel();
    const selectedId = slot()?.dataset.dealId;
    const selectedCard = selectedId && [...board.querySelectorAll('[data-deal-card]')]
      .find((card) => card.dataset.dealId === selectedId);
    if (selectedCard && selectedCard.dataset.dealStage !== stageForm()?.elements.expected_stage.value
        && !hasUnsavedChanges()) {
      loadPanel(location.href, { moveFocus: false }).catch(() => location.reload());
    }
  });
  document.addEventListener('deal-board-refreshed', () => { currentPanelUrl = location.href; syncPanel(); });
  new ResizeObserver(() => syncPanel()).observe(workspace);
  syncPanel();
})();
