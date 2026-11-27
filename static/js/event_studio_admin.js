(() => {
  const form = document.getElementById('studio-draft');
  if (!form) return;
  const source = form.elements.config;
  const fields = [...form.querySelectorAll('[data-config-path]')];
  const errors = new Map();
  form.querySelector('[data-studio-source]').open = false;
  form.querySelector('[data-studio-fields]').hidden = false;

  function path(field) {
    return field.dataset.configPath.split('.');
  }

  function showError(field, message) {
    field.setCustomValidity(message);
    let note = errors.get(field);
    if (!note && message) {
      note = document.createElement('span');
      note.id = `studio-field-error-${errors.size + 1}`;
      note.className = 'studio-field-error';
      note.setAttribute('role', 'status');
      field.setAttribute('aria-describedby', note.id);
      errors.set(field, note);
      field.after(note);
    }
    if (note) {
      note.textContent = message;
      note.hidden = !message;
    }
    if (message) field.setAttribute('aria-invalid', 'true');
    else field.removeAttribute('aria-invalid');
  }

  function refresh(config, except = null) {
    form.querySelector('[data-kind-boss]').hidden = config.kind !== 'boss';
    form.querySelector('[data-kind-moment]').hidden =
      !(config.kind === 'moment' || config.kind === 'boss' && config.challenge_style === 'moment');
    for (const field of fields) {
      if (field === except || field.getAttribute('aria-invalid') === 'true') continue;
      const value = path(field).reduce((object, key) => object?.[key], config);
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else field.value = field.dataset.valueType === 'json' ? JSON.stringify(value ?? null, null, 2) : value ?? '';
    }
  }

  function readSource() {
    const config = JSON.parse(source.value);
    if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('Invalid configuration');
    return config;
  }

  for (const field of fields) {
    field.addEventListener('input', () => {
      let config;
      let value = field.value;
      try {
        config = readSource();
      } catch (_error) {
        showError(source, 'Correct the JSON in Full configuration before changing a section.');
        source.closest('details').open = true;
        return;
      }
      try {
        if (field.dataset.valueType === 'json') value = JSON.parse(value);
        else if (field.dataset.valueType === 'nullable') value = value || null;
        if (field.type === 'number') value = field.valueAsNumber;
        if (field.type === 'checkbox') value = field.checked;
        const keys = path(field);
        const target = keys.slice(0, -1).reduce((object, key) => object[key] ??= {}, config);
        target[keys.at(-1)] = value;
      } catch (_error) {
        showError(field, 'Enter valid JSON before saving this draft.');
        return;
      }
      showError(field, '');
      source.value = JSON.stringify(config, null, 2);
      refresh(config, field);
    });
  }
  source.addEventListener('input', () => {
    try {
      refresh(readSource());
      showError(source, '');
    } catch (_error) {
      showError(source, 'Enter a valid configuration object before saving.');
    }
  });
  form.addEventListener('invalid', event => {
    const details = event.target.closest('details');
    if (details) details.open = true;
  }, true);
  refresh(readSource());
})();
