(() => {
  const views = new Set(['cards', 'compact', 'list']);

  const syncButtons = (catalog) => {
    const current = catalog.dataset.view;
    catalog.querySelectorAll('[data-catalog-option]').forEach((button) => {
      const selected = button.dataset.catalogOption === current;
      button.classList.toggle('is-active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  };
  const alignControls = (catalog) => {
    if (catalog.dataset.view === 'list') {
      catalog.style.removeProperty('--catalog-control-inset');
      return;
    }
    const cards = catalog.querySelectorAll('.catalog-card-grid__item > .property-card');
    if (!cards.length) {
      catalog.style.removeProperty('--catalog-control-inset');
      return;
    }
    let rightmostCard = -Infinity;
    cards.forEach((card) => { rightmostCard = Math.max(rightmostCard, card.getBoundingClientRect().right); });
    const inset = Math.max(0, catalog.getBoundingClientRect().right - rightmostCard);
    catalog.style.setProperty('--catalog-control-inset', `${inset.toFixed(1)}px`);
  };

  const catalogs = [...document.querySelectorAll('[data-catalog-view]')];
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver((entries) => {
      entries.forEach(({ target }) => alignControls(target));
    });
    catalogs.forEach((catalog) => observer.observe(catalog));
  } else {
    window.addEventListener('resize', () => catalogs.forEach(alignControls));
  }

  const propertyCatalog = document.querySelector('[data-catalog-view="properties"]');
  const filterForm = propertyCatalog?.querySelector('#property-filters');
  const filterTrigger = propertyCatalog?.querySelector('.property-filter-trigger');
  const filterCount = filterTrigger?.querySelector('.property-filter-trigger__count');
  const propertyCount = propertyCatalog?.querySelector('[data-property-count]');
  const filterNames = ['q', 'property_type', 'deal_type', 'status', 'min_price', 'max_price', 'sort'];
  const syncFilterCount = (count) => {
    if (!filterTrigger || !filterCount) return;
    filterCount.hidden = count === 0;
    filterCount.textContent = String(count);
    filterTrigger.setAttribute('aria-label', count ? `Поиск и фильтры, активных условий: ${count}` : 'Поиск и фильтры');
    filterTrigger.dataset.tooltip = count ? `Поиск и фильтры · Активных условий: ${count}` : 'Поиск и фильтры';
  };
  const countFormFilters = () => filterNames.reduce((count, name) => {
    const value = filterForm?.elements.namedItem(name)?.value.trim();
    return count + Number(Boolean(value && (name !== 'sort' || value !== 'newest')));
  }, 0);

  catalogs.forEach((catalog) => {
    try {
      const saved = localStorage.getItem(`reaicrm.catalog.view.${catalog.dataset.catalogView}`);
      if (views.has(saved)) catalog.dataset.view = saved;
    } catch (_) {
      // The view switch still works for this page if browser storage is disabled.
    }
    syncButtons(catalog);
    alignControls(catalog);
  });

  if (filterForm && filterTrigger) {
    syncFilterCount(propertyCatalog.querySelectorAll('#property-results .property-filter-chip').length);
    filterForm.addEventListener('input', () => syncFilterCount(countFormFilters()));
    filterForm.addEventListener('change', () => syncFilterCount(countFormFilters()));
  }

  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const card = target?.closest('[data-catalog-card-open]');
    if (card && !target.closest('a, button, input, select, textarea, label, summary, details, form, .property-card__actions, [role="button"]')
        && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !(event.button > 0)) {
      const opener = card.querySelector('.property-card__title a[href]');
      if (opener) {
        event.preventDefault();
        opener.click();
        return;
      }
    }
    const button = target?.closest('[data-catalog-option]');
    const catalog = button?.closest('[data-catalog-view]');
    if (!catalog || !views.has(button.dataset.catalogOption)) return;
    catalog.dataset.view = button.dataset.catalogOption;
    syncButtons(catalog);
    alignControls(catalog);
    try {
      localStorage.setItem(`reaicrm.catalog.view.${catalog.dataset.catalogView}`, catalog.dataset.view);
    } catch (_) {
      // Keep the selected view until the page is reloaded.
    }
  });

  document.addEventListener('htmx:afterSwap', (event) => {
    const catalog = event.detail.target.closest('[data-catalog-view]');
    if (catalog) {
      syncButtons(catalog);
      alignControls(catalog);
    }
    if (catalog === propertyCatalog) {
      syncFilterCount(catalog.querySelectorAll('#property-results .property-filter-chip').length);
      const resultCount = catalog.querySelector('#property-results [data-result-count]');
      if (propertyCount && resultCount) propertyCount.textContent = resultCount.dataset.resultCount;
    }
  });
})();
