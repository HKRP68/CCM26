(() => {
  const storageKey = 'cmu-appearance';
  const hapticsKey = 'cmu_haptic_feedback';
  const modes = new Set(['dark', 'light', 'auto']);
  const system = window.matchMedia('(prefers-color-scheme: light)');
  const root = document.documentElement;
  let webApp;
  let saved = 'dark';
  let haptics = true;

  try {
    const stored = localStorage.getItem(storageKey) || localStorage.getItem('crickidex_theme');
    if (modes.has(stored)) saved = stored;
    haptics = localStorage.getItem(hapticsKey) !== 'off';
  } catch (_) {
    // WebView storage is optional.
  }

  let current = saved;
  function apply(preference) {
    current = preference;
    const scheme = preference === 'auto'
      ? webApp?.colorScheme || (system.matches ? 'light' : 'dark')
      : preference;
    root.dataset.theme = scheme;
    const background = getComputedStyle(root).getPropertyValue('--bg-main').trim()
      || (scheme === 'light' ? '#FAF8F4' : '#08090C');
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', background);
    for (const method of ['setHeaderColor', 'setBackgroundColor', 'setBottomBarColor']) {
      try { webApp?.[method]?.(background); } catch (_) { /* Optional native chrome. */ }
    }
  }

  function checkAuto() {
    if (current === 'auto') apply(current);
  }

  function safeArea() {
    ['top', 'right', 'bottom', 'left'].forEach(edge => {
      const inset = Math.max(0, webApp?.safeAreaInset?.[edge] || 0, webApp?.contentSafeAreaInset?.[edge] || 0);
      root.style.setProperty('--telegram-safe-' + edge, inset + 'px');
    });
  }

  function keyboard() {
    const viewport = window.visualViewport;
    const field = document.activeElement?.matches('input, textarea, [contenteditable="true"]');
    const sized = viewport && window.innerHeight - viewport.height > 120 && viewport.scale === 1;
    const native = webApp?.viewportStableHeight && webApp.viewportStableHeight - webApp.viewportHeight > 120;
    root.dataset.keyboard = field && (sized || native) ? 'open' : 'closed';
  }

  function persist(mode) {
    saved = mode;
    apply(mode);
    localStorage.setItem(storageKey, saved);
    localStorage.setItem(hapticsKey, haptics ? 'on' : 'off');
  }

  apply(saved);
  if (system.addEventListener) system.addEventListener('change', checkAuto);
  else system.addListener?.(checkAuto);

  window.MiniappTheme = {
    start(telegram) {
      webApp = telegram;
      apply(saved);
      safeArea();
      for (const [event, listener] of [
        ['themeChanged', checkAuto], ['safeAreaChanged', safeArea],
        ['contentSafeAreaChanged', safeArea], ['viewportChanged', keyboard],
      ]) {
        try { webApp?.onEvent?.(event, listener); } catch (_) { /* Optional native event. */ }
      }
      window.visualViewport?.addEventListener('resize', keyboard);
      document.addEventListener('focusin', keyboard);
      document.addEventListener('focusout', () => requestAnimationFrame(keyboard));
    },
    setMode(mode) {
      if (!modes.has(mode)) return;
      try { persist(mode); } catch (_) { /* The preference applies for this session. */ }
    },
    get haptics() { return haptics; },
  };

  window.CmuTheme = {
    restore() { apply(saved); },
    open() {
      const form = document.getElementById('cmu-theme-form');
      const status = document.getElementById('cmu-appearance-status');
      form.elements.appearance.value = saved;
      form.elements.haptics.checked = haptics;
      status.textContent = '';
      form.onchange = () => {
        apply(form.elements.appearance.value);
        status.textContent = 'Previewing appearance.';
      };
      form.onsubmit = event => {
        event.preventDefault();
        haptics = form.elements.haptics.checked;
        try {
          persist(form.elements.appearance.value);
          status.textContent = 'Appearance saved.';
        } catch (_) {
          status.textContent = 'Appearance saved for this session.';
        }
      };
    },
  };
})();
