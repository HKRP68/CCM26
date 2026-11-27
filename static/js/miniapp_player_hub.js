/* global api, AUTH_VALUE, escapeHtml, navigate, currentScreen */
(() => {
  const byId = id => document.getElementById(id);
  const text = value => escapeHtml(String(value ?? ''));
  const num = value => Number(value).toLocaleString();
  const date = value => new Date(value).toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'});
  const metric = (label, value, detail = '') => `<div class="hub-panel hub-metric"><span>${label}</span><strong>${text(value)}</strong>
    ${detail ? '<span>' + text(detail) + '</span>' : ''}</div>`;
  let profileRequest = 0;
  let achievementRequest = 0;
  let achievementData;

  async function loadAvatar(request) {
    const image = byId('hub-profile-photo');
    if (!image) return;
    try {
      const response = await fetch('/api/webapp/profile/photo', {
        method: 'POST', headers: {'Authorization': AUTH_VALUE},
      });
      if (response.status !== 200 || request !== profileRequest || !image.isConnected) return;
      const blob = await response.blob();
      const src = URL.createObjectURL(blob);
      image.onload = () => { image.hidden = false; URL.revokeObjectURL(src); };
      image.onerror = () => URL.revokeObjectURL(src);
      image.src = src;
    } catch (_) {
      // Initials are already visible.
    }
  }

  async function profile() {
    const request = ++profileRequest;
    const root = byId('hub-profile-body');
    root.innerHTML = '<div class="empty">Loading profile…</div>';
    const {ok, data: p} = await api('/api/webapp/profile', {});
    if (request !== profileRequest || currentScreen !== 'profile') return;
    if (!ok) {
      root.innerHTML = '<div class="empty">Could not load your profile. <button class="hub-button" onclick="PlayerHub.profile()">Retry</button></div>';
      return;
    }
    const initials = p.name.trim().split(/\s+/).slice(0, 2).map(part => Array.from(part)[0] || '').join('').toUpperCase();
    const badges = p.achievements.recent.map(a => `${text(a.emoji)} ${text(a.name)}`).join(' · ');
    root.innerHTML = `<div class="hub-hero">
      <div class="hub-identity"><div class="hub-avatar" aria-hidden="true">${text(initials)}
        <img id="hub-profile-photo" alt="" hidden></div>
        <div><h2>${text(p.name)}</h2>
          ${p.username ? `<p>@${text(p.username)}</p>` : ''}
          ${p.team ? `<p>${text(p.team)}</p>` : ''}
          ${p.joined_at ? `<p>Playing since ${date(p.joined_at)}</p>` : ''}</div></div>
      </div>
      <h3 class="hub-section-title">Match record</h3>
      <div class="hub-metrics">
        ${metric('Matches', num(p.played), `${num(p.wins)} wins · ${num(p.losses)} losses`)}
        ${metric('Win rate', p.win_rate == null ? '—' : p.win_rate + '%', `Best streak · ${p.best_streak}`)}
        ${metric('Ranked ladder', p.ranked ? '#' + num(p.ranked.rank) : 'Unranked', p.ranked ? num(p.ranked.rating) + ' rating' : '')}
        ${metric('Quick Match', num(p.quick_played), `${num(p.quick_wins)} wins`)}
      </div>
      <h3 class="hub-section-title">Batting and bowling</h3>
      <div class="hub-metrics">
        ${metric('Runs', num(p.runs), `${num(p.hundreds)} hundreds`)}
        ${metric('Wickets', num(p.wickets), `${num(p.five_fers)} five-wicket hauls`)}
        ${p.best_batter ? metric('Highest score', p.best_batter[1] + (p.best_batter[2] ? '*' : ''), p.best_batter[0]) : ''}
        ${p.best_bowler ? metric('Best figures', p.best_bowler[1] + '/' + p.best_bowler[2], p.best_bowler[0]) : ''}
      </div>
      <div class="hub-panel"><h3>Achievements</h3><p>${p.achievements.unlocked} of ${p.achievements.total} unlocked</p>
        ${badges ? `<p>${badges}</p>` : ''}
        <div class="hub-actions"><button class="hub-button primary" onclick="navigate('achievements')">View achievements</button>
        <button class="hub-button" onclick="navigate('matchcentre')">Match history</button>
        <button class="hub-button" onclick="navigate('appearance')">Appearance</button></div>
      </div>`;
    loadAvatar(request);
  }

  function renderAchievements() {
    if (!achievementData) return;
    const status = byId('achievement-status').value;
    const category = byId('achievement-category').value;
    const groups = Object.entries(achievementData.groups);
    byId('achievements-content').innerHTML = groups.filter(([name]) => !category || name === category).map(([name, items]) => {
      const visible = items.filter(a => status === 'all' || a.unlocked === (status === 'earned'));
      visible.sort((a, b) => Number(b.unlocked) - Number(a.unlocked) ||
        (b.unlocked ? String(b.unlocked_at).localeCompare(String(a.unlocked_at)) : 0));
      if (!visible.length) return '';
      return `<h3 class="hub-section-title">${text(name)} · ${items.filter(a => a.unlocked).length}/${items.length}</h3>` +
        visible.map(a => {
          const reward = [a.coins ? num(a.coins) + ' coins' : '', a.gems ? num(a.gems) + ' gems' : ''].filter(Boolean).join(' · ');
          const p = a.progress;
          return `<article class="hub-panel hub-ach-row ${a.unlocked ? 'earned' : ''}">
            <span aria-hidden="true">${text(a.emoji)}</span><div><h3>${text(a.name)}</h3><p>${text(a.desc)}</p>
            ${a.unlocked ? `<div class="hub-ach-meta">Unlocked${a.unlocked_at ? ' · ' + date(a.unlocked_at) : ''}</div>` :
              p ? `<progress class="hub-progress" aria-label="${text(a.name)} progress" value="${p.current}" max="${p.target}"></progress>
                <div class="hub-ach-meta">${num(p.current)} / ${num(p.target)} ${text(p.unit)}</div>` : '<div class="hub-ach-meta">Locked</div>'}
            ${reward ? `<p>${a.unlocked ? 'Reward earned' : 'Reward'} · ${reward}</p>` : ''}
            </div></article>`;
        }).join('');
    }).join('') || '<div class="empty">No achievements match these filters.</div>';
    const fx = window.CMURewardsFX;
    if (fx && fx.available) fx.stagger(byId('achievements-content'), '.hub-ach-row');
  }

  async function achievements() {
    const request = ++achievementRequest;
    const root = byId('achievements-content');
    root.innerHTML = '<div class="empty">Loading achievements…</div>';
    byId('achievement-controls').hidden = true;
    const {ok, data} = await api('/api/webapp/achievements', {});
    if (request !== achievementRequest || currentScreen !== 'achievements') return;
    if (!ok) {
      root.innerHTML = '<div class="empty">Could not load achievements. <button class="hub-button" onclick="PlayerHub.achievements()">Retry</button></div>';
      return;
    }
    // Anything unlocked since the last look at this screen gets a celebration.
    // Compared against the previous payload only — this never decides whether
    // an achievement is unlocked, it just notices that the server says so.
    const previouslyUnlocked = achievementData ? achievementData.unlocked : null;
    achievementData = data;
    const bar = byId('ach-progress-bar');
    bar.max = data.total || 1;
    bar.value = data.unlocked;
    const fx = window.CMURewardsFX;
    if (fx && fx.available) {
      fx.setCount(byId('ach-progress-text'), data.unlocked,
                  { format: (n) => `${num(n)} of ${num(data.total)} unlocked` });
      if (previouslyUnlocked != null && data.unlocked > previouslyUnlocked) {
        fx.celebrate('epic', byId('achievements-content').closest('.screen'));
      }
    } else {
      byId('ach-progress-text').textContent = `${data.unlocked} of ${data.total} unlocked`;
    }
    const category = byId('achievement-category');
    const selected = category.value;
    const categories = Object.keys(data.groups);
    category.replaceChildren(new Option('All categories', ''), ...categories.map(name => new Option(name, name)));
    if (categories.includes(selected)) category.value = selected;
    const next = Object.values(data.groups).flat().filter(a => !a.unlocked && a.progress?.current > 0)
      .sort((a, b) => b.progress.current / b.progress.target - a.progress.current / a.progress.target)[0];
    byId('achievement-next').innerHTML = next ? `<div class="hub-ach-next"><strong>${text(next.name)}</strong>
      <p>${num(next.progress.target - next.progress.current)} more ${text(next.progress.unit)} to unlock</p></div>` : '';
    byId('achievement-controls').hidden = false;
    renderAchievements();
  }

  byId('achievement-status').addEventListener('change', renderAchievements);
  byId('achievement-category').addEventListener('change', renderAchievements);
  window.PlayerHub = {profile, achievements};
})();
