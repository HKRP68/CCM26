/* The single franchise auction room, including live play and shared tools. */
const AuctionArena = (() => {
  'use strict';
  let root, seasonId=null, data=null, tab='arena', teamId=null, panel=false;
  let poll=null, clock=null, epoch=0, busy=false, updatedAt=0, offset=0, connected=false;
  let lastEvent=null, resultCard=null, resultUntil=0, logTab='activity', teamQuery='';
  let toolView=null, exitingToHome=false;
  let bodyOverflow='', noticeTimer=null;
  const artShapes=new Map();
  const $=id=>document.getElementById(id);
  const esc=value=>escapeHtml(String(value??''));
  const money=value=>(data?.season?.currency||'₹')+(Number(value||0)/100).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})+' Cr';
  const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${{
    arena:'<path d="m14 3 7 7-4 4-7-7zM11 8l-8 9 4 4 9-8M2 22h11"/>',
    squads:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M21 21v-3a6 6 0 0 0-3-5"/>',
    logs:'<path d="M3 5v5h5M3 10a9 9 0 1 1 2 8M12 7v5l3 2"/>',
    settings:'<path d="m9 3-1 3-3 1-2 4 2 2v4l4 3 3-1 3 1 4-3v-4l2-2-2-4-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
    back:'<path d="m14 5-7 7 7 7M7 12h14"/>',
    refresh:'<path d="M20 4v6h-6M20 10a8 8 0 1 0-1 8"/>',
    chevron:'<path d="m6 9 6 6 6-6"/>',
    pause:'<path d="M8 4v16M16 4v16"/>',
    play:'<path d="m7 3 14 9-14 9z"/>',
    telegram:'<path d="m3 10 18-7-5 18-5-7-8-4ZM11 14l10-11"/>',
    close:'<path d="m5 5 14 14M19 5 5 19"/>',
    more:'<path d="M4 6h16M4 12h16M4 18h16"/>'
  }[name]||''}</svg>`;
  const own=()=>data?.teams?.find(t=>t.id===teamId);
  const context=lot=>({lot_id:lot.id,stage:lot.rtm_stage,expected_bids:lot.bids});
  const liveEnough=()=>connected&&Date.now()-updatedAt<6500;
  const isWorking=()=>busy||!!AuctionApp.isBusy?.();
  const action=(label,name,args={},disabled=false,style='')=>`<button type="button" class="aa-button ${style}" data-aa-action="${name}" data-aa-args="${esc(JSON.stringify(args))}" ${disabled?'disabled':''}>${esc(label)}</button>`;
  const ui=(label,name,ico='')=>`<button type="button" class="aa-icon-button" data-aa-ui="${name}" aria-label="${esc(label)}" title="${esc(label)}">${icon(ico||name)}</button>`;
  const empty=(title,detail)=>`<section class="aa-empty"><div class="aa-empty-icon">${icon('arena')}</div><h2>${esc(title)}</h2><p>${esc(detail)}</p></section>`;
  const initials=name=>String(name||'P').split(/\s+/).map(s=>s[0]).slice(0,2).join('');
  function portrait(p) {
    return `<div class="aa-portrait ${artShapes.get(Number(p.player_id))?'card-art':''}"><span class="aa-avatar-fallback">${esc(initials(p.name))}</span>${p.player_id?`<img data-aa-player-art="${Number(p.player_id)}" src="/webapp/player-card/${Number(p.player_id)}?portrait=1" alt="${esc(p.name)} player artwork" decoding="async">`:''}</div>`;
  }
  function mount() {
    if(root) return;
    root=document.createElement('dialog');root.id='auction-arena';root.className='auction-arena';
    root.setAttribute('aria-label','Franchise Auction Arena');
    root.innerHTML='<div class="aa-shell"><header id="aa-header" class="aa-header"></header><div id="aa-connection" class="aa-connection" role="status" aria-live="polite"></div><main id="aa-body" class="aa-body"></main><nav id="aa-nav" class="aa-nav" aria-label="Arena sections"></nav><div id="aa-toast" class="aa-toast" role="status" aria-live="polite" hidden></div></div>';
    document.body.append(root);
    root.addEventListener('cancel',e=>{
      e.preventDefault();if(isWorking())return;
      if(toolView)showTab('more');else if(panel){panel=false;render();}else close();
    });
    root.addEventListener('close',()=>{
      suspend();leaveTools();document.body.style.overflow=bodyOverflow;
      const goHome=exitingToHome;exitingToHome=false;
      if(goHome&&typeof navigate==='function')navigate('home');
    });
    root.addEventListener('error',e=>{if(e.target.tagName==='IMG')e.target.hidden=true;},true);
    root.addEventListener('load',e=>{if(e.target.tagName==='IMG'&&e.target.parentElement.classList.contains('aa-portrait')){const landscape=e.target.naturalWidth>e.target.naturalHeight;artShapes.set(Number(e.target.dataset.aaPlayerArt),landscape);e.target.parentElement.classList.toggle('card-art',landscape);}},true);
    root.addEventListener('click',async e=>{
      const a=e.target.closest('[data-aa-action]');
      if(a) {await act(a.dataset.aaAction,JSON.parse(a.dataset.aaArgs||'{}'));return;}
      const button=e.target.closest('[data-aa-ui]');if(!button||isWorking())return;
      const key=button.dataset.aaUi;
      if(key==='close')close();
      if(key==='refresh') {if(toolView)await AuctionApp.refreshTools();else await refresh();}
      if(key==='tab')await showTab(button.dataset.tab);
      if(key==='logs') {logTab=button.dataset.log;renderBody();}
      if(key==='admin')await openAdmin();
      if(key==='dismiss-panel') {panel=false;render();}
      if(key==='full-admin')await openTools('admin');
      if(key==='manage-team')await openTools('teams',Number(button.dataset.team||teamId));
      if(key==='tool')await openTools(button.dataset.tool);
      if(key==='download-results')await downloadResults();
      if(key==='dismiss-result') {resultCard=null;resultUntil=0;renderBody();tick();}
      if(key==='telegram') {
        const link=data?.season?.telegram_url;
        if(link) {if(tg?.openTelegramLink)tg.openTelegramLink(link);else window.open(link,'_blank','noopener');}
        else notice('Open the auction group in Telegram.');
      }
    });
    root.addEventListener('change',e=>{
      if(isWorking())return;
      if(e.target.id==='aa-season') {leaveTools();seasonId=Number(e.target.value);AuctionApp.setSeason(seasonId);data=null;lastEvent=null;resultCard=null;teamId=null;panel=false;refresh();}
      if(e.target.id==='aa-team') {teamId=Number(e.target.value);renderBody();tick();}
    });
    root.addEventListener('input',e=>{
      if(e.target.id==='aa-team-search') {teamQuery=e.target.value;root.querySelectorAll('[data-aa-team-name]').forEach(t=>t.hidden=!t.dataset.aaTeamName.includes(teamQuery.trim().toLowerCase()));}
    });
    root.addEventListener('submit',async e=>{
      const form=e.target.closest('[data-aa-form]');if(!form)return;e.preventDefault();
      const values=Object.fromEntries(new FormData(form));
      await act(form.dataset.aaForm,{...JSON.parse(form.dataset.aaArgs||'{}'),...values});
    });
  }
  function open(id) {
    if(root?.open){leaveTools();tab='arena';panel=false;if(id!==seasonId){seasonId=id||null;data=null;}render();refresh();return;}
    mount();suspend();seasonId=id||null;data=null;tab='arena';panel=false;teamId=null;lastEvent=null;resultCard=null;resultUntil=0;teamQuery='';toolView=null;exitingToHome=false;
    bodyOverflow=document.body.style.overflow;document.body.style.overflow='hidden';
    $('aa-toast').hidden=true;
    root.showModal();render();clock=setInterval(tick,250);refresh();
  }
  function suspend() {clearTimeout(poll);clearInterval(clock);poll=null;clock=null;epoch++;connected=false;}
  function leaveTools() {if(toolView){AuctionApp.stopTools();toolView=null;connected=false;}}
  function stop() {exitingToHome=false;suspend();leaveTools();if(root?.open)root.close();}
  function close() {if(isWorking()||!root?.open)return;exitingToHome=true;root.close();}
  async function showTab(next) {
    if(isWorking()||!['squads','arena','logs','more'].includes(next))return;
    leaveTools();tab=next;panel=false;$('aa-toast').hidden=true;render();root.scrollTop=0;
    if(!clock)clock=setInterval(tick,250);
    await refresh();
  }
  async function openTools(view,team=null) {
    if(isWorking()||!['pool','teams','preseason','trades','rules','admin'].includes(view))return;
    if(view==='admin'&&!data?.bot_admin){notice('Only bot admins can use setup.');return;}
    leaveTools();suspend();toolView=view;panel=false;tab='more';
    $('aa-body').innerHTML='<section class="auction-app aa-tools" id="auction-app" aria-label="Auction tools"></section>';
    renderNav();root.scrollTop=0;
    await AuctionApp.openTools(view,seasonId,team);
    tick();
  }
  async function syncSeason(id) {seasonId=id;data=null;lastEvent=null;resultCard=null;if(root?.open)await refresh();}
  function reflectToolBusy(value) {
    root?.setAttribute('aria-busy',String(value));
    root?.querySelectorAll('.aa-header button,.aa-header select,.aa-nav button').forEach(b=>{
      if(value){if(!('aaToolBusy' in b.dataset))b.dataset.aaToolBusy=b.disabled?'1':'0';b.disabled=true;}
      else if('aaToolBusy' in b.dataset){b.disabled=b.dataset.aaToolBusy==='1';delete b.dataset.aaToolBusy;}
    });
  }
  function notice(message,kind='info') {const el=$('aa-toast');if(!el)return;clearTimeout(noticeTimer);el.textContent=message;el.dataset.kind=kind;el.hidden=false;noticeTimer=setTimeout(()=>{el.hidden=true;},5500);}
  async function refresh(background=false) {
    clearTimeout(poll);if(!root?.open||document.hidden)return;
    if(!toolView&&!clock)clock=setInterval(tick,250);
    const request=++epoch, sent=Date.now();
    const result=await api('/api/webapp/auction',{season_id:seasonId,view:'arena'},10000);
    if(request!==epoch||!root.open)return;
    connected=!!result.ok;
    if(result.ok) {
      const next=result.data;
      offset=Date.parse(next.server_time)-(sent+Date.now())/2;updatedAt=Date.now();
      if(data?.lot&&next.lot?.id===data.lot.id&&next.my_team_ids?.includes(data.lot.bidder_id)&&next.lot.bidder_id&&!next.my_team_ids.includes(next.lot.bidder_id)) {haptic('heavy');notice('You have been outbid. Next bid: '+money(next.lot.next_lakh));}
      if(next.last_result?.event_id!==lastEvent) {
        if(next.last_result&&(data||!next.lot)) {resultCard=next.last_result;resultUntil=Date.now()+3200;}
        lastEvent=next.last_result?.event_id;
      }
      data=next;seasonId=next.season?.id||null;
      if(!next.my_team_ids?.includes(teamId))teamId=next.my_team_ids?.[0]||null;
      if(panel&&!next.bot_admin)panel=false;
      renderHeader();renderNav();
      const editing=$('aa-body').contains(document.activeElement)&&document.activeElement?.matches('input,select,textarea');
      if(!background||!editing)renderBody();
    } else {
      if(!data)$('aa-body').innerHTML=empty('Arena unavailable',result.data?.message||'Check your connection and retry.');
      notice(result.data?.message||'Connection interrupted. Refresh before bidding.');
    }
    tick();if(root.open&&!toolView)poll=setTimeout(()=>refresh(true),result.ok?2000:5000);
  }
  function render() {renderHeader();renderNav();renderBody();tick();}
  function renderHeader() {
    if(!$('aa-header'))return;
    const seasons=data?.auctions||[];
    $('aa-header').innerHTML=`<div class="aa-room-identity"><span class="aa-eyebrow">FRANCHISE AUCTION</span><select id="aa-season" aria-label="Select auction">${seasons.length?seasons.map(s=>`<option value="${s.id}" ${s.id===seasonId?'selected':''}>${esc(s.name)}</option>`).join(''):'<option>Auction Arena</option>'}</select></div><div class="aa-header-actions"><span class="aa-room-state"><i class="${data?.season?.status==='live'?'live':''}"></i>${esc(data?.season?.status||'Connecting')}</span>${ui('Refresh auction','refresh')}${ui('Open in Telegram','telegram')}${data?.bot_admin?ui('Bot admin panel','admin','settings'):''}${ui('Back to Mini App','close','back')}</div>`;
  }
  function renderNav() {
    const current=toolView?'more':tab;
    $('aa-nav').innerHTML=[['squads','Squads'],['arena','Arena'],['logs','Logs'],['more','More']].map(([key,label])=>`<button type="button" data-aa-ui="tab" data-tab="${key}" class="${current===key&&!panel?'selected':''}" ${current===key&&!panel?'aria-current="page"':''}><span>${icon(key)}</span>${label}</button>`).join('');
  }
  function renderBody() {
    if(toolView)return;
    if(!data) {$('aa-body').innerHTML=empty('Opening the Arena','Connecting to your shared auction…');return;}
    if(!data.season) {$('aa-body').innerHTML=empty('Your auction will appear here',data.bot_admin?'Open the admin control room to create an auction.':'Join your franchise or open the auction from its Telegram group.')+(data.bot_admin?'<button type="button" class="aa-button aa-primary" data-aa-ui="full-admin">Open admin control room</button>':'');return;}
    const openDetails=[...$('aa-body').querySelectorAll('details[open]')].map(d=>d.dataset.aaTeam);
    $('aa-body').innerHTML=panel?renderAdmin():tab==='arena'?renderArena():tab==='squads'?renderSquads():tab==='logs'?renderLogs():renderMore();
    $('aa-body').querySelectorAll('details[data-aa-team]').forEach(d=>{d.open=openDetails.includes(d.dataset.aaTeam)||(!openDetails.length&&Number(d.dataset.aaTeam)===teamId);});
    setBusy(busy);
  }
  function lotStrip(lot) {
    const bidder=data.teams.find(t=>t.id===lot?.bidder_id);
    return `<div class="aa-lot-strip"><span class="aa-set">${esc(lot?.set_name||'Auction room')}</span><span>BASE <b>${money(lot?.base_lakh)}</b></span><span class="aa-bidder ${bidder?'has-bid':''}">${esc(bidder?.short_name||bidder?.name||'NO BIDS')}</span></div>`;
  }
  function renderArena() {
    const c=data.counts||{}, total=c.total||0, settled=Math.min(total,(c.sold||0)+(c.unsold||0)+(c.withdrawn||0));
    return `<section class="aa-room-progress" aria-label="Auction progress"><div><span><b>${settled}/${total}</b> settled</span><span>${c.sold||0} sold · ${c.unsold||0} unsold · ${c.queued||0} waiting</span></div><progress value="${settled}" max="${total||1}" aria-label="Settled auction players"></progress></section>`+renderLiveContent()+renderUpcoming();
  }
  function renderLiveContent() {
    const l=data.lot, t=own(), result=resultCard&&(!l||Date.now()<resultUntil)?resultCard:null;
    const ownTeams=data.teams.filter(t=>data.my_team_ids?.includes(t.id));
    const chooser=ownTeams.length>1?`<label class="aa-field">Bidding as<select id="aa-team">${ownTeams.map(t=>`<option value="${t.id}" ${t.id===teamId?'selected':''}>${esc(t.name)}</option>`).join('')}</select></label>`:'';
    if(result) return chooser+lotStrip(l||result)+renderResult(result)+`<div class="aa-result-actions">${l?'<button class="aa-button aa-primary" type="button" data-aa-ui="dismiss-result">Go to current player →</button>':data.bot_admin&&data.season.status==='live'?action('Open next player →','admin_next',{},false,'aa-primary'):''}<button class="aa-button" type="button" data-aa-ui="tab" data-tab="squads">View squads</button></div>`;
    if(!l) return chooser+empty(data.season.status==='completed'?'Auction complete':data.season.status==='setup'?'The stage is set':'Waiting for the next player',data.season.status==='setup'?'Your auction admin is preparing the room. Squads and budgets are ready below.':'Follow the results in Logs or review every franchise in Squads.')+`<div class="aa-mini-stats"><span><b>${data.counts?.sold||0}</b> SOLD</span><span><b>${data.counts?.unsold||0}</b> UNSOLD</span><span><b>${data.counts?.queued||0}</b> IN QUEUE</span></div>`;
    const paused=data.season.status==='paused', c={...context(l),team_id:teamId};
    let controls='', customBid='';
    if(!t) controls='<p class="aa-watch-note">Spectator view · Franchise owners and co-owners can bid.</p>';
    else if(l.status==='rtm_offered'&&l.rtm_holder_id===t.id&&['intent','decision'].includes(l.rtm_stage)) controls=`<p class="aa-watch-note">${l.rtm_stage==='intent'?'Use your Right To Match?':'Match the final price?'} ${money(l.rtm_price_lakh)}</p><div class="aa-split-actions">${action('Match / use RTM','rtm',{...c,accept:true},paused,'aa-primary')}${action('Pass','rtm',{...c,accept:false},paused)}</div>`;
    else {
      controls=action((l.rtm_stage==='final_offer'?'FINAL RAISE: ':'PLACE BID: ')+money(l.next_lakh),'bid',{...c,amount_lakh:l.next_lakh},!!t.bid_reason||paused,'aa-bid-button');
      if(l.rtm_stage==='final_offer'&&l.bidder_id===t.id)controls+=action('Stand at '+money(l.bid_lakh),'rtm_stand',c,paused);
      if(t.bid_reason)controls+=`<p class="aa-watch-note">${esc(t.bid_reason)}</p>`;
      if(data.direct_bids&&!t.bid_reason)customBid=`<details class="aa-custom-bid"><summary>Enter a custom bid</summary><form data-aa-form="bid" data-aa-args="${esc(JSON.stringify(c))}"><label class="aa-field">Bid in lakh · 100 = 1 Cr<input name="amount_lakh" type="number" min="${l.next_lakh}" step="1" value="${l.next_lakh}" required></label><button type="submit" class="aa-button">Place custom bid</button></form></details>`;
    }
    const tiles=[['Overall',l.rating,'white'],['Batting',l.bat_rating,'gold'],['Bowling',l.bowl_rating,'green'],['Edition',l.version||'Base','purple']];
    return chooser+lotStrip(l)+`<article class="aa-player-stage"><div class="aa-timer-line" id="aa-timer-line"></div><span class="aa-lot-number">LOT ${l.number}</span>${portrait(l)}<div class="aa-player-tags"><span>${esc(l.role)}</span><span>${l.overseas?'OVERSEAS':esc(l.country)}</span></div><h1>${esc(l.name)}</h1><div class="aa-player-stats">${tiles.map(([label,value,color])=>`<div><span>${label}</span><strong class="aa-${color}">${esc(value??'—')}</strong></div>`).join('')}</div></article><div class="aa-bid-dock"><div class="aa-price-row"><div><span>CURRENT BID</span><strong>${money(l.bid_lakh)}</strong><small>${l.bidder_id?esc(data.teams.find(t=>t.id===l.bidder_id)?.name||'Leading franchise'):'Opening bid: '+money(l.base_lakh)}</small></div><div class="aa-timer" id="aa-timer"><strong id="aa-seconds">—</strong><span id="aa-timer-label">SEC</span></div></div>${t?`<div class="aa-purse-strip"><span>SQUAD <b>${t.size}/${data.season.max_squad}</b></span><span>OS <b>${t.overseas}/${data.max_overseas}</b></span><span>PURSE <b>${money(t.purse_lakh)}</b></span></div>`:''}<div class="aa-bid-controls">${controls}</div><div class="aa-dock-meta">${customBid}${t?`<p class="aa-dock-limit">Max legal bid <b>${money(t.max_bid_lakh)}</b> · ${Math.max(0,data.season.max_squad-t.size)} squad slots left</p>`:''}</div></div>${t?`<p class="aa-legal-max">Maximum legal bid <b>${money(t.max_bid_lakh)}</b> · ${esc(t.name)}</p>${(t.warnings||[]).map(w=>`<p class="aa-warning">${esc(w.message)}</p>`).join('')}`:''}`;
  }
  function renderResult(p) {
    const sold=p.status==='sold', team=data.teams.find(t=>t.id===p.sold_to_id);
    return `<article class="aa-result-card ${sold?'sold':'unsold'}"><div class="aa-result-heading">${icon('arena')}<div><span>${esc(data.season.name)}</span><b>${sold?'SOLD':'UNSOLD'}</b></div></div>${portrait(p)}<h1>${esc(p.name)}</h1><p>${esc(p.role)} · ${p.overseas?'OVERSEAS':esc(p.country)}</p><div class="aa-result-stamp">${sold?'SOLD · '+money(p.price_lakh):'UNSOLD'}</div>${sold?`<p class="aa-result-team">${esc(team?.name||'Winning franchise')}</p>`:'<p class="aa-result-team">Available for a future round</p>'}</article>`;
  }
  function renderSquadPlayer(t,p) {
    const releasable=t.mine&&p.acquisition==='retained'&&data.season.status==='paused'&&!data.season.published;
    return `<div class="aa-squad-player ${releasable?'aa-releasable':''}"><span><strong>${esc(p.name)}</strong><small>${p.rating} OVR · ${esc(p.role)} · ${esc(p.acquisition)}</small></span><b>${money(p.price_lakh)}</b>${releasable?action('Release & refund','release',{team_id:t.id,lot_id:p.id}):''}</div>`;
  }
  function renderSquads() {
    return `<div class="aa-section-heading"><span>THE FRANCHISES</span><h1>Auction squads</h1><p>Every team. Every signing. Every rupee.</p></div><label class="aa-field aa-search">Find a team<input id="aa-team-search" type="search" placeholder="Search franchises…" value="${esc(teamQuery)}"></label><div class="aa-squad-list">${data.teams.map((t,i)=>{
      const size=t.squad?.length||0, spent=(t.squad||[]).reduce((n,p)=>n+(p.price_lakh||0),0), average=size?((t.squad||[]).reduce((n,p)=>n+p.rating,0)/size).toFixed(1):'—';
      const logo=/^(https?:\/\/|\/(?!\/))/.test(t.logo_url||'')?`<img src="${esc(t.logo_url)}" alt="" loading="lazy">`:'';
      return `<details data-aa-team="${t.id}" data-aa-team-name="${esc(t.name.toLowerCase())}" ${t.name.toLowerCase().includes(teamQuery.trim().toLowerCase())?'':'hidden'}><summary><span class="aa-team-rank">${i+1}</span><span class="aa-team-logo">${logo||esc(t.short_name||initials(t.name))}</span><span class="aa-team-name"><strong>${esc(t.name)}</strong><small class="${t.mine?'mine':''}">${t.mine?'● Your franchise':size>=data.season.max_squad?'SQUAD COMPLETE':'AVAILABLE'}</small></span><span class="aa-team-budget"><b>${money(t.purse_lakh)}</b><small>${t.size}/${data.season.max_squad}</small></span>${icon('chevron')}</summary><div class="aa-squad-detail"><div class="aa-squad-metrics"><span>SPENT <b>${money(spent)}</b></span><span>RATING <b>${average}</b></span><span>OS <b>${t.overseas}/${data.max_overseas}</b></span></div>${size?(t.squad||[]).map(p=>renderSquadPlayer(t,p)).join(''):'<p class="aa-no-players">No players signed yet</p>'}${(t.warnings||[]).map(w=>`<p class="aa-warning">${esc(w.message)}</p>`).join('')}${data.bot_admin?`<button type="button" class="aa-button" data-aa-ui="manage-team" data-team="${t.id}">Manage players & purse →</button>`:''}</div></details>`;
    }).join('')||empty('No franchises yet','Your auction admin will add the teams.')}</div>`;
  }
  function ago(at) {const seconds=Math.max(0,Math.floor((Date.now()+offset-Date.parse(at))/1000));return seconds<60?seconds+'s ago':seconds<3600?Math.floor(seconds/60)+'m ago':new Date(at).toLocaleString();}
  function renderUpcoming() {
    const players=data.upcoming||[];
    if(!players.length)return '';
    return `<details class="aa-upcoming"><summary><span>Coming up next</span><b>${data.counts?.queued||players.length} waiting</b>${icon('chevron')}</summary>${players.map(p=>`<div class="aa-upcoming-player"><span><strong>${esc(p.name)}</strong><small>${p.rating} OVR · ${esc(p.role)} · ${esc(p.set_name)}</small></span><b>${money(p.base_lakh)}</b>${data.bot_admin?action('Put next','admin_force_next',{lot_id:p.id}):''}</div>`).join('')}<button type="button" class="aa-button" data-aa-ui="tool" data-tool="pool">Browse all players →</button></details>`;
  }
  function renderMore() {
    const items=[['pool','Player pool','Find players by name, role, set and sale status.','arena'],['preseason','Retention & draft','Retention offers, negotiations, expansion picks and readiness.','squads'],['trades','Trades','Build offers, review trades and browse the trade block.','logs'],['rules','Rulebook','Purse, squad, increment, clock and RTM rules.','settings']];
    if(data.bot_admin||data.my_team_ids?.length)items.splice(1,0,['teams',data.bot_admin?'Players & purses':'My purse & bids',data.bot_admin?'Assign players, refund removals and adjust a team’s purse.':'Review your squad budget, purse ledger and bid history.','squads']);
    if(data.bot_admin)items.push(['admin','Setup & rules','Prepare franchises, player sets, retention and the next season.','settings']);
    return `<div class="aa-section-heading"><span>ONE AUCTION WORKSPACE</span><h1>More auction tools</h1><p>Everything stays in this auction. Return to Arena for live bidding.</p></div><div class="aa-more-grid">${items.map(([view,title,description,glyph])=>`<button type="button" class="aa-more-tile" data-aa-ui="tool" data-tool="${view}"><span class="aa-more-icon">${icon(glyph)}</span><span><strong>${esc(title)}</strong><small>${esc(description)}</small></span><span aria-hidden="true">→</span></button>`).join('')}</div><section class="aa-download-card"><div><h2>Download auction results</h2><p>A CSV snapshot of every franchise, signing price and remaining purse.</p></div><button type="button" class="aa-button aa-primary" data-aa-ui="download-results" ${data.teams?.length?'':'disabled'}>Download CSV</button></section>`;
  }
  async function downloadResults() {
    if(!liveEnough())await refresh();
    if(!liveEnough()||!data?.season){notice('Refresh the auction before downloading results.');return;}
    const rows=[['Franchise','Player','Rating','Role','Country','Signing price (lakh)','Acquisition','Purse remaining (lakh)']];
    for(const t of data.teams||[]) {
      const squad=t.squad?.length?t.squad:[null];
      for(const p of squad)rows.push([t.name,p?.name||'',p?.rating??'',p?.role||'',p?.country||'',p?.price_lakh??'',p?.acquisition||'',t.purse_lakh]);
    }
    const cell=value=>{
      let text=String(value??'');
      if(typeof value!=='number'&&/^[\s]*[=+\-@\t\r\n]/.test(text))text="'"+text;
      return '"'+text.replaceAll('"','""')+'"';
    };
    const blob=new Blob(['\uFEFF'+rows.map(row=>row.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'});
    const url=URL.createObjectURL(blob), link=document.createElement('a');
    link.href=url;link.download=`auction-${data.season.id}-results.csv`;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),10000);notice('Auction results downloaded.');
  }
  function renderLogs() {
    const bids=logTab==='bids', events=(data.events||[]).filter(e=>e.kind!=='miniapp_admin');
    return `<div class="aa-log-tabs"><button type="button" class="${!bids?'selected':''}" data-aa-ui="logs" data-log="activity">${icon('logs')} Live activity</button><button type="button" class="${bids?'selected':''}" data-aa-ui="logs" data-log="bids">Current player bids</button></div><ol class="aa-timeline">${(bids?data.bids:events).map(e=>`<li><span class="aa-event-dot ${!bids&&e.kind==='lot_sold'?'sold':!bids&&e.kind==='lot_unsold'?'unsold':''}"></span><p>${bids?esc(e.team)+' bid '+money(e.amount_lakh)+(e.void?' · voided':''):esc(e.text)}</p><time datetime="${esc(e.at)}">${esc(ago(e.at))}</time></li>`).join('')||'<li><p>No activity yet. Updates will appear here as the auction runs.</p></li>'}</ol>`;
  }
  async function openAdmin() {
    if(!data?.bot_admin)return;
    leaveTools();
    setBusy(true);
    const result=await api('/api/webapp/auction',{season_id:seasonId,view:'admin'},10000);
    setBusy(false);
    if(!root.open)return;
    if(!result.ok||!result.data.bot_admin) {
      if(result.status===403){data.bot_admin=false;data.admin=false;}
      panel=false;tab='more';render();notice(result.data?.message||'Only bot admins can enter this panel.','error');return;
    }
    panel=true;await refresh();render();root.scrollTop=0;
  }
  function renderAdmin() {
    if(!data.bot_admin)return empty('Bot admin access required','Return to the Arena to follow the auction.');
    const l=data.lot, s=data.season, c=l?context(l):{};
    return `<div class="aa-admin-heading"><div><span class="aa-eyebrow">VERIFIED BOT ADMIN</span><h1>Auction control panel</h1><p>Run the room and manage your franchises.</p></div>${ui('Close admin panel','dismiss-panel','close')}</div><section class="aa-admin-card"><h2>Live controls</h2><div class="aa-admin-actions">${action(s.status==='paused'?'Resume auction':'Start auction','admin_start',{},!['setup','paused'].includes(s.status),'aa-primary')}${action('Pause auction','admin_pause',{},s.status!=='live')}${action('Next player','admin_next',{},!!l||s.status!=='live')}</div>${l?`<h3>${esc(l.name)}</h3><p class="aa-subtle">${money(l.bid_lakh||l.base_lakh)} · ${l.bids} bids</p>${l.status==='on_block'?`<div class="aa-admin-actions">${action('Sell player','admin_sold',c,!l.bidder_id,'aa-positive')}${action('Mark unsold','admin_unsold',c,false,'aa-danger')}${action('+15 seconds','admin_extend',{...c,seconds:15})}${action('Undo last bid','admin_undo_bid',c,!l.bids)}</div><details class="aa-custom-bid"><summary>Bid on behalf of a franchise</summary><form data-aa-form="admin_bid" data-aa-args="${esc(JSON.stringify(c))}"><label class="aa-field">Franchise<select name="team_id">${data.teams.map(t=>`<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select></label><label class="aa-field">Bid in lakh<input type="number" name="amount_lakh" min="${l.next_lakh}" step="1" value="${l.next_lakh}" required></label><button type="submit" class="aa-button">Place admin bid</button></form></details>`:`<div class="aa-admin-actions">${l.rtm_stage==='final_offer'?action('Stand on final offer','admin_rtm_stand',c):action('Accept RTM','admin_rtm',{...c,accept:true})+action('Decline RTM','admin_rtm',{...c,accept:false})}</div>`}`:'<p class="aa-subtle">No player is on the block.</p>'}</section><section class="aa-admin-card"><h2>Players & team purses</h2><p class="aa-subtle">Add or remove players, add or deduct funds, set a balance and review the ledger.</p>${data.teams.map(t=>`<button type="button" class="aa-admin-team" data-aa-ui="manage-team" data-team="${t.id}"><span>${esc(t.name)}<small>${t.size}/${s.max_squad} players</small></span><b>${money(t.purse_lakh)} →</b></button>`).join('')}</section><section class="aa-admin-card"><h2>Season setup & rules</h2><p class="aa-subtle">Player pool, owners, retention, draft, RTM, rules, imports and exports.</p><button type="button" class="aa-button aa-primary" data-aa-ui="full-admin">Open setup & rules →</button></section>`;
  }
  function setBusy(value) {
    busy=value;root?.setAttribute('aria-busy',String(value));
    root?.querySelectorAll('button,input,select').forEach(b=>{
      if(value){if(!('aaBusy' in b.dataset))b.dataset.aaBusy=b.disabled?'1':'0';b.disabled=true;}
      else if('aaBusy' in b.dataset){b.disabled=b.dataset.aaBusy==='1';delete b.dataset.aaBusy;}
    });
  }
  async function confirm(message) {
    if(tg?.showConfirm) {try{return await new Promise(resolve=>tg.showConfirm(message,resolve));}catch(_) {}}
    return window.confirm(message);
  }
  async function act(name,args) {
    if(busy||!data?.season)return;
    if(name.startsWith('admin_')&&!data.bot_admin){notice('Only bot admins can use this panel.');return;}
    if(['bid','rtm','rtm_stand'].includes(name)&&!liveEnough()){notice('Refresh the live room before acting.');await refresh();return;}
    setBusy(true);clearTimeout(poll);epoch++;
    try {
      const message={admin_sold:'Sell the current player at the displayed winning price?',admin_unsold:'Mark the current player unsold?',release:'Release this retained player into the auction pool and refund the original retention price? RTM cards stay unchanged.',rtm:'Confirm your Right To Match decision?'}[name];
      if(message&&!(await confirm(message)))return;
      const result=await api('/api/webapp/auction/action',{...args,action:name,season_id:seasonId},10000);
      if(root.open)await refresh();
      if(result.ok){haptic('medium');notice(result.data.message||'Auction updated.');}
      else notice(result.status===0?'Connection interrupted. Refresh to check the result before retrying.':result.data?.message||'Action refused. Refresh and try again.');
    } finally {setBusy(false);tick();if(root.open&&!poll)poll=setTimeout(()=>refresh(true),2000);}
  }
  function tick() {
    if(!root?.open)return;
    const live=liveEnough();
    const connection=$('aa-connection');connection.textContent=toolView?'Auction tools · shared with Telegram':live?'Live updates · shared with Telegram':'Updates delayed · refresh before bidding';connection.classList.toggle('delayed',!toolView&&!live);
    const l=data?.lot;
    const left=l?.deadline?Math.max(0,Math.ceil((Date.parse(l.deadline)-Date.now()-offset)/1000)):null;
    if($('aa-seconds')) {
      const paused=data.season.status==='paused';
      $('aa-seconds').textContent=paused?'Ⅱ':left===null?'—':left;
      $('aa-timer-label').textContent=paused?'PAUSED':left===0?'WAIT':'SEC';
      $('aa-timer').classList.toggle('urgent',left!==null&&left<=10&&!paused);
      $('aa-timer-line').style.width=(paused?100:Math.min(100,(left||0)/(data.clock_seconds||30)*100))+'%';
    }
    root.querySelectorAll('[data-aa-action="bid"], [data-aa-action="rtm"], [data-aa-action="rtm_stand"], [data-aa-form="bid"] button').forEach(b=>{
      if(!live||left===0||data?.season?.status!=='live'||busy)b.disabled=true;
    });
    if(resultCard&&data?.lot&&resultUntil&&Date.now()>=resultUntil){resultUntil=0;resultCard=null;if(tab==='arena'&&!panel)renderBody();}
  }
  document.addEventListener('visibilitychange',()=>{clearTimeout(poll);if(root?.open&&!document.hidden)refresh();});
  window.addEventListener('online',()=>{if(root?.open)refresh();});
  return {open,close,stop,showTab,syncSeason,reflectToolBusy,notify:notice};
})();
