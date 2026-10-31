(() => {
  'use strict';
  const root = document.querySelector('[data-stats-series]');
  if (!root) return;
  const base = `/api/stats/${root.dataset.statsSeries}/`;
  const season = root.dataset.statsSeason;
  const byId = id => document.getElementById(id);
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = text; if (cls) node.className = cls; return node; };
  const list = value => Array.isArray(value) ? value : [];
  const value = (n, digits) => n === null || n === undefined || n === '' ? '—' : typeof n === 'number' ? (digits === undefined ? n.toLocaleString() : n.toFixed(digits)) : String(n);
  const rate = (runs, balls, factor) => balls ? value(factor * runs / balls, 2) : '—';
  const overs = (balls, bpu = 6) => balls === null || balls === undefined ? '—' : bpu === 5 ? `${balls}b` : `${Math.floor(balls / 6)}.${balls % 6}`;
  const pretty = text => String(text || '').replace(/_/g, ' ').replace(/\b\w/g, m => m.toUpperCase());
  const date = raw => { if (!raw) return ''; const utc = typeof raw === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(raw) && !/(Z|[+-]\d{2}:\d{2})$/i.test(raw) ? raw + 'Z' : raw; const d = new Date(utc); return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, {day:'numeric',month:'short',year:'numeric'}); };
  const empty = text => el('p', text, 'empty');
  const link = (label, href) => { const node = el('a', label); if (typeof href === 'string' && href.startsWith('/') && !href.startsWith('//')) node.href = href; return node; };
  const profileButton = (name, type = 'player') => { const button = el('button', name || 'Unknown', 'text-link'); button.type = 'button'; button.dataset.profile = type; button.dataset.name = name || ''; return button; };
  const table = (headings, rows, message = 'No qualifying statistics yet.') => {
    const t = el('table'), head = el('thead'), hr = el('tr'), body = el('tbody');
    headings.forEach(h => { const th = el('th', h); th.scope = 'col'; hr.append(th); }); head.append(hr); t.append(head);
    rows.forEach(row => { const tr = el('tr'); row.forEach(v => { const td = el('td'); if (v instanceof Node) td.append(v); else td.textContent = value(v); tr.append(td); }); body.append(tr); });
    if (!rows.length) { const td = el('td', message); td.colSpan = headings.length; const tr = el('tr'); tr.append(td); body.append(tr); }
    t.append(body); return t;
  };
  const wrappedTable = (headings, rows, message) => { const wrap = el('div', undefined, 'table-wrap'); wrap.append(table(headings, rows, message)); return wrap; };
  const metrics = entries => { const grid = el('div', undefined, 'metric-grid'); entries.forEach(([label, val]) => { const box = el('div', undefined, 'metric'); box.append(el('strong', value(val)), el('span', label)); grid.append(box); }); return grid; };
  const formBadges = form => { const box = el('div', undefined, 'form-badges'); list(form).slice(-5).forEach(result => box.append(el('span', result, `form-badge ${String(result).toLowerCase()}`))); return box; };
  const section = (title, content) => { const s = el('section', undefined, 'profile-section'); s.append(el('h3', title), content); return s; };
  const statsEntries = stats => [['Matches',stats.matches],['Runs',stats.runs],['Average',value(stats.average,2)],['Strike rate',value(stats.sr,2)],['Wickets',stats.wickets],['Economy',value(stats.economy,2)]];
  let players = [], records = [], fixtures = [], discipline = 'batting', fixtureFilter = 'upcoming', teamNames = new Set();
  let liveTimer, recordsTimer, portalTimer, livePending = false, recordsPending = false, portalPending = false, refreshRecords = false, refreshPortal = false;
  let lastLiveSignature = '', lastRecordsSignature = '', lastPortalSignature = '', liveIds = new Set();
  let explorerPage = 1, explorerTotal = 0, explorerSize = 20, explorerRequest = 0, explorerController;
  let profileRequest = 0, profileController;

  function renderLive(matches) {
    const panel = byId('live-matches'), opened = new Set([...panel.querySelectorAll('details[open]')].map(n => n.dataset.match));
    panel.replaceChildren();
    if (!matches.length) { panel.append(empty('No innings in progress. Explore the next fixtures and confirmed results below.')); return; }
    matches.forEach(m => {
      const card = el('article', undefined, 'live-card'), meta = el('div', undefined, 'match-meta');
      meta.append(el('span', `Live · Innings ${m.innings}`), el('span', `Match #${m.id}`)); card.append(meta);
      if (m.first_innings) card.append(el('div', `${m.first_innings.team}  ${m.first_innings.runs}/${m.first_innings.wickets}`, 'previous-score'));
      const line = el('div', undefined, 'score-line'), score = el('span', `${m.runs}/${m.wickets}`, 'score');
      score.append(el('small', ` (${overs(m.balls,m.balls_per_over)}${m.balls_per_over === 5 ? '' : ' ov'})`));
      line.append(profileButton(m.batting_team,'team'),score);card.append(line,el('div',`vs ${m.bowling_team}`,'muted'));
      if (m.target) { const need = Math.max(0,m.target-m.runs), remaining = Math.max(0,m.overs*m.balls_per_over-m.balls); card.append(el('p',need ? `${m.batting_team} need ${need} from ${remaining} balls` : 'Target reached · result being confirmed','chase')); }
      const rates=el('div',undefined,'run-rates');if(m.run_rate!==undefined)rates.append(el('span',`Run rate ${value(m.run_rate,2)}`));if(m.required_rate!==undefined&&m.required_rate!==null)rates.append(el('span',`Required ${value(m.required_rate,2)}`));if(rates.childNodes.length)card.append(rates);
      const details = el('details');details.dataset.match=String(m.id);details.open=opened.has(String(m.id));details.append(el('summary','Live batting & bowling'));
      details.append(el('p','Batting','mini-title'),wrappedTable(['Batter','R','B','4s','6s','SR'],list(m.batting).map(p=>[profileButton(p.name),p.runs,p.balls,p.fours,p.sixes,rate(p.runs,p.balls,100)])));
      details.append(el('p','Bowling','mini-title'),wrappedTable(['Bowler',m.balls_per_over===5?'Balls':'Overs','R','W','Econ'],list(m.bowling).map(p=>[profileButton(p.name),overs(p.balls,m.balls_per_over),p.runs,p.wickets,rate(p.runs,p.balls,6)])));
      card.append(details);
      if(list(m.commentary).length){const commentary=el('details');commentary.dataset.match=`commentary-${m.id}`;commentary.open=opened.has(commentary.dataset.match);commentary.append(el('summary','Latest commentary'));const lines=el('div',undefined,'commentary-list');list(m.commentary).slice(-12).reverse().forEach(item=>{const row=el('div',undefined,item.is_wicket?'commentary-line wicket':'commentary-line');row.append(el('span',value(item.over),'ball'),el('span',item.text||item.name||''));lines.append(row);});commentary.append(lines);card.append(commentary);}
      card.append(link('Open full match →',m.url));panel.append(card);
    });
  }
  function playerRows(rows, mode) {
    return mode === 'batting'
      ? rows.map(p=>[profileButton(p.name),p.team,p.matches,p.runs,p.balls,value(p.average,2),value(p.sr,2) === '—' ? rate(p.runs,p.balls,100) : value(p.sr,2),p.fours,p.sixes])
      : rows.map(p=>[profileButton(p.name),p.team,p.matches,p.wickets,overs(p.bowled),p.conceded,value(p.economy,2) === '—' ? rate(p.conceded,p.bowled,6) : value(p.economy,2)]);
  }
  function renderPlayers() {
    const search = byId('player-search').value.trim().toLowerCase();
    const rows = players.filter(p=>`${p.name} ${p.team||''}`.toLowerCase().includes(search)).slice().sort((a,b)=>discipline==='batting'?b.runs-a.runs:b.wickets-a.wickets||a.conceded-b.conceded);
    byId('player-table').replaceChildren(table(discipline==='batting'?['Player','Team','Matches','Runs','Balls','Average','SR','4s','6s']:['Player','Team','Matches','Wickets','Overs','Runs conceded','Economy'],playerRows(rows,discipline)));
  }
  function renderRecords() {
    const search = byId('record-search').value.trim().toLowerCase(), rows = records.filter(r=>`${r.label} ${list(r.holders).map(h=>h.name).join(' ')}`.toLowerCase().includes(search));
    byId('record-cards').replaceChildren(...rows.map(record=>{
      const tile=el('article',undefined,'record-tile');tile.append(el('h3',record.label));
      if (!list(record.holders).length) tile.append(el('p','No qualifying performance yet.','muted'));
      else { const val=el('div',record.holders[0].display,'record-value');val.append(el('small',` ${record.unit||''}`));tile.append(val);record.holders.forEach(holder=>{const p=el('p',undefined,'holder');p.append(profileButton(holder.name,teamNames.has(holder.name)?'team':'player'));tile.append(p);}); }
      return tile;
    }));
    if (!rows.length) byId('record-cards').append(empty('No matching records.'));
  }
  function fixtureCard(m, compact=false) {
    const card=el('article',undefined,compact?'fixture-card compact':'fixture-card'),meta=el('div',undefined,'fixture-meta');
    const status=String(m.status||'scheduled').toLowerCase();meta.append(el('span',pretty(m.stage||'League')),el('span',pretty(status),`status ${status}`));card.append(meta);
    const context=[m.season,date(m.date),m.round_no?`Round ${m.round_no}`:''].filter(Boolean).join(' · ');if(context)card.append(el('p',context,'muted fixture-context'));
    const a=el('div',undefined,'fixture-side'),b=el('div',undefined,'fixture-side');a.append(profileButton(m.team1,'team'),el('strong',value(m.runs1)));b.append(profileButton(m.team2,'team'),el('strong',value(m.runs2)));card.append(a,b);
    if(m.result)card.append(el('p',m.result,'result-text'));else card.append(el('p',status==='live'||status==='playing'?'Play in progress':'Awaiting the first ball','muted'));
    const location=[m.venue,m.pitch].filter(Boolean).join(' · ');if(location)card.append(el('p',location,'muted'));
    if(m.url)card.append(link(status==='completed'?'Scorecard →':'Match centre →',m.url));return card;
  }
  function renderFixtures() {
    const status=m=>String(m.status||'scheduled').toLowerCase();
    const rows=fixtures.filter(m=>fixtureFilter==='all'||(fixtureFilter==='upcoming'?!['completed','cancelled','abandoned','live','playing','active'].includes(status(m)):fixtureFilter==='live'?['live','playing','active'].includes(status(m)):['completed','abandoned','cancelled'].includes(status(m))));
    byId('fixture-list').replaceChildren(...rows.map(m=>fixtureCard(m)));
    if(!rows.length)byId('fixture-list').append(empty(fixtureFilter==='upcoming'?'No upcoming fixtures in this selection.':fixtureFilter==='live'?'No fixtures are live right now.':'No fixtures match this view.'));
  }
  function renderStandings(rows) {
    byId('standings-table').replaceChildren(table(['#','Team','P','W','L','T/NR','Points','NRR'],list(rows).map((t,i)=>[i+1,profileButton(t.name,'team'),t.played,t.won,t.lost,Number(t.tied||0)+Number(t.no_result||0),t.points,value(t.nrr,3)]),'A points table is available when a season has recorded standings.'));
  }
  function renderCaps(tournament) {
    const caps=byId('cap-leaders');caps.replaceChildren();
    [['orange_cap','Orange Cap','Runs','orange'],['purple_cap','Purple Cap','Wickets','purple']].forEach(([key,title,label,color])=>{
      const card=el('article',undefined,`cap-card ${color}`);card.append(el('p',title,'cap-label'));
      const leaders=list(tournament[key]);if(!leaders.length)card.append(el('h3','The race is open'),el('p','No qualifying performance yet.','muted'));
      else {const score=el('strong',value(leaders[0].value),'cap-value');score.append(el('small',` ${label}`));card.append(score);leaders.forEach(p=>{const line=el('p');line.append(profileButton(p.name));if(p.team)line.append(el('span',` · ${p.team}`,'muted'));card.append(line);});}
      caps.append(card);
    });
  }
  function renderPortal(data) {
    fixtures=list(data.fixtures);teamNames=new Set(list(data.teams).map(t=>t.name));renderFixtures();renderRecords();
    byId('news-list').replaceChildren(...list(data.news).slice(0,6).map((story,index)=>{const card=el('article',undefined,index===0?'news-card lead-story':'news-card');card.append(el('span',pretty(story.kind||'Match news'),'story-kind'),el('h3',story.title||'Competition update'));if(story.body)card.append(el('p',story.body,'story-body'));const bottom=el('div',undefined,'story-footer');if(story.date)bottom.append(el('span',date(story.date)));if(story.url)bottom.append(link('Match report →',story.url));if(story.article_id){const read=el('button','Read story →','text-link');read.type='button';read.dataset.article=story.article_id;bottom.append(read);}card.append(bottom);return card;}));
    if(!list(data.news).length)byId('news-list').append(empty('Match victories and recorded competition updates will appear here.'));
    const tournament=data.tournament||{};renderCaps(tournament);if(tournament.standings)renderStandings(tournament.standings);
    const playoffs=list(tournament.playoffs);byId('playoff-list').replaceChildren(...playoffs.map(m=>fixtureCard(m,true)));if(!playoffs.length)byId('playoff-list').append(empty('Playoff fixtures will appear once they are scheduled.'));
    byId('team-cards').replaceChildren(...list(data.teams).map(team=>{const card=el('article',undefined,'team-card'),top=el('div',undefined,'team-card-heading');const mark=el('span',String(team.name||'?').slice(0,2).toUpperCase(),'team-monogram');if(typeof team.logo_url==='string'&&team.logo_url.startsWith('/')&&!team.logo_url.startsWith('//')){const img=el('img');img.src=team.logo_url;img.alt='';img.loading='lazy';img.addEventListener('error',()=>img.remove());mark.replaceChildren(img);}top.append(mark,profileButton(team.name,'team'));card.append(top);if(team.captain)card.append(el('p',`${team.captain_role||'Team manager'}: ${team.captain}`,'muted'));card.append(metrics([['Played',team.played],['Won',team.won],['Lost',team.lost]]),formBadges(team.form));const more=profileButton(team.name,'team');more.textContent='Team dashboard →';more.classList.add('card-action');card.append(more);return card;}));
    if(!list(data.teams).length)byId('team-cards').append(empty('Teams will appear once squads or match records are available.'));
    byId('venue-cards').replaceChildren(...list(data.venues).map(venue=>{const card=el('article',undefined,'venue-card');card.append(el('span','GROUND GUIDE','eyebrow'),profileButton(venue.name,'venue'),metrics([['Matches',venue.matches],['Avg. first innings',value(venue.average_first_innings,1)],['Highest total',venue.highest_total]]));const more=profileButton(venue.name,'venue');more.textContent='Explore ground →';more.classList.add('card-action');card.append(more);return card;}));
    if(!list(data.venues).length)byId('venue-cards').append(empty('Ground statistics appear when venues are recorded on scorecards.'));
  }
  function renderConfirmed(data) {
    const summary=data.summary||{};byId('summary').replaceChildren(...[['matches','Scored matches'],['runs','Team runs'],['wickets','Bowler wickets'],['fours','Fours'],['sixes','Sixes']].map(([key,label])=>{const box=el('div',undefined,'summary-cell');box.append(el('strong',value(summary[key])),el('span',label));return box;}));
    players=list(data.players);records=list(data.records);renderPlayers();renderRecords();if(list(data.standings).length)renderStandings(data.standings);
    const results=list(data.matches).map(m=>fixtureCard({...m,status:'completed',match_id:m.id,url:m.id?`/cricket?match_id=${m.id}`:null},true));byId('result-list').replaceChildren(...(results.length?results:[empty('No confirmed matches yet.')]));
  }
  async function load(kind, params={}, signal) {
    const query=new URLSearchParams({season,...params}),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
    const abort=()=>controller.abort();if(signal)signal.addEventListener('abort',abort,{once:true});
    try{const endpoint=kind==='article'?`/api/stats/news/${encodeURIComponent(params.id)}`:`${base}${kind}?${query}`;const response=await fetch(endpoint,{cache:'no-store',signal:controller.signal});if(!response.ok)throw new Error(response.status===404?'This profile is not available for the selected competition.':'The cricket feed is temporarily unavailable.');const data=await response.json();if(data.ok===false)throw new Error(data.message||'No recorded data is available.');return data;}
    finally{clearTimeout(timer);if(signal)signal.removeEventListener('abort',abort);}
  }
  async function pollLive() {
    if(document.hidden||livePending)return;livePending=true;
    try{const data=await load('live'),matches=list(data.matches),signature=JSON.stringify(matches),ids=new Set(matches.map(m=>m.id));
      if([...liveIds].some(id=>!ids.has(id))){clearTimeout(recordsTimer);clearTimeout(portalTimer);if(recordsPending)refreshRecords=true;else pollRecords();if(portalPending)refreshPortal=true;else pollPortal();}liveIds=ids;
      if(signature!==lastLiveSignature){renderLive(matches);lastLiveSignature=signature;}byId('feed-status').textContent=matches.length?'● Live scores connected':'Scores up to date';byId('live-updated').textContent=`Last checked ${new Date().toLocaleTimeString()} · Following the latest ball / over.`;
    }catch(_){byId('feed-status').textContent='Reconnecting · scores may be out of date';}finally{livePending=false;liveTimer=setTimeout(pollLive,3000);}
  }
  async function pollRecords() {
    if(document.hidden||recordsPending)return;recordsPending=true;
    try{const data=await load('records'),signature=JSON.stringify({...data,updated_at:0});if(signature!==lastRecordsSignature){renderConfirmed(data);lastRecordsSignature=signature;}byId('records-status').textContent=`Confirmed scorecards · ${new Date((data.updated_at||Date.now()/1000)*1000).toLocaleTimeString()}`;}
    catch(_){byId('records-status').textContent='Stats temporarily unavailable · retrying';}finally{recordsPending=false;recordsTimer=setTimeout(pollRecords,refreshRecords?0:30000);refreshRecords=false;}
  }
  async function pollPortal() {
    if(document.hidden||portalPending)return;portalPending=true;
    try{const data=await load('portal'),signature=JSON.stringify({...data,updated_at:0});if(signature!==lastPortalSignature){renderPortal(data);lastPortalSignature=signature;}byId('portal-status').textContent='Schedule and competition updates refresh automatically.';}
    catch(_){byId('portal-status').textContent='Schedule and competition updates temporarily unavailable · retrying';}finally{portalPending=false;portalTimer=setTimeout(pollPortal,refreshPortal?0:30000);refreshPortal=false;}
  }
  function fillFilters(options) {
    [['team','teams','All teams'],['opponent','opponents','Any opponent'],['pitch','pitches','Any pitch'],['venue','venues','Any ground'],['stage','stages','Any stage']].forEach(([name,key,label])=>{const select=byId('explorer-form').elements.namedItem(name),current=select.value;select.replaceChildren(new Option(label,''));list(options[key]).forEach(item=>{const text=typeof item==='object'?(item.name||item.label||item.value):item;select.add(new Option(text,text));});select.value=current;});
  }
  async function searchExplorer(page=1) {
    const request=++explorerRequest;if(explorerController)explorerController.abort();explorerController=new AbortController();
    byId('explorer-status').textContent='Searching recorded performances…';byId('explorer-prev').disabled=true;byId('explorer-next').disabled=true;
    const params=Object.fromEntries(new FormData(byId('explorer-form')));params.page=page;params.limit=20;
    try{const data=await load('explorer',params,explorerController.signal);if(request!==explorerRequest)return;fillFilters(data.filters||{});explorerPage=data.page||page;explorerSize=data.page_size||20;explorerTotal=data.total||0;
      const bowling=params.discipline==='bowling',rows=list(data.rows);byId('explorer-table').replaceChildren(table(bowling?['Player','Team','Matches','Wickets','Overs','Conceded','Economy','OVR','BOWL']:['Player','Team','Matches','Runs','Balls','Outs','Average','SR','OVR','BAT'],rows.map(p=>bowling?[profileButton(p.name),p.team,p.matches,p.wickets,overs(p.bowled),p.conceded,value(p.economy,2),p.ovr,p.bowl]:[profileButton(p.name),p.team,p.matches,p.runs,p.balls,p.outs,value(p.average,2),value(p.sr,2),p.ovr,p.bat]),'No performances match these filters.'));
      const sum=data.summary||{};byId('explorer-status').textContent=`${value(explorerTotal)} players · ${value(sum.matches)} matches · ${value(sum.runs)} runs · ${value(sum.wickets)} wickets`;if((data.coverage||{}).aggregate_only_seasons)byId('explorer-status').textContent+=' · Includes historical season totals; match-level detail is unavailable for those seasons.';byId('explorer-page').textContent=`Page ${explorerPage} of ${Math.max(1,Math.ceil(explorerTotal/explorerSize))}`;byId('explorer-prev').disabled=explorerPage<=1;byId('explorer-next').disabled=explorerPage*explorerSize>=explorerTotal;
    }catch(error){if(request===explorerRequest&&error.name!=='AbortError'){byId('explorer-status').textContent='Search is temporarily unavailable. Try again.';byId('explorer-table').replaceChildren(empty('Use Explore records to retry.'));}}
  }
  function phaseTable(phases) {
    return wrappedTable(['Discipline','Phase','Runs','Balls','Wickets','SR','Economy'],list(phases).map(p=>[pretty(p.side),pretty(p.phase),p.runs,p.balls,p.wickets,value(p.sr,2),value(p.economy,2)]),'Phase detail is unavailable in these scorecards.');
  }
  function recordList(rows) {
    const box=el('div',undefined,'profile-records');list(rows).forEach(r=>{const row=el('div');row.append(el('span',r.label),el('strong',`${value(r.value)} ${r.unit||''}`));box.append(row);});if(!list(rows).length)box.append(empty('No individual record marks in this selection.'));return box;
  }
  function renderPlayerProfile(data) {
    const body=byId('profile-body'),id=data.identity||{};body.replaceChildren();const bio=el('div',undefined,'profile-bio');bio.append(el('p',[id.role,id.country,id.version].filter(Boolean).join(' · ')||'Player profile','muted'),metrics([['OVR',id.ovr],['BAT',id.bat],['BOWL',id.bowl]]));body.append(bio);
    if(id.ambiguous)body.append(el('p','Several player versions share this name. Ratings are shown only when the identity is certain.','notice'));
    if(data.roster_only)body.append(el('p','Registered squad member · no recorded appearances yet.','notice'));
    if((data.coverage||{}).career_aggregate_only_seasons>0)body.append(el('p','Career totals include historical season summaries. Individual match, opponent, pitch and phase details are unavailable for those seasons.','notice'));
    body.append(section(season==='all'?'All linked seasons':'Selected season',metrics(statsEntries(data.selected||{}))),section('Career · all linked seasons',metrics(statsEntries(data.career||{}))));
    const form=data.form||{};body.append(section('Recent form',metrics([['Recent matches',form.matches],['Recent runs',form.recent_runs],['Recent wickets',form.recent_wickets]])));
    body.append(section('Recent performances',wrappedTable(['Date','Opponent','Ground','Batting','Bowling','Scorecard'],list(data.recent).map(r=>[date(r.date)||'—',r.opponent,r.venue,r.runs===null||r.runs===undefined?'—':`${r.runs} (${value(r.balls)})`,r.wickets===null||r.wickets===undefined?'—':`${r.wickets}/${value(r.conceded)}`,r.url?link('View →',r.url):'—']))));
    body.append(section('Personal records',recordList(data.records)),section('Phase-wise performance',phaseTable(data.phases)));
    const splits=data.splits||{};
    [['opponent','By opponent'],['pitch','By pitch'],['venue','By ground']].forEach(([key,title])=>{
      const rows=list(splits[key]).map(r=>{const s=r.stats||{};return[r.name,s.matches,s.runs,value(s.average,2),value(s.sr,2),s.wickets,value(s.economy,2)];});
      body.append(section(title,wrappedTable(['Split','Matches','Runs','Average','SR','Wickets','Economy'],rows)));
    });
  }
  function renderTeamProfile(data) {
    const body=byId('profile-body');body.replaceChildren();body.append(el('p',data.captain?`${data.captain_role||'Team manager'}: ${data.captain}`:'Team manager information has not been recorded.','profile-subtitle'));
    const p=data.performance||{};body.append(metrics([['Played',p.played],['Won',p.won],['Lost',p.lost],['Win %',value(p.win_percentage,1)],['Runs',p.runs],['Wickets',p.wickets]]),formBadges(p.form));
    if(p.played&&p.innings_with_scores<p.played)body.append(el('p',`Scores are recorded for ${value(p.innings_with_scores)} of ${value(p.played)} completed matches. Complete team totals are unavailable.`,'notice'));
    body.append(section('Squad & ratings',wrappedTable(['Player','Role','Country','OVR','BAT','BOWL','Form'],list(data.squad).map(r=>[profileButton(r.name),r.role,r.country,r.ovr,r.bat,r.bowl,value(r.form)]),'No squad has been recorded for this team.')));if(data.squad_source)body.append(el('p',data.squad_source==='recorded_appearances'?'Players with recorded appearances; a full historical squad is unavailable.':'Current registered squad.','muted'));
    body.append(section('Team records',recordList(data.records)));
    [['Upcoming fixtures',data.upcoming_fixtures],['Recent results',data.recent_results]].forEach(([title,rows])=>{const box=el('div',undefined,'fixture-grid');box.append(...list(rows).map(m=>fixtureCard(m,true)));if(!list(rows).length)box.append(empty('No fixtures in this view.'));body.append(section(title,box));});
    if(list(data.injuries).length)body.append(section('Injury report',wrappedTable(['Player','Recorded injury','Matches remaining'],data.injuries.map(r=>[profileButton(r.player),r.injury,r.matches_remaining]))));
  }
  function renderVenueProfile(data) {
    const body=byId('profile-body'),s=data.summary||{};body.replaceChildren(metrics([['Matches',s.matches],['Innings',s.innings],['Runs',s.runs],['Avg. first innings',value(s.average_first_innings,1)],['Highest total',s.highest_total],['Successful chases',s.successful_chases]]));
    body.append(section('Teams at this ground',wrappedTable(['Team','Matches','Runs','Wins'],list(data.teams).map(t=>[profileButton(t.name,'team'),t.matches,t.runs,t.wins]))),section('Pitch statistics',wrappedTable(['Pitch','Matches','Runs'],list(data.pitches).map(p=>[p.name,p.matches,p.runs]))),section('Leading player performances',wrappedTable(['Player','Team','Matches','Runs','SR','Wickets','Economy'],list(data.players).map(p=>[profileButton(p.name),p.team,p.matches,p.runs,value(p.sr,2),p.wickets,value(p.economy,2)]))),section('Phase-wise scoring',phaseTable(data.phases)));
  }
  function renderArticle(data){const body=byId('profile-body');body.replaceChildren(el('p',[data.author,date(data.date)].filter(Boolean).join(' · '),'profile-subtitle'));if(typeof data.image_url==='string'&&data.image_url.startsWith('/')&&!data.image_url.startsWith('//')){const image=el('img',undefined,'article-image');image.src=data.image_url;image.alt='';image.loading='lazy';body.append(image);}body.append(el('div',data.body||'','article-body'));}
  async function openProfile(type,name) {
    if(!name)return;const dialog=byId('profile-dialog'),request=++profileRequest;if(profileController)profileController.abort();profileController=new AbortController();
    byId('profile-kind').textContent=type==='article'?'COMPETITION NEWS':type==='venue'?'GROUND & PITCH GUIDE':`${type.toUpperCase()} PROFILE`;byId('profile-title').textContent=name;byId('profile-body').replaceChildren(empty('Loading recorded performances…'));if(!dialog.open)dialog.showModal();
    try{const data=await load(type,type==='article'?{id:name}:{name},profileController.signal);if(request!==profileRequest||!dialog.open)return;byId('profile-title').textContent=data.title||data.name||name;if(type==='article')renderArticle(data);else if(type==='player')renderPlayerProfile(data);else if(type==='team')renderTeamProfile(data);else renderVenueProfile(data);}
    catch(error){if(request===profileRequest&&dialog.open&&error.name!=='AbortError')byId('profile-body').replaceChildren(empty(error.message||'This profile is temporarily unavailable.'));}
  }
  byId('profile-close').addEventListener('click',()=>byId('profile-dialog').close());
  byId('profile-dialog').addEventListener('close',()=>{profileRequest++;if(profileController)profileController.abort();});
  document.addEventListener('click',event=>{const button=event.target.closest('button[data-profile]');if(button)openProfile(button.dataset.profile,button.dataset.name);const article=event.target.closest('button[data-article]');if(article)openProfile('article',article.dataset.article);});
  byId('player-search').addEventListener('input',renderPlayers);byId('record-search').addEventListener('input',renderRecords);
  document.querySelectorAll('[data-discipline]').forEach(button=>button.addEventListener('click',()=>{discipline=button.dataset.discipline;document.querySelectorAll('[data-discipline]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderPlayers();}));
  document.querySelectorAll('[data-fixtures]').forEach(button=>button.addEventListener('click',()=>{fixtureFilter=button.dataset.fixtures;document.querySelectorAll('[data-fixtures]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderFixtures();}));
  byId('explorer-form').addEventListener('submit',event=>{event.preventDefault();searchExplorer(1);});byId('explorer-form').addEventListener('reset',()=>setTimeout(()=>searchExplorer(1),0));
  byId('explorer-prev').addEventListener('click',()=>searchExplorer(explorerPage-1));byId('explorer-next').addEventListener('click',()=>searchExplorer(explorerPage+1));
  document.addEventListener('visibilitychange',()=>{clearTimeout(liveTimer);clearTimeout(recordsTimer);clearTimeout(portalTimer);if(!document.hidden){pollLive();pollRecords();pollPortal();}});
  pollLive();pollRecords();pollPortal();searchExplorer(1);
})();
