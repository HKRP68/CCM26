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
  currentCountry: '',
  countries: [],
  countryRules: {},
  roleRules: {},
  pendingRolePid: null,
};

/* ── Locked-out screens ───────────────────────────────── */
/* Every /api/fantasy/* endpoint returns 503 {error:"maintenance"} while the
   game is under maintenance, and 403 {error:"rookie_required"} when Rookie
   mode is on and the player has no membership. Show either once instead of an
   empty picker. */
var maintenanceShown = false;

function isMaintenance(d) {
  return !!(d && d.ok === false
            && (d.error === 'maintenance' || d.error === 'rookie_required'));
}

function showMaintenance(d) {
  if (maintenanceShown) return true;
  maintenanceShown = true;
  var locked = !!(d && d.error === 'rookie_required');
  var wrap = document.createElement('div');
  wrap.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;'
    + 'align-items:center;justify-content:center;padding:1.5rem;text-align:center;'
    + 'background:#0b0f17;color:#fff;font:inherit;';
  var icon = document.createElement('div');
  icon.style.cssText = 'font-size:2.75rem;margin-bottom:.85rem;';
  icon.textContent = locked ? '🔒' : '🛠️';
  var body = document.createElement('div');
  body.style.cssText = 'max-width:22rem;line-height:1.65;white-space:pre-line;';
  // textContent, so an admin-written message can never inject markup here.
  body.textContent = ((d && d.message)
    || (locked ? 'Members only — send /membership in the bot to see the plans.'
               : 'The game is under maintenance.'))
    .replace(/<[^>]+>/g, '');
  var card = document.createElement('div');
  card.appendChild(icon);
  card.appendChild(body);
  wrap.appendChild(card);
  document.body.appendChild(wrap);
  return true;
}

/* ── Init ─────────────────────────────────────────────── */
function preloadSquad(squad) {
  (squad || []).forEach(function(pick) {
    state.selected[pick.player_id] = {
      player_id: pick.player_id, name: pick.name,
      country: pick.country, category: pick.category,
      role: pick.role, points: pick.points || 0,
    };
  });
}

