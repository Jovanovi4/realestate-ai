(() => {
  const root = document.documentElement;
  if (!root.hasAttribute('data-color-scheme')) return;
  const colorSchemeMeta = document.querySelector('meta[name="color-scheme"]');
  const choices = new Set(['light', 'dark', 'system']);
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let saved = choices.has(root.dataset.colorScheme) ? root.dataset.colorScheme : 'light';
  let selected = saved;

  const apply = (value) => {
    selected = value;
    root.dataset.colorScheme = value;
    root.dataset.bsTheme = value === 'system' ? (system.matches ? 'dark' : 'light') : value;
    colorSchemeMeta?.setAttribute('content', root.dataset.bsTheme);
  };
  // This script runs before stylesheets so the system theme is resolved before paint.
  apply(saved);
  system.addEventListener('change', () => {
    if (selected === 'system') apply(selected);
  });

  document.addEventListener('DOMContentLoaded', () => {
    const form = document.querySelector('[data-color-scheme-form]');
    if (!form) return;
    const fieldset = form.querySelector('fieldset');
    const inputs = [...form.querySelectorAll('input[name="color_scheme"]')];
    const status = form.querySelector('[data-color-scheme-status]');
    const csrf = form.querySelector('[name="csrfmiddlewaretoken"]').value;
    let saving = false;
    const select = (value) => {
      inputs.forEach((input) => { input.checked = input.value === value; });
      apply(value);
    };
    const report = (text, error = false) => {
      status.textContent = text;
      status.classList.toggle('is-error', error);
    };
    const save = async () => {
      if (saving) return;
      const value = inputs.find((input) => input.checked)?.value;
      if (!choices.has(value)) return;
      select(value);
      if (value === saved) { report('Тема сохранена.'); return; }
      saving = true;
      fieldset.disabled = true;
      form.setAttribute('aria-busy', 'true');
      report('Сохраняем…');
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(form.action, {
          method: 'POST', credentials: 'same-origin', signal: controller.signal,
          headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf },
          body: new URLSearchParams({ color_scheme: value }),
        });
        if (!response.ok || response.redirected) throw new Error('Theme save failed');
        const result = await response.json();
        if (result.color_scheme !== value) throw new Error('Unexpected theme response');
        saved = value;
        report('Тема сохранена.');
      } catch (_) {
        select(saved);
        report('Не удалось сохранить тему. Проверьте соединение и повторите выбор.', true);
      } finally {
        window.clearTimeout(timeout);
        saving = false;
        fieldset.disabled = false;
        form.removeAttribute('aria-busy');
      }
    };
    form.addEventListener('change', (event) => {
      if (inputs.includes(event.target)) save();
    });
    form.addEventListener('submit', (event) => { event.preventDefault(); save(); });
    select(saved);
  });
})();
