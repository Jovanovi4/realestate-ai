(() => {
  if (window.reaicrmLeadEditor) return;
  const titles = { contact: 'Записать первый контакт', plan: 'Изменить план', complete: 'Записать итог', decline: 'Закрыть без сделки' };
  const setMode = (root, mode) => {
    const shell = root.querySelector('[data-lead-editor-shell]');
    if (!shell) return false;
    const view = mode === 'contact' || mode === 'plan' ? 'process' : mode;
    if (mode && (!titles[mode] || !root.querySelector(`[data-lead-editor-view="${view}"]`))) return false;
    root.dataset.leadEditorState = mode;
    shell.hidden = !mode;
    shell.querySelectorAll('[data-lead-editor-view]').forEach((section) => { section.hidden = section.dataset.leadEditorView !== view; });
    const contactFields = shell.querySelector('[data-lead-contact-fields]');
    if (contactFields) contactFields.hidden = mode !== 'contact';
    shell.querySelectorAll('[data-lead-contact-submit], [data-lead-plan-submit]').forEach((button) => {
      const visible = button.hasAttribute('data-lead-contact-submit') ? mode === 'contact' : mode === 'plan';
      button.hidden = !visible;
      button.disabled = !visible;
    });
    shell.querySelector('#lead-panel-editor-title').textContent = titles[mode] || '';
    return true;
  };
  const chooseDate = (button) => {
    const group = button.closest('[data-lead-date-field]');
    const input = group?.querySelector('input[type="datetime-local"]');
    if (!input) return;
    input.setCustomValidity('');
    if (button.dataset.leadDate === 'custom') {
      input.focus();
      try { input.showPicker?.(); } catch { /* The editable field remains available. */ }
      return;
    }
    // Match Django's timezone even when the user's device uses another one.
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: group.dataset.timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date()).map(({ type, value }) => [type, value]));
    const today = `${parts.year}-${parts.month}-${parts.day}`;
    const offset = Number(button.dataset.leadDate);
    const day = new Date(`${today}T12:00:00Z`);
    day.setUTCDate(day.getUTCDate() + offset);
    const currentMinute = Number(parts.hour) * 60 + Number(parts.minute);
    const [hour, minute] = (input.value.split('T')[1] || '10:00').split(':').map(Number);
    let chosenMinute = hour * 60 + minute;
    if (!offset && chosenMinute <= currentMinute) chosenMinute = Math.min(currentMinute + 60, 1439);
    if (!offset && chosenMinute <= currentMinute) {
      input.setCustomValidity('Сегодня уже нет доступного времени. Выберите завтра или другую дату.');
      input.reportValidity();
      return;
    }
    input.value = `${day.toISOString().slice(0, 10)}T${String(Math.floor(chosenMinute / 60)).padStart(2, '0')}:${String(chosenMinute % 60).padStart(2, '0')}`;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  window.reaicrmLeadEditor = { setMode };
  document.addEventListener('click', (event) => {
    const shortcut = event.target.closest('[data-lead-date]');
    if (shortcut) chooseDate(shortcut);
  });
  document.addEventListener('input', (event) => {
    if (event.target.matches('[data-lead-date-field] input')) event.target.setCustomValidity('');
  });
})();
