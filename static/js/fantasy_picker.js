/* Fantasy Squad Picker — Mini App JS */

var tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

var state = {
  leagueId: null,
  userId: null,
  locked: false,
  selected: {},   // {player_id: {player_id, name, country, category, role, points}}
  allPlayers: [],
  filtered: [],
  page: 1,
  loading: false,
  hasMore: true,
  currentFilter: 'all',
  currentSearch: '',
  pendingRolePid: null,
};

/* ── Init ─────────────────────────────────────────────── */
(function init() {
  var params = new URLSearchParams(window.location.search);
  state.leagueId = params.get('league_id');
  state.userId = params.get('user_id');

  // Load league info & existing picks
  if (state.leagueId && state.userId) {
    fetch('/api/fantasy/entry?league_id=' + state.leagueId + '&user_id=' + state.userId)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.ok && d.squad) {
          d.squad.forEach(function(pick) {
            state.selected[pick.player_id] = {
              player_id: pick.player_id, name: pick.name,
              country: pick.country, category: pick.category,
              role: pick.role, points: pick.points || 0,
            };
          });
        }
        state.locked = d.locked || false;
        updateFooter();
      }).catch(function() {});
  }

  // Fetch league name
  if (state.leagueId) {
    var initData = (tg && tg.initData) || '';
    fetch('/api/fantasy/league', {
      headers: { 'Authorization': 'tma ' + initData }
    }).then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.ok && d.league) {
          document.getElementById('league-title').textContent = '🏏 ' + d.league.name;
          if (d.league.status === 'locked') {
            state.locked = true;
            showLockedBanner();
          }
        }
      }).catch(function() {});
  }

  loadPlayers();
})();

/* ── Player loading ───────────────────────────────────── */
function loadPlayers(reset) {
  if (state.loading || (!state.hasMore && !reset)) return;
  if (reset) {
    state.page = 1;
    state.hasMore = true;
    state.allPlayers = [];
    document.getElementById('player-list').innerHTML = '<div class="loading">Loading…</div>';
  }
  state.loading = true;

  var url = '/api/fantasy/players?page=' + state.page
    + '&role=' + encodeURIComponent(state.currentFilter)
    + '&q=' + encodeURIComponent(state.currentSearch);

  fetch(url).then(function(r) { return r.json(); })
    .then(function(d) {
      state.loading = false;
      if (!d.ok) return;
      state.allPlayers = state.allPlayers.concat(d.players || []);
      state.hasMore = d.players && d.players.length === 30;
      state.page++;
      renderPlayers();
    }).catch(function() { state.loading = false; });
}

function renderPlayers() {
  var list = document.getElementById('player-list');
  var html = '';

  if (state.locked) {
    html += '<div class="locked-banner">🔒 Squads are locked. Changes are not allowed.</div>';
  }

  state.allPlayers.forEach(function(p) {
    var sel = state.selected[p.id];
    var cls = 'player-card';
    var badge = '';
    if (sel) {
      if (sel.role === 'captain') { cls += ' captain'; badge = '<span class="role-badge badge-captain">C</span>'; }
      else if (sel.role === 'vc') { cls += ' vc'; badge = '<span class="role-badge badge-vc">VC</span>'; }
      else { cls += ' selected'; badge = '<span class="role-badge badge-selected">✓</span>'; }
    }

    var roleIcon = { BAT: '🏏', BOWL: '🎳', WK: '🧤', AR: '🌟' };
    var icon = roleIcon[p.category] || '🏏';
    var pts = sel ? ' · <span style="color:var(--accent);">' + sel.points + 'pts</span>' : '';

    html += '<div class="' + cls + '" onclick="onPlayerClick(' + p.id + ')">'
      + '<div class="player-avatar">' + icon + '</div>'
      + '<div class="player-info">'
      + '<div class="player-name">' + escHtml(p.name) + '</div>'
      + '<div class="player-meta">' + escHtml(p.country) + ' · ' + escHtml(p.category) + pts + '</div>'
      + '</div>'
      + '<span class="rating-pill">' + p.rating + '</span>'
      + badge
      + '</div>';
  });

  if (!state.allPlayers.length) {
    html += '<div class="loading">No players found.</div>';
  } else if (state.hasMore) {
    html += '<div class="loading" id="load-more-trigger">Loading more…</div>';
    setupInfiniteScroll();
  }

  list.innerHTML = html;
}

