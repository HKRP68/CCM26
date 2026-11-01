/* Browser-only shell for the shared franchise auction workspace. */
'use strict';
window.FRANCHISE_WEBSITE = true;
const tg = null;
let currentScreen = 'home';
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function haptic() {}
function navigate(screen) { if (screen === 'home') FranchiseSite.home(); }
async function api(path, body = {}, timeout = 15000) { return FranchiseSite.request(path, body, timeout); }
const FranchiseSite = (() => {
  const $ = id => document.getElementById(id);
  const config = JSON.parse($('fs-config').textContent);
  const params = new URLSearchParams(window.location.search);
  const positiveId = value => /^\d+$/.test(String(value || '')) && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;
  let selectedSeason = positiveId(params.get('season_id'));
  const chatHint = /^-\d+$/.test(params.get('chat_id') || '') ? params.get('chat_id') : null;
  let startHint = selectedSeason && chatHint ? `auction_${selectedSeason}_c${chatHint}` : '';
  let csrfToken = config.csrf_token || '', user = null, data = null, timer = null, requestId = 0;
  let loginBusy = false, expired = false, toastTimer = null, widgetLoaded = false;
  let stopUpdates=null, updateSeason=null, updateState='fallback', dashboardReading=false, dashboardPending=false;
  const icons = {
    overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    arena:'<path d="m14 3 7 7-4 4-7-7zM11 8l-8 9 4 4 9-8M2 22h11"/>',
    squads:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M21 21v-3a6 6 0 0 0-3-5"/>',
    pool:'<rect x="4" y="3" width="13" height="17" rx="2"/><path d="M8 7h5M8 11h5M8 15h3M20 7v13a2 2 0 0 1-2 2H8"/>',
    picks:'<path d="m12 3 3 6 6 .9-4.5 4.4 1.1 6.2L12 17.5l-5.6 3 1.1-6.2L3 9.9 9 9z"/>',
    trades:'<path d="M3 7h17l-4-4M21 17H4l4 4M20 7l-4 4M4 17l4-4"/>',
    rules:'<path d="M12 5c-3-2-6-2-9 0v15c3-2 6-2 9 0 3-2 6-2 9 0V5c-3-2-6-2-9 0ZM12 5v15"/>',
    settings:'<path d="m9 3-1 3-3 1-2 4 2 2v4l4 3 3-1 3 1 4-3v-4l2-2-2-4-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
    refresh:'<path d="M20 4v6h-6M20 10a8 8 0 1 0-1 8"/>',
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
    logs:'<path d="M4 5v5h5M4 10a8 8 0 1 1 1 8M12 7v5l3 2"/>'
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.overview}</svg>`;
  const money = value => `${data?.season?.currency || '₹'}${(Number(value || 0) / 100).toLocaleString('en-IN', {maximumFractionDigits:2})} Cr`;
  const initials = value => String(value || 'F').trim().split(/\s+/).map(v => v[0]).slice(0,2).join('').toUpperCase();
  const viewNames = {home:'Overview',arena:'Live auction',squads:'Franchises & squads',pool:'Player pool',preseason:'Retention & picks',trades:'Trade centre',rules:'Rulebook',admin:'Auction settings',teams:'Players & purses',logs:'Auction activity'};
  const button = (label, view, style = 'fs-button') => `<button type="button" class="${style}" data-fs-view="${view}">${escapeHtml(label)}${icon('arrow')}</button>`;
  const roleNames = {bot_owner:'Bot Owner',bot_admin:'Bot Admin',auction_manager:'Auction Manager',team_owner:'Team Owner',team_co_owner:'Team Co-owner',team_manager:'Team Manager',spectator:'Spectator'};
  const roleHelp = {bot_owner:'Manages all auction settings and assigns Auction Managers.',bot_admin:'Manages all auction settings and appoints Auction Managers.',auction_manager:'Manages all auction settings, squads, purses, retention, picks and trades.',team_owner:'Bids and answers RTM for assigned franchises.',team_co_owner:'Bids and answers RTM for assigned franchises.',team_manager:'Bids and answers RTM for assigned franchises without changing auction settings.',spectator:'Watches live bidding, squads and auction results.'};
  const roleIds = () => Array.isArray(data?.roles) ? data.roles.filter(role => Object.hasOwn(roleNames,role)) : !data ? [] : [data.bot_owner?'bot_owner':data.bot_admin?'bot_admin':data.admin?'auction_manager':data.my_team_ids?.length?'team_owner':'spectator'];
  const teamRoles = team => (Array.isArray(team.my_roles)?team.my_roles:[team.my_role||'team_owner']).filter(role=>Object.hasOwn(roleNames,role)).map(role=>roleNames[role]).join(' · ');
  const roleBadges = () => roleIds().map(role => `<span class="fs-role fs-role-${role}">${roleNames[role]}</span>`).join('');
  const roleGuide = () => `<details class="fs-role-guide"><summary>What your roles allow</summary>${roleIds().map(role => `<p><b>${roleNames[role]}</b> ${roleHelp[role]}</p>`).join('')}</details>`;

  function renderIcons() { document.querySelectorAll('[data-fs-icon]').forEach(el => {el.innerHTML = icon(el.dataset.fsIcon);}); }
  function toast(message) {
    clearTimeout(toastTimer);$('fs-toast').textContent = message;$('fs-toast').hidden = false;
    toastTimer = setTimeout(() => {$('fs-toast').hidden = true;},5500);
  }
  function setStatus(message = '', retry = false) {
    $('fs-page-status').classList.toggle('has-error', retry);
    $('fs-page-status').innerHTML = escapeHtml(message) + (retry ? '<button type="button" data-fs-retry="true">Try again</button>' : '');
  }
  function setLoginError(message, retry = false) {
    const el = $('fs-login-error');el.hidden = !message;
    el.innerHTML = escapeHtml(message) + (retry ? '<button type="button" data-fs-widget-retry="true">Retry sign-in</button>' : '');
  }
  function requestHeaders(path = '/api/webapp/auction') {
    const headers = {'Content-Type':'application/json','X-CSRF-Token':csrfToken};
    if(startHint && path.startsWith('/api/webapp/auction')) headers['X-Tg-Start-Param'] = startHint;
    return headers;
  }
  async function request(path, body = {}, timeout = 15000) {
    const controller = new AbortController(), limit = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = requestHeaders(path);
      const response = await fetch(path, {method:'POST',credentials:'same-origin',cache:'no-store',headers,body:JSON.stringify(body),signal:controller.signal});
      let result;
      try { result = await response.json(); }
      catch (_) { result = {ok:false,message:'The server returned an unexpected response. Refresh and try again.'}; }
      if(response.status === 401 && user && path.startsWith('/api/webapp/auction')) sessionExpired();
      return {ok:response.ok && result.ok !== false,data:result,status:response.status};
    } catch (error) {
      return {ok:false,status:0,data:{ok:false,message:error.name === 'AbortError' ? 'The request timed out. Refresh to check the latest state.' : 'Could not connect. Check your connection and try again.'}};
    } finally { clearTimeout(limit); }
  }
  function updateAccess() {
    document.querySelectorAll('[data-fs-access]').forEach(el => {
      el.hidden = !user || !(data?.admin || (el.dataset.fsAccess === 'owner' && data?.my_team_ids?.length));
    });
    $('fs-signout').hidden = !user;
    const name = user ? user.first_name || user.username || 'Telegram user' : 'Your franchise awaits';
    $('fs-account').innerHTML = `<span class="fs-avatar" aria-hidden="true">${escapeHtml(initials(name))}</span><div><b>${escapeHtml(name)}</b><small>${user ? data ? roleIds().map(role => roleNames[role]).join(' · ') : 'Checking auction role…' : 'Sign in with Telegram'}</small></div>`;
    const roles = $('fs-current-roles');
    if(roles) {roles.hidden = !user || !data;roles.innerHTML = roleBadges();}
  }
  function showLanding() {
    clearTimeout(timer);stopDashboardUpdates();data = null;updateAccess();
    $('fs-landing').hidden = false;$('fs-dashboard').hidden = true;
    $('fs-title').innerHTML = 'Build a team.<br>Own the season.';
    $('fs-login-box').hidden = !config.login_configured;
    $('fs-login-setup').hidden = !!config.login_configured;
    if(config.login_configured) loadWidget();
  }
  function loadWidget() {
    if(widgetLoaded || !config.bot_username) return;
    widgetLoaded = true;setLoginError('');
    const script = document.createElement('script');
    script.src = 'https://telegram.org/js/telegram-widget.js?22';script.async = true;
    script.setAttribute('data-telegram-login', config.bot_username);
    script.setAttribute('data-size','large');script.setAttribute('data-radius','8');
    script.setAttribute('data-onauth','FranchiseSite.telegramLogin(user)');
    script.setAttribute('data-userpic','false');
    script.onerror = () => {widgetLoaded = false;setLoginError('Telegram sign-in could not load. Check your connection and try again.',true);};
    $('fs-telegram-login').replaceChildren(script);
  }
  async function sessionCheck() {
    setStatus('Opening your workspace…');
    try {
      const controller = new AbortController();const timeout = setTimeout(() => controller.abort(),12000);
      let response;
      try {response = await fetch('/api/franchise/auth/session',{credentials:'same-origin',cache:'no-store',signal:controller.signal});}
      finally {clearTimeout(timeout);}
      const result = await response.json();
      if(!response.ok || result.ok === false) throw new Error('Session unavailable');
      csrfToken = result.csrf_token || csrfToken;user = result.user || null;expired = false;setStatus('');
      if(user) await home(); else showLanding();
    } catch (_) {
      $('fs-landing').hidden = true;$('fs-dashboard').hidden = true;
      setStatus('Your workspace could not be opened. Check your connection, then try again.',true);
    }
  }
  async function telegramLogin(signedUser) {
    if(loginBusy) return;
    loginBusy = true;setLoginError('Signing you in…');
    $('fs-telegram-login').setAttribute('aria-busy','true');
    try {
      const result = await request('/api/franchise/auth/login',signedUser);
      if(!result.ok) {
        setLoginError(result.data.message || 'Sign-in could not be verified. Please try Telegram sign-in again.');
        // A restarted server may have invalidated the pre-login session token.
        if(result.status === 403) await refreshCsrf();
        return;
      }
      csrfToken = result.data.csrf_token || csrfToken;
      user = result.data.user || null;expired = false;
      if(!user) {await sessionCheck();return;}
      setLoginError('');await home();
    } finally {loginBusy = false;$('fs-telegram-login').setAttribute('aria-busy','false');}
  }
  async function refreshCsrf() {
    try {
      const response = await fetch('/api/franchise/auth/session',{credentials:'same-origin',cache:'no-store'});
      const result = await response.json();
      if(response.ok && result.csrf_token) csrfToken = result.csrf_token;
    } catch (_) {}
  }
  async function sessionExpired() {
    if(expired) return;
    expired = true;clearTimeout(timer);stopDashboardUpdates();user = null;data = null;requestId++;
    if(typeof AuctionApp !== 'undefined') AuctionApp.stop();
    currentScreen = 'home';setStatus('');showLanding();toast('Your session expired. Sign in with Telegram to continue.');
    await refreshCsrf();
  }
  async function signOut() {
    $('fs-signout').disabled = true;
    try {
      const result = await request('/api/franchise/auth/logout');
      if(!result.ok) {toast(result.data.message || 'Sign-out could not be completed. Try again.');return;}
      clearTimeout(timer);user = null;data = null;expired = false;requestId++;currentScreen = 'home';
      if(typeof AuctionApp !== 'undefined') AuctionApp.stop();
      csrfToken = result.data.csrf_token || '';
      showLanding();setStatus('');await refreshCsrf();
    } finally {$('fs-signout').disabled = false;}
  }
  async function home() {
    currentScreen = 'home';clearTimeout(timer);
    document.querySelectorAll('.fs-navigation button').forEach(el => {el.classList.toggle('selected',el.dataset.fsView === 'home');if(el.dataset.fsView === 'home')el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
    if(!user) {showLanding();return;}
    $('fs-landing').hidden = true;$('fs-dashboard').hidden = false;
    $('fs-title').textContent = 'Your season, at a glance.';updateAccess();
    await refreshDashboard();
  }
  async function refreshDashboard(background = false) {
    clearTimeout(timer);
    if(!user || currentScreen !== 'home' || document.hidden) return;
    if(dashboardReading){dashboardPending=true;return;}
    dashboardReading=true;
    const id = ++requestId;
    const sent=Date.now();
    try {
    if(!background) {$('fs-refresh').disabled = true;setStatus('Loading your auctions…');}
    const result = await request('/api/webapp/auction',{season_id:selectedSeason,view:'arena'},12000);
    if(id !== requestId || !user || currentScreen !== 'home') return;
    $('fs-refresh').disabled = false;
    if(!result.ok) {
      if([401,403].includes(result.status)){data=null;updateAccess();$('fs-dashboard-content').innerHTML='';stopDashboardUpdates();}
      setStatus(result.data.message || 'The auction could not be loaded. Please try again.',true);
      if(selectedSeason && [400,403,404].includes(result.status)) $('fs-page-status').innerHTML += '<button type="button" data-fs-clear-season="true">Show my auctions</button>';
      timer = setTimeout(() => refreshDashboard(true),4000);return;
    }
    data = result.data;selectedSeason = data.season?.id || null;
    updateAccess();setStatus('');renderDashboard();
    $('fs-updated').textContent = (updateState==='live'?'Live connection':'Automatic refresh')+' · updated just now · '+Math.max(0,Date.now()-sent)+' ms round trip';
    ensureDashboardUpdates();
    timer = setTimeout(() => refreshDashboard(true),updateState==='live'?4000:2000);
    } finally {
      dashboardReading=false;
      if(dashboardPending&&user&&currentScreen==='home'&&!document.hidden){dashboardPending=false;clearTimeout(timer);timer=setTimeout(()=>refreshDashboard(true),0);}
    }
  }
  function stopDashboardUpdates() {const stop=stopUpdates;stopUpdates=null;updateSeason=null;updateState='fallback';stop?.();}
  function ensureDashboardUpdates() {
    if(typeof AuctionUpdates==='undefined'||!selectedSeason||!user||currentScreen!=='home')return;
    if(stopUpdates&&updateSeason===selectedSeason)return;
    stopDashboardUpdates();updateSeason=selectedSeason;
    stopUpdates=AuctionUpdates.watch({seasonId:selectedSeason,headers:()=>requestHeaders(),
      onChange:()=>refreshDashboard(true),onAccess:()=>refreshDashboard(true),onStatus:status=>{updateState=status.state;}});
  }
  function renderDashboard() {
    const seasons = data.auctions || [], season = data.season;
    $('fs-season').innerHTML = seasons.length ? seasons.map(s => `<option value="${Number(s.id)}" ${s.id === selectedSeason ? 'selected' : ''}>${escapeHtml(s.name)}</option>`).join('') : '<option>No auctions yet</option>';
    $('fs-season').disabled = !seasons.length;
    if(!season) {
      $('fs-dashboard-content').innerHTML = `<section class="fs-empty"><h2>${data.admin ? 'A new season starts here.' : 'Your franchise will appear here.'}</h2><p>${data.admin ? 'Create your auction, assign franchises and prepare the player pool. Retention and draft picks are part of the same workspace.' : 'Your organiser can assign your Telegram account as a Team Owner, Co-owner or Team Manager. To watch as a spectator, open the website link from your auction group.'}</p>${data.admin ? button('Set up your first auction','admin','fs-button fs-button-dark') : button('Refresh auctions','home','fs-button')}</section>`;return;
    }
    const teams = data.teams || [], mine = teams.filter(t => t.mine), counts = data.counts || {};
    const total = Number(counts.total || 0), resolved = Number(counts.sold || 0) + Number(counts.unsold || 0);
    const progress = total ? Math.min(100,Math.round(resolved / total * 100)) : 0;
    const description = season.status === 'live' && data.lot ? `${data.lot.name} is on the block. Follow the clock and place your next bid in the live room.` : season.status === 'paused' ? 'The room is paused. Review squads and budgets while you prepare for the next bid.' : season.status === 'completed' ? 'The auction has finished. Review the squads and every signing from the season.' : season.status === 'cancelled' ? 'This auction has been cancelled. Its recorded squads and auction activity are available to review.' : 'Your season is taking shape. Review the squads, prepare your picks, and be ready when bidding opens.';
    const metrics = [['Franchises',teams.length],['Auction player pool',total],['Players sold at auction',Number(counts.sold || 0)],['Remaining team purses',money(teams.reduce((sum,t) => sum + Number(t.purse_lakh || 0),0))]];
    const displayedTeams = mine.length ? [...mine,...teams.filter(t => !t.mine)] : teams;
    const teamList = displayedTeams.slice(0,5).map(t => `<div class="fs-team ${t.mine ? 'mine' : ''}"><span class="fs-team-avatar" aria-hidden="true">${escapeHtml(t.short_name || initials(t.name))}</span><div class="fs-team-info"><b>${escapeHtml(t.name)}</b><small>${t.mine ? escapeHtml(teamRoles(t))+' · ' : ''}${Number(t.size || 0)} / ${Number(season.max_squad || 0)} players</small></div><div class="fs-team-purse"><strong>${escapeHtml(money(t.purse_lakh))}</strong><small>purse remaining</small></div></div>`).join('');
    const actions = [['arena','arena','Live auction','Bids, countdown & Right To Match'],['squads','squads','Franchises & squads','Every signing and remaining purse']];
    if(data.admin) actions.push(['preseason','picks','Retention & picks','Retain your core, run draft & expansion'],['trades','trades','Trade centre','Offers, approvals & completed moves'],['admin','settings','Auction settings','Teams, player pool, rules & publication']);
    else if(mine.length) actions.push(['pool','pool','Player pool','Search upcoming and available players'],['rules','rules','Season rulebook','Squad rules, bidding & RTM']);
    const actionList = actions.map(([view,ico,title,detail]) => `<button type="button" data-fs-view="${view}"><span>${icon(ico)}</span><span><b>${escapeHtml(title)}</b><small>${escapeHtml(detail)}</small></span><span class="fs-action-arrow" aria-hidden="true">${icon('arrow')}</span></button>`).join('');
    const warnings = mine.flatMap(t => (t.warnings || []).map(w => ({name:t.name,message:w.message}))).slice(0,3);
    const events = (data.events || []).slice(0,4).map(e => `<div class="fs-activity"><p>${escapeHtml(e.text)}</p>${e.at ? `<time datetime="${escapeHtml(e.at)}">${escapeHtml(new Date(e.at).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}))}</time>` : ''}</div>`).join('');
    $('fs-dashboard-content').innerHTML = `<section class="fs-season-banner"><div><div class="fs-role-badges" aria-label="Your auction roles">${roleBadges()}</div><span class="fs-state fs-state-${escapeHtml(season.status)}">${escapeHtml(season.status)}</span><h2>${escapeHtml(season.name)}</h2><p>${escapeHtml(description)}</p>${roleGuide()}</div>${button(season.status === 'live' ? 'Enter live auction' : 'Open auction room','arena','fs-button fs-button-primary')}</section><dl class="fs-metrics">${metrics.map(([label,value]) => `<div class="fs-metric"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl><div class="fs-dashboard-grid"><section class="fs-panel"><div class="fs-panel-head"><h2>${mine.length ? 'Your franchise & the field' : 'The franchises'}</h2><span>TEAM OVERVIEW</span></div>${teamList || '<div class="fs-empty"><p>No franchises have been added yet.</p></div>'}${button('View all squads','squads','fs-text-link')}${warnings.map(w => `<div class="fs-warning"><b>${escapeHtml(w.name)}:</b> ${escapeHtml(w.message)}</div>`).join('')}</section><section class="fs-panel"><div class="fs-panel-head"><h2>Your next move</h2><span>SEASON WORKSPACE</span></div><div class="fs-actions-list">${actionList}</div></section><section class="fs-panel"><div class="fs-panel-head"><h2>Auction progress</h2><span>${progress}% RESOLVED</span></div><div class="fs-progress" role="progressbar" aria-label="Auction lots resolved" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100"><span style="width:${progress}%"></span></div><div class="fs-progress-line"><span>${resolved} of ${total} auction lots resolved</span><span>${Number(counts.unsold || 0)} unsold</span></div><div class="fs-progress-line"><span>${Number(counts.retained || 0)} retained</span><span>${Number(counts.drafted || 0)} draft signings</span></div>${(data.admin || mine.length) ? button('Explore the player pool','pool','fs-text-link') : ''}</section><section class="fs-panel"><div class="fs-panel-head"><h2>From the auction room</h2><span>RECENT ACTIVITY</span></div>${events || '<div class="fs-activity"><p>No activity yet. Season updates will appear here.</p></div>'}${button('View auction activity','logs','fs-text-link')}</section></div>`;
  }
  async function openView(view) {
    if(view === 'home') {await home();return;}
    if(!Object.hasOwn(viewNames,view)) return;
    if(!user) {toast('Sign in with Telegram to open your franchise workspace.');$('fs-login-box').scrollIntoView({behavior:'smooth',block:'center'});return;}
    if(!data) {toast('Wait for your auctions to load, then try again.');return;}
    if(['preseason','trades','admin','teams'].includes(view) && !data.admin) {toast('Your Auction Manager can use these season tools.');return;}
    if(['pool','rules'].includes(view) && !data.admin && !data.my_team_ids?.length) {toast('This section is available to franchise owners and Auction Managers.');return;}
    clearTimeout(timer);stopDashboardUpdates();requestId++;currentScreen = 'auction';
    if(typeof AuctionArena === 'undefined' || typeof AuctionArena.openWorkspace !== 'function') {currentScreen = 'home';toast('The auction workspace could not load. Refresh the page and try again.');return;}
    try {await AuctionArena.openWorkspace(view,selectedSeason);}
    catch (_) {currentScreen='home';toast('The auction could not be opened. Refresh and try again.');timer=setTimeout(() => refreshDashboard(true),2000);}
  }
  function boot() {
    renderIcons();updateAccess();
    document.addEventListener('click',event => {
      const view = event.target.closest('[data-fs-view]');if(view) {openView(view.dataset.fsView);return;}
      if(event.target.closest('[data-fs-retry]')) {if(user)refreshDashboard();else sessionCheck();}
      if(event.target.closest('[data-fs-clear-season]')) {selectedSeason=null;startHint='';refreshDashboard();}
      if(event.target.closest('[data-fs-widget-retry]')) {widgetLoaded=false;loadWidget();}
    });
    $('fs-signout').addEventListener('click',signOut);
    $('fs-refresh').addEventListener('click',() => refreshDashboard());
    $('fs-season').addEventListener('change',() => {selectedSeason=positiveId($('fs-season').value);requestId++;stopDashboardUpdates();refreshDashboard();});
    document.addEventListener('visibilitychange',() => {if(document.hidden)clearTimeout(timer);else if(user && currentScreen === 'home')refreshDashboard(true);});
    window.addEventListener('online',() => {if(user && currentScreen === 'home')refreshDashboard(true);});
    sessionCheck();
  }
  document.addEventListener('DOMContentLoaded',boot);
  return {request,requestHeaders,telegramLogin,home};
})();
