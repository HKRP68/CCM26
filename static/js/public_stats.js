(() => {
  'use strict';
  const root = document.querySelector('[data-stats-series]');
  if (!root) return;
  const base = `/api/stats/${root.dataset.statsSeries}/`;
  const query = `?season=${encodeURIComponent(root.dataset.statsSeason)}`;
  const byId = id => document.getElementById(id);
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
  let players = [], discipline = 'batting', liveTimer, recordsTimer, lastLiveSignature = '', lastRecordsSignature = '', livePending = false, recordsPending = false, refreshRecords = false;
  let liveIds = new Set();
  const overs = (balls, bpu = 6) => bpu === 5 ? `${balls}b` : `${Math.floor(balls / 6)}.${balls % 6}`;
  const rate = (runs, balls, factor) => balls ? (factor * runs / balls).toFixed(2) : '—';
  const table = (headings, rows) => {
    const t = el('table'), head = el('thead'), hr = el('tr'), body = el('tbody');
    headings.forEach(h => { const th = el('th', h); th.scope = 'col'; hr.append(th); }); head.append(hr); t.append(head);
    rows.forEach(row => { const tr = el('tr'); row.forEach(value => tr.append(el('td', value ?? '—'))); body.append(tr); });
    if (!rows.length) { const td = el('td', 'No qualifying statistics yet.'); td.colSpan = headings.length; const tr = el('tr'); tr.append(td); body.append(tr); }
    t.append(body); return t;
  };
  function renderLive(matches) {
    const panel = byId('live-matches'), opened = new Set([...panel.querySelectorAll('details[open]')].map(n => n.dataset.match));
    panel.replaceChildren();
    if (!matches.length) { panel.append(el('p', 'No innings in progress. Confirmed results and records are below.', 'empty')); return; }
    for (const m of matches) {
      const card = el('article', undefined, 'live-card'), meta = el('div', undefined, 'match-meta');
      meta.append(el('span', `Live · Innings ${m.innings}`), el('span', `Match #${m.id}`)); card.append(meta);
      if (m.first_innings) card.append(el('div', `${m.first_innings.team}  ${m.first_innings.runs}/${m.first_innings.wickets}`, 'previous-score'));
      const scoreline = el('div', undefined, 'score-line'), score = el('span', `${m.runs}/${m.wickets}`, 'score');
      score.append(el('small', ` (${overs(m.balls, m.balls_per_over)}${m.balls_per_over === 5 ? '' : ' ov'})`));
      scoreline.append(el('span', m.batting_team, 'team-name'), score); card.append(scoreline);
      card.append(el('div', `vs ${m.bowling_team}`, 'muted'));
      if (m.target) {
        const need = Math.max(0, m.target - m.runs), remaining = Math.max(0, m.overs * m.balls_per_over - m.balls);
        card.append(el('p', need ? `${m.batting_team} need ${need} from ${remaining} balls` : 'Target reached · result being confirmed', 'chase'));
      }
      const details = el('details'); details.dataset.match = String(m.id); details.open = opened.has(String(m.id)); details.append(el('summary', 'Live batting & bowling'));
      details.append(el('p', 'Batting', 'mini-title'), table(['Batter', 'R', 'B', '4s', '6s', 'SR'], m.batting.map(p => [p.name + (p.out ? '' : ' *'), p.runs, p.balls, p.fours, p.sixes, rate(p.runs,p.balls,100)])));
      details.append(el('p', 'Bowling', 'mini-title'), table(['Bowler', m.balls_per_over === 5 ? 'Balls' : 'Overs', 'R', 'W', 'Econ'], m.bowling.map(p => [p.name, overs(p.balls,m.balls_per_over), p.runs, p.wickets, rate(p.runs,p.balls,6)])));
      card.append(details); const link = el('a', 'Open full match →'); link.href = m.url; card.append(link); panel.append(card);
    }
  }
  function renderPlayers() {
    const search = byId('player-search').value.trim().toLowerCase();
    const list = players.filter(p => `${p.name} ${p.team || ''}`.toLowerCase().includes(search)).slice().sort((a,b) => discipline === 'batting' ? b.runs-a.runs : b.wickets-a.wickets || a.conceded-b.conceded);
    byId('player-table').replaceChildren(discipline === 'batting'
      ? table(['Player', 'Team', 'Matches', 'Runs', 'Balls', 'SR', '4s', '6s'], list.map(p => [p.name,p.team,p.matches,p.runs,p.balls,rate(p.runs,p.balls,100),p.fours,p.sixes]))
      : table(['Player', 'Team', 'Matches', 'Wickets', 'Overs', 'Runs conceded', 'Economy'], list.map(p => [p.name,p.team,p.matches,p.wickets,overs(p.bowled),p.conceded,rate(p.conceded,p.bowled,6)])));
  }
  function renderConfirmed(data) {
    byId('summary').replaceChildren(...[['matches','Scored matches'],['runs','Team runs'],['wickets','Bowler wickets'],['fours','Fours'],['sixes','Sixes']].map(([key,label]) => { const cell=el('div',undefined,'summary-cell'); cell.append(el('strong', Number(data.summary[key]).toLocaleString()),el('span',label)); return cell; }));
    players = data.players; renderPlayers();
    byId('record-cards').replaceChildren(...data.records.map(record => {
      const tile=el('article',undefined,'record-tile'); tile.append(el('h3',record.label));
      if (!record.holders.length) tile.append(el('p','No qualifying performance yet.','muted'));
      else { const value=el('div',record.holders[0].display,'record-value'); value.append(el('small',` ${record.unit}`)); tile.append(value); record.holders.forEach(holder => tile.append(el('p',holder.name,'holder'))); }
      return tile;
    }));
    const standings=byId('standings'); standings.hidden=!data.standings.length;
    if (data.standings.length) byId('standings-table').replaceChildren(table(['Team','P','W','L','Points','NRR'],data.standings.map(t => [t.name,t.played,t.won,t.lost,t.points,Number(t.nrr).toFixed(3)])));
    const results=data.matches.map(m => { const card=el('article',undefined,'result'); card.append(el('div',`Match #${m.id || '—'} · ${m.stage || 'League'}`,'muted'),el('p',`${m.team1 || 'Team 1'} ${m.runs1 ?? '—'} · ${m.team2 || 'Team 2'} ${m.runs2 ?? '—'}`,'teams'),el('p',m.result || 'Result recorded','result-text')); if(m.id){ const link=el('a','Scorecard →');link.href=`/cricket?match_id=${m.id}`;card.append(link); } return card; });
    byId('result-list').replaceChildren(...(results.length ? results : [el('p','No confirmed matches yet.','empty')]));
  }
  async function load(kind) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(base+kind+query,{cache:'no-store',signal:controller.signal});
      if(!response.ok) throw new Error('Feed unavailable');
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  async function pollLive() {
    if(document.hidden || livePending) return;
    livePending = true;
    try { const data=await load('live'), signature=JSON.stringify(data.matches);
      const currentIds=new Set(data.matches.map(m=>m.id));
      if([...liveIds].some(id=>!currentIds.has(id))){clearTimeout(recordsTimer);if(recordsPending) refreshRecords=true;else pollRecords();}
      liveIds=currentIds;
      if(signature!==lastLiveSignature){ renderLive(data.matches); lastLiveSignature=signature; }
      byId('feed-status').textContent=data.matches.length ? '● Live scores connected' : 'Scores up to date';
      byId('live-updated').textContent=`Last checked ${new Date().toLocaleTimeString()} · Following the latest ball / over.`;
    } catch (_) { byId('feed-status').textContent='Reconnecting · scores may be out of date'; }
    finally { livePending=false;liveTimer=setTimeout(pollLive,3000); }
  }
  async function pollRecords() {
    if(document.hidden || recordsPending) return;
    recordsPending = true;
    try { const data=await load('records'), signature=JSON.stringify({...data,updated_at:0});
      if(signature!==lastRecordsSignature){ renderConfirmed(data); lastRecordsSignature=signature; }
      byId('records-status').textContent=`Confirmed scorecards · refreshed ${new Date(data.updated_at*1000).toLocaleTimeString()}`;
    } catch (_) { byId('records-status').textContent='Stats temporarily unavailable · retrying'; }
    finally { recordsPending=false;recordsTimer=setTimeout(pollRecords,refreshRecords ? 0 : 30000);refreshRecords=false; }
  }
  byId('player-search').addEventListener('input',renderPlayers);
  document.querySelectorAll('[data-discipline]').forEach(button => button.addEventListener('click',()=>{discipline=button.dataset.discipline;document.querySelectorAll('[data-discipline]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderPlayers();}));
  document.addEventListener('visibilitychange',()=>{clearTimeout(liveTimer);clearTimeout(recordsTimer);if(!document.hidden){pollLive();pollRecords();}});
  pollLive();pollRecords();
})();
