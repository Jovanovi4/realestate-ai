(() => {
  const editor = document.querySelector('form.property-editor');
  const panel = document.querySelector('[data-landing-presets]');
  if (!editor || !panel) return;

  const colorText = editor.querySelector('[name="landing_custom_color"]');
  const colorPicker = editor.querySelector('[data-custom-color-picker]');
  const colorControl = editor.querySelector('[data-custom-color-control]');
  const customRadio = editor.querySelector('[name="landing_accent"][value="custom"]');
  const select = panel.querySelector('[data-preset-select]');
  const applyButton = panel.querySelector('[data-preset-apply]');
  const deleteButton = panel.querySelector('[data-preset-delete]');
  const saveButton = panel.querySelector('[data-preset-save]');
  const nameInput = panel.querySelector('[data-preset-name]');
  const message = panel.querySelector('[data-preset-message]');
  const dataNode = document.getElementById('landing-style-presets-data');
  let presets = dataNode ? JSON.parse(dataNode.textContent) : [];
  const fields = ['landing_template', 'landing_accent', 'landing_custom_color',
    'landing_button_style', 'landing_hero_layout', 'landing_gallery_style'];
  const validColor = (value) => /^#[0-9a-f]{6}$/i.test(value);
  const selectedRadio = (name) => editor.querySelector(`[name="${name}"]:checked`);
  const csrf = editor.querySelector('[name="csrfmiddlewaretoken"]')?.value;

  function updateColor() {
    const custom = Boolean(customRadio?.checked);
    colorControl.hidden = !custom;
    if (custom && !colorText.value) colorText.value = colorPicker.value.toUpperCase();
    if (validColor(colorText.value)) colorPicker.value = colorText.value;
  }
  editor.querySelectorAll('[name="landing_accent"]').forEach((radio) => radio.addEventListener('change', updateColor));
  colorPicker?.addEventListener('input', () => {
    colorText.value = colorPicker.value.toUpperCase();
    customRadio.checked = true;
    customRadio.dispatchEvent(new Event('change', { bubbles: true }));
  });
  colorText?.addEventListener('input', () => {
    if (validColor(colorText.value)) colorPicker.value = colorText.value;
  });
  updateColor();

  function notice(text, error = false) {
    message.textContent = text;
    message.classList.toggle('is-error', error);
  }

  function renderOptions(chosen = '') {
    select.replaceChildren(new Option('Выберите пресет', ''));
    presets.forEach((preset) => select.add(new Option(preset.name, String(preset.id))));
    select.value = chosen;
    applyButton.disabled = !select.value;
    if (deleteButton) deleteButton.disabled = !select.value;
  }

  select.addEventListener('change', () => renderOptions(select.value));
  applyButton.addEventListener('click', () => {
    const preset = presets.find((item) => String(item.id) === select.value);
    if (!preset) return;
    fields.forEach((field) => {
      if (field === 'landing_custom_color') {
        colorText.value = preset[field] || '';
        colorText.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        const input = [...editor.querySelectorAll(`[name="${field}"]`)].find((item) => item.value === preset[field]);
        if (input) { input.checked = true; input.dispatchEvent(new Event('change', { bubbles: true })); }
      }
    });
    updateColor();
    notice(`Оформление «${preset.name}» применено. Сохраните объект, чтобы закрепить изменения.`);
  });

  async function send(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json' },
      body: new URLSearchParams(body),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'Не удалось сохранить пресет.');
    return result;
  }

  saveButton?.addEventListener('click', async () => {
    const name = nameInput.value.trim();
    if (!name) { notice('Введите название пресета.', true); nameInput.focus(); return; }
    const body = { name, property_id: panel.dataset.propertyId || '' };
    for (const field of fields) body[field] = field === 'landing_custom_color' ? colorText.value : selectedRadio(field)?.value || '';
    saveButton.disabled = true;
    try {
      const result = await send(panel.dataset.createUrl, body);
      presets.push(result.preset);
      presets.sort((a, b) => a.name.localeCompare(b.name, 'ru'));
      renderOptions(String(result.preset.id));
      nameInput.value = '';
      notice('Стилевой пресет сохранён. Текущий объект сохраняется отдельно.');
    } catch (error) {
      notice(error.message, true);
    } finally {
      saveButton.disabled = false;
    }
  });

  deleteButton?.addEventListener('click', async () => {
    const preset = presets.find((item) => String(item.id) === select.value);
    if (!preset || !window.confirm(`Удалить пресет «${preset.name}»? Оформление объектов останется прежним.`)) return;
    deleteButton.disabled = true;
    try {
      await send(preset.delete_url, { property_id: panel.dataset.propertyId || '' });
      presets = presets.filter((item) => item.id !== preset.id);
      renderOptions();
      notice('Пресет удалён. Оформление объектов не изменилось.');
    } catch (error) {
      notice(error.message, true);
      deleteButton.disabled = false;
    }
  });
  renderOptions();
})();
