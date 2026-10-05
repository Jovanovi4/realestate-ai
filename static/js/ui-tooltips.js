(() => {
  const mobile = window.matchMedia('(max-width: 44rem)');
  const selector = '[data-tooltip], [data-ui-help]';
  const popup = document.createElement('div');
  popup.className = 'ui-tooltip-popover';
  popup.setAttribute('role', 'tooltip');
  popup.hidden = true;
  document.body.appendChild(popup);

  let active = null;
  let pinned = false;

  const triggerFor = (target) => target instanceof Element ? target.closest(selector) : null;
  const helpContent = (trigger) => trigger.nextElementSibling;
  let helpIndex = 0;
  const describeHelp = (trigger) => {
    const content = helpContent(trigger);
    if (!content.id) content.id = `ui-help-${++helpIndex}`;
    trigger.setAttribute('aria-controls', content.id);
    trigger.setAttribute('aria-describedby', content.id);
  };
  document.querySelectorAll('[data-ui-help]').forEach(describeHelp);
  const closeInline = (except = null) => {
    document.querySelectorAll('[data-ui-help][aria-expanded="true"]').forEach((trigger) => {
      if (trigger === except) return;
      trigger.setAttribute('aria-expanded', 'false');
      helpContent(trigger).hidden = true;
    });
  };
  const closePopup = (force = false) => {
    if (pinned && !force) return;
    if (active?.matches('[data-ui-help]')) active.setAttribute('aria-expanded', 'false');
    popup.hidden = true;
    active = null;
    pinned = false;
  };
  const positionPopup = () => {
    if (!active || popup.hidden) return;
    if (!active.isConnected) { closePopup(true); return; }
    const anchor = active.getBoundingClientRect();
    const margin = 12;
    const gap = 8;
    const left = Math.min(
      Math.max(margin, anchor.left + anchor.width / 2 - popup.offsetWidth / 2),
      Math.max(margin, window.innerWidth - popup.offsetWidth - margin),
    );
    let top = anchor.bottom + gap;
    if (top + popup.offsetHeight > window.innerHeight - margin) top = anchor.top - popup.offsetHeight - gap;
    popup.style.left = `${left}px`;
    popup.style.top = `${Math.max(margin, top)}px`;
  };
  const showPopup = (trigger, lock = false) => {
    if (!trigger || mobile.matches) return;
    if (trigger.matches('[data-ui-help]')) describeHelp(trigger);
    const message = trigger.matches('[data-ui-help]')
      ? helpContent(trigger)?.textContent.trim()
      : trigger.dataset.tooltip;
    if (!message) return;
    if (active && active !== trigger) closePopup(true);
    active = trigger;
    pinned = lock;
    if (trigger.matches('[data-ui-help]')) trigger.setAttribute('aria-expanded', String(lock));
    popup.textContent = message;
    popup.hidden = false;
    positionPopup();
  };

  document.addEventListener('mouseover', (event) => {
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget)) showPopup(trigger, active === trigger && pinned);
  });
  document.addEventListener('mouseout', (event) => {
    if (active && active.contains(event.target) && !active.contains(event.relatedTarget)) closePopup();
  });
  document.addEventListener('focusin', (event) => {
    const trigger = triggerFor(event.target);
    if (trigger) showPopup(trigger, active === trigger && pinned);
  });
  document.addEventListener('focusout', (event) => {
    if (active && active.contains(event.target) && !active.contains(event.relatedTarget)) closePopup();
  });
  document.addEventListener('click', (event) => {
    if (popup.contains(event.target)) return;
    const trigger = triggerFor(event.target);
    if (trigger?.matches('[data-ui-help]')) {
      describeHelp(trigger);
      if (mobile.matches) {
        closePopup(true);
        const willOpen = trigger.getAttribute('aria-expanded') !== 'true';
        closeInline(trigger);
        trigger.setAttribute('aria-expanded', String(willOpen));
        helpContent(trigger).hidden = !willOpen;
      } else if (active === trigger && pinned) {
        closePopup(true);
      } else {
        showPopup(trigger, true);
      }
    } else {
      closeInline();
      closePopup(true);
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    closePopup(true);
    closeInline();
  });
  window.addEventListener('scroll', positionPopup, true);
  window.addEventListener('resize', positionPopup);
  mobile.addEventListener('change', () => { closePopup(true); closeInline(); });
})();
