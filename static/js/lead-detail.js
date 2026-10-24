(() => {
  window.addEventListener('DOMContentLoaded', () => {
    const workspace = document.querySelector('[data-lead-detail-actions]');
    if (!workspace) return;
    const panel = workspace.querySelector('#lead-detail-editor');
    const backdrop = document.querySelector('[data-lead-detail-backdrop]');
    const triggers = [...workspace.querySelectorAll('[data-lead-editor-open]')];
    const forms = [...panel.querySelectorAll('form')];
    const snapshots = new WeakMap();
    const compact = window.matchMedia('(max-width: 1199.98px)');
    const warning = panel.querySelector('#lead-detail-discard');
    const more = workspace.querySelector('#lead-detail-more');
    const moreToggle = workspace.querySelector('[data-lead-more-toggle]');
    let activeTrigger = null;
    let lastMode = workspace.dataset.leadEditorState;
    let pendingNavigation = null;
    let allowNavigation = false;
    let allowUnload = false;
    const formValues = (form) => new URLSearchParams(new FormData(form)).toString();
    forms.forEach((form) => snapshots.set(form, formValues(form)));
    const isDirty = (form) => form.dataset.unsaved === 'true' || formValues(form) !== snapshots.get(form);
    const toggleMore = (open) => {
      more.hidden = !open;
      moreToggle.setAttribute('aria-expanded', String(open));
    };
    const syncLayout = () => {
      const modal = !panel.hidden && compact.matches;
      document.body.classList.toggle('lead-next-panel-open', modal);
      panel.setAttribute('role', modal ? 'dialog' : 'region');
      if (modal) panel.setAttribute('aria-modal', 'true');
      else panel.removeAttribute('aria-modal');
      backdrop.hidden = !modal;
    };
    const openEditor = (mode, trigger = triggers.find((item) => item.dataset.leadEditorOpen === mode)) => {
      if (!window.reaicrmLeadEditor.setMode(workspace, mode)) return;
      const overview = document.getElementById('lead-overview-tab');
      if (overview && window.bootstrap?.Tab) window.bootstrap.Tab.getOrCreateInstance(overview).show();
      lastMode = mode;
      activeTrigger = trigger;
      panel.hidden = false;
      triggers.forEach((item) => item.setAttribute('aria-expanded', String(item === trigger)));
      toggleMore(false);
      syncLayout();
      panel.querySelector('.lead-panel__body').scrollTop = 0;
      panel.querySelector('#lead-panel-editor-title').focus({ preventScroll: true });
      if (!compact.matches) panel.scrollIntoView({ block: 'nearest' });
    };
    const closeEditor = () => {
      if (!warning.hidden) return;
      window.reaicrmLeadEditor.setMode(workspace, '');
      panel.hidden = true;
      triggers.forEach((item) => item.setAttribute('aria-expanded', 'false'));
      syncLayout();
      if (activeTrigger?.dataset.leadEditorOpen === 'decline') toggleMore(true);
      activeTrigger?.focus({ preventScroll: true });
    };
    const warnBeforeLeaving = (action, trigger, submitWarning = '') => {
      if (panel.hidden) openEditor(lastMode || 'plan');
      pendingNavigation = { action, trigger };
      warning.querySelector('#lead-detail-discard-text').textContent = submitWarning || 'Сохраните запись или выйдите без изменений.';
      warning.querySelector('[data-lead-discard-leave]').textContent = submitWarning ? 'Продолжить отправку' : 'Выйти без сохранения';
      warning.hidden = false;
      warning.querySelector('[data-lead-discard-stay]').focus({ preventScroll: true });
    };
    const stay = () => {
      const trigger = pendingNavigation?.trigger;
      pendingNavigation = null;
      warning.hidden = true;
      (trigger?.getClientRects().length ? trigger : panel.querySelector('#lead-panel-editor-title')).focus({ preventScroll: true });
    };
    workspace.addEventListener('click', (event) => {
      if (event.target.closest('[data-lead-discard-stay]')) { stay(); return; }
      if (event.target.closest('[data-lead-discard-leave]')) {
        const action = pendingNavigation?.action;
        pendingNavigation = null;
        warning.hidden = true;
        if (action) {
          allowNavigation = true;
          action();
          allowNavigation = false;
        }
        return;
      }
      if (!warning.hidden) return;
      const trigger = event.target.closest('[data-lead-editor-open]');
      if (trigger) { openEditor(trigger.dataset.leadEditorOpen, trigger); return; }
      if (event.target.closest('[data-lead-editor-back]')) { closeEditor(); return; }
      if (event.target.closest('[data-lead-more-toggle]')) toggleMore(more.hidden);
    });
    backdrop.addEventListener('click', closeEditor);
    compact.addEventListener('change', syncLayout);
    document.addEventListener('submit', (event) => {
      const form = event.target;
      // Other page forms retain their own confirmations; beforeunload protects drafts.
      if (!forms.includes(form)) { allowUnload = false; return; }
      const otherDraft = forms.some((other) => other !== form && isDirty(other));
      const unsavedContact = event.submitter?.value === 'save' && (
        form.querySelector('[name="contact_method"]:checked') || form.querySelector('[name="contact_note"]')?.value.trim()
      );
      if (!allowNavigation && (otherDraft || unsavedContact)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        warnBeforeLeaving(() => form.requestSubmit(event.submitter || undefined), event.submitter,
          otherDraft ? 'После отправки этой формы несохранённые изменения в другом действии будут потеряны.' : 'Сохранится только план. Способ связи и итог разговора не будут записаны. Чтобы записать контакт, вернитесь к первому контакту.');
        return;
      }
      allowUnload = true;
    }, true);
    document.addEventListener('click', (event) => {
      const link = event.target.closest('a[href]');
      if (!link || allowNavigation || !forms.some(isDirty) || link.hasAttribute('download') || link.target === '_blank'
          || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
      const destination = new URL(link.href, window.location.href);
      if (!['http:', 'https:'].includes(destination.protocol)) return;
      if (destination.pathname === window.location.pathname && destination.search === window.location.search && destination.hash) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      warnBeforeLeaving(() => { allowUnload = true; window.location.assign(destination.href); }, link);
    }, true);
    document.addEventListener('keydown', (event) => {
      if (panel.hidden) {
        if (event.key === 'Escape' && !more.hidden) { event.preventDefault(); toggleMore(false); moreToggle.focus(); }
        return;
      }
      if (event.key === 'Escape') { event.preventDefault(); if (!warning.hidden) stay(); else closeEditor(); return; }
      if (event.key !== 'Tab' || !compact.matches) return;
      const focusRoot = warning.hidden ? panel : warning;
      const focusable = [...focusRoot.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter((element) => element.getClientRects().length);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); panel.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || !focusable.includes(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !focusable.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
    });
    window.addEventListener('beforeunload', (event) => {
      if (allowUnload || !forms.some(isDirty)) return;
      event.preventDefault();
      event.returnValue = '';
    });
    window.addEventListener('pageshow', () => { allowUnload = false; });
    document.addEventListener('hidden.bs.modal', (event) => {
      if (event.target.id === 'action-confirmation-modal') allowUnload = false;
    });
    document.getElementById('lead-tabs')?.addEventListener('show.bs.tab', (event) => {
      if (event.target.dataset.bsTarget !== '#lead-overview') closeEditor();
    });
    if (lastMode) {
      openEditor(lastMode);
      const error = panel.querySelector('[aria-invalid="true"], .errorlist, .alert-danger');
      if (error) {
        if (!error.matches('input, select, textarea')) error.setAttribute('tabindex', '-1');
        error.focus();
      }
    } else syncLayout();
  });
})();
