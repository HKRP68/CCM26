"""Settings forms use real API shapes and preserve explicit rule choices."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_franchise_website import ADMIN, DELEGATE, site  # noqa: F401


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required')
def test_manager_settings_forms_and_selected_team_actions(site):
    from services import auction_webapp as W
    site.A.add_auction_admin(site.db, DELEGATE, by_tg_id=ADMIN)
    site.A.set_franchise_picks(site.db, site.season, site.owner, 1)
    site.A.set_franchise_picks(site.db, site.season, site.other, 1)
    site.season.draft_timeout_action = 'shortlist'
    site.db.commit()
    admin = W.payload(site.db, site.season, DELEGATE, [site.season], {'view':'admin'})
    admin['settings']['retention_mode'] = 'ranges'
    admin['trade_deadline_at'] = '2035-01-01T12:00:00Z'
    admin['trade_deadline_matches'] = 5
    preseason = {str(t.id): W.payload(site.db, site.season, DELEGATE, [site.season], {'view':'preseason', 'team_id':t.id}) for t in (site.owner, site.other)}
    source = Path(__file__).resolve().parents[1] / 'static/js/auction_miniapp.js'
    script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8'),fixtures=JSON.parse(process.argv[2]);
const handlers={},calls=[],notices=[];
const elements=Object.fromEntries(['auction-app','au-body','au-title','au-status','au-nav','au-connection'].map(id=>[id,{
 dataset:{},innerHTML:'',textContent:'',setAttribute(){},addEventListener(name,fn){if(id==='auction-app')handlers[name]=fn;},
 querySelectorAll(){return[];},contains(){return false;}
}]));
const first=Number(Object.keys(fixtures.preseason)[0]),second=Number(Object.keys(fixtures.preseason)[1]);
const context=vm.createContext({document:{hidden:false,getElementById:id=>elements[id],addEventListener(){},querySelectorAll(){return[];}},
 window:{confirm(){return true;},addEventListener(){}},currentScreen:'auction',escapeHtml:v=>String(v??''),haptic(){},
 FormData:class{constructor(form){return new Map(Object.entries(form.values));}},
 AuctionArena:{reflectToolBusy(){},reflectIdentity(){},notify(message){notices.push(message);},syncSeason:async()=>{},showTab(){}},
 api:async(url,body)=>{calls.push({url,body});return {ok:true,status:200,data:url.endsWith('/action')?{}:
 body.view==='preseason'?fixtures.preseason[String(body.team_id||first)]:fixtures.admin};},
 setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){}});
const settle=()=>new Promise(setImmediate);
async function submit(action,values,args={}){const form={dataset:{auForm:action,auArgs:JSON.stringify(args)},values};await handlers.submit({target:{closest:()=>form},preventDefault(){}});}
const lastAction=()=>calls.filter(call=>call.url.endsWith('/action')).at(-1).body;
(async()=>{
 vm.runInContext(source,context);await vm.runInContext('AuctionApp.openTools("admin",'+fixtures.admin.season.id+')',context);
 const html=elements['au-body'].innerHTML;
 for(const name of ['currency_label','retention_mode','retention_categories','retention_prices','retention_max_spend_lakh','retention_range_max_total','rtm_window_seconds','team_managers','timeout_action','deadline_at','lot_ids_a'])assert(html.includes('name="'+name+'"'),'Missing management field '+name);
 assert.match(html,/<option value="shortlist" selected/,'Saving a draft clock preserves shortlist timeout');
 assert.match(html,/<option value="ranges" selected/,'Saving retention rules preserves ranges mode');
 assert(html.includes('value="2035-01-01T12:00:00Z"'),'Current trade deadline must populate the form');
 assert(html.includes('value="5"'),'Current deadline match count must populate the form');
 assert(!html.includes('data-au-form="admin_add_admin"'),'Delegated managers cannot appoint global managers');
 for(const name of ['admin_pool_clear','admin_set_delete','admin_set_move','admin_retention_offer','admin_trade_force_form'])assert(html.includes(name),'Missing management control '+name);
 await submit('admin_settings_form',{retention_mode:'ranges',retention_max_spend_lakh:'',retention_min_rating:'',retention_range_max_total:'',retention_categories:'',retention_prices:'',retention_deadline:'',rtm_enabled:'false'});
 const settings=lastAction().settings;assert.equal(settings.retention_mode,'ranges');
 for(const key of ['retention_max_spend_lakh','retention_min_rating','retention_range_max_total','retention_categories','retention_prices','retention_deadline'])assert.equal(settings[key],null,key+' clears explicitly');
 assert.equal(settings.rtm_enabled,false);
 await submit('admin_franchise',{name:'Team',co_owners:'101, 102',team_managers:'201; 202',owner_tg_id:'303'});
 assert.deepEqual(Array.from(lastAction().team_managers),['201','202']);assert.deepEqual(Array.from(lastAction().co_owners),['101','102']);
 await submit('admin_auto_sets_form',{marquee:'2',set_size:'4',min_rating:'80',pool_size:'',versions:'Base, Icon',editions:'true',replace:'false'});
 assert.equal(lastAction().options.min_rating,80);assert.equal(lastAction().options.editions,true);assert.equal(lastAction().replace,false);
 await submit('admin_trade_deadline_form',{deadline_at:'',deadline_matches:''});assert.equal(lastAction().deadline_at,null);assert.equal(lastAction().deadline_matches,null);
 await vm.runInContext('AuctionApp.openTools("preseason",'+fixtures.admin.season.id+')',context);
 assert.match(elements['au-body'].innerHTML,/id="au-preseason-team"/,'Managers can choose every franchise');
 assert.match(elements['au-body'].innerHTML,/admin_draft_pick/,'Managers get explicit on-behalf draft actions');
 handlers.change({target:{id:'au-preseason-team',value:String(second),dataset:{}}});await settle();
 assert.equal(calls.filter(c=>!c.url.endsWith('/action')).at(-1).body.team_id,second,'Selection is not reset to an owned franchise');
 assert.match(elements['au-body'].innerHTML,new RegExp('value="'+second+'" selected'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run(['node','-e',script,str(source),json.dumps(dict(admin=admin,preseason=preseason),default=str)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
