/* global api, escapeHtml, navigate, openLiveMatch, openScorecard, refreshDashboard, askConfirm, switchPacksTab, hapticNotif, toast */
(() => {
  const screens = {
    matchcentre: {path: 'matches', body: 'club-match-body', filter: 'matches', key: 'view'},
    wallet: {path: 'wallet', body: 'club-wallet-body', filter: 'all', key: 'currency'},
    rewards: {path: 'rewards', body: 'club-reward-body', filter: 'gifts'},
  };
  const money = value => Number(value).toLocaleString();
  const labels = {coins: 'coins', gems: 'gems', quest_points: 'quest points'};
  const labelsTitle = {coins: 'Coins', gems: 'Gems', quest_points: 'Quest points'};
  const giftValue = gift => gift.item || (labels[gift.currency] ? money(gift.amount) + ' ' + labels[gift.currency] : 'Reward unavailable');
  const date = value => value ? new Date(value).toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'}) : '';
  let activeScreen;
  let generation = 0;
  let busy = false;
  let revealedGift;
  const button = (label, action, id = '', primary = false) =>
    `<button type="button" class="club-link${primary ? ' primary' : ''}" data-club-action="${action}" data-id="${id}">${label}</button>`;
  const cardArt = playerId => playerId
    ? '<img class="club-card-art" src="/webapp/player-card/' + Number(playerId) + '" alt="" loading="lazy" decoding="async" onerror="this.hidden=true">'
    : '';
  const empty = message => `<div class="club-empty"><p>${message}</p></div>`;

  function giftCount(count) {
    const badge = document.getElementById('club-gift-count');
    badge.hidden = !count;
    badge.textContent = (count > 99 ? '99+' : count) + ' new';
    badge.setAttribute('aria-label', count + ' gifts to claim');
  }

  function matchCards(data, mode) {
    if (mode === 'quick') {
      return data.matches.map(match => `<article class="club-card club-row">
        <div><h3>${escapeHtml(match.summary)}</h3><p>${date(match.date)}</p></div>
        ${match.coins > 0 ? `<span class="club-credit club-amount">+${money(match.coins)} coins</span>` : ''}
        </article>`).join('') || empty('Your completed Quick Matches will appear here.');
    }
    const live = data.live.map(match => `<article class="club-card">
      <div class="club-row"><div class="club-inner"><h3>${escapeHtml(match.opponent)}</h3>
      <p>${escapeHtml(match.mode)} · ${escapeHtml(match.status.replaceAll('_', ' '))}</p></div>
      ${match.can_open ? button('Open match', 'resume', match.id, true) : ''}</div>
      </article>`).join('');
    const completed = data.matches.map(match => {
      const resultStyle = match.result === 'Won' ? 'win' : match.result === 'Lost' ? 'loss' : '';
      const score = match.scores.map(innings => money(innings.runs) + (innings.wickets == null ? '' : '/' + innings.wickets)).join(' · ');
      return `<article class="club-card">
        <div class="club-row"><div class="club-inner"><h3>${escapeHtml(match.opponent)}</h3>
        <p>${escapeHtml(match.mode)} · ${date(match.date)}</p></div>
        <span class="club-result ${resultStyle}">${escapeHtml(match.result)}</span></div>
        ${score ? `<p class="club-score">${score}</p>` : ''}
        ${match.margin ? `<p>Margin ${escapeHtml(match.margin)}</p>` : ''}
        ${match.has_scorecard ? button('Scorecard', 'scorecard', match.id) : ''}
        </article>`;
    }).join('');
    return (live ? '<h3 class="club-section">In progress</h3>' + live : '')
      + (!data.continued ? '<h3 class="club-section">Results</h3>' : '')
      + (completed || (!data.continued ? empty('Your completed matches will appear here.') : ''));
  }

  function walletCards(data) {
    const balances = document.getElementById('club-balances');
    balances.innerHTML = Object.entries(data.balance).map(([key, value]) =>
      `<div><span>${labelsTitle[key]}</span><strong data-cmu-balance="${key}">${money(value)}</strong></div>`).join('');
    // Count each balance up from what was last on screen. The confirmed value
    // is written to the element first, so an interrupted count still leaves
    // the right number behind.
    const fx = window.CMURewardsFX;
    if (fx && fx.available) {
      for (const [key, value] of Object.entries(data.balance)) {
        fx.setCount(balances.querySelector(`[data-cmu-balance="${key}"]`), value, { format: money });
      }
    }
    return data.transactions.map(row => {
      const changes = ['coins', 'gems', 'quest_points'].filter(key => row[key]).map(key =>
        `<div class="${row[key] > 0 ? 'club-credit' : 'club-debit'}">${row[key] > 0 ? '+' : '−'}${money(Math.abs(row[key]))} ${labels[key]}</div>`).join('');
      const after = ['coins', 'gems', 'quest_points'].filter(key => row[key] && row[key + '_after'] != null)
        .map(key => money(row[key + '_after']) + ' ' + labels[key]).join(' · ');
      return `<article class="club-card club-row">
        <div class="club-inner"><h3>${escapeHtml(row.label)}</h3>
        <p>${row.date ? new Date(row.date).toLocaleString() : ''}${row.player ? ' · ' + escapeHtml(row.player) : ''}</p>
        <p>Reference ${escapeHtml(row.reference || String(row.id))} · Settled</p>
        ${after ? `<p>Balance after ${after}</p>` : ''}</div><div class="club-amount">${changes}</div>
        </article>`;
    }).join('') || empty('No recorded activity for this filter.');
  }

  function rewardCards(data, mode) {
    giftCount(data.unclaimed);
    if (mode === 'gifts') {
      return data.gifts.map(gift => `<article class="club-card club-gift ${revealedGift === gift.id ? 'is-revealed' : ''}">
        <div class="club-row">
          <div class="club-inner">
            <h3>${escapeHtml(gift.title)}</h3>
            <p class="club-claim-label">${escapeHtml(giftValue(gift))}</p>
            <p>${gift.claimed_at ? 'Claimed ' + date(gift.claimed_at) : 'Received ' + date(gift.created_at)}</p>
            ${!gift.claimed_at ? button('Review claim', 'claim', gift.id, true)
              : gift.currency === 'pack' ? button('Pack inventory', 'inventory')
              : gift.currency === 'player' ? button('My squad', 'squad') : ''}
          </div>
          ${gift.claimed_at ? cardArt(gift.player_id) : '<span class="club-gift-icon" aria-hidden="true">🎁</span>'}
        </div>
        </article>`).join('') || empty('Gifts sent by CCM26 admins will appear here.');
    }
    const joinLink = /^https:\/\/t\.me\/[a-z0-9_+/=-]+$/i.test(data.group_link || '')
      ? `<a class="club-link" href="${escapeHtml(data.group_link)}" target="_blank" rel="noopener">Official group</a>` : '';
    const cards = data.giveaways.map(giveaway => {
      const status = giveaway.status === 'running' && Date.parse(giveaway.ends_at) <= Date.now() ? 'closed' : giveaway.status;
      const footer = status === 'running'
        ? (giveaway.joined ? 'Entry confirmed' : 'Official group members only')
        : giveaway.won ? 'You won' : status === 'cancelled' ? 'Cancelled' : 'Entries closed';
      return `<article class="club-card">
        <div class="club-row"><div class="club-inner"><h3>${escapeHtml(giveaway.title)}</h3>
        <p class="club-score">${escapeHtml(giveaway.prize)}</p></div>
        <span class="club-result ${giveaway.won || giveaway.joined ? 'win' : ''}">${money(giveaway.entries)} ${giveaway.entries === 1 ? 'entry' : 'entries'}</span></div>
        <p>${status === 'running' ? 'Closes ' + new Date(giveaway.ends_at).toLocaleString() + ' · ' : ''}${footer}</p>
        ${giveaway.requires_match && !giveaway.joined && status === 'running' ? '<p>Requires a completed eligible match.</p>' : ''}
        ${status === 'running' && !giveaway.joined ? button('Enter giveaway', 'join', giveaway.id, true) : ''}
        </article>`;
    }).join('');
    return (joinLink ? `<div class="club-row" style="margin-bottom:8px;">${joinLink}</div>` : '')
      + (cards || empty('There are no giveaways open right now.'));
  }

  async function load(name, before = null) {
    const screen = screens[name];
    const request = ++generation;
    const body = document.getElementById(screen.body);
    const paging = body.querySelector('[data-club-action="more"]');
    if (before && paging) paging.disabled = true;
    if (!before) body.innerHTML = empty('Loading…');
    body.setAttribute('aria-busy', 'true');
    const {ok, data} = await api('/api/webapp/clubhouse/' + screen.path, {[screen.key || 'view']: screen.filter, before});
    if (request !== generation) return;
    body.setAttribute('aria-busy', 'false');
    if (!ok) {
      if (before && paging) {
        paging.disabled = false;
        toast('Could not load more. Try again.', 'error');
      } else {
        body.innerHTML = empty('Could not load this screen.') + button('Try again', 'retry');
      }
      return;
    }
    data.continued = Boolean(before);
    const html = name === 'matchcentre' ? matchCards(data, screen.filter)
      : name === 'wallet' ? walletCards(data) : rewardCards(data, screen.filter);
    if (before) paging?.remove();
    else body.replaceChildren();
    body.insertAdjacentHTML('beforeend', html + (data.next_before ? button('Load more', 'more', data.next_before) : ''));
  }

  for (const name of Object.keys(screens)) {
    document.getElementById('screen-' + name).addEventListener('click', async event => {
      const el = event.target.closest('button');
      if (!el || el.disabled) return;
      if (el.dataset.clubFilter) {
        const screen = screens[name];
        if (screen.filter === el.dataset.clubFilter) return;
        screen.filter = el.dataset.clubFilter;
        for (const control of el.parentElement.querySelectorAll('button')) {
          control.setAttribute('aria-pressed', String(control === el));
        }
        await load(name);
        return;
      }
      const action = el.dataset.clubAction;
      const id = Number(el.dataset.id);
      if (action === 'more' || action === 'retry') await load(name, id || null);
      if (action === 'resume') openLiveMatch(id);
      if (action === 'squad') navigate('roster');
      if (action === 'inventory') {
        navigate('packs');
        switchPacksTab('inventory');
      }
      if (action === 'scorecard') await openScorecard(id);
      if ((action === 'claim' || action === 'join') && !busy) {
        busy = true;
        el.disabled = true;
        const feedback = document.getElementById('club-reward-feedback');
        feedback.textContent = '';
        try {
          if (action === 'claim') {
            const prize = el.closest('article').querySelector('.club-claim-label').textContent;
            if (!await askConfirm(`Claim ${prize}?`)) return;
          }
          const path = action === 'claim' ? `gifts/${id}/claim` : `giveaways/${id}/join`;
          const {ok, data} = await api('/api/webapp/clubhouse/' + path, {});
          if (!ok) {
            feedback.textContent = data.message || 'Something went wrong. Try again.';
            toast(feedback.textContent, 'error');
            return;
          }
          if (action === 'claim') {
            revealedGift = id;
            feedback.textContent = `Claimed ${giftValue(data.gift)}.`;
            // Cosmetic, and only once the server has confirmed the claim. The
            // gift card below re-renders with its real contents either way.
            const fx = window.CMURewardsFX;
            if (fx && fx.available) {
              const gift = data.gift || {};
              fx.chest({
                title: gift.title || 'Gift claimed',
                eyebrow: 'Gift',
                tier: gift.currency === 'player' ? 'legend'
                      : gift.currency === 'pack' || gift.currency === 'gems' ? 'epic' : 'rare',
                rewardHtml: `<p class="cmu-gift-prize">${escapeHtml(giftValue(gift))}</p>`,
                doneLabel: 'Collect',
              });
            }
            refreshDashboard();
          } else {
            feedback.textContent = 'Entry confirmed.';
            refreshDashboard();
          }
          if (activeScreen === name) await load(name);
          hapticNotif('success');
          toast(feedback.textContent, 'success');
        } finally {
          el.disabled = false;
          busy = false;
        }
      }
    });
  }

  window.Clubhouse = {
    open(name) {
      activeScreen = name;
      generation++;
      if (screens[name]) load(name);
    },
    openGiveaways() {
      const controls = document.querySelectorAll('#screen-rewards [data-club-filter]');
      for (const control of controls) control.setAttribute('aria-pressed', String(control.dataset.clubFilter === 'giveaways'));
      screens.rewards.filter = 'giveaways';
      navigate('rewards');
    },
    giftCount,
  };
})();