function setupInfiniteScroll() {
  var trigger = document.getElementById('load-more-trigger');
  if (!trigger) return;
  var obs = new IntersectionObserver(function(entries) {
    if (entries[0].isIntersecting) { obs.disconnect(); loadPlayers(); }
  });
  obs.observe(trigger);
}

/* ── Player interaction ───────────────────────────────── */
function onPlayerClick(pid) {
  if (state.locked) return;
  var player = state.allPlayers.find(function(p) { return p.id === pid; });
  if (!player) return;

  if (state.selected[pid]) {
    // Already selected — open role picker
    state.pendingRolePid = pid;
    document.getElementById('role-popup-name').textContent = player.name;
    document.getElementById('role-popup').style.display = 'flex';
  } else {
    // Not selected — add if room
    if (Object.keys(state.selected).length >= 11) {
      if (tg) tg.showAlert('You can only pick 11 players. Remove one first.');
      else alert('Select exactly 11 players.');
      return;
    }
    state.selected[pid] = { player_id: pid, name: player.name, country: player.country,
                             category: player.category, role: 'player', points: 0 };
    updateFooter();
    renderPlayers();
  }
}

function setRole(role) {
  var pid = state.pendingRolePid;
  if (!pid || !state.selected[pid]) { closeRolePopup(); return; }

  if (role === 'captain' || role === 'vc') {
    // Remove existing captain/vc
    Object.values(state.selected).forEach(function(p) {
      if (p.player_id !== pid && p.role === role) p.role = 'player';
    });
  }

  if (role === 'player' && state.selected[pid].role === 'player') {
    // Deselect
    delete state.selected[pid];
  } else {
    state.selected[pid].role = role;
  }

  closeRolePopup();
  updateFooter();
  renderPlayers();
}

function closeRolePopup() {
  document.getElementById('role-popup').style.display = 'none';
  state.pendingRolePid = null;
}

/* ── Filters & search ─────────────────────────────────── */
function setFilter(role, btn) {
  document.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  state.currentFilter = role;
  loadPlayers(true);
}

var searchTimeout;
function onSearch() {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(function() {
    state.currentSearch = document.getElementById('search-input').value.trim();
    loadPlayers(true);
  }, 300);
}

/* ── Footer ───────────────────────────────────────────── */
function updateFooter() {
  var picks = Object.values(state.selected);
  var n = picks.length;
  var cap = picks.filter(function(p) { return p.role === 'captain'; }).length;
  var vc = picks.filter(function(p) { return p.role === 'vc'; }).length;
  var pts = picks.reduce(function(acc, p) { return acc + (p.points || 0); }, 0);

  document.getElementById('count-text').textContent = n + '/11';
  document.getElementById('cap-text').textContent = cap ? '✓' : '✗';
  document.getElementById('vc-text').textContent = vc ? '✓' : '✗';
  document.getElementById('pts-text').textContent = pts.toFixed(1);

  var ready = !state.locked && n === 11 && cap === 1 && vc === 1;
  document.getElementById('confirm-btn').disabled = !ready;
  document.getElementById('confirm-btn').textContent = state.locked ? '🔒 Squads Locked' : 'Confirm Squad';
}

/* ── Confirm ──────────────────────────────────────────── */
function confirmSquad() {
  var picks = Object.values(state.selected).map(function(p) {
    return { player_id: p.player_id, role: p.role };
  });
  if (picks.length !== 11) return;

  var payload = JSON.stringify({ type: 'fantasy_picks', league_id: state.leagueId, picks: picks });

  if (tg && tg.sendData) {
    tg.sendData(payload);
  } else {
    // Fallback: POST directly (e.g. dev mode)
    var initData = (tg && tg.initData) || '';
    fetch('/api/fantasy/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'tma ' + initData },
      body: JSON.stringify({ league_id: parseInt(state.leagueId), picks: picks }),
    }).then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.ok) { alert('Squad saved!'); }
        else { alert('Error: ' + (d.error || 'Unknown')); }
      });
  }
}

function showLockedBanner() {
  updateFooter();
  document.getElementById('confirm-btn').textContent = '🔒 Squads Locked';
  document.getElementById('confirm-btn').disabled = true;
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
