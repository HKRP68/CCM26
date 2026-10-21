(function () {
  'use strict';
  const root = document.getElementById('preseason-live');
  if (!root) return;
  const status = document.getElementById('preseason-live-status');
  const money = (value, currency) => `${currency}${(value / 100).toFixed(2)} Cr`;
  function row(values) {
    const tr = document.createElement('tr');
    values.forEach(value => {
      const td = document.createElement('td');
      td.textContent = value;
      tr.appendChild(td);
    });
    return tr;
  }
  async function refresh() {
    if (document.hidden) { window.setTimeout(refresh, 5000); return; }
    try {
      const params = new URLSearchParams(window.location.search);
      const browser = document.getElementById('preseason-players').closest('details');
      if (!browser.open) params.set('eligible_refresh', '0');
      if (!params.has('eligible_team') && root.dataset.team) params.set('eligible_team', root.dataset.team);
      const response = await fetch(root.dataset.endpoint + '?' + params.toString(), {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('Readiness refresh failed');
      const data = await response.json();
      const ready = data.readiness;
      document.getElementById('preseason-teams').replaceChildren(...ready.teams.map(team => row([
        team.name, team.new ? 'New team' : (team.final ? 'Final' : 'Pending'), team.retained,
        money(team.spent_lakh, data.currency), money(team.purse_lakh, data.currency),
        team.ranges.map(r => `${r.min_rating}–${r.max_rating}: ${r.count} (min ${r.min_count}, max ${r.max_count})`).join('; '), team.picks_left
      ])));
      document.getElementById('preseason-next').textContent = 'Next draft turn: ' + (ready.next_team || 'Waiting for retention / no picks remain');
      const left = ready.seconds_left === null ? 'waiting' : `${ready.seconds_left}s left`;
      document.getElementById('preseason-clock').textContent = ready.timer_seconds
        ? `Clock: ${ready.timer_paused ? 'paused' : left} · timeout: ${ready.timeout_action}` : 'Draft clock is off.';
      const matches = data.eligible;
      if (matches) {
      document.getElementById('preseason-matches').textContent = `${matches.total} matching players · page ${matches.page}/${matches.pages}`;
      document.getElementById('preseason-players').replaceChildren(...(matches.players.length
        ? matches.players.map(p => row([p.name, p.rating, p.role, p.country, money(p.price_lakh, data.currency)]))
        : [row(['No eligible players match.', '', '', '', ''])]));
      }
      status.textContent = 'Live · updates every 5 seconds.';
    } catch (_) {
      status.textContent = 'Live refresh unavailable; retrying.';
    }
    window.setTimeout(refresh, 5000);
  }
  refresh();
}());
