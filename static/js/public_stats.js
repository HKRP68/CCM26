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
  const empty = text => el('p', text, /^Loading/i.test(text)?'empty busy-state':'empty');
  const link = (label, href) => { const node = el('a', label); if (typeof href === 'string' && href.startsWith('/') && !href.startsWith('//')) node.href = href; return node; };
  const playerImages = new Map(), teamImages = new Map();
  const nameKey = name => String(name || '').trim().toLowerCase();
  const mediaURL = src => typeof src === 'string' && ((src.startsWith('/') && !src.startsWith('//')) || /^https?:\/\//i.test(src));
  function avatar(name, type='player', src, large=false) {
    const wrap=el('span',undefined,`entity-avatar ${type} ${large?'large':''}`);
    const source=src||(type==='team'?teamImages:playerImages).get(nameKey(name));
    const initial=el('span',String(name||'?').split(/\s+/).map(n=>n[0]).join('').slice(0,2).toUpperCase(),'avatar-initial');
    if(mediaURL(source)||type==='player'){
      const img=el('img');img.src=mediaURL(source)?source:'/static/images/card-placeholder.svg';img.alt='';img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';
      img.addEventListener('error',()=>{if(type==='player'&&!img.src.endsWith('card-placeholder.svg'))img.src='/static/images/card-placeholder.svg';else wrap.replaceChildren(initial);},{once:true});if(large&&type==='player'&&mediaURL(source)){const full=el('a');full.href=source;full.target='_blank';full.rel='noopener noreferrer';full.setAttribute('aria-label',`Open ${name} full player card`);full.append(img);wrap.append(full);}else wrap.append(img);
    }else wrap.append(initial);
    return wrap;
  }
  const profileButton = (name, type = 'player') => { const button = el('button',undefined,'text-link entity-link');button.append(avatar(name,type),el('span',name||'Unknown'));button.type='button';button.dataset.profile=type;button.dataset.name=name||'';return button; };
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
    renderSpotlight(rows);
    byId('player-table').replaceChildren(table(discipline==='batting'?['Player','Team','Matches','Runs','Balls','Average','SR','4s','6s']:['Player','Team','Matches','Wickets','Overs','Runs conceded','Economy'],playerRows(rows,discipline)));
  }
  function renderRecords() {
    const search = byId('record-search').value.trim().toLowerCase(), rows = records.filter(r=>`${r.label} ${list(r.holders).map(h=>h.name).join(' ')}`.toLowerCase().includes(search));
    byId('record-cards').replaceChildren(...rows.map(record=>{
      const tile=el('article',undefined,'record-tile'),title=el('h3'),open=el('button',record.label,'record-title-button');open.type='button';open.dataset.record=record.key;title.append(open);tile.append(title);
      if (!list(record.holders).length) tile.append(el('p','No qualifying performance yet.','muted'));
      else { const val=el('div',record.holders[0].display,'record-value');val.append(el('small',` ${record.unit||''}`));tile.append(val);record.holders.forEach(holder=>{const p=el('p',undefined,'holder');p.append(profileButton(holder.name,teamNames.has(holder.name)?'team':'player'));tile.append(p);}); }
      const more=el('button','View all rankings →','record-open');more.type='button';more.dataset.record=record.key;more.setAttribute('aria-label',`View all ${record.label.toLowerCase()}`);tile.append(more);
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
    list(data.teams).forEach(t=>teamImages.set(nameKey(t.name),t.logo_url));
    fixtures=list(data.fixtures);teamNames=new Set(list(data.teams).map(t=>t.name));renderFixtures();renderRecords();
    byId('news-list').replaceChildren(...list(data.news).slice(0,6).map((story,index)=>{const card=el('article',undefined,index===0?'news-card lead-story':'news-card');card.append(el('span',pretty(story.kind||'Match news'),'story-kind'),el('h3',story.title||'Competition update'));if(story.body)card.append(el('p',story.body,'story-body'));const bottom=el('div',undefined,'story-footer');if(story.date)bottom.append(el('span',date(story.date)));if(story.url)bottom.append(link('Match report →',story.url));if(story.article_id){const read=el('button','Read story →','text-link');read.type='button';read.dataset.article=story.article_id;bottom.append(read);}card.append(bottom);return card;}));
    if(!list(data.news).length)byId('news-list').append(empty('Match victories and recorded competition updates will appear here.'));
    const tournament=data.tournament||{};renderCaps(tournament);
    if(list(tournament.standings).length)renderStandings(tournament.standings);
    else if(list(tournament.tables).length){const tables=tournament.tables,box=byId('standings-table'),select=el('select');select.setAttribute('aria-label','Points table season');tables.slice().reverse().forEach((t,i)=>select.add(new Option(t.season,i)));const holder=el('div');const draw=()=>{const t=tables.slice().reverse()[Number(select.value)];holder.replaceChildren(table(['Team','P','W','L','Points','NRR'],list(t.standings).map(r=>[profileButton(r.name,'team'),r.played,r.won,r.lost,r.points,value(r.nrr,3)]),'No retained table for this season.'));};select.addEventListener('change',draw);box.replaceChildren(select,holder);draw();}
    else renderStandings([]);
    const playoffs=list(tournament.playoffs);byId('playoff-list').replaceChildren(...playoffs.map(m=>fixtureCard(m,true)));if(!playoffs.length)byId('playoff-list').append(empty('Playoff fixtures will appear once they are scheduled.'));
    byId('team-cards').replaceChildren(...list(data.teams).map(team=>{const card=el('article',undefined,'team-card'),top=el('div',undefined,'team-card-heading');const mark=avatar(team.name,'team',team.logo_url,true);top.append(mark,profileButton(team.name,'team'));card.append(top);if(team.captain)card.append(el('p',`${team.captain_role||'Team manager'}: ${team.captain}`,'muted'));card.append(metrics([['Played',team.played],['Won',team.won],['Lost',team.lost]]),formBadges(team.form));const more=profileButton(team.name,'team');more.textContent='Team dashboard →';more.classList.add('card-action');card.append(more);return card;}));
    if(!list(data.teams).length)byId('team-cards').append(empty('Teams will appear once squads or match records are available.'));
    byId('venue-cards').replaceChildren(...list(data.venues).map(venue=>{const card=el('article',undefined,'venue-card');card.append(el('span','GROUND GUIDE','eyebrow'),profileButton(venue.name,'venue'),metrics([['Matches',venue.matches],['Avg. first innings',value(venue.average_first_innings,1)],['Highest total',venue.highest_total]]));const more=profileButton(venue.name,'venue');more.textContent='Explore ground →';more.classList.add('card-action');card.append(more);return card;}));
    comparisonOptions();renderPlayers();renderSeasons();
    if(!list(data.venues).length)byId('venue-cards').append(empty('Ground statistics appear when venues are recorded on scorecards.'));
  }
  function renderConfirmed(data) {
    const summary=data.summary||{};byId('summary').replaceChildren(...[['matches','Scored matches'],['runs','Team runs'],['wickets','Bowler wickets'],['fours','Fours'],['sixes','Sixes']].map(([key,label])=>{const box=el('div',undefined,'summary-cell');box.append(el('strong',value(summary[key])),el('span',label));return box;}));
    players=list(data.players);players.forEach(p=>playerImages.set(nameKey(p.name),p.image));records=list(data.records);renderPlayers();comparisonOptions();renderRecords();if(list(data.standings).length)renderStandings(data.standings);
    const results=list(data.matches).map(m=>fixtureCard({...m,status:'completed',match_id:m.id,url:m.id?`/cricket?match_id=${m.id}`:null},true));byId('result-list').replaceChildren(...(results.length?results:[empty('No confirmed matches yet.')]));
  }
  async function load(kind, params={}, signal) {
    const query=new URLSearchParams({season,...params}),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
    const abort=()=>controller.abort();if(signal)signal.addEventListener('abort',abort,{once:true});
    try{const endpoint=kind==='article'?`/api/stats/news/${encodeURIComponent(params.id)}`:`${base}${kind}?${query}`;const response=await fetch(endpoint,{cache:'no-store',signal:controller.signal});if(!response.ok){let detail;try{detail=await response.json();}catch(_){}throw new Error((detail&&detail.error)||(response.status===404?'This profile is not available for the selected competition.':'The cricket feed is temporarily unavailable.'));}const data=await response.json();if(data.ok===false)throw new Error(data.message||'No recorded data is available.');return data;}
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
    try{const data=await load('records'),signature=JSON.stringify({...data,updated_at:0});if(signature!==lastRecordsSignature){renderConfirmed(data);loadSeasons();lastRecordsSignature=signature;}byId('records-status').textContent=`Confirmed scorecards · ${new Date((data.updated_at||Date.now()/1000)*1000).toLocaleTimeString()}`;}
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
    const body=byId('profile-body'),id=data.identity||{};body.replaceChildren();const bio=el('div',undefined,'profile-bio');bio.append(avatar(data.name,'player',id.image,true),el('p',[id.role,id.country,id.version].filter(Boolean).join(' · ')||'Player profile','muted'),metrics([['OVR',id.ovr],['BAT',id.bat],['BOWL',id.bowl]]));body.append(bio);
    if(id.ambiguous)body.append(el('p','Several player versions share this name. Ratings are shown only when the identity is certain.','notice'));
    if(data.roster_only)body.append(el('p','Registered squad member · no recorded appearances yet.','notice'));
    if((data.coverage||{}).career_aggregate_only_seasons>0)body.append(el('p','Career totals include historical season summaries. Individual match, opponent, pitch and phase details are unavailable for those seasons.','notice'));
    body.append(section(season==='all'?'All linked seasons':'Selected season',metrics(statsEntries(data.selected||{}))),section('Career · all linked seasons',metrics(statsEntries(data.career||{}))));
    body.append(section('Season-by-season record',wrappedTable(['Season','Matches','Innings','Runs','Average','SR','Wickets','Economy','Record book'],list(data.seasons).map(row=>{const st=row.stats||{};return[row.name,st.matches,st.innings,st.runs,value(st.average,2),value(st.sr,2),st.wickets,value(st.economy,2),link('Season records →',`/stats/${root.dataset.statsSeries}?season=${row.id}#records`)];}))));
    body.append(section('Complete batting & bowling figures',fullStats(data.selected||{})));
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
    const body=byId('profile-body');body.replaceChildren();body.append(avatar(data.name,'team',data.logo_url,true));body.append(el('p',data.captain?`${data.captain_role||'Team manager'}: ${data.captain}`:'Team manager information has not been recorded.','profile-subtitle'));
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
    if(!name)return;recordRequest++;if(recordController)recordController.abort();const dialog=byId('profile-dialog'),request=++profileRequest;if(profileController)profileController.abort();profileController=new AbortController();
    byId('profile-kind').textContent=type==='article'?'COMPETITION NEWS':type==='venue'?'GROUND & PITCH GUIDE':`${type.toUpperCase()} PROFILE`;byId('profile-title').textContent=name;byId('profile-body').replaceChildren(empty('Loading recorded performances…'));if(!dialog.open)dialog.showModal();
    try{const data=await load(type,type==='article'?{id:name}:{name},profileController.signal);if(request!==profileRequest||!dialog.open)return;byId('profile-title').textContent=data.title||data.name||name;if(type==='article')renderArticle(data);else if(type==='player')renderPlayerProfile(data);else if(type==='team')renderTeamProfile(data);else renderVenueProfile(data);}
    catch(error){if(request===profileRequest&&dialog.open&&error.name!=='AbortError')byId('profile-body').replaceChildren(empty(error.message||'This profile is temporarily unavailable.'));}
  }

  function fullStats(stats) {
    return wrappedTable(['Statistic','Value'],[['Matches',stats.matches],['Batting innings',stats.innings],['Runs',stats.runs],['Balls faced',stats.balls],['Dismissals',stats.outs],['Average',value(stats.average,2)],['Strike rate',value(stats.sr,2)],['Not outs',stats.not_outs],['Fifties',stats.fifties],['Hundreds',stats.hundreds],['Ducks',stats.ducks],['Fours',stats.fours],['Sixes',stats.sixes],['Dot balls',stats.bat_dots],['Dot percentage',value(stats.dot_percentage,2)],['Boundary percentage',value(stats.boundary_percentage,2)],['Highest score',stats.highest_score],['Bowling innings',stats.bowling_innings],['Wickets',stats.wickets],['Balls bowled',stats.bowled],['Runs conceded',stats.conceded],['Economy',value(stats.economy,2)],['Bowling average',value(stats.bowling_average,2)],['Bowling strike rate',value(stats.bowling_strike_rate,2)],['Three-plus wicket hauls',stats.three_fers],['Five-plus wicket hauls',stats.five_fers],['Bowling dot balls',stats.bowl_dots],['Player of the Match awards',stats.potm],['Best bowling',stats.best_bowling]]);
  }
  function renderSpotlight(rows) {
    const box=byId('player-spotlight');box.replaceChildren(...rows.slice(0,4).map(p=>{const card=el('article',undefined,'player-card');card.append(avatar(p.name,'player',p.image,true),profileButton(p.name),el('span',p.team||'Competition player','muted'),metrics(discipline==='batting'?[['Runs',p.runs],['SR',value(p.sr,2)]]:[['Wickets',p.wickets],['Economy',value(p.economy,2)]]));return card;}));
  }
  let archiveRows=[], archivePending=false, comparisonRequest=0;
  function renderSeasons() {
    const term=byId('season-search').value.trim().toLowerCase();
    const rows=archiveRows.filter(s=>`${s.name} ${s.champion||''}`.toLowerCase().includes(term));
    byId('season-archive').replaceChildren(...rows.map(s=>{const card=el('article',undefined,'season-card');card.append(el('div',`${s.archived?'ARCHIVED SEASON':'SEASON'} ${s.number}`,'eyebrow'),el('h3',s.name),el('span',s.status,'season-status'));if(s.champion){const winner=el('div',undefined,'season-champion');winner.append(avatar(s.champion,'team'),el('span',`🏆 ${s.champion}`));card.append(winner);}else card.append(el('p','A season of cricket history.','muted'));card.append(metrics([['Matches',s.matches],['Recorded players',s.players]]));[['Orange Cap',s.orange_cap],['Purple Cap',s.purple_cap]].forEach(([label,holders])=>{if(list(holders).length){const row=el('div',undefined,'season-cap');row.append(el('span',label),profileButton(holders[0].name),el('strong',holders[0].value));if(holders.length>1)row.append(el('small',`+${holders.length-1} joint holders`));card.append(row);}});const awards=Object.entries(s.honours||{}).filter(([key,val])=>key!=='champion'&&val&&typeof val==='object'&&(val.player||val.team));if(awards.length){const more=el('details',undefined,'season-honours');more.append(el('summary','Season honours'));awards.forEach(([key,val])=>{const row=el('p');row.append(el('span',pretty(key)+': '),profileButton(val.player||val.team,val.player?'player':'team'));if(val.value!==undefined&&val.value!==null)row.append(el('small',` · ${value(val.value)}`));more.append(row);});card.append(more);}if(s.aggregate_only)card.append(el('p','Historical totals retained; match-level detail is incomplete.','muted'));const links=el('div',undefined,'season-actions');links.append(link('Explore season →',s.url),link('Records →',s.records_url));card.append(links);return card;}));
    if(!rows.length)byId('season-archive').append(empty('No seasons match this search.'));
  }
  async function loadSeasons() {
    if(archivePending)return;archivePending=true;
    try{const data=await load('seasons');archiveRows=list(data.seasons);renderSeasons();}catch(_){byId('season-archive').replaceChildren(empty('The season archive is temporarily unavailable. Try refreshing.'));}finally{archivePending=false;}
  }
  function comparisonOptions() {
    const mode=byId('comparison-mode').value,names=mode==='team'?[...teamNames]:players.map(p=>p.name);
    byId('comparison-options').replaceChildren(...[...new Set(names)].sort().map(name=>{const option=el('option');option.value=name;return option;}));
    ['comparison-left','comparison-right'].forEach(id=>byId(id).placeholder=mode==='team'?'Choose a team':'Choose a player');
  }
  function comparisonHead(entity,type) {const box=el('div',undefined,'comparison-entity');box.append(avatar(entity.name,type,type==='team'?entity.logo_url:(entity.identity||{}).image,true),profileButton(entity.name,type));return box;}
  function renderComparison(data) {
    const box=byId('comparison-result');box.replaceChildren();const heads=el('div',undefined,'comparison-heads');heads.append(comparisonHead(data.left,data.kind),el('span','VS','versus'),comparisonHead(data.right,data.kind));box.append(heads);
    if(data.kind==='team'){
      const a=data.left.stats||{},b=data.right.stats||{};box.append(metrics([['Meetings',data.matches],['Ties',data.tied],['No result',data.no_result]]));
      const bar=el('div',undefined,'rivalry-bar');const total=a.won+b.won;[[data.left.name,a.won],[data.right.name,b.won]].forEach(([name,wins])=>{const part=el('span',`${name} · ${wins} wins`);part.style.flex=total?Math.max(wins,0.15):1;bar.append(part);});box.append(bar);
      box.append(wrappedTable(['Record',data.left.name,data.right.name],[['Wins',a.won,b.won],['Win percentage',value(a.win_percentage,1),value(b.win_percentage,1)],['Recorded runs',a.runs,b.runs],['Highest recorded score',a.highest_score,b.highest_score],['Average recorded score',value(a.average_score,2),value(b.average_score,2)]]));
      box.append(section('Season-by-season rivalry',wrappedTable(['Season','Meetings',`${data.left.name} wins`,`${data.right.name} wins`],list(data.seasons).map(s=>[s.name,s.matches,s.left.won,s.right.won]))));
      const recent=el('div',undefined,'fixture-grid');recent.append(...list(data.recent).map(m=>fixtureCard(m,true)));box.append(section('Recent meetings',recent));
    }else{
      const a=data.left.selected||{},b=data.right.selected||{};
      box.append(wrappedTable(['Statistic',data.left.name,data.right.name],[['Matches',a.matches,b.matches],['Batting innings',a.innings,b.innings],['Runs',a.runs,b.runs],['Balls faced',a.balls,b.balls],['Batting average',value(a.average,2),value(b.average,2)],['Strike rate',value(a.sr,2),value(b.sr,2)],['Highest score',a.highest_score,b.highest_score],['Fifties',a.fifties,b.fifties],['Hundreds',a.hundreds,b.hundreds],['Not outs',a.not_outs,b.not_outs],['Ducks',a.ducks,b.ducks],['Fours',a.fours,b.fours],['Sixes',a.sixes,b.sixes],['Bowling innings',a.bowling_innings,b.bowling_innings],['Wickets',a.wickets,b.wickets],['Economy',value(a.economy,2),value(b.economy,2)],['Bowling average',value(a.bowling_average,2),value(b.bowling_average,2)],['Three-plus wicket hauls',a.three_fers,b.three_fers],['Five-plus wicket hauls',a.five_fers,b.five_fers],['Player of the Match awards',a.potm,b.potm],['Best bowling',a.best_bowling,b.best_bowling]]));
      const seasonRows=new Map();[['left',data.left.seasons],['right',data.right.seasons]].forEach(([side,rows])=>list(rows).forEach(row=>{if(!byId('comparison-all').checked&&season!=='all'&&String(row.id)!==String(season))return;const bucket=seasonRows.get(row.id)||{name:row.name};bucket[side]=row.stats;seasonRows.set(row.id,bucket);}));
      box.append(section('Season-by-season player comparison',wrappedTable(['Season',`${data.left.name} runs`,`${data.right.name} runs`,`${data.left.name} wickets`,`${data.right.name} wickets`],[...seasonRows.values()].map(row=>[row.name,row.left?row.left.runs:'—',row.right?row.right.runs:'—',row.left?row.left.wickets:'—',row.right?row.right.wickets:'—']))));
      box.append(section('Direct batter–bowler encounters',wrappedTable(['Batter','Bowler','Runs','Balls','Dismissals','4s','6s','SR'],list(data.duels).map(d=>[profileButton(d.batter),profileButton(d.bowler),d.runs,d.balls,d.dismissals,d.fours,d.sixes,value(d.sr,2)]),'No saved delivery-level encounters for this pair.')));
      box.append(el('p',`${value(data.shared_matches)} recorded matches feature both players. Career totals above cover all their appearances in the comparison scope.`,'muted'));
    }
    box.append(el('p',data.coverage,'notice'));
  }
  async function compareSides() {
    const left=byId('comparison-left').value.trim(),right=byId('comparison-right').value.trim(),mode=byId('comparison-mode').value;if(!left||!right)return;
    const request=++comparisonRequest;byId('comparison-status').textContent='Comparing recorded history…';byId('comparison-share').hidden=true;
    try{const data=await load('compare',{mode,left,right,season:byId('comparison-all').checked?'all':season});if(request!==comparisonRequest)return;renderComparison(data);byId('comparison-status').textContent=byId('comparison-all').checked?'Comparison across every linked season.':'Comparison for the selected season.';byId('comparison-share').hidden=false;const url=new URL(location.href);url.searchParams.set('compare',mode);url.searchParams.set('left',left);url.searchParams.set('right',right);url.searchParams.set('comparison_season',byId('comparison-all').checked?'all':season);url.hash='compare';history.replaceState(null,'',url);}catch(error){if(request===comparisonRequest){byId('comparison-status').textContent=error.message;byId('comparison-result').replaceChildren(empty('Choose different valid names and compare again.'));}}
  }
  byId('season-search').addEventListener('input',renderSeasons);
  byId('comparison-form').addEventListener('submit',event=>{event.preventDefault();compareSides();});
  byId('comparison-mode').addEventListener('change',()=>{comparisonOptions();byId('comparison-left').value='';byId('comparison-right').value='';});
  byId('comparison-swap').addEventListener('click',()=>{const left=byId('comparison-left'),right=byId('comparison-right');[left.value,right.value]=[right.value,left.value];if(left.value&&right.value)compareSides();});
  byId('comparison-all').checked=season==='all';
  byId('comparison-share').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(location.href);byId('comparison-status').textContent='Comparison link copied.';}catch(_){byId('comparison-status').textContent=`Share this link: ${location.href}`;}});
  const initial=new URLSearchParams(location.search);if(['team','player'].includes(initial.get('compare'))){byId('comparison-mode').value=initial.get('compare');byId('comparison-left').value=initial.get('left')||'';byId('comparison-right').value=initial.get('right')||'';byId('comparison-all').checked=initial.get('comparison_season')==='all';compareSides();}
  loadSeasons();

  let recordRequest=0,recordController,recordCategory='',recordPage=1,recordQuery='',recordScope=season;
  function recordURL(){const url=new URL(location.href);['compare','left','right','comparison_season'].forEach(key=>url.searchParams.delete(key));url.searchParams.set('record',recordCategory);url.searchParams.set('season',recordScope);if(recordQuery)url.searchParams.set('record_q',recordQuery);else url.searchParams.delete('record_q');if(recordPage>1)url.searchParams.set('record_page',recordPage);else url.searchParams.delete('record_page');url.hash='records';return url.href;}
  async function fetchRecord(page=1){
    const request=++recordRequest;if(recordController)recordController.abort();recordController=new AbortController();recordPage=page;
    const target=byId('record-board');if(!target)return;target.setAttribute('aria-busy','true');target.replaceChildren(empty('Loading the full record book…'));
    byId('record-board-status').textContent='Loading rankings…';byId('record-board-prev').disabled=true;byId('record-board-next').disabled=true;
    try{const data=await load('leaderboard',{category:recordCategory,q:recordQuery,page,limit:25,season:recordScope},recordController.signal);if(request!==recordRequest||!byId('profile-dialog').open)return;
      byId('profile-title').textContent=data.title;byId('record-qualification').textContent=[data.scope,data.qualification,data.note].filter(Boolean).join(' · ');
      list(data.rows).forEach(row=>{if(row.image)playerImages.set(nameKey(row.name),row.image);});
      target.replaceChildren(table(['Rank','Player / team',data.unit||'Record','Team','Season','Ground','Scorecard'],list(data.rows).map(row=>[row.rank,row.kind==='partnership'?row.name:profileButton(row.name,row.kind==='team'?'team':'player'),row.display,row.team,row.season||'Combined totals',row.venue,row.url?link('View →',row.url):'—']),'No qualifying records match this view.'));
      recordPage=data.page;byId('record-board-status').textContent=data.total?`${Math.min((data.page-1)*data.page_size+1,data.total)}–${Math.min(data.page*data.page_size,data.total)} of ${data.total} ranked entries${recordQuery?` · original ranks retained`:''}`:'No qualifying entries.';
      byId('record-board-page').textContent=`Page ${data.page} of ${Math.max(1,Math.ceil(data.total/data.page_size))}`;byId('record-board-prev').disabled=data.page<=1;byId('record-board-next').disabled=data.page*data.page_size>=data.total;
    }catch(error){if(request===recordRequest&&error.name!=='AbortError'&&byId('record-board-status')){byId('record-board-status').textContent=error.message;target.replaceChildren(empty('Rankings could not be loaded. Use Search to retry.'));}}
    finally{if(target.isConnected)target.setAttribute('aria-busy','false');}
  }
  function openRecord(key,query='',page=1){
    profileRequest++;if(profileController)profileController.abort();recordCategory=key;recordQuery=query;recordScope=season;
    const dialog=byId('profile-dialog'),body=byId('profile-body');byId('profile-kind').textContent='THE COMPLETE RECORD BOOK';byId('profile-title').textContent=(records.find(r=>r.key===key)||{}).label||'Record rankings';body.replaceChildren();
    const intro=el('p',undefined,'notice');intro.id='record-qualification';body.append(intro);
    const scopeControl=el('label','Season','record-scope-control'),scopeSelect=el('select');scopeSelect.id='record-board-season';scopeSelect.setAttribute('aria-label','Record leaderboard season');Array.from(byId('season').options).forEach(option=>scopeSelect.add(new Option(option.text,option.value)));scopeSelect.value=recordScope;scopeControl.append(scopeSelect);body.append(scopeControl);scopeSelect.addEventListener('change',()=>{recordScope=scopeSelect.value;fetchRecord(1);});
    const form=el('form',undefined,'record-board-search'),label=el('label','Search rankings'),input=el('input'),submit=el('button','Search');input.type='search';input.id='record-board-query';input.placeholder='Player, team, season or ground';input.value=query;input.maxLength=150;label.htmlFor=input.id;submit.type='submit';form.append(label,input,submit);body.append(form);
    const status=el('p','Loading rankings…','muted');status.id='record-board-status';status.setAttribute('role','status');body.append(status);
    const target=el('div',undefined,'table-wrap');target.id='record-board';body.append(target);
    const controls=el('div',undefined,'pagination'),prev=el('button','← Previous','secondary'),next=el('button','Next →','secondary'),number=el('span',undefined,'muted');prev.id='record-board-prev';next.id='record-board-next';number.id='record-board-page';prev.type=next.type='button';controls.append(prev,number,next);body.append(controls);
    const share=el('button','Copy ranking link','secondary');share.type='button';share.addEventListener('click',async()=>{const url=recordURL();try{await navigator.clipboard.writeText(url);status.textContent='Ranking link copied.';}catch(_){status.textContent=`Share this ranking: ${url}`;}});body.append(share);
    form.addEventListener('submit',event=>{event.preventDefault();recordQuery=input.value.trim();fetchRecord(1);});prev.addEventListener('click',()=>fetchRecord(recordPage-1));next.addEventListener('click',()=>fetchRecord(recordPage+1));
    if(!dialog.open)dialog.showModal();fetchRecord(page);
  }
  document.addEventListener('click',event=>{const record=event.target.closest('button[data-record]');if(record)openRecord(record.dataset.record);});
  const requestedRecord=new URLSearchParams(location.search);if(requestedRecord.get('record'))openRecord(requestedRecord.get('record'),requestedRecord.get('record_q')||'',Math.max(1,Number(requestedRecord.get('record_page'))||1));
  byId('profile-close').addEventListener('click',()=>byId('profile-dialog').close());
  byId('profile-dialog').addEventListener('close',()=>{recordRequest++;if(recordController)recordController.abort();profileRequest++;if(profileController)profileController.abort();});
  document.addEventListener('click',event=>{const button=event.target.closest('button[data-profile]');if(button)openProfile(button.dataset.profile,button.dataset.name);const article=event.target.closest('button[data-article]');if(article)openProfile('article',article.dataset.article);});
  byId('player-search').addEventListener('input',renderPlayers);byId('record-search').addEventListener('input',renderRecords);
  document.querySelectorAll('[data-discipline]').forEach(button=>button.addEventListener('click',()=>{discipline=button.dataset.discipline;document.querySelectorAll('[data-discipline]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderPlayers();}));
  document.querySelectorAll('[data-fixtures]').forEach(button=>button.addEventListener('click',()=>{fixtureFilter=button.dataset.fixtures;document.querySelectorAll('[data-fixtures]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderFixtures();}));
  byId('explorer-form').addEventListener('submit',event=>{event.preventDefault();searchExplorer(1);});byId('explorer-form').addEventListener('reset',()=>setTimeout(()=>searchExplorer(1),0));
  byId('explorer-prev').addEventListener('click',()=>searchExplorer(explorerPage-1));byId('explorer-next').addEventListener('click',()=>searchExplorer(explorerPage+1));
  document.addEventListener('visibilitychange',()=>{clearTimeout(liveTimer);clearTimeout(recordsTimer);clearTimeout(portalTimer);if(!document.hidden){pollLive();pollRecords();pollPortal();}});
  pollLive();pollRecords();pollPortal();searchExplorer(1);
})();
