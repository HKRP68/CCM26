/* global api, AUTH_VALUE, escapeHtml, navigate, askConfirm, lastInitData, userData, openTournamentDirectory, openBrandingLink, toast, refreshDashboard */
(() => {
  const byId = id => document.getElementById(id);
  let giveaway;
  let pack;
  let dashboard;
  let read = new Set();
  let account;
  let avatarAccount;
  const storageKey = () => 'cmu-notices-' + account;
  const fmtDate = value => new Intl.DateTimeFormat(undefined, {dateStyle: 'medium'}).format(new Date(value));

  function notices() {
    const items = [];
    const giftCount = dashboard?.unclaimed_gifts || 0;
    if (giftCount) items.push(['gifts-' + giftCount, 'Reward available', giftCount + ' gifts to claim', () => navigate('rewards')]);
    if (dashboard?.login_streak?.claimable_today) {
      const streak = dashboard.login_streak;
      items.push(['login-' + (streak.today || streak.streak), 'Reward available', 'Daily login reward', () => {
        navigate('home');
        showLoginStreakModal();
      }]);
    }
    if (pack?.ready) items.push(['free-pack-' + (pack.last_opened_at || 'first'), 'Pack event', 'Free Pack ready', () => navigate('freepack')]);
    if (giveaway) items.push(['giveaway-' + giveaway.id, 'Giveaway', giveaway.title, openGiveaways]);
    if (dashboard?.tournament?.live) {
      const ids = dashboard.tournament.tournaments.map(t => t.id).join('-');
      items.push(['tournament-' + ids, 'League reminder', 'Live tournaments', openTournamentDirectory]);
    }
    const latest = dashboard?.latest_match;
    if (latest) items.push(['match-' + latest.id, 'Match result', latest.result, () => navigate('matchcentre')]);
    const news = dashboard?.news_unread || 0;
    if (news) items.push(['news', 'CMU News', news + (news === 1 ? ' unread article' : ' unread articles'), () => {
      navigate('explore');
      toggleCommunityCard('news-box', true);
      byId('news-box').scrollIntoView({block: 'center'});
    }]);
    return items;
  }

  function updateBadge() {
    const count = notices().filter(([key]) => key === 'news' || !read.has(key)).length;
    const badge = byId('cmu-unread');
    badge.hidden = !count;
    badge.textContent = count || '';
    byId('cmu-notification-button').setAttribute('aria-label', count ? 'Notifications, ' + count + ' unread' : 'Notifications');
  }

  function notifications() {
    const root = byId('cmu-notification-list');
    root.replaceChildren();
    const list = notices();
    if (!list.length) {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = 'You’re all caught up.';
      root.append(empty);
    }
    for (const [key, category, title, open] of list) {
      const button = document.createElement('button');
      button.type = 'button';
      const label = document.createElement('span');
      label.textContent = title;
      button.append(label);
      const state = document.createElement('small');
      state.className = 'cmu-notice-state';
      state.textContent = read.has(key) ? category : category + ' · New';
      button.append(state);
      button.onclick = () => {
        if (key !== 'news') {
          read.add(key);
          try { localStorage.setItem(storageKey(), JSON.stringify([...read].slice(-100))); } catch (_) {}
        }
        updateBadge();
        open();
      };
      root.append(button);
    }
  }

  async function loadAvatar(accountId) {
    const image = byId('home-avatar-photo');
    image.hidden = true;
    try {
      const response = await fetch('/api/webapp/profile/photo', {method: 'POST', headers: {Authorization: AUTH_VALUE}});
      if (response.status !== 200) return;
      const blob = await response.blob();
      if (avatarAccount !== accountId) return;
      const src = URL.createObjectURL(blob);
      image.onload = () => { image.hidden = false; URL.revokeObjectURL(src); };
      image.onerror = () => URL.revokeObjectURL(src);
      image.src = src;
    } catch (_) {}
  }

  function update(data) {
    dashboard = data;
    const profile = data.user;
    if (account !== profile.user_id) {
      account = profile.user_id;
      read = new Set();
      try { read = new Set(JSON.parse(localStorage.getItem(storageKey()) || '[]')); } catch (_) {}
    }
    if (avatarAccount !== account) {
      avatarAccount = account;
      const name = (profile.first_name || profile.username || 'Player').trim();
      byId('home-avatar-initials').textContent = name.split(/\s+/).slice(0, 2)
        .map(part => [...part][0] || '').join('').toUpperCase();
      loadAvatar(account);
    }
    byId('home-debut').textContent = profile.joined_at ? 'Debut ' + fmtDate(profile.joined_at) : '';
    giveaway = data.home_giveaway;
    byId('home-giveaway').hidden = !giveaway;
    if (giveaway) {
      byId('home-giveaway-title').textContent = giveaway.title;
      byId('home-giveaway-prize').textContent = giveaway.prize;
    }
    updateBadge();
    if (currentScreen === 'notifications') notifications();
  }

  let traitsRequest = 0;
  async function traits() {
    const request = ++traitsRequest;
    const root = byId('cmu-traits-list');
    root.innerHTML = '<div class="empty">Loading traits…</div>';
    const {ok, data} = await api('/api/webapp/traits');
    if (request !== traitsRequest || currentScreen !== 'traits') return;
    if (!ok) {
      root.innerHTML = '<div class="empty">Could not load the Traits Market. <button class="hub-button" onclick="CmuOverview.traits()">Retry</button></div>';
      return;
    }
    root.innerHTML = `<div class="club-card"><h3>${data.gems.toLocaleString()} gems</h3>
      <p>${data.remaining} purchases left today</p><p>Open a player’s Traits tab in your Squad to equip a purchased trait.</p>
      <button class="hub-button" onclick="navigate('roster')">Open Squad</button></div>` +
      (data.slots.map(slot => `<article class="club-card">
        <h3>${escapeHtml(slot.name)}</h3><p>${escapeHtml(slot.effect)}</p>
        <p>${escapeHtml(slot.compatibility)}</p>
        <button type="button" class="hub-button" data-trait-slot="${slot.id}" ${slot.sold_out || slot.price > data.gems || !data.remaining ? 'disabled' : ''}>
        ${slot.sold_out ? 'Sold out' : 'Review · ' + slot.price.toLocaleString() + ' gems'}</button></article>`).join('') ||
        '<div class="empty">No traits are listed right now.</div>');
    for (const button of root.querySelectorAll('[data-trait-slot]')) {
      const slot = data.slots.find(item => item.id === Number(button.dataset.traitSlot));
      button.onclick = async () => {
        button.disabled = true;
        try {
          const quote = await api('/api/webapp/economy/quote', {action: 'trait', target: slot.id});
          if (!quote.ok) { toast(quote.data.message || 'This trait is unavailable.', 'error'); return; }
          const offer = quote.data;
          if (!await askConfirm(`Buy ${offer.name} for ${offer.price.toLocaleString()} gems? Balance after purchase: ${offer.balance_after.toLocaleString()} gems.\n\n${slot.effect}\n${slot.compatibility}\n\nThe trait goes to your inventory.`)) return;
          const result = await confirmEconomy(offer.quote_id);
          toast(result.ok ? `Trait purchased. Receipt ${result.data.receipt}.` :
            result.data.message || 'Could not confirm purchase.', result.ok ? 'success' : 'error');
          if (result.ok) {
            if (currentScreen === 'traits') traits();
            refreshDashboard();
          }
        } finally {
          button.disabled = false;
        }
      };
    }
  }

  window.CmuOverview = {
    update,
    notifications,
    traits,
    freePack(data) { pack = data; updateBadge(); },
    news(count) {
      if (dashboard) dashboard.news_unread = count;
      updateBadge();
    },
  };
})();

