"""Check the single Arena entry and its embedded auction tools.

Uses Node's built-in VM with a small DOM adapter; no browser package or
network access is needed. Browser walkthroughs cover the interactive forms.
"""

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is needed to run the Mini App controller')
class AuctionMiniappUITests(unittest.TestCase):
    def test_single_arena_entry_and_embedded_team_management(self):
        source = Path(__file__).resolve().parents[1] / 'static/js/auction_miniapp.js'
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

async function boot(data,view='teams') {
  const handlers={}, calls=[], confirmations=[], notices=[], arenaCalls=[], parentCalls=[];
  let holdRead=false, releaseRead=null;
  let editing=false,backgroundPoll=null;
  const control={dataset:{},disabled:false};
  const ids = ['auction-app', 'au-body', 'au-nav', 'au-title', 'au-status', 'au-connection'];
  const elements = Object.fromEntries(ids.map(id => [id, {
    dataset: {}, innerHTML: '', textContent: '', disabled: false, setAttribute() {},
    addEventListener(type,fn) { if(id==='auction-app')handlers[type]=fn; }, querySelectorAll() { return []; }, contains() { return editing; }
  }]));
  const context = vm.createContext({
    document: {getElementById: id => elements[id], addEventListener() {},
               querySelectorAll(selector) { return selector.startsWith('#au-body button')?[control]:[]; }, activeElement: null},
    window: {addEventListener() {}, confirm(message) { confirmations.push(message); return true; }}, currentScreen: 'auction',
    AuctionArena:{open(id){arenaCalls.push(id);},stop(){},showTab(view){parentCalls.push(view);},reflectToolBusy(){},notify(message){notices.push(message);},syncSeason:async()=>{}},
    FormData: class { constructor(form) { return new Map(Object.entries(form.values)); } },
    escapeHtml: v => String(v ?? ''), haptic() {}, toast(message) {notices.push(message);}, tg: undefined,
    api: async (url,body) => {
      calls.push({url,body});
      if(holdRead&&!url.endsWith('/action')) await new Promise(resolve=>{releaseRead=resolve;});
      return {ok: true, data:url.endsWith('/action')?{}:data};
    },
    setTimeout(fn,delay) { if(delay===5000)backgroundPoll=fn;return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {}
  });
  vm.runInContext(source, context);
  await vm.runInContext(`AuctionApp.openTools(${JSON.stringify(view)},${data.season?.id||null})`, context);
  await new Promise(setImmediate);
  elements.calls=calls; elements.confirmations=confirmations; elements.arenaCalls=arenaCalls; elements.parentCalls=parentCalls; elements.context=context;
  elements.notices=notices; elements.control=control;
  elements.holdRead=()=>{holdRead=true;};
  elements.releaseRead=()=>{holdRead=false;releaseRead();};
  elements.setData=value=>{data=value;};
  elements.setEditing=value=>{editing=value;context.document.activeElement=value?{matches:()=>true}:null;};
  elements.poll=async()=>{assert(backgroundPoll,'Budget tools must keep refreshing');const run=backgroundPoll;backgroundPoll=null;await run();};
  elements.click=async (key,args={})=>handlers.click({target:{closest:selector=>selector==='[data-au-ui]'?{dataset:{auUi:key,...args}}:null}});
  elements.submit=async (action,values,args={})=>{
    const form={dataset:{auForm:action,auArgs:JSON.stringify(args)},values};
    await handlers.submit({target:{closest:()=>form},preventDefault(){}});
  };
  return elements;
}

(async () => {
  const base = {server_time: new Date().toISOString(), auctions: [], my_team_ids: [],
                teams: [], events: [], bids: [], upcoming: [], counts: {}, lot: null};
  const setup = {id: 1, name: 'Test auction', status: 'setup', max_squad: 5};
  for (const season of [null, setup]) {
    const player = await boot({...base, admin: false, season,players:[],sets:[],page:1,pages:1,total:0},'pool');
    assert.doesNotMatch(player['au-body'].innerHTML, /data-au-(form|action)="admin_/);
    assert.doesNotMatch(player['au-nav'].innerHTML, /data-view="admin"|data-view="live"/);
    assert.doesNotMatch(player['au-nav'].innerHTML, /data-view="preseason"|data-view="trades"/);
    assert.match(player['au-nav'].innerHTML, /data-view="teams"/);
    const admin = await boot({...base, admin: true, season,players:[],sets:[],page:1,pages:1,total:0},'pool');
    assert.match(admin['au-nav'].innerHTML, /data-view="admin"/);
    if(!season)assert.match(admin['au-body'].innerHTML,/admin_create/);
    const before=player.calls.length;
    vm.runInContext('AuctionApp.setSeason(42);AuctionApp.open()',player.context);
    assert.equal(player.arenaCalls.at(-1),42);
    assert.equal(player.calls.length,before,'Main entry opens Arena without a duplicate live-room read');
  }
  const team={id:1,name:'Mumbai',mine:true,purse_lakh:1000,max_bid_lakh:1000,size:1,overseas:0,
              retained:0,rtm_left:0,warnings:[],squad:[{id:9,name:'Player One',price_lakh:100,rating:90}]};
  const owner=await boot({...base,admin:false,season:setup,my_team_ids:[1],teams:[team]});
  await owner.click('view',{view:'teams'});
  assert.match(owner['au-body'].innerHTML,/Max legal bid/);
  assert.doesNotMatch(owner['au-body'].innerHTML,/admin_team_|Manage a team/);
  for(const view of ['admin','preseason','trades']) {
    const before=owner.calls.length;await owner.click('view',{view});
    assert.equal(owner.calls.length,before,'Team users cannot open '+view);
  }
  await owner.click('manage-team',{team:'1'});
  await owner.click('export',{format:'franchises'});
  await owner.submit('team_catalogue',{query:'Player'});
  for(const action of ['admin_start','admin_pause','admin_resume','admin_team_purse','admin_team_add_player','admin_team_remove_player','retain','release','trade_open','draft_pick'])await owner.submit(action,{}, {team_id:1});
  assert.equal(owner.calls.filter(c=>c.url.endsWith('/action')||c.url.endsWith('/catalogue')||c.url.endsWith('/export')).length,0);
  const spectator=await boot({...base,admin:false,season:setup});
  for(const view of ['admin','preseason','trades','pool','rules']) {
    const before=spectator.calls.length;await spectator.click('view',{view});
    assert.equal(spectator.calls.length,before,'Spectators cannot open '+view);
  }
  for(const action of ['bid','rtm','rtm_stand','admin_pause','admin_resume','release'])await spectator.submit(action,{}, {team_id:1});
  assert.equal(spectator.calls.filter(c=>c.url.endsWith('/action')).length,0,'Spectators cannot submit auction actions');
  const manager=await boot({...base,admin:true,season:setup,teams:[team],managed_team_id:1,
                            roster_block_reason:'',team_ledger:[]});
  for(const action of ['admin_add_admin','admin_remove_admin'])await manager.submit(action,{telegram_id:'123456'});
  assert.equal(manager.calls.filter(c=>['admin_add_admin','admin_remove_admin'].includes(c.body.action)).length,0,'Delegated managers cannot appoint more managers');
  await manager.click('view',{view:'teams'});
  for(const action of ['admin_team_purse','admin_team_add_player','admin_team_remove_player']) {
    assert.match(manager['au-body'].innerHTML,new RegExp(action==='admin_team_add_player'?'Search player name':action));
  }
  await manager.submit('admin_team_purse',{mode:'deduct',amount_lakh:'200',expected_purse_lakh:'1000',note:'Penalty'},{team_id:1});
  assert.match(manager.confirmations.at(-1),/Mumbai.*10 Cr.*8 Cr/);
  const change=manager.calls.find(call=>call.body.action==='admin_team_purse');
  assert.equal(change.body.expected_purse_lakh,'1000');
  assert.equal(change.body.team_id,1);
  assert.equal(change.body.mode,'deduct');
  await manager.submit('admin_team_remove_player',{lot_id:'9',note:'Correct squad'},{team_id:1});
  assert.match(manager.confirmations.at(-1),/Player One.*Mumbai.*1 Cr/);
  const count=manager.notices.length;
  manager.holdRead();
  const pending=manager.submit('admin_team_purse',{mode:'add',amount_lakh:'25',expected_purse_lakh:'1000',note:'Wait for refresh'},{team_id:1});
  await new Promise(setImmediate);
  assert.equal(manager.control.disabled,true,'Form controls remain busy during the post-action refresh');
  assert.equal(manager.notices.length,count,'Success feedback waits for refreshed forms');
  manager.releaseRead();await pending;
  assert.equal(manager.control.disabled,false);
  assert.equal(manager.notices.length,count+1);
  await manager.click('view',{view:'live'});
  assert.equal(manager.parentCalls.at(-1),'arena','Live links return to the one Arena');
  const demoted=await boot({...base,admin:true,bot_admin:false,season:setup,teams:[team],my_team_ids:[1],managed_team_id:1,roster_block_reason:'',team_ledger:[]});
  assert.match(demoted['au-body'].innerHTML,/admin_team_purse/);demoted.setEditing(true);
  demoted.setData({...base,admin:false,bot_admin:false,season:setup,teams:[team],my_team_ids:[1]});await demoted.poll();
  assert.doesNotMatch(demoted['au-body'].innerHTML,/admin_team_|Manage a team/,'Manager controls disappear during an active form after revocation');
  assert.match(demoted['au-body'].innerHTML,/Max legal bid/);
  await demoted.submit('admin_team_purse',{mode:'add',amount_lakh:'25',expected_purse_lakh:'1000',note:'Revoked'},{team_id:1});
  assert.equal(demoted.calls.filter(c=>c.url.endsWith('/action')).length,0);
  const poolData={...base,season:setup,teams:[team],my_team_ids:[1],players:[{id:9,name:'Player One',status:'queued',rating:90,role:'Batsman',base_lakh:20}],sets:[],page:1,pages:1,total:1};
  const poolManager=await boot({...poolData,admin:true},'pool');
  assert.match(poolManager['au-body'].innerHTML,/admin_force_next/);poolManager.setEditing(true);
  poolManager.setData({...poolData,admin:false});await poolManager.poll();
  assert.doesNotMatch(poolManager['au-body'].innerHTML,/data-au-(form|action)="admin_/,'Player-pool controls also disappear when manager access is removed');
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