(function init() {
  var params = new URLSearchParams(window.location.search);
  state.leagueId = params.get('league_id');   // may be null on deep-link launch
  state.userId = params.get('user_id');

  // Resolve the active league + this user's existing squad via initData.
  // This path works for every launch type: the private-chat Web App button,
  // and the t.me deep link used in groups / DMs / broadcasts (no user_id).
  var initData = (tg && tg.initData) || '';
  fetch('/api/fantasy/league', {
    headers: { 'Authorization': 'tma ' + initData }
  }).then(function(r) { return r.json(); })
    .then(function(d) {
      if (isMaintenance(d)) return showMaintenance(d);
      if (d.ok && d.league) {
        if (!state.leagueId) state.leagueId = d.league.id;
        document.getElementById('league-title').textContent = '🏏 ' + d.league.name;
        state.countryRules = d.league.country_rules || {};
        state.roleRules = d.league.role_rules || {};
        updateCountryOptions(Object.keys(state.countryRules));
        if (d.league.locked || d.league.status === 'locked') {
          state.locked = true;
          showLockedBanner();
        }
        if (d.team && d.team.squad) preloadSquad(d.team.squad);
        loadPlayers(true);
      } else {
        loadPlayers(true);
      }
      updateFooter();
      renderPlayers();
    }).catch(function() {
      // Fallback for non-Telegram/dev contexts where initData is unavailable.
      if (state.leagueId && state.userId) {
        fetch('/api/fantasy/entry?league_id=' + state.leagueId + '&user_id=' + state.userId)
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (d.ok) { preloadSquad(d.squad); state.locked = d.locked || false; }
            updateFooter();
            renderPlayers();
            loadPlayers(true);
          }).catch(function() {});
      } else {
        loadPlayers(true);
      }
    });
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
    + '&q=' + encodeURIComponent(state.currentSearch)
    + '&league_id=' + encodeURIComponent(state.leagueId || '')
    + '&country=' + encodeURIComponent(state.currentCountry || '');

  fetch(url).then(function(r) { return r.json(); })
    .then(function(d) {
      state.loading = false;
      if (isMaintenance(d)) return showMaintenance(d);
      if (!d.ok) return;
      state.allPlayers = state.allPlayers.concat(d.players || []);
      updateCountryOptions(d.countries || []);
      mergeCountries(d.players || []);
      if (d.country_rules) state.countryRules = d.country_rules;
      if (d.role_rules) state.roleRules = d.role_rules;
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

function mergeCountries(players) {
  var seen = {};
  state.countries.forEach(function(c) { seen[c] = true; });
  players.forEach(function(p) {
    if (p.country && !seen[p.country]) {
      state.countries.push(p.country);
      seen[p.country] = true;
    }
  });
  state.countries.sort();
  updateCountryOptions(state.countries);
}

function updateCountryOptions(extraCountries) {
  var select = document.getElementById('country-filter');
  if (!select) return;
  var existing = {};
  state.countries.forEach(function(c) { existing[c] = true; });
  (extraCountries || []).forEach(function(c) {
    if (c && !existing[c]) { state.countries.push(c); existing[c] = true; }
  });
  state.countries.sort();
  var current = select.value;
  select.innerHTML = '<option value="">All Countries</option>' + state.countries.map(function(c) {
    return '<option value="' + escHtml(c) + '">' + escHtml(c) + '</option>';
  }).join('');
  select.value = current;
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

  state.selected[pid].role = role;

  closeRolePopup();
  updateFooter();
  renderPlayers();
}

function removePendingPlayer() {
  var pid = state.pendingRolePid;
  if (pid && state.selected[pid]) {
    delete state.selected[pid];
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

function onCountryFilter() {
  state.currentCountry = document.getElementById('country-filter').value || '';
  loadPlayers(true);
}

function normalizeRoleKey(category) {
  var cat = String(category || '').trim().toUpperCase().replace(/_/g, ' ');
  var compact = cat.replace(/[- ]/g, '');
  if (cat.indexOf('WK') !== -1 || cat.indexOf('KEEP') !== -1) return 'wk';
  if (cat === 'AR' || compact.indexOf('ALL') !== -1) return 'ar';
  if (cat.indexOf('BOWL') !== -1) return 'bowl';
  if (cat.indexOf('BAT') !== -1) return 'bat';
  return 'bat';
}

function roleRuleStatus() {
  var counts = {};
  Object.values(state.selected).forEach(function(p) {
    var key = normalizeRoleKey(p.category);
    counts[key] = (counts[key] || 0) + 1;
  });
  var messages = [];
  var valid = true;
  Object.keys(state.roleRules || {}).forEach(function(roleKey) {
    var rule = state.roleRules[roleKey] || {};
    var count = counts[roleKey] || 0;
    var min = parseInt(rule.min || 0, 10);
    var max = rule.max === null || rule.max === undefined || rule.max === '' ? null : parseInt(rule.max, 10);
    var label = rule.label || roleKey;
    if (min || max !== null) {
      messages.push(label + ': ' + count + (min ? ' / min ' + min : '') + (max !== null ? ' / max ' + max : ''));
    }
    if (min && count < min) valid = false;
    if (max !== null && count > max) valid = false;
  });
  return { valid: valid, text: messages.join(' · ') };
}

function countryRuleStatus() {
  var counts = {};
  Object.values(state.selected).forEach(function(p) {
    counts[p.country] = (counts[p.country] || 0) + 1;
  });
  var messages = [];
  var valid = true;
  Object.keys(state.countryRules || {}).forEach(function(country) {
    var rule = state.countryRules[country] || {};
    var count = counts[country] || 0;
    var min = parseInt(rule.min || 0, 10);
    var max = rule.max === null || rule.max === undefined || rule.max === '' ? null : parseInt(rule.max, 10);
    if (min || max !== null) {
      messages.push(country + ': ' + count + (min ? ' / min ' + min : '') + (max !== null ? ' / max ' + max : ''));
    }
    if (min && count < min) valid = false;
    if (max !== null && count > max) valid = false;
  });
  return { valid: valid, text: messages.join(' · ') };
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

  var countryStatus = countryRuleStatus();
  var roleStatus = roleRuleStatus();
  var ruleEl = document.getElementById('country-rule-text');
  if (ruleEl) {
    var textParts = [];
    if (roleStatus.text) textParts.push(roleStatus.text);
    if (countryStatus.text) textParts.push(countryStatus.text);
    ruleEl.textContent = textParts.join(' · ');
    ruleEl.style.color = (countryStatus.valid && roleStatus.valid) ? 'var(--muted)' : 'var(--red)';
  }

  var ready = !state.locked && n === 11 && cap === 1 && vc === 1 && countryStatus.valid && roleStatus.valid;
  document.getElementById('confirm-btn').disabled = !ready;
  document.getElementById('confirm-btn').textContent = state.locked ? '🔒 Squads Locked' : 'Confirm Squad';
}

/* ── Confirm ──────────────────────────────────────────── */
function confirmSquad() {
  var picks = Object.values(state.selected).map(function(p) {
    return { player_id: p.player_id, role: p.role };
  });
  if (picks.length !== 11) return;
  var countryStatus = countryRuleStatus();
  var roleStatus = roleRuleStatus();
  if (!countryStatus.valid || !roleStatus.valid) {
    var msg = [roleStatus.text, countryStatus.text].filter(Boolean).join(' · ');
    if (tg && tg.showAlert) tg.showAlert('Squad rules are not satisfied: ' + msg);
    else alert('Squad rules are not satisfied: ' + msg);
    return;
  }
  if (!state.leagueId) {
    if (tg) tg.showAlert('No active fantasy league found.');
    else alert('No active fantasy league found.');
    return;
  }

  var btn = document.getElementById('confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  // Save via the API using Telegram initData. This works for every launch
  // type (private Web App button AND group/broadcast t.me deep links);
  // tg.sendData only delivers for keyboard-button Web Apps, which this is not.
  var initData = (tg && tg.initData) || '';
  fetch('/api/fantasy/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'tma ' + initData },
    body: JSON.stringify({ league_id: parseInt(state.leagueId, 10), picks: picks }),
  }).then(function(r) { return r.json(); })
    .then(function(d) {
      if (isMaintenance(d)) return showMaintenance(d);
      if (d.ok) {
        if (tg && tg.showAlert) {
          tg.showAlert('✅ Squad saved! Good luck.', function() { if (tg.close) tg.close(); });
        } else {
          alert('Squad saved!');
        }
      } else {
        btn.disabled = false;
        btn.textContent = 'Confirm Squad';
        var msg = d.message || d.error || 'Could not save squad.';
        if (tg && tg.showAlert) tg.showAlert('❌ ' + msg); else alert('Error: ' + msg);
      }
    }).catch(function() {
      btn.disabled = false;
      btn.textContent = 'Confirm Squad';
      if (tg && tg.showAlert) tg.showAlert('❌ Network error. Try again.');
      else alert('Network error. Try again.');
    });
}

function showLockedBanner() {
  updateFooter();
  document.getElementById('confirm-btn').textContent = '🔒 Squads Locked';
  document.getElementById('confirm-btn').disabled = true;
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