function openGiveaways() {
  window.Clubhouse.openGiveaways();
}

function openBotMatchModes() {
  const username = lastInitData?.bot_username;
  if (!/^[a-zA-Z0-9_]{5,32}$/.test(username || '')) {
    toast('Open the CMU bot in Telegram for PvP, Ranked, and VS Bot matches.');
    return;
  }
  const url = 'https://t.me/' + username;
  if (window.Telegram?.WebApp?.openTelegramLink) window.Telegram.WebApp.openTelegramLink(url);
  else window.open(url, '_blank', 'noopener');
}

async function confirmEconomy(quoteId) {
  const url = '/api/webapp/economy/confirm';
  savePendingQuote(quoteId, true);
  let result = await api(url, {quote_id: quoteId});
  if (result.status === 0 || result.status === 504) {
    toast('Checking transaction…');
    result = await api('/api/webapp/economy/status', {quote_id: quoteId});
    if (result.ok && result.data.state === 'created') result = await api(url, {quote_id: quoteId});
  }
  if (result.data.state === 'succeeded' || result.data.state === 'expired' ||
      (result.status >= 400 && result.status < 500 && result.status !== 429)) savePendingQuote(quoteId, false);
  if (result.data.state !== 'succeeded' && result.ok) return {...result, ok: false};
  return result;
}

function pendingQuotes() {
  try { return JSON.parse(localStorage.getItem('cmu-orders-' + userData?.user_id) || '[]'); }
  catch (_) { return []; }
}

