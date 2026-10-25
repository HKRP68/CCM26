"""Boot the actual auction controller for player/admin and empty-room states.

Uses Node's built-in VM with a small DOM adapter; no browser package or
network access is needed. Browser walkthroughs cover the interactive forms.
"""

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is needed to run the Mini App controller')
class AuctionMiniappUITests(unittest.TestCase):
    def test_boot_does_not_require_a_lot_and_players_never_get_admin_setup(self):
        source = Path(__file__).resolve().parents[1] / 'static/js/auction_miniapp.js'
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

async function boot(data) {
  const handlers={}, calls=[], confirmations=[], notices=[];
  let holdRead=false, releaseRead=null;
  const control={dataset:{},disabled:false};
  const ids = ['auction-app', 'au-body', 'au-nav', 'au-season', 'au-status', 'au-connection'];
  const elements = Object.fromEntries(ids.map(id => [id, {
    dataset: {}, innerHTML: '', textContent: '', disabled: false,
    addEventListener(type,fn) { if(id==='auction-app')handlers[type]=fn; }, querySelectorAll() { return []; }, contains() { return false; }
  }]));
  const context = vm.createContext({
    document: {getElementById: id => elements[id], addEventListener() {},
               querySelectorAll(selector) { return selector.startsWith('#au-body button')?[control]:[]; }, activeElement: null},
    window: {addEventListener() {}, confirm(message) { confirmations.push(message); return true; }}, currentScreen: 'auction',
    FormData: class { constructor(form) { return new Map(Object.entries(form.values)); } },
    escapeHtml: v => String(v ?? ''), haptic() {}, toast(message) {notices.push(message);}, tg: undefined,
    api: async (url,body) => {
      calls.push({url,body});
      if(holdRead&&!url.endsWith('/action')) await new Promise(resolve=>{releaseRead=resolve;});
      return {ok: true, data:url.endsWith('/action')?{}:data};
    },
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {}
  });
  vm.runInContext(source, context);
  await vm.runInContext('AuctionApp.open()', context);
  await new Promise(setImmediate);
  elements.calls=calls; elements.confirmations=confirmations;
  elements.notices=notices; elements.control=control;
  elements.holdRead=()=>{holdRead=true;};
  elements.releaseRead=()=>{holdRead=false;releaseRead();};
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
    const player = await boot({...base, admin: false, season});
    assert.doesNotMatch(player['au-body'].innerHTML, /Build the pool|data-au-(form|action)="admin_/);
    assert.doesNotMatch(player['au-nav'].innerHTML, /data-view="admin"/);
    assert.match(player['au-nav'].innerHTML, />Players<\/button>/);
    assert.match(player['au-body'].innerHTML, season ? /admins are preparing/ : /Waiting for your auction/);

    const admin = await boot({...base, admin: true, season});
    assert.match(admin['au-nav'].innerHTML, /data-view="admin"/);
    assert.match(admin['au-body'].innerHTML, season ? /Build the pool/ : /admin_create/);
  }
  const live = await boot({...base, admin: false,
    season: {...setup, status: 'live'}, my_team_ids: [1],
    lot: {id: 1, name: 'Player', number: 1, rating: 90, base_lakh: 20,
          next_lakh: 20, step_lakh: 10, bids: 0, status: 'on_block', rtm_stage: null},
    teams: [{id: 1, name: 'Mine', mine: true, purse_lakh: 1000, max_bid_lakh: 1000, size: 0}]
  });
  assert.match(live['au-body'].innerHTML, /data-au-action="bid"/);
  assert.doesNotMatch(live['au-body'].innerHTML, /data-au-(form|action)="admin_/);
  const team={id:1,name:'Mumbai',purse_lakh:1000,max_bid_lakh:1000,size:1,overseas:0,
              retained:0,rtm_left:0,warnings:[],squad:[{id:9,name:'Player One',price_lakh:100,rating:90}]};
  const owner=await boot({...base,admin:false,season:setup,my_team_ids:[1],teams:[team]});
  await owner.click('view',{view:'teams'});
  assert.match(owner['au-body'].innerHTML,/Squad slots|squad filled|au-progress/);
  assert.doesNotMatch(owner['au-body'].innerHTML,/admin_team_|Manage a team/);
  const manager=await boot({...base,admin:true,season:setup,teams:[team],managed_team_id:1,
                            roster_block_reason:'',team_ledger:[]});
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
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
