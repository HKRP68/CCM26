/* Shared auction entry and tools. Arena owns the single live bidding screen. */
const AuctionApp = (() => {
  'use strict';
  let seasonId = null, view = 'pool', data = null, toolsOpen = false;
  let poll = null, clock = null, epoch = 0, busy = false, loading = false;
  let offset = 0, selectedTeam = null;
  let filters = {query:'', status:'', role:'', set_name:'', page:1};
  let eligibleQuery = '';
  let managedTeamId = null, catalogueQuery = '', cataloguePlayers = [], signingPlayer = null, catalogueEpoch = 0;
  const $ = id => document.getElementById(id);
  const esc = value => escapeHtml(String(value ?? ''));
  const notify = (...args) => AuctionArena.notify(...args);
  const money = value => {
    const n = Number(value || 0), sign = n < 0 ? '−' : '';
    const abs = Math.abs(n), symbol = data?.season?.currency || '₹';
    return sign + symbol + (abs >= 100 ? (abs / 100).toLocaleString('en-IN', {maximumFractionDigits:2}) + ' Cr' : abs + ' L');
  };
  const btn = (text, action, args={}, primary=false, disabled=false) =>
    `<button type="button" class="au-btn ${primary?'primary':''}" data-au-action="${esc(action)}" data-au-args="${esc(JSON.stringify(args))}" ${disabled?'disabled':''}>${esc(text)}</button>`;
  const empty = (title, text='') => `<div class="au-empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
  const field = (name,label,value='',type='text',required=true) => `<label class="au-field"><span>${esc(label)}</span><input class="au-input" name="${esc(name)}" type="${esc(type)}" value="${esc(value)}" ${required?'required':''} ${type==='number'?'step="1"':''}></label>`;
  const hidden = (name,value) => `<input type="hidden" name="${esc(name)}" value="${esc(value)}">`;
  const teamsOptions = (teams, mineOnly=false) => teams.filter(t=>!mineOnly||t.mine).map(t=>`<option value="${t.id}" ${t.id===selectedTeam?'selected':''}>${esc(t.name)}</option>`).join('');
  const teamSelect = (teams,mineOnly=false,name='team_id') => `<label class="au-field"><span>Franchise</span><select class="au-select" name="${name}">${teamsOptions(teams,mineOnly)}</select></label>`;
  const form = (action,body,label='Save',args={}) => `<form data-au-form="${esc(action)}" data-au-args="${esc(JSON.stringify(args))}">${body}<div class="au-actions"><button class="au-btn primary" type="submit">${esc(label)}</button></div></form>`;
  const details = (title,body) => `<details><summary>${esc(title)}</summary>${body}</details>`;
  const date = value => value ? new Date(value).toLocaleString() : '—';
  const ownId = () => selectedTeam || data?.my_team_ids?.[0];
  const managedTeam = () => (data?.teams||[]).find(t=>t.id===(managedTeamId||data.managed_team_id));
  const active = () => toolsOpen && typeof currentScreen !== 'undefined' && currentScreen === 'auction' && !!$('auction-app');
  const stats = rows => `<dl class="au-stats">${rows.map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>`;
  const viewButton = (label,target) => `<button type="button" class="au-btn" data-au-ui="view" data-view="${target}">${esc(label)} →</button>`;

  function setSeason(id) { seasonId = id ? Number(id) : null; selectedTeam=null; managedTeamId=null; signingPlayer=null; cataloguePlayers=[]; catalogueQuery=''; filters={query:'',status:'',role:'',set_name:'',page:1}; data=null; epoch++; catalogueEpoch++; }
  function pauseTools() { clearTimeout(poll); clearInterval(clock); poll=null; clock=null; epoch++; loading=false; }
  function stopTools() { pauseTools(); toolsOpen=false; catalogueEpoch++; }
  function stop() { stopTools(); AuctionArena.stop(); }
  function open() { stopTools(); AuctionArena.open(seasonId); }
  async function openTools(target,id,teamId=null) {
    if(!['pool','teams','preseason','trades','rules','admin'].includes(target)) return;
    pauseTools(); toolsOpen=true;
    if(id!==seasonId) setSeason(id);
    view=target; managedTeamId=teamId; signingPlayer=null;
    shell(); bind(); clock=setInterval(tick,250);
    await refresh();
  }
  function shell() {
    $('auction-app').innerHTML = `<div class="au-head"><h2 id="au-title">Auction tools</h2><button type="button" class="au-btn" data-au-ui="back-live">Back to live auction</button></div>
      <div class="au-status" id="au-status"></div><nav class="au-nav" id="au-nav" aria-label="Auction tools"></nav>
      <div class="au-toolbar"><div class="au-refresh-status" id="au-connection" role="status" aria-live="polite"></div><button type="button" class="au-btn au-refresh" data-au-ui="refresh">↻ Refresh</button></div>
      <div id="au-body">${empty('Loading auction tools…')}</div>`;
  }
  function bind() {
    const root=$('auction-app');
    if (root.dataset.bound) return;
    root.dataset.bound='1';
    root.addEventListener('click', async e => {
      const action=e.target.closest('[data-au-action]');
      if(action) { await act(action.dataset.auAction,JSON.parse(action.dataset.auArgs||'{}')); return; }
      const ui=e.target.closest('[data-au-ui]');
      if(!ui) return;
      const key=ui.dataset.auUi;
      if(busy) return;
      if(key==='back-live') {AuctionArena.showTab('arena');return;}
      if(key==='view') { if(ui.dataset.view==='live') {AuctionArena.showTab('arena');return;} view=ui.dataset.view; epoch++; refresh(); }
      if(key==='refresh') refresh();
      if(key==='page') { filters.page=Number(ui.dataset.page); refresh(); }
      if(key==='export') download(ui.dataset.format);
      if(key==='team') { selectedTeam=Number(ui.dataset.team); refresh(); }
      if(key==='manage-team') { managedTeamId=Number(ui.dataset.team); signingPlayer=null; view='teams'; await refresh(); $('au-team-manager')?.scrollIntoView?.({block:'start'}); }
      if(key==='select-signing') { signingPlayer=cataloguePlayers.find(p=>p.id===Number(ui.dataset.player)); renderBody(); }
      if(key==='clear-signing') { signingPlayer=null; renderBody(); }
    });
    root.addEventListener('change',e=>{
      if(e.target.dataset.auFilter) { filters[e.target.dataset.auFilter]=e.target.value; filters.page=1; refresh(); }
      if(e.target.id==='au-managed-team') { managedTeamId=Number(e.target.value); signingPlayer=null; refresh(); }
      previewChange(e.target);
    });
    root.addEventListener('input',e=>{
      previewChange(e.target);
    });
    root.addEventListener('submit',async e=>{
      const f=e.target.closest('[data-au-form]');
      if(!f) return; e.preventDefault();
      const values=Object.fromEntries(new FormData(f));
      let action=f.dataset.auForm;
      if(action==='filter') { filters.query=values.query; filters.page=1; refresh(); return; }
      if(action==='catalogue') { catalogue(values.query); return; }
      if(action==='team_catalogue') { catalogue(values.query,'team'); return; }
      if(action==='eligible') { eligibleQuery=values.query; loadEligible(); return; }
      if(action==='admin_settings_basic') {
        const settings={};
        ['min_squad_size','max_squad_size','min_overseas','max_overseas','opening_purse_lakh','bid_seconds','min_retentions','max_retentions','expansion_picks'].forEach(k=>settings[k]=Number(values[k]));
        settings.home_country=values.home_country;
        await act('admin_settings',{settings}); return;
      }
      if(action==='admin_settings_json') {
        try { await act('admin_settings',{settings:JSON.parse(values.json)}); }
        catch(_) { notify('Check the settings JSON.','error'); } return;
      }
      if(action==='admin_trade_rules_json') {
        try { await act('admin_trade_rules',{rules:JSON.parse(values.json)}); }
        catch(_) { notify('Check the trade rules JSON.','error'); } return;
      }
      if(action==='admin_sets_import' || action==='admin_franchise_import') {
        try { await act(action,{[action==='admin_sets_import'?'sets':'franchises']:JSON.parse(values.json)}); }
        catch(_) { notify('Check the import JSON.','error'); } return;
      }
      if(action==='admin_franchise') values.co_owners=values.co_owners.split(/[,;\s]+/).filter(Boolean);
      if(action==='admin_draft_order') values.team_ids=values.team_ids.split(/[,\s]+/).filter(Boolean).map(Number);
      if(action==='admin_set_order') values.names=values.names.split('\n').map(v=>v.trim()).filter(Boolean);
      await act(action,{...JSON.parse(f.dataset.auArgs||'{}'),...values});
    });
  }
  async function refresh(background=false) {
    clearTimeout(poll);
    if(!active()) return;
    const requestEpoch=++epoch, requestedSeason=seasonId, requestedView=view;
    loading=true;
    const sent=Date.now();
    const body={season_id:seasonId,view,...(view==='pool'?filters:{}),...(view==='teams'&&managedTeamId?{managed_team_id:managedTeamId}:{}),...(view==='preseason'&&ownId()?{team_id:ownId()}:{})};
    const result=await api('/api/webapp/auction',body,10000);
    if(requestEpoch!==epoch || !active()) return;
    loading=false;
    if(result.ok) {
      const next=result.data;
      // Estimate server time at the request midpoint, not from the phone clock.
      offset=Date.parse(next.server_time)-(sent+Date.now())/2;
      data=next;
      if(data.season) seasonId=data.season.id;
      if(view==='teams'&&data.admin) managedTeamId=data.managed_team_id;
      if(!selectedTeam || !data.my_team_ids?.includes(selectedTeam)) selectedTeam=data.my_team_ids?.[0]||null;
      renderHeader();
      // Polls must not erase a half-typed custom bid or close its keyboard.
      const editing = $('au-body').contains(document.activeElement) && document.activeElement?.matches('input, textarea, select');
      if(!background || !editing) renderBody();
      tick();
    } else {
      $('au-connection').innerHTML=`<span class="au-error">${esc(result.data.message||'Could not load the auction.')}</span> <button class="au-btn" data-au-ui="refresh">Retry</button>`;
      document.querySelectorAll('#au-body [data-au-action],#au-body button[type="submit"]').forEach(b=>b.disabled=true);
      if(result.status===403) {$('au-body').innerHTML=empty('Access denied',result.data.message||'Your account cannot use these controls.');return;}
      if(!data) $('au-body').innerHTML=empty('The auction could not be loaded','Check your connection, then retry. Open the auction from its Telegram group if you are a spectator.');
    }
    if(active() && (requestedSeason===seasonId || requestedSeason===null) && ['preseason','trades'].includes(view)) {
      poll=setTimeout(()=>refresh(true),12000);
    }
  }
  function renderHeader() {
    const labels={pool:'Player pool',teams:data.admin?'Players & purses':'My purse & bids',preseason:'Retention & draft',trades:'Trades',rules:'Rulebook',admin:'Setup & rules'};
    if($('au-title')) $('au-title').textContent=labels[view]||'Auction tools';
    $('au-status').innerHTML=data.season?`<span class="au-tag au-state-${esc(data.season.status)}">${esc(data.season.status)}</span><span class="au-dot ${data.season.status==='live'?'live':''}"></span>Shared with Telegram${data.admin?' · Auction admin':data.my_team_ids?.length?' · Owner / co-owner':' · Spectator'}${data.season.published?' · Published to league':''}`:(data.admin?'Create or manage an auction':'Your assigned auctions appear here');
    const tabs=[['pool','Players'],['teams',data.admin?'Players & purses':'My purse & bids'],['preseason','Retention & draft'],['trades','Trades'],['rules','Rulebook']];
    if(data.admin) tabs.push(['admin','Setup & rules']);
    $('au-nav').innerHTML=tabs.map(([key,label])=>`<button type="button" class="${view===key?'active':''}" data-au-ui="view" data-view="${key}" aria-current="${view===key?'page':'false'}">${label}</button>`).join('');
    $('au-connection').textContent='Updated just now';
  }
  function renderBody() {
    const openDetails = [...$('au-body').querySelectorAll('details[open]')].map(d=>d.querySelector('summary')?.textContent);
    if(!data.season) {
      $('au-body').innerHTML=data.admin?empty('Manage your auctions','Create an auction or select one above.')+newAuction():empty('Waiting for your auction','Your assigned auctions will appear here. You can also open Switch to Miniapp in your auction group.'); return;
    }
    if(view==='admin'&&!data.admin) { $('au-body').innerHTML=empty('Bot admin access required','Only verified bot admins can use setup and management.'); return; }
    const renderer={pool:renderPool,teams:renderTeams,preseason:renderPreseason,trades:renderTrades,rules:renderRules,admin:renderAdmin}[view];
    $('au-body').innerHTML=renderer?renderer():'';
    $('au-body').querySelectorAll('details').forEach(d=>{ d.open=openDetails.includes(d.querySelector('summary')?.textContent); });
    disableBusy();
  }
  function warnings(t) { return (t.warnings||[]).map(w=>`<p class="au-warning">${esc(w.message)}</p>`).join(''); }
  function playerRow(p,extra='') {
    return `<div class="au-row"><div class="au-row-main"><strong>${esc(p.name)}</strong><div class="au-muted">${p.rating} OVR · ${esc(p.role)} · ${esc(p.country||'')}${p.overseas?' ✈':''}${p.version?' · '+esc(p.version):''}</div>${extra}</div><div class="au-row-money">${money(p.price_lakh??p.base_lakh)}<div class="au-result">${esc(p.status||'')}</div></div></div>`;
  }
  function renderPool() {
    const options=(values,current)=>'<option value="">All</option>'+values.map(v=>`<option value="${esc(v)}" ${v===current?'selected':''}>${esc(v.replaceAll('_',' '))}</option>`).join('');
    const filter=(name,values)=>`<label class="au-field"><span>${esc(name.replace('_',' '))}</span><select class="au-select" data-au-filter="${name}">${options(values,filters[name])}</select></label>`;
    let out=`<div class="au-section-head"><div><div class="au-kicker">Player directory</div><h3>${data.admin?'Player pool':'Players'}</h3></div>${data.admin?viewButton('Add players','admin'):''}</div>`+form('filter',field('query','Search players',filters.query,'search',false),'Search');
    out+=`<div class="au-filters">${filter('status',['queued','on_block','sold','unsold','withdrawn','rtm_offered'])}${filter('role',['Batsman','Bowler','All-rounder','Wicket Keeper'])}${filter('set_name',(data.sets||[]).map(s=>s.name))}</div>`;
    out+=details('Set order',`<div>${(data.sets||[]).map(s=>`<div class="au-row"><span class="au-muted">${s.set_no}. ${esc(s.name)} · ${esc(s.state)} · ${s.queued} waiting</span>${data.admin?btn('Bring forward','admin_set_forward',{set_name:s.name}):''}</div>`).join('')}</div>`);
    const lotControls=p=>{
      const actions=p.status==='queued'?[['Put next','force_next'],['Withdraw','withdraw']]:p.status==='unsold'?[['Relist','relist'],['Put next','force_next']]:p.status==='withdrawn'?[['Reinstate','reinstate']]:p.status==='sold'?(p.acquisition==='rtm'?[['Undo RTM','undo_rtm']]:p.acquisition==='drafted'?[['Undo draft pick','undo_pick']]:p.acquisition==='auction'?[['Recall sale','recall'],['Undo sale','undo_sale']]:[]):[];
      return details('Manage '+p.name,`<div class="au-actions">${actions.map(([label,action])=>btn(label,'admin_'+action,{lot_id:p.id})).join('')}${p.status==='sold'?`<button class="au-btn" type="button" data-au-ui="manage-team" data-team="${p.sold_to_id}">Manage this team</button>`:''}</div>${p.status==='queued'?form('admin_base_price',field('amount_lakh','Base price in lakh',p.base_lakh,'number'),'Save base price',{lot_id:p.id}):''}`);
    };
    out+=`<div class="au-card">${(data.players||[]).map(p=>playerRow(p,data.admin?lotControls(p):'')).join('')||empty('No players match','Try a different search or filter.')}</div>`;
    out+=`<div class="au-actions"><button class="au-btn" data-au-ui="page" data-page="${data.page-1}" ${data.page<=1?'disabled':''}>← Previous</button><span class="au-muted" style="align-self:center">${data.total} players · ${data.page}/${data.pages}</span><button class="au-btn" data-au-ui="page" data-page="${data.page+1}" ${data.page>=data.pages?'disabled':''}>Next →</button></div>`;
    return out;
  }
  function renderTeams() {
    const teams=data.teams||[], ownTeams=teams.filter(t=>t.mine);
    let content=data.admin?renderTeamManager():ownTeams.map(t=>`<section class="au-card"><h3>${esc(t.name)}</h3>${stats([['Purse',money(t.purse_lakh)],['Max legal bid',money(t.max_bid_lakh)],['Squad',t.size+'/'+data.season.max_squad]])}${warnings(t)}</section>`).join('');
    if(!teams.length)content+=empty('No franchises yet',data.admin?'Add franchises in Setup & rules.':'The auction admins are preparing the teams.');
    if(data.my_team_ids?.length)content+=details('Your purse ledger',ledgerRows(data.ledger))+details('Your bid history',(data.my_bids||[]).map(b=>`<div class="au-row"><div class="au-muted">${esc(b.name)}${b.void?' · voided':''}<br>${esc(date(b.at))}</div><strong>${money(b.amount_lakh)}</strong></div>`).join('')||'<p class="au-muted">No bids yet.</p>');
    return content||empty('Franchise owners can review their purse here','Open Squads to browse the complete field.');
  }
  function ledgerRows(rows) {
    return (rows||[]).map(e=>`<div class="au-row"><div class="au-muted">${esc(e.kind)} · ${esc(e.note||'')}<br>${esc(date(e.at))}</div><strong class="${e.amount_lakh<0?'au-error':'au-credit'}">${e.amount_lakh>0?'+':''}${money(e.amount_lakh)}</strong></div>`).join('')||'<p class="au-muted">No ledger entries yet.</p>';
  }
  function renderTeamManager() {
    const teams=data.teams||[], t=managedTeam();
    if(!t) return '';
    const args={team_id:t.id}, blocked=data.roster_block_reason, locked=data.management_locked;
    const choice=`<label class="au-field"><span>Team to manage</span><select class="au-select" id="au-managed-team">${teams.map(team=>`<option value="${team.id}" ${team.id===t.id?'selected':''}>${esc(team.name)} · ${money(team.purse_lakh)}</option>`).join('')}</select></label>`;
    const purse=form('admin_team_purse',hidden('expected_purse_lakh',t.purse_lakh)+`<label class="au-field"><span>Action</span><select class="au-select" name="mode"><option value="add">Add funds</option><option value="deduct">Deduct funds</option><option value="set">Set balance</option></select></label>`+field('amount_lakh','Amount in lakh · 100 = 1 Cr','','number')+field('note','Reason for this change')+`<output class="au-change-preview" data-au-preview aria-live="polite">Current balance: ${money(t.purse_lakh)}</output>`,'Review purse change',args);
    const removal=t.squad.length?form('admin_team_remove_player',hidden('expected_price_lakh',t.squad[0].price_lakh??0)+hidden('expected_sold_at',t.squad[0].sold_at||'')+`<label class="au-field"><span>Player to remove</span><select class="au-select" name="lot_id">${t.squad.map(p=>`<option value="${p.id}">${esc(p.name)} · refund ${money(p.price_lakh)}</option>`).join('')}</select></label>`+field('note','Reason for removing player')+`<p class="au-muted">Returns the player to the auction queue and refunds their full signing price. Retention and trade restrictions still apply.</p>`,'Review removal & refund',args):'<p class="au-muted">This team has no players to remove.</p>';
    const signing=signingPlayer?`<div class="au-selected-player"><strong>${esc(signingPlayer.name)}</strong><p class="au-muted">${signingPlayer.rating} OVR · ${esc(signingPlayer.role)} · ${esc(signingPlayer.version||'Base')}</p><button class="au-btn" type="button" data-au-ui="clear-signing">Choose another player</button></div>`+form('admin_team_add_player',hidden('player_id',signingPlayer.id)+field('price_lakh','Signing price in lakh · 0 = free',signingPlayer.base_lakh??0,'number')+field('note','Reason for adding player')+`<output class="au-change-preview" data-au-preview aria-live="polite">Purse after signing: ${money(t.purse_lakh-(signingPlayer.base_lakh??0))}</output>`,'Review signing',args):form('team_catalogue',field('query','Search player name',catalogueQuery,'search',false),'Find players')+`<div id="au-team-catalogue" class="au-catalogue-results">${cataloguePlayers.length?catalogueRows('team'):'<p class="au-muted">Search the catalogue, then select a player. Already signed players are marked unavailable.</p>'}</div>`;
    return `<section class="au-card au-manager" id="au-team-manager"><div class="au-kicker">Admin workspace</div><h3>Manage a team</h3><p class="au-muted">Every change is recorded in the purse ledger and auction activity.</p>${choice}${stats([['Available purse',money(t.purse_lakh)],['Signed players',t.size+'/'+data.season.max_squad],['Legal bid ceiling',money(t.max_bid_lakh)]])}${locked?'<p class="au-warning">This auction is published or cancelled. Management is locked.</p>':`<div class="au-management-grid"><section><h4>Adjust purse</h4>${purse}</section><section><h4>Add a player</h4>${blocked?`<p class="au-warning">${esc(blocked)}</p>${viewButton('Open live controls','live')}`:signing}</section><section><h4>Remove a player</h4>${blocked?`<p class="au-muted">${esc(blocked)}</p>`:removal}</section></div>`}${details(t.name+' · recent purse ledger',ledgerRows(data.team_ledger))}</section>`;
  }
  function previewChange(input) {
    const f=input.closest?.('[data-au-form]');
    if(f?.dataset.auForm==='admin_team_remove_player'&&input.name==='lot_id') {
      const p=managedTeam()?.squad.find(p=>p.id===Number(input.value));
      if(p) {f.elements.expected_price_lakh.value=p.price_lakh??0;f.elements.expected_sold_at.value=p.sold_at||'';}
    }
    const out=f?.querySelector('[data-au-preview]');
    if(!out) return;
    const t=managedTeam(); if(!t) return;
    const values=Object.fromEntries(new FormData(f));
    const amount=Number(values.amount_lakh??values.price_lakh);
    if(!Number.isSafeInteger(amount)||amount<0) {out.textContent='Enter a whole, non-negative number of lakh.';return;}
    const next=f.dataset.auForm==='admin_team_add_player'?t.purse_lakh-amount:values.mode==='set'?amount:t.purse_lakh+(values.mode==='deduct'?-amount:amount);
    out.textContent=`${money(t.purse_lakh)} → ${money(next)}${next<0?' · Balance cannot be negative':''}`;
    out.classList.toggle('au-error',next<0);
  }
  function renderPreseason() {
    const r=data.readiness||{}, tid=data.team_id;
    let out=`<div class="au-card"><h3>Season readiness</h3><p class="au-muted">Retention ${r.retention_closed?'closed':'open'} · ${esc(data.mode)} mode${r.next_team?' · Next pick: '+esc(r.next_team):''}</p>${r.deadline?`<div class="au-clock" id="au-clock" data-deadline="${esc(r.deadline)}"></div>`:''}${(r.teams||[]).map(t=>`<div class="au-row"><strong>${esc(t.name)}</strong><span class="au-muted">${t.new?'Expansion':t.final?'Finalized':'Retaining'} · ${t.retained} kept · ${t.picks_left} picks</span></div>`).join('')}</div>`;
    if(data.my_team_ids?.length>1) out+=`<div class="au-actions">${r.teams.filter(t=>data.my_team_ids.includes(t.id)).map(t=>`<button class="au-btn ${t.id===tid?'primary':''}" data-au-ui="team" data-team="${t.id}">${esc(t.name)}</button>`).join('')}</div>`;
    if(!tid) return out+empty('Follow preseason here','Retention and draft controls appear for franchise owners and co-owners.');
    const a={team_id:tid};
    out+=(data.offers||[]).map(o=>`<section class="au-card"><h3>Retention offer · ${esc(o.name)}</h3><div class="au-live-price">${money(o.price_lakh)}</div><div class="au-actions">${btn('Accept','retention_answer',{...a,offer_id:o.id,accept:true},true)}${btn('Decline','retention_answer',{...a,offer_id:o.id,accept:false})}</div></section>`).join('');
    out+=(data.talks||[]).map(t=>`<section class="au-card"><h3>${esc(t.name)} · ${esc(t.status)}</h3><p class="au-muted">${esc(t.reply||'Make your opening offer.')} · ${t.chances} chances left</p>${t.counter_lakh?`<p>Counter: <strong>${money(t.counter_lakh)}</strong></p>`:''}${t.status==='open'?form('talk_offer',field('amount_lakh','Offer in lakh',t.counter_lakh||t.last_offer_lakh||100,'number'),'Make offer',{...a,talk_id:t.id,attempts:t.attempts})+`<div class="au-actions">${t.counter_lakh?btn('Accept counter','talk_accept',{...a,talk_id:t.id,attempts:t.attempts}):''}${btn('Withdraw','talk_withdraw',{...a,talk_id:t.id,attempts:t.attempts})}</div>`:''}</section>`).join('');
    if(data.retention_open && !data.retention?.finalized) out+=`<section class="au-card"><h3>Previous squad · choose your retentions</h3>${(data.candidates||[]).map(p=>`<div class="au-row"><div class="au-row-main"><strong>${esc(p.name)}</strong><div class="au-muted">${p.rating} OVR · ${esc(p.role)} · ${esc(p.note||p.state||'')}</div></div>${btn(data.mode==='dynamic'?'Negotiate':'Retain',data.mode==='dynamic'?'talk_start':'retain',{...a,player_id:p.id},false,p.state!=='free')}</div>`).join('')||`<p class="au-muted">${data.admin?'Link the previous league and squads to see candidates.':'Your previous squad will appear after the auction admins finish setup.'}</p>`}</section>`;
    if(data.retention) {
      const r=data.retention;
      out+=`<section class="au-card"><h3>Your retention review</h3><p class="au-muted">${r.players.length} kept · ${money(r.spent_lakh)} spent · ${money(r.purse_lakh)} remains</p>${r.players.map(p=>`<div class="au-row"><strong>${esc(p.name)}</strong><span>${money(p.price_lakh)}</span></div>`).join('')}${(data.releasable||[]).map(p=>`<div class="au-row"><span class="au-muted">${esc(p.name)}</span>${btn('Release','release',{...a,lot_id:p.id},false,!data.retention_open||r.finalized)}</div>`).join('')}<div class="au-actions">${btn(r.finalized?'Retention finalized':'Finalize these retentions','retention_confirm',{...a,token:r.token},true,r.finalized||!data.retention_open)}</div></section>`;
    }
    if(data.eligible) out+=`<section class="au-card"><h3>Expansion draft</h3><p class="au-muted">${r.next_team?'On the clock: '+esc(r.next_team):'Complete retention before drafting.'} Players below meet your current rules.</p>${form('eligible',field('query','Search eligible players',eligibleQuery,'search',false),'Search')}<div id="au-eligible">${eligibleRows(data.eligible.players,tid)}</div><div class="au-actions">${btn('Skip my pick','draft_skip',a)}</div></section>`;
    if(data.shortlist?.length) out+=details('Your private draft shortlist',data.shortlist.map(p=>`<div class="au-row"><div class="au-row-main"><strong>${esc(p.name)}</strong><div class="au-muted">${esc(p.reason||'Eligible')}${p.price_lakh?' · '+money(p.price_lakh):''}</div></div>${btn('Remove','shortlist',{...a,operation:'remove',player_id:p.player_id})}</div>`).join(''));
    return out;
  }
  function eligibleRows(players,tid) {
    return (players||[]).map(p=>playerRow({...p,base_lakh:p.price_lakh},`<div class="au-actions">${btn('Pick','draft_pick',{team_id:tid,player_id:p.player_id},true)}${btn('Shortlist','shortlist',{team_id:tid,player_id:p.player_id,operation:'add'})}</div>`)).join('')||empty('No eligible players');
  }
  async function loadEligible() {
    const id=seasonId,tid=ownId();
    const r=await api('/api/webapp/auction',{season_id:id,view:'eligible',team_id:tid,query:eligibleQuery});
    if(r.ok&&id===seasonId&&view==='preseason'&&$('au-eligible')) $('au-eligible').innerHTML=eligibleRows(r.data.players,tid);
    else if(!r.ok) notify(r.data.message||'Could not search.','error');
  }
  function renderTrades() {
    const teams=data.teams||[];
    let out=`<div class="au-notice">${esc(data.window)}</div>`;
    if(data.my_team_ids?.length) out+=details('Build a new trade',form('trade_open',teamSelect(teams,true)+`<label class="au-field"><span>Trade with</span><select class="au-select" name="other_team_id">${teamsOptions(teams)}</select></label>`,'Start offer'));
    out+=(data.trades||[]).map(t=>{
      const tid=t.mine_a?t.a_id:t.b_id, args={team_id:tid,trade_id:t.id,version:t.version};
      let actions='';
      if(t.status==='building'&&t.mine_a) {
        for(const [id,label,chosen] of [[t.a_id,'You send',t.a_lots],[t.b_id,'You receive',t.b_lots]]) {
          const team=teams.find(v=>v.id===id);
          actions+=details(label,(team?.players||[]).map(p=>`<div class="au-row"><span class="au-muted">${esc(p.name)} · ${money(p.price_lakh||0)}</span>${btn(chosen.some(c=>c.id===p.id)?'✓ Remove':'Add','trade_toggle',{...args,lot_id:p.id})}</div>`).join('')||'<p class="au-muted">No tradable players.</p>');
        }
        actions+=form('trade_cash',field('amount_lakh','Cash in lakh (+ you pay, − you receive)',t.cash_lakh,'number'),'Update cash',args)+form('trade_note',field('note','Note',t.note||'','text',false),'Update note',args)+`<div class="au-actions">${btn('Send offer','trade_send',args,true)}${btn('Cancel','trade_cancel',args)}</div>`;
      }
      if(t.status==='offered'&&!t.expired&&t.mine_b) actions+=`<div class="au-actions">${btn('Accept trade','trade_accept',args,true)}${btn('Counter','trade_counter',args)}${btn('Reject','trade_reject',args)}</div>`;
      if(t.status==='offered'&&t.mine_a) actions+=`<div class="au-actions">${btn('Withdraw','trade_cancel',args)}</div>`;
      if(data.bot_admin) actions+=`<div class="au-actions">${t.status==='pending_admin'?btn('Approve','admin_trade_approve',{trade_id:t.id},true)+btn('Veto','admin_trade_veto',{trade_id:t.id}):''}${t.status==='completed'?btn('Reverse trade','admin_trade_reverse',{trade_id:t.id}):''}</div>`;
      return `<section class="au-card"><h3>Trade #${t.id} · ${esc(t.status.replaceAll('_',' '))}</h3><p><strong>${esc(t.a)} ↔ ${esc(t.b)}</strong></p><p class="au-muted">${esc(t.a)} sends: ${esc(t.a_lots.map(l=>l.name).join(', ')||'Cash only')}<br>${esc(t.b)} sends: ${esc(t.b_lots.map(l=>l.name).join(', ')||'Cash only')}</p><p class="au-muted">Cash: ${money(t.cash_lakh)} from ${esc(t.a)} to ${esc(t.b)}${t.note?' · '+esc(t.note):''}${t.expires?' · '+(t.expired?'Expired':esc(date(t.expires))):''}</p>${actions}</section>`;
    }).join('')||empty('No trades yet','Offers and completed deals appear here.');
    out+=details('Trade block',(data.block||[]).map(b=>`<div class="au-row"><strong>${esc(b.name)}</strong><span class="au-muted">${esc(b.team)} · ${esc(b.note||'Available for trade')}</span></div>`).join('')||'<p class="au-muted">No listed players.</p>');
    if(data.my_team_ids?.length) out+=details('List a player for trade',form('trade_block_add',teamSelect(teams,true)+field('lot_id','Lot id (from player pool)','','number')+field('note','What are you looking for?','','text',false),'List player'));
    return out;
  }
  function renderRules() {
    const r=data.rules;
    return `<section class="au-card"><h3>The rulebook</h3><p class="au-muted">${r.min_squad}–${r.max_squad} players · ${r.min_overseas}–${r.max_overseas} overseas · Home: ${esc(r.home_country)}</p><p class="au-muted">Opening purse ${money(r.opening_lakh)} · ${r.bid_seconds}s opening clock · ${r.direct_bids?'Custom bids allowed':'Next increment only'}</p><p class="au-muted">${r.staged?'Staged clock: '+r.staged.join(' / ')+' seconds':'Anti-snipe: last '+r.snipe_window+'s extend by '+r.snipe_extend+'s, up to '+r.max_extensions+' times'}</p><p class="au-muted">Retention: ${r.min_retentions}–${r.max_retentions} · ${r.retention_deadline?'Closes '+esc(date(r.retention_deadline)):'Admin closes the window'}</p><p class="au-muted">RTM ${r.rtm_enabled?'on · '+r.rtm_window+'s per stage · '+r.rtm_per_team+' base cards':'off'} · ${r.expansion_picks} expansion picks</p></section>
      <section class="au-card"><h3>Base prices</h3>${r.base_prices.map(b=>`<div class="au-row"><span class="au-muted">${b.min_rating}–${b.max_rating??100} OVR</span><strong>${money(b.base_lakh)}</strong></div>`).join('')}</section>
      <section class="au-card"><h3>Bid increments</h3>${r.increments.map(b=>`<div class="au-row"><span class="au-muted">${b.upto_lakh?'Below '+money(b.upto_lakh):'Above the final band'}</span><strong>+${money(b.step_lakh)}</strong></div>`).join('')}</section>
      ${details('Role, rating and retention limits',`<pre class="au-pre">${esc(JSON.stringify({role_minimums:r.roles_min,role_maximums:r.roles_max,rating_rules:r.rating_rules,retention_ranges:r.retention_ranges,draft_ranges:r.draft_ranges,retention_slots:r.retention_slots},null,2))}</pre>`)}
      <p class="au-notice">Bids reserve enough purse and squad slots to meet the minimum squad and role requirements. The server clock decides whether a bid arrives in time.</p>`;
  }
  function newAuction() { return `<section class="au-card"><h3>Create an auction</h3>${form('admin_create',field('name','Auction name'),'Create auction')}</section>`; }
  function jsonForm(action,label,value) { return form(action,`<label class="au-field"><span>${esc(label)}</span><textarea class="au-textarea" name="json" spellcheck="false" required>${esc(JSON.stringify(value,null,2))}</textarea></label>`,'Save'); }
  function renderAdmin() {
    if(!data.admin) return empty('Admin access required');
    const s=data.settings||{}, teams=data.teams||[],r=data.rules;
    const setup=details('1 · Group and season',form('admin_bind',field('chat_id','Telegram group id (negative)',data.season.chat_id||'','text'),'Bind group')+form('admin_previous_league',field('league_id','Previous Challenge League id',data.previous_league_id||'','number'),'Link previous league')+`<div class="au-actions">${btn('Carry previous squads','admin_carry_previous')}</div>`+form('admin_clone',field('name','Next season name'),'Clone next season'));
    const franchise=(t={})=>form('admin_franchise',hidden('team_id',t.id||'')+field('name','Team name',t.name||'')+field('owner_tg_id','Owner Telegram id',t.owner_tg_id||'','text',false)+field('owner_name','Owner name',t.owner_name||'','text',false)+field('co_owners','Co-owner Telegram ids (comma separated)',(t.co_owners||[]).join(', '),'text',false),t.id?'Save team':'Add team');
    const fieldSetup=details('2 · Franchises and owners',franchise()+teams.map(t=>details(t.name,franchise(t))).join('')+details('Import franchises',jsonForm('admin_franchise_import','Paste an exported franchise JSON file',{franchises:[]})));
    const basicFields=[['opening_purse_lakh','Opening purse in lakh'],['min_squad_size','Minimum squad'],['max_squad_size','Maximum squad'],['min_overseas','Minimum overseas'],['max_overseas','Maximum overseas'],['bid_seconds','Opening clock in seconds'],['min_retentions','Minimum retentions'],['max_retentions','Maximum retentions'],['expansion_picks','Expansion picks']];
    const rules=details('3 · Auction rules',`<p class="au-muted">Configure before starting. All money fields use lakh (100 L = 1 Cr).</p>${form('admin_settings_basic',basicFields.map(([k,label])=>field(k,label,s[k]??0,'number')).join('')+field('home_country','Home country',s.home_country||'India'),'Save rules')}<div class="au-actions">${btn(s.direct_bids?'Disable custom bids':'Allow custom bids','admin_settings',{settings:{direct_bids:!s.direct_bids}})}${btn(s.rtm_enabled?'Disable RTM':'Enable RTM','admin_settings',{settings:{rtm_enabled:!s.rtm_enabled}})}</div>${details('Advanced rule tables',jsonForm('admin_settings_json','Edit rule tables',{
      role_minimums:r.roles_min,role_maximums:r.roles_max,rating_rules:r.rating_rules,
      base_prices:r.base_prices,increments:r.increments,
      retention_mode:s.retention_mode||'classic',retention_ranges:r.retention_ranges,draft_ranges:r.draft_ranges,
      retention_slots:r.retention_slots,retention_rules:data.retention_rules,
      rtm_enabled:!!s.rtm_enabled,rtm_per_team:s.rtm_per_team,rtm_window_seconds:s.rtm_window_seconds,rtm_extra_lakh:s.rtm_extra_lakh,
      auto_accelerated:!!s.auto_accelerated,focus_mode:!!s.focus_mode,direct_bids:!!s.direct_bids,
      snipe_window_seconds:s.snipe_window_seconds,snipe_extend_seconds:s.snipe_extend_seconds,max_extensions:s.max_extensions
    }))}`);
    const pool=details('4 · Build the player pool',form('admin_pool_range',field('low','Minimum rating',70,'number')+field('high','Maximum rating',100,'number')+field('set_name','Set name','','text',false),'Add rating band')+`<div class="au-actions">${btn('Generate automatic sets','admin_pool_auto_sets')}${btn('Relist all unsold','admin_relist_all')}</div>`+form('catalogue',field('query','Search the player catalogue','','search',false),'Search')+'<div id="au-catalogue"></div>'+details('Import player sets',jsonForm('admin_sets_import','Paste an exported set JSON file',{sets:[]}))+`<div class="au-actions"><button class="au-btn" data-au-ui="export" data-format="sets">Export player sets</button><button class="au-btn" data-au-ui="export" data-format="franchises">Export franchises</button></div>`+form('admin_set_order',`<label class="au-field"><span>Set order (one exact name per line)</span><textarea class="au-textarea" name="names" required></textarea></label>`,'Save set order'));
    const pre=details('5 · Retention, expansion and RTM',`<div class="au-actions">${btn('Close retention','admin_retention_lock')}${btn('Reopen retention','admin_retention_unlock')}</div>`+form('admin_retention_offer',teamSelect(teams)+field('player_id','Player id','','number')+field('amount_lakh','Price in lakh (blank = slab)','','number',false),'Send retention offer')+form('admin_draft_picks',teamSelect(teams)+field('picks','Expansion picks',0,'number'),'Set team picks')+form('admin_draft_order',field('team_ids','Draft team ids in order (comma separated)'),'Save draft order')+form('admin_draft_clock',field('seconds','Pick clock in seconds (0 = off)',s.draft_pick_seconds||0,'number')+`<label class="au-field"><span>On timeout</span><select class="au-select" name="timeout_action"><option value="pause">Pause</option><option value="skip">Skip</option><option value="shortlist">Auto-pick shortlist</option></select></label>`,'Save pick clock')+form('admin_rtm_cards',teamSelect(teams)+field('cards','RTM cards',0,'number'),'Set RTM cards'));
    const liveRules=details('Live clock and bidding rules',form('admin_timer',field('seconds','Clock: seconds, or opening reset warning1 warning2 countdown',s.bid_seconds),'Set clock')+form('admin_increments',field('rules','Bid ladder (for example 2cr:10l, 5cr:20l, 50l)'),'Set increments')+form('admin_countdown',field('seconds','Final countdown seconds',s.countdown_seconds,'number'),'Set countdown')+form('admin_bid_gap',field('seconds','Quiet gap after each bid',s.bid_gap_seconds,'number'),'Set gap')+form('admin_snipe',field('window','Anti-snipe window seconds',s.snipe_window_seconds,'number')+field('extend','Extension seconds',s.snipe_extend_seconds,'number')+field('max_extensions','Maximum extensions',s.max_extensions,'number'),'Set anti-snipe')+`<div class="au-actions">${btn(s.direct_bids?'Disable custom bids':'Allow custom bids','admin_direct',{enabled:!s.direct_bids})}${btn(s.focus_mode?'Turn focus mode off':'Turn focus mode on','admin_focus',{enabled:!s.focus_mode})}${btn(s.auto_accelerated?'Disable automatic accelerated round':'Enable automatic accelerated round','admin_accelerated',{enabled:!s.auto_accelerated})}</div>`);
    const operations=details('6 · Live operations',viewButton('Go to live auction','live')+viewButton('Manage team players & purses','teams')+form('admin_previous_team',teamSelect(teams)+field('team_name','Exact previous team name'),'Link previous team')+form('admin_retention_force',teamSelect(teams)+field('player_id','Player id','','number')+field('amount_lakh','Retention price in lakh','','number',false),'Force retention')+form('admin_unretain',teamSelect(teams)+field('lot_id','Retained lot id','','number'),'Undo retention')+liveRules);
    const trading=details('7 · Trade administration',`<div class="au-actions">${btn('Open trade window','admin_trade_window',{open:true})}${btn('Close trade window','admin_trade_window',{open:false})}</div>${jsonForm('admin_trade_rules_json','Trade rules',data.trade_rules)}`);
    const lifecycle=details('8 · Finish and next season',`<p class="au-muted">These actions need the auction name to confirm: ${esc(data.season.name)}</p>`+['publish','simulate','restart','cancel'].map(action=>details({publish:'Publish squads to Challenge League',simulate:'Simulate the remaining auction',restart:'Restart auction',cancel:'Cancel auction'}[action],form('admin_'+action,field('confirm_name','Type the auction name'),'Confirm '+action))).join('')+details('Remove a franchise',form('admin_franchise_remove',teamSelect(teams)+field('confirm_name',"Type the team's exact name"),'Remove team')));
    const admins=data.bot_admin?details('Auction admin access',form('admin_add_admin',field('telegram_id','Telegram user id','','text'),'Add auction admin')+form('admin_remove_admin',field('telegram_id','Telegram user id','','text'),'Remove auction admin')):'';
    const counts=data.counts||{};
    const checks=[['Telegram group',!!data.season.chat_id,'Bind the auction group in step 1.'],['Franchises & owners',teams.length>=2&&teams.every(t=>t.owner_tg_id||t.co_owners?.length),'Add at least two teams and assign their owners in step 2.'],['Player pool',!!counts.total,'Build the player pool in step 4.'],['Squad capacity',counts.total>=teams.length*(s.min_squad_size||0)&&!!teams.length,'Check that the pool can fill every minimum squad.']];
    const needs=teams.filter(t=>t.warnings?.length);
    const dashboard=`<section class="au-card au-dashboard"><div class="au-kicker">Auction setup</div><div class="au-section-head"><div><h3>${esc(data.season.name)}</h3><p class="au-muted">Prepare the room. Keep every team on track.</p></div><span class="au-tag au-state-${esc(data.season.status)}">${esc(data.season.status)}</span></div>${stats([['Franchises',teams.length],['Players in pool',counts.total||0],['Players sold',counts.sold||0],['Purse remaining',money(teams.reduce((n,t)=>n+(t.purse_lakh||0),0))]])}<div class="au-actions">${viewButton('Manage players & purses','teams')}${viewButton('Live auction desk','live')}${viewButton('Browse pool','pool')}</div></section><section class="au-card"><h3>Setup checklist</h3><p class="au-muted">Quick checks for setup. Start also validates retention, draft and auction rules.</p><ul class="au-checklist">${checks.map(([title,ok,hint])=>`<li><span class="${ok?'au-credit':'au-warning-icon'}" aria-label="${ok?'Ready':'Needs attention'}">${ok?'✓':'○'}</span><div><strong>${esc(title)}</strong><p class="au-muted">${ok?'Ready':esc(hint)}</p></div></li>`).join('')}</ul></section>${needs.length?`<section class="au-card"><h3>Teams needing attention</h3>${needs.map(t=>`<div class="au-row"><div class="au-row-main"><strong>${esc(t.name)}</strong>${warnings(t)}</div><button type="button" class="au-btn" data-au-ui="manage-team" data-team="${t.id}">Review</button></div>`).join('')}</section>`:''}<div class="au-notice">Suggested routine: check team warnings before starting, pause and settle the current player before squad edits, and review the ledger after each correction.</div>`;
    return dashboard+setup+fieldSetup+rules+pool+pre+operations+trading+lifecycle+admins+details('Create another auction',newAuction());
  }
  function catalogueRows(mode) {
    return cataloguePlayers.map(p=>`<div class="au-row"><div class="au-row-main"><strong>${esc(p.name)}</strong><div class="au-muted">${p.rating} OVR · ${esc(p.role)} · ${esc(p.version||'Base')} · ${esc(p.country||'')}<br>${esc(p.status.replaceAll('_',' '))} · base ${money(p.base_lakh)}</div></div>${mode==='team'?`<button class="au-btn" type="button" data-au-ui="select-signing" data-player="${p.id}" ${p.available?'':'disabled'}>${p.available?'Select':'Unavailable'}</button>`:btn(p.status==='not_in_pool'?'Add to pool':'In pool','admin_pool_players',{player_ids:[p.id]},false,p.status!=='not_in_pool')}</div>`).join('')||empty('No matching players','Try another name.');
  }
  async function catalogue(query,mode='pool') {
    const id=seasonId, request=++catalogueEpoch, team=managedTeamId;
    const target=mode==='team'?'au-team-catalogue':'au-catalogue';
    if($(target)) $(target).innerHTML='<p class="au-muted" role="status">Searching players…</p>';
    const result=await api('/api/webapp/auction/catalogue',{season_id:id,query});
    if(request!==catalogueEpoch||id!==seasonId||!$(target)||(mode==='team'?(view!=='teams'||team!==managedTeamId):view!=='admin')) return;
    if(!result.ok) {$(target).innerHTML='<p class="au-error">Could not load players. Search again to retry.</p>';notify(result.data.message||'Could not load players.','error');return;}
    catalogueQuery=query; cataloguePlayers=result.data.players;
    $(target).innerHTML=catalogueRows(mode);
  }
  function disableBusy() {
    $('auction-app')?.setAttribute('aria-busy',String(busy));
    AuctionArena.reflectToolBusy(busy);
    document.querySelectorAll('#au-body button, #au-body input, #au-body select, #au-body textarea, #au-nav button').forEach(b=>{
      if(busy) {if(!('auBusy' in b.dataset))b.dataset.auBusy=b.disabled?'1':'0';b.disabled=true;}
      else if('auBusy' in b.dataset) {b.disabled=b.dataset.auBusy==='1';delete b.dataset.auBusy;}
    });
  }
  async function confirmAction(message) {
    if(tg?.showConfirm) {
      try { return await new Promise(resolve=>tg.showConfirm(message,resolve)); }
      catch(_) { /* Older Telegram clients use the browser dialog. */ }
    }
    return window.confirm(message);
  }
  async function act(action,args) {
    if(busy) return;
    if(action.startsWith('admin_')&&!data?.admin) { notify('Only bot admins can manage an auction.','error'); return; }
    const confirmations={retention_confirm:'Finalize this retention list? Finalized retentions cannot be changed by the owner.',release:'Release this player into the auction pool?',trade_accept:'Accept this trade, including its players and cash?',admin_trade_reverse:'Reverse this completed trade?',admin_sold:'Sell the current player at the displayed price?',admin_unsold:'Mark the current player unsold?',admin_force_next:'Bring this player onto the block?',admin_recall:'Recall this sale?',admin_undo_sale:'Undo this sale?',admin_retention_force:'Force this retention as an admin?'};
    const t=managedTeam();
    if(action==='admin_team_purse'&&t) {
      const amount=Number(args.amount_lakh), before=Number(args.expected_purse_lakh);
      const after=args.mode==='set'?amount:before+(args.mode==='deduct'?-amount:amount);
      confirmations[action]=`${t.name}: ${money(before)} → ${money(after)}. Apply this purse change? Reason: ${args.note}`;
    }
    if(action==='admin_team_add_player'&&t) confirmations[action]=`Add ${signingPlayer?.name||'this player'} to ${t.name} for ${money(args.price_lakh)}? The price will be deducted from the team purse.`;
    if(action==='admin_team_remove_player'&&t) {
      const p=t.squad.find(p=>p.id===Number(args.lot_id));
      confirmations[action]=`Remove ${p?.name||'this player'} from ${t.name} and refund ${money(p?.price_lakh)}? The player returns to the auction queue.`;
    }
    if(confirmations[action]) {
      busy=true;disableBusy();
      const accepted=await confirmAction(confirmations[action]);
      busy=false;disableBusy();
      if(!accepted) return;
    }
    const id=seasonId;
    busy=true;disableBusy();
    clearTimeout(poll); epoch++;
    const result=await api('/api/webapp/auction/action',{...args,season_id:id,action},10000);
    if(result.ok) {
      if(action==='admin_team_add_player') {signingPlayer=null;cataloguePlayers=[];}
      if(result.data.season_id) {setSeason(result.data.season_id);view='admin';await AuctionArena.syncSeason(seasonId);}
    }
    if(active()) await refresh();
    busy=false;disableBusy();tick();
    if(result.ok) {haptic('medium');notify(result.data.message||'Auction updated.','success');}
    else notify(result.status===0?'Connection interrupted. Refresh to check whether the action landed before retrying.':(result.data.message||'The action was refused.'),'error');
  }
  function tick() {
    const el=$('au-clock');
    if(el&&el.dataset.deadline) {
      const left=Math.max(0,Math.ceil((Date.parse(el.dataset.deadline)-(Date.now()+offset))/1000));
      el.textContent=data?.season?.status==='paused'?'Paused':left?left+'s':'Awaiting result';
      el.classList.toggle('urgent',left<=10);
    } else if(el) el.textContent=data?.season?.status==='paused'?'Paused':'—';

  }
  async function download(format) {
    const result=await api('/api/webapp/auction/export',{season_id:seasonId,format});
    if(!result.ok) {notify(result.data.message||'Export failed.','error');return;}
    const blob=new Blob([result.data.content],{type:'application/json'}),url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;a.download=result.data.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden) pauseTools();
    else if(active()) {clock=setInterval(tick,250);refresh();}
  });
  window.addEventListener('online',()=>{if(active())refresh();});
  return {open,stop,setSeason,openTools,stopTools,refreshTools:refresh,isBusy:()=>busy&&toolsOpen};
})();
