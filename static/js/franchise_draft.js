/* First-class browser entry to the existing franchise draft and Telegram controls. */
const FranchiseDraftApp = (() => {
  'use strict';
  let host=null, seasonId=null, part='board', page=1, teamId=null, timer=null, epoch=0;
  const tabs={board:'📊 Draft Board',pool:'🏏 Available Players',order:'🔄 Draft Order',squads:'👥 Squads',purses:'💰 Purses',history:'📜 History',rules:'📖 Rules'};
  const esc=value=>escapeHtml(String(value??''));
  function stop() {clearTimeout(timer);timer=null;epoch++;if(host)host.innerHTML='';host=null;}
  function link(url,label) {
    return url && /^https:\/\/t\.me\//.test(url) ? `<a class="au-btn primary" href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>` : '';
  }
  async function refresh() {
    clearTimeout(timer);
    if(!host)return;
    const root=host, version=++epoch;
    const refreshButton=root.querySelector('[data-fd-refresh]');
    if(refreshButton)refreshButton.disabled=true;
    let result;
    try {
      result=await api('/api/webapp/auction',{mode:'draft',view:'franchisedraft',season_id:seasonId,part,page,team_id:teamId},12000);
    } catch (_) {result={ok:false,data:{message:'The draft could not be loaded. Refresh to try again.'}};}
    if(host!==root || version!==epoch)return;
    if(!result.ok) {
      root.innerHTML=`<section class="au-card"><h2>🏏 Franchise Draft</h2><p role="alert">${esc(result.data?.message||'Sign in with Telegram to open your draft.')}</p><button class="au-btn" data-fd-refresh>Retry</button><button class="au-btn" data-fd-clear>Choose another draft</button></section>`;
      return;
    }
    const data=result.data;
    seasonId=data.season?.id||null;page=data.page||1;
    if(part==='squads')teamId=data.team_id||null;
    const seasons=(data.auctions||[]).filter(s=>s.mode==='draft');
    const choices=seasons.map(s=>`<option value="${Number(s.id)}" ${s.id===seasonId?'selected':''}>${esc(s.name)} · ${esc(s.status)}</option>`).join('');
    const select=choices?`<label class="au-field"><span>Franchise Draft season</span><select class="au-select" data-fd-season>${choices}</select></label>`:'';
    const nav=Object.entries(tabs).map(([key,label])=>`<button class="au-btn ${part===key?'primary':''}" ${part===key?'aria-current="page"':''} data-fd-part="${key}">${esc(label)}</button>`).join('');
    const teams=part==='squads'&&data.teams?.length?`<label class="au-field"><span>Franchise</span><select class="au-select" data-fd-team>${data.teams.map(t=>`<option value="${Number(t.id)}" ${t.id===teamId?'selected':''}>${esc(t.name)}</option>`).join('')}</select></label>`:'';
    const pages=data.pages>1?`<div class="au-actions"><button class="au-btn" data-fd-page="${page-1}" ${page<=1?'disabled':''}>← Previous</button><span>Page ${page}/${data.pages}</span><button class="au-btn" data-fd-page="${page+1}" ${page>=data.pages?'disabled':''}>Next →</button></div>`:'';
    root.innerHTML=`<section class="au-card"><div class="au-section-head"><h2>🏏 Franchise Draft</h2><button class="au-btn" data-fd-refresh>↻ Refresh</button></div><p class="au-muted">Fixed-price signings · Rotating order · Shared franchise squads and purses</p>${select}<div class="au-actions">${link(data.draft_bot_url,'Open Draft Controls in Telegram')}${link(data.draft_group_url,'Open Draft Group')}</div></section>${data.season?`<nav class="au-actions" aria-label="Franchise Draft views">${nav}</nav>${teams}<section class="au-card fd-rich">${data.draft_html||''}</section>${pages}`:`<section class="au-card"><h3>No franchise drafts available yet</h3><p>A Draft Admin can create one with <code>/dnew Season Name</code>, or continue an existing competition through <b>Existing leagues → Draft</b>. Owners see the drafts assigned to their Telegram account.</p></section>`}`;
    if(data.season)timer=setTimeout(refresh,5000);
  }
  async function open(target='screen-franchisedraft',id=null) {
    stop();host=document.getElementById(target);
    if(!host)return;
    seasonId=id;part='board';page=1;teamId=null;
    if(!host.dataset.fdBound) {
      host.dataset.fdBound='1';
      host.addEventListener('click',event=>{
        const button=event.target.closest('[data-fd-refresh],[data-fd-part],[data-fd-page],[data-fd-clear]');
        if(!button||button.disabled)return;
        if(button.dataset.fdPart){part=button.dataset.fdPart;page=1;}
        if(button.dataset.fdPage)page=Number(button.dataset.fdPage);
        if(button.hasAttribute('data-fd-clear')){seasonId=null;teamId=null;part='board';page=1;}
        refresh();
      });
      host.addEventListener('change',event=>{
        if(event.target.hasAttribute('data-fd-season')){seasonId=Number(event.target.value);page=1;teamId=null;}
        else if(event.target.hasAttribute('data-fd-team'))teamId=Number(event.target.value);
        else return;
        refresh();
      });
    }
    host.innerHTML='<section class="au-card" role="status">Opening Franchise Draft…</section>';
    await refresh();
  }
  return {open,stop};
})();
