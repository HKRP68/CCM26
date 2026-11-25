(() => {
  const storageKey = 'cmu-appearance';
  const options = new Set(['dark', 'light', 'auto']);
  const webApp = window.Telegram?.WebApp;
  const system = window.matchMedia('(prefers-color-scheme: light)');
  let saved = 'dark';

  try {
    const stored = localStorage.getItem(storageKey);
    if (options.has(stored)) saved = stored;
  } catch (_) {
    // The current session still supports appearance controls.
  }

  let current = saved;
  function apply(preference) {
    current = preference;
    const scheme = preference === 'auto'
      ? webApp?.colorScheme || (system.matches ? 'light' : 'dark')
      : preference;
    document.documentElement.dataset.theme = scheme;
    try {
      const background = scheme === 'light' ? '#FAF8F4' : '#08090C';
      webApp?.setHeaderColor?.(background);
      webApp?.setBackgroundColor?.(background);
      webApp?.setBottomBarColor?.(background);
    } catch (_) {
      // Older Telegram versions cannot recolor every part of the native frame.
    }
  }

  function checkAuto() {
    if (current === 'auto') apply(current);
  }

  apply(saved);
  webApp?.onEvent?.('themeChanged', checkAuto);
  system.addEventListener?.('change', checkAuto);

  window.CmuTheme = {
    restore() { apply(saved); },
    open() {
      const form = document.getElementById('cmu-theme-form');
      form.elements.appearance.value = saved;
      document.getElementById('cmu-appearance-status').textContent = '';
      form.onchange = () => {
        apply(form.elements.appearance.value);
        document.getElementById('cmu-appearance-status').textContent = 'Previewing appearance.';
      };
      form.onsubmit = event => {
        event.preventDefault();
        saved = form.elements.appearance.value;
        apply(saved);
        try {
          localStorage.setItem(storageKey, saved);
          document.getElementById('cmu-appearance-status').textContent = 'Appearance saved.';
        } catch (_) {
          document.getElementById('cmu-appearance-status').textContent = 'Appearance saved for this session.';
        }
      };
    },
  };
})();
