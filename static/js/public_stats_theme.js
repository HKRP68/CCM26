(() => {
  'use strict';
  const root = document.documentElement;
  let theme = 'dark';
  try { const saved = localStorage.getItem('cmu-cricket-theme'); if (saved === 'light' || saved === 'dark') theme = saved; } catch (_) {}
  const apply = choice => {
    theme = choice; root.dataset.theme = choice; root.style.colorScheme = choice;
    const button = document.getElementById('theme-toggle');
    if (button) { button.textContent = choice === 'dark' ? 'Light mode' : 'Dark mode'; button.setAttribute('aria-label', `Switch to ${choice === 'dark' ? 'light' : 'dark'} mode`); }
    const color = document.querySelector('meta[name="theme-color"]'); if (color) color.content = choice === 'dark' ? '#080f1a' : '#f3f6f9';
  };
  apply(theme);
  const ready = () => {
    apply(theme);
    const button = document.getElementById('theme-toggle');
    if (button) button.addEventListener('click', () => { apply(theme === 'dark' ? 'light' : 'dark'); try { localStorage.setItem('cmu-cricket-theme', theme); } catch (_) {} });
    document.body.classList.remove('page-opening');
    const loader = document.getElementById('page-loader'); if (loader) { loader.setAttribute('aria-hidden', 'true'); loader.removeAttribute('role'); }
    document.querySelectorAll('.empty').forEach(node => { if (/^Loading/i.test(node.textContent)) node.classList.add('busy-state'); });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, {once:true}); else ready();
  window.addEventListener('pageshow', () => { if (document.body) document.body.classList.remove('page-opening'); });
})();
