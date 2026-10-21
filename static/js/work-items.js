(() => {
  let selected = new Set();
  let scope = null;
  let filtersOpen = false;
  let lastOpened = null;
  let hadPanel = false;
  let restoreScroll = null;
  const workspace = () => document.getElementById('work-workspace');
  const checkboxes = () => [...(workspace()?.querySelectorAll('[data-work-select]') || [])];
  const visibleKeys = () => new Set(checkboxes().map((input) => input.value));

  const syncSelection = () => {
    const root = workspace();
    if (!root) return;
    const keys = visibleKeys();
    if (scope !== root.dataset.workScope) selected.clear();
    scope = root.dataset.workScope;
    selected = new Set([...selected].filter((key) => keys.has(key)));
    checkboxes().forEach((input) => { input.checked = selected.has(input.value); });
    root.querySelectorAll('[data-work-row]').forEach((row) => row.classList.toggle('is-selected', selected.has(row.dataset.workRow)));
    root.querySelectorAll('[data-work-select-all]').forEach((input) => {
      input.checked = keys.size > 0 && selected.size === keys.size;
      input.indeterminate = selected.size > 0 && selected.size < keys.size;
    });
    const bar = root.querySelector('[data-work-selection]');
    if (bar) bar.hidden = !selected.size;
    const count = root.querySelector('[data-work-selected-count]');
    if (count) count.textContent = `Выбрано: ${selected.size}`;
  };
  const syncFilters = () => {
    const panel = document.getElementById('work-filter-panel');
    if (panel) panel.hidden = !filtersOpen;
    document.querySelector('[data-work-filter-toggle]')?.setAttribute('aria-expanded', String(filtersOpen));
  };
  const syncPanel = (moveFocus = false) => {
    const panel = document.getElementById('work-panel');
    if (!panel) {
      if (hadPanel && lastOpened) {
        const links = [...(workspace()?.querySelectorAll('[data-work-panel-open]') || [])];
        const visible = links.find((link) => link.dataset.workPanelOpen === lastOpened && link.getClientRects().length);
        visible?.focus({ preventScroll: true });
      }
      hadPanel = false;
      return;
    }
    hadPanel = true;
    const current = workspace()?.querySelector('[data-work-panel-open][aria-current="true"]');
    if (current) lastOpened = current.dataset.workPanelOpen;
    if (moveFocus) {
      panel.focus({ preventScroll: true });
      if (workspace()?.getBoundingClientRect().width < 704) panel.scrollIntoView({ block: 'start' });
    }
  };
  const sync = (moveFocus = false) => { syncSelection(); syncFilters(); syncPanel(moveFocus); };

  document.addEventListener('change', (event) => {
    if (!event.target.closest('#work-workspace')) return;
    if (event.target.matches('[data-work-select-all]')) {
      selected = event.target.checked ? visibleKeys() : new Set();
    } else if (event.target.matches('[data-work-select]')) {
      if (event.target.checked) selected.add(event.target.value);
      else selected.delete(event.target.value);
    } else return;
    syncSelection();
  });
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-work-filter-toggle]')) {
      filtersOpen = !filtersOpen;
      syncFilters();
      if (filtersOpen) document.getElementById('work-kind')?.focus();
      return;
    }
    if (event.target.closest('[data-work-clear-selection]')) {
      selected.clear(); syncSelection(); return;
    }
    const row = event.target.closest('[data-work-row]');
    if (!row || event.target.closest('a, button, input, select, textarea, label, form') || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (window.getSelection?.()?.toString()) return;
    row.querySelector('[data-work-panel-open]')?.click();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (filtersOpen && event.target.closest('#work-filter-panel')) {
      event.preventDefault(); filtersOpen = false; syncFilters();
      document.querySelector('[data-work-filter-toggle]')?.focus();
    } else if (document.getElementById('work-panel')) {
      event.preventDefault();
      document.querySelector('[data-work-panel-close]')?.click();
    }
  });
  document.body.addEventListener('htmx:beforeRequest', (event) => {
    if (event.detail.target?.id !== 'work-workspace') return;
    const error = document.querySelector('[data-work-request-error]');
    if (error) error.hidden = true;
    if (event.detail.elt?.matches('[data-work-panel-open], [data-work-panel-close]')) {
      restoreScroll = { x: window.scrollX, y: window.scrollY };
    }
  });
  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target?.id !== 'work-workspace') return;
    if (restoreScroll) { window.scrollTo(restoreScroll.x, restoreScroll.y); restoreScroll = null; }
    sync(true);
  });
  document.body.addEventListener('htmx:afterRequest', (event) => {
    if (event.detail.target?.id !== 'work-workspace' || event.detail.successful) return;
    restoreScroll = null;
    const error = document.querySelector('[data-work-request-error]');
    if (error) error.hidden = false;
  });
  document.body.addEventListener('htmx:historyRestore', () => sync());
  sync();
})();
