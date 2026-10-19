(() => {
  const grid = document.getElementById('record-board-grid');
  const buttons = [...document.querySelectorAll('[data-record-view]')];
  if (!grid) return;
  const select = mode => {
    grid.classList.toggle('compact-view', mode === 'compact');
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.recordView === mode)));
    try { localStorage.setItem('cmu_record_view', mode); } catch (_) {}
  };
  buttons.forEach(button => button.addEventListener('click', () => select(button.dataset.recordView)));
  try { select(localStorage.getItem('cmu_record_view') || 'cards'); } catch (_) { select('cards'); }
})();
