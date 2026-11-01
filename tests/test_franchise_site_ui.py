"""Exercise standalone browser auth recovery, role gates and auction navigation."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser controller tests')
class FranchiseSiteUITests(unittest.TestCase):
    def test_browser_controller(self):
        source = Path(__file__).resolve().parents[1] / 'static/js/franchise_site.js'
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const baseAuction = () => ({ok:true,admin:false,roles:['team_owner'],role_labels:['Team Owner'],auctions:[{id:7,name:'Real season'}],season:{id:7,name:'Real season',status:'setup',max_squad:20,currency:'₹'},my_team_ids:[12],teams:[{id:12,name:'Actual franchise',mine:true,my_role:'team_owner',size:2,purse_lakh:7500,short_name:'AF',warnings:[{message:'Needs a wicket keeper'}]}],counts:{total:8,sold:2,unsold:1,retained:1,drafted:0},events:[]});
async function boot(options={}) {
  const handlers={},calls=[],opened=[];let stops=0,failNext=0;
  let serverUser=options.user || null;
  const auction=options.auction || baseAuction();
  function element(id) {
    return {id,dataset:{},hidden:false,innerHTML:'',textContent:'',value:'7',disabled:false,attributes:{},
      classList:{toggle(){},add(){},remove(){}},
      setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];},
      addEventListener(name,fn){handlers[id+':'+name]=fn;},
      replaceChildren(...children){this.children=children;},scrollIntoView(){}};
  }
  const ids=['fs-config','fs-main','fs-signout','fs-navigation','fs-account','fs-current-roles','fs-title','fs-landing','fs-dashboard','fs-login-box','fs-login-error','fs-login-setup','fs-telegram-login','fs-page-status','fs-season','fs-updated','fs-refresh','fs-dashboard-content','fs-toast'];
  const els=Object.fromEntries(ids.map(id=>[id,element(id)]));
  els['fs-config'].textContent=JSON.stringify({bot_username:'example_bot',login_configured:true,csrf_token:'initial-token'});
  const managerNav=element('manager-nav');managerNav.dataset.fsAccess='manager';
  const ownerNav=element('owner-nav');ownerNav.dataset.fsAccess='owner';
  const homeNav=element('home-nav');homeNav.dataset.fsView='home';
  const context=vm.createContext({
    document:{hidden:false,getElementById:id=>els[id],createElement:tag=>element(tag),
      querySelectorAll(selector){return selector==='[data-fs-access]'?[managerNav,ownerNav]:selector==='.fs-navigation button'?[homeNav]:[];},
      addEventListener(name,fn){handlers[name]=fn;}},
    window:{location:{search:options.search || ''},addEventListener(name,fn){handlers['window:'+name]=fn;}},
    URLSearchParams,AbortController,console,
    setTimeout(){return 1;},clearTimeout(){},
    AuctionApp:{stop(){stops++;}},
    AuctionArena:{async openWorkspace(view,id){opened.push({view,id});}},
    fetch:async(path,init={})=>{
      calls.push({path,init});
      if(path==='/api/franchise/auth/session') {
        if(options.sessionUnavailable) return {ok:false,status:503,json:async()=>({ok:false})};
        return {ok:true,status:200,json:async()=>({ok:true,user:serverUser,csrf_token:'session-token'})};
      }
      if(path==='/api/franchise/auth/login') {
        serverUser={id:42,first_name:'Alice <script>',username:'alice'};
        return {ok:true,status:200,json:async()=>({ok:true,user:serverUser,csrf_token:'authenticated-token'})};
      }
      if(path==='/api/franchise/auth/logout') {serverUser=null;return {ok:true,status:200,json:async()=>({ok:true,user:null,csrf_token:'logout-token'})};}
      if(path==='/api/webapp/auction') {
        const status=failNext || (options.inaccessible && JSON.parse(init.body).season_id===999 ? 403 : 200);failNext=0;
        return {ok:status===200,status,json:async()=>status===200?auction:{ok:false,message:'Access denied'}};
      }
      throw new Error('Unexpected path: '+path);
    }
  });
  vm.runInContext(source+'\nthis.site=FranchiseSite;this.publicApi=api;',context);
  handlers.DOMContentLoaded();await tick();await tick();
  return {els,calls,opened,managerNav,ownerNav,handlers,site:context.site,api:context.publicApi,stops:()=>stops,fail:status=>{failNext=status;}};
}
(async()=>{
  const loggedOut=await boot();
  assert.equal(loggedOut.els['fs-landing'].hidden,false);
  assert.equal(loggedOut.els['fs-dashboard'].hidden,true);
  assert.equal(loggedOut.managerNav.hidden,true);
  assert.equal(loggedOut.calls.filter(c=>c.path==='/api/webapp/auction').length,0,'No auction requests before authentication');
  const payload={id:42,auth_date:123,hash:'telegram-signature',first_name:'Alice'};
  await loggedOut.site.telegramLogin(payload);
  const login=loggedOut.calls.find(c=>c.path.endsWith('/login'));
  assert.equal(login.init.credentials,'same-origin');
  assert.equal(login.init.headers['X-CSRF-Token'],'session-token');
  assert.deepEqual(JSON.parse(login.init.body),payload,'Widget payload is forwarded without an invented user identity');
  const auctionRequest=loggedOut.calls.find(c=>c.path==='/api/webapp/auction');
  assert.equal(auctionRequest.init.headers['X-CSRF-Token'],'authenticated-token');
  assert.equal(loggedOut.els['fs-dashboard'].hidden,false);
  assert.equal(loggedOut.managerNav.hidden,true,'Owners cannot see manager shortcuts');
  assert.equal(loggedOut.ownerNav.hidden,false);
  assert.match(loggedOut.els['fs-dashboard-content'].innerHTML,/Actual franchise/);
  assert.match(loggedOut.els['fs-dashboard-content'].innerHTML,/Needs a wicket keeper/);
  assert.match(loggedOut.els['fs-account'].innerHTML,/Team Owner/);
  assert.match(loggedOut.els['fs-current-roles'].innerHTML,/Team Owner/);
  assert.match(loggedOut.els['fs-dashboard-content'].innerHTML,/What your roles allow/);
  assert.match(loggedOut.els['fs-dashboard-content'].innerHTML,/Bids and answers RTM for assigned franchises/);
  assert(!loggedOut.els['fs-account'].innerHTML.includes('<script>'),'Authenticated profile is escaped');
  assert.equal(loggedOut.site.requestHeaders()['X-CSRF-Token'],'authenticated-token','Live transport reads the latest rotated CSRF token');

  const privilegedData={...baseAuction(),admin:true,bot_admin:true,roles:['bot_admin','team_owner'],role_labels:['Bot Admin','Team Owner']};
  const privileged=await boot({user:{id:42,first_name:'Administrator'},auction:privilegedData});
  assert.match(privileged.els['fs-account'].innerHTML,/Bot Admin · Team Owner/);
  assert.match(privileged.els['fs-current-roles'].innerHTML,/Bot Admin/);
  assert.doesNotMatch(privileged.els['fs-current-roles'].innerHTML,/fs-role-auction_manager/,'Bot Admin is identified separately from a delegated Auction Manager');
  const coownerData={...baseAuction(),roles:['team_co_owner'],role_labels:['Team Co-owner']};
  coownerData.teams[0].my_role='team_co_owner';
  const coowner=await boot({user:{id:43,first_name:'Co-owner'},auction:coownerData});
  assert.match(coowner.els['fs-dashboard-content'].innerHTML,/Team Co-owner · 2/);
  assert.match(coowner.els['fs-account'].innerHTML,/Team Co-owner/);
  const managerData={...baseAuction(),admin:true,roles:['auction_manager','team_owner'],role_labels:['Auction Manager','Team Owner']};
  const manager=await boot({user:{id:42,first_name:'Manager owner'},auction:managerData});
  assert.match(manager.els['fs-current-roles'].innerHTML,/Auction Manager/);
  managerData.admin=false;managerData.roles=['team_owner'];managerData.role_labels=['Team Owner'];
  await manager.site.home();
  assert.equal(manager.managerNav.hidden,true,'Current role changes remove manager navigation');
  assert.doesNotMatch(manager.els['fs-account'].innerHTML,/Auction Manager/,'Revoked role labels do not survive a refresh');
  const spectator=await boot({user:{id:44,first_name:'Viewer'},auction:{...baseAuction(),roles:['spectator'],role_labels:['Spectator'],my_team_ids:[],teams:[]}});
  assert.match(spectator.els['fs-current-roles'].innerHTML,/Spectator/);
  assert.equal(spectator.ownerNav.hidden,true);

  loggedOut.fail(401);await loggedOut.api('/api/webapp/auction',{});await tick();
  assert.equal(loggedOut.stops(),1);assert.equal(loggedOut.els['fs-landing'].hidden,false);
  await loggedOut.site.telegramLogin(payload);
  loggedOut.fail(401);await loggedOut.api('/api/webapp/auction',{});await tick();
  assert.equal(loggedOut.stops(),2,'Session expiry recovers after signing in again');

  const empty=await boot({user:{id:5,first_name:'Manager'},auction:{ok:true,admin:true,auctions:[],season:null}});
  assert.equal(empty.managerNav.hidden,false);
  assert.match(empty.els['fs-dashboard-content'].innerHTML,/Set up your first auction/);
  empty.handlers.click({target:{closest:selector=>selector==='[data-fs-view]'?{dataset:{fsView:'admin'}}:null}});
  await tick();assert.deepEqual(empty.opened,[{view:'admin',id:null}]);

  const failed=await boot({sessionUnavailable:true});
  assert.equal(failed.els['fs-landing'].hidden,true,'Session outages are not presented as logged-out success');
  assert.equal(failed.els['fs-dashboard'].hidden,true);
  assert.match(failed.els['fs-page-status'].innerHTML,/Try again/);

  const linked=await boot({user:{id:42,first_name:'Owner'},search:'?season_id=999&chat_id=-100123',inaccessible:true});
  assert.match(linked.els['fs-page-status'].innerHTML,/Show my auctions/);
  assert.equal(linked.calls.find(c=>c.path==='/api/webapp/auction').init.headers['X-Tg-Start-Param'],'auction_999_c-100123');
  linked.handlers.click({target:{closest:selector=>selector==='[data-fs-clear-season]'?{}:null}});
  await tick();await tick();
  const recovered=linked.calls.filter(c=>c.path==='/api/webapp/auction').at(-1);
  assert.equal(JSON.parse(recovered.init.body).season_id,null);
  assert.equal(recovered.init.headers['X-Tg-Start-Param'],undefined);
  assert.match(linked.els['fs-dashboard-content'].innerHTML,/Real season/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(source)], text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