function savePendingQuote(quoteId, pending) {
  const remaining = pendingQuotes().filter(id => id !== quoteId);
  if (pending) remaining.push(quoteId);
  try { localStorage.setItem('cmu-orders-' + userData?.user_id, JSON.stringify(remaining.slice(-20))); }
  catch (_) {}
}

async function recoverWalletTransactions() {
  const root = document.getElementById('wallet-pending');
  root.replaceChildren();
  for (const quoteId of pendingQuotes()) {
    const {ok, data, status} = await api('/api/webapp/economy/status', {quote_id: quoteId});
    if (!ok && status === 404 || data.state === 'expired') {
      savePendingQuote(quoteId, false);
      continue;
    }
    const row = document.createElement('p');
    if (ok && data.state === 'succeeded') {
      row.textContent = 'Transaction confirmed. Receipt ' + quoteId;
      savePendingQuote(quoteId, false);
    } else {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'hub-button';
      button.textContent = ok ? 'Retry confirmed transaction' : 'Check transaction status';
      button.onclick = async () => {
        button.disabled = true;
        if (ok) {
          const result = await confirmEconomy(quoteId);
          toast(result.ok ? 'Transaction confirmed.' : result.data.message || 'Transaction status unavailable.',
            result.ok ? 'success' : 'error');
          window.Clubhouse.open('wallet');
        } else await recoverWalletTransactions();
      };
      row.append(button);
    }
    root.append(row);
  }
}

function reviewEconomy(title, rows, explanation = '', confirmLabel = 'Confirm') {
  const sheet = document.getElementById('cmu-review');
  if (sheet.open) return Promise.resolve(false);
  const opener = document.activeElement;
  const amounts = document.getElementById('cmu-review-amounts');
  document.getElementById('cmu-review-title').textContent = title;
  document.getElementById('cmu-review-explanation').textContent = explanation;
  document.getElementById('cmu-review-confirm').textContent = confirmLabel;
  amounts.replaceChildren();
  for (const [label, amount] of rows) {
    const name = document.createElement('dt');
    const value = document.createElement('dd');
    name.textContent = label;
    value.textContent = amount;
    amounts.append(name, value);
  }
  return new Promise(resolve => {
    sheet.addEventListener('close', () => {
      opener?.focus();
      resolve(sheet.returnValue === 'confirm');
    }, {once: true});
    sheet.returnValue = '';
    sheet.showModal();
  });
}

async function reviewConversion(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const {ok, data} = await api('/api/webapp/economy/quote', {action: 'convert', target: form.elements.coins.valueAsNumber});
    if (!ok) { toast(data.message || 'Conversion is unavailable.', 'error'); return; }
    if (!await reviewEconomy('Convert Coins to Gems', [
      ['Rate', `${data.rate.toLocaleString()} coins per gem`],
      ['You spend', `${data.price.toLocaleString()} coins`],
      ['You receive', `${data.gems.toLocaleString()} ${data.gems === 1 ? 'gem' : 'gems'}`],
      ['Balances after', `${data.balance_after.toLocaleString()} coins · ${data.gems_after.toLocaleString()} gems`],
    ], 'Conversions are irreversible.')) return;
    const result = await confirmEconomy(data.quote_id);
    if (result.ok) {
      toast('Conversion complete. Receipt ' + result.data.receipt, 'success');
      window.Clubhouse.open('wallet');
      refreshDashboard();
      form.reset();
    } else toast(result.data.message || 'Could not confirm conversion. Check your Wallet.', 'error');
  } finally {
    button.disabled = false;
  }
}

const rewardAttempts = new Map();
function pendingRewardId(kind) {
  const key = `cmu-reward-${userData?.user_id}-${kind}`;
  try { return localStorage.getItem(key) || rewardAttempts.get(key); } catch (_) { return rewardAttempts.get(key); }
}

async function submitReward(kind, endpoint, adToken, timeout) {
  const key = `cmu-reward-${userData?.user_id}-${kind}`;
  const requestId = pendingRewardId(kind) || Array.from(
    crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  rewardAttempts.set(key, requestId);
  try { localStorage.setItem(key, requestId); } catch (_) {}
  const result = await api(endpoint, {ad_token: adToken, request_id: requestId}, timeout);
  if (result.ok || (result.status >= 400 && result.status < 500 && result.data.error !== 'in_progress' && result.data.error !== 'rate_limited')) {
    rewardAttempts.delete(key);
    try { localStorage.removeItem(key); } catch (_) {}
  }
  return result;
}
