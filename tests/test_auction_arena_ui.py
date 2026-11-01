"""Run the single auction Arena controller with a small Node DOM adapter."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is needed for the Arena controller')
class AuctionArenaUITests(unittest.TestCase):
    def test_single_room_tools_permissions_bidding_and_csv_results(self):
        source = Path(__file__).resolve().parents[1] / 'static/js/auction_arena.js'
        script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const settle=()=>new Promise(setImmediate);
async function boot(data,gate=true,options={}) {
 const elements={},events={},windowEvents={},calls=[],toolCalls=[],homes=[],blobs=[],downloads=[],subscriptions=[];let toolBusy=false;
 let editing=false,backgroundPoll=null,readFailure=null,bodyWrites=0,customInput=null,quickBid=null,customContext=null;
 let holdRead=false,releaseRead=null,holdAction=false,releaseAction=null,activeReads=0,maxActiveReads=0,nextTimer=1;
 const timers=new Map();
 let clockNow=Date.now();
 class MockDate extends Date {static now(){return clockNow;}}
 const decode=value=>value.replaceAll('&quot;','"').replaceAll('&lt;','<');
 function node(id) {
   const item={id,dataset:{},open:false,textContent:'',hidden:false,className:'',style:{},disabled:false,value:'',
     classList:{toggle(){}},setAttribute(){},contains(){return editing;},
     querySelectorAll(selector){return quickBid&&id==='dialog'&&((selector==='[data-aa-action="bid"]')||selector.includes('[data-aa-action="bid"]')||selector==='button,input,select')?[quickBid]:[];},addEventListener(type,fn){if(id==='dialog')events[type]=fn;},
     showModal(){this.open=true;},close(){this.open=false;events.close();},click(){downloads.push(this.download);}};
   let html='';Object.defineProperty(item,'innerHTML',{get(){return html;},set(value){
     html=value;for(const m of value.matchAll(/id="([^"]+)"/g))if(!elements[m[1]])elements[m[1]]=node(m[1]);
     if(id==='aa-body') {
       bodyWrites++;customInput=null;quickBid=null;customContext=null;
       const input=value.match(/<input name="amount_lakh"[^>]*value="([^"]*)"/);
       if(input){customInput=node('custom-input');customInput.value=input[1];}
       const form=value.match(/data-aa-form="bid" data-aa-args="([^"]+)"/);
       if(form)customContext=JSON.parse(decode(form[1]));
       const bid=value.match(/<button[^>]*data-aa-action="bid" data-aa-args="([^"]+)"([^>]*)>([^<]*)<\/button>/);
       if(bid){quickBid=node('quick-bid');quickBid.dataset.aaArgs=decode(bid[1]);quickBid.disabled=bid[2].includes('disabled');quickBid.textContent=bid[3];quickBid.closest=()=>null;}
     }
   }});
   return item;
 }
 const context=vm.createContext({document:{hidden:false,activeElement:null,getElementById:id=>elements[id],
     createElement:tag=>{if(tag==='a')return node('link');const root=node('dialog');elements['auction-arena']=root;return root;},body:{style:{overflow:'auto'},append(){}},addEventListener(){}},
   window:{addEventListener(name,fn){windowEvents[name]=fn;},confirm(){return true;}},tg:undefined,Date:MockDate,
   Blob,URL:{createObjectURL(blob){blobs.push(blob);return 'blob:test';},revokeObjectURL(){}},
   navigate(screen){homes.push(screen);},
   escapeHtml:v=>String(v??'').replaceAll('<','&lt;').replaceAll('"','&quot;'),haptic(){},
   api:async(url,body)=>{
     calls.push({url,body});const mutation=url.endsWith('/action');
     if(!mutation){activeReads++;maxActiveReads=Math.max(maxActiveReads,activeReads);}
     try {
       if(mutation&&holdAction){holdAction=false;await new Promise(resolve=>{releaseAction=resolve;});}
       if(!mutation&&holdRead){holdRead=false;await new Promise(resolve=>{releaseRead=resolve;});}
       if(readFailure&&!mutation)return {ok:false,status:readFailure,data:{message:'Access revoked.'}};
       return body.view==='admin'?{ok:gate,data:{...data,admin:gate,bot_admin:data.bot_admin&&gate}}:{ok:true,data:mutation?{message:body.action==='bid'?'Bid placed.':'Auction updated.'}:data};
     } finally {if(!mutation)activeReads--;}
   },
   AuctionApp:{openTools:async(...args)=>{toolCalls.push(args);},stopTools(){},setSeason(){},isBusy:()=>toolBusy,refreshTools:async()=>{}},
   ...(options.updates?{AuctionUpdates:{watch(config){const subscription={...config,stopped:false};subscriptions.push(subscription);return ()=>{subscription.stopped=true;};}}}:{}),
   setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{fn,delay});if([1000,2000,4000,5000].includes(delay))backgroundPoll=id;return id;},
   clearTimeout(id){timers.delete(id);if(backgroundPoll===id)backgroundPoll=null;},setInterval(){return nextTimer++;},clearInterval(){}});
 vm.runInContext(source,context);vm.runInContext('AuctionArena.open(1)',context);await new Promise(setImmediate);
 return {elements,calls,toolCalls,homes,blobs,downloads,subscriptions,setToolBusy:value=>{toolBusy=value;},
   workspace:async view=>vm.runInContext('AuctionArena.openWorkspace('+JSON.stringify(view)+',1)',context),
   setData:value=>{data=value;},setEditing:value=>{editing=value;context.document.activeElement=value?(customInput||{matches:()=>true}):null;if(value)context.document.activeElement.matches=()=>true;},denyReads:status=>{readFailure=status;},
   bodyWrites:()=>bodyWrites,input:()=>customInput,quickBid:()=>quickBid,customContext:()=>customContext,maxReads:()=>maxActiveReads,
   advanceTime:milliseconds=>{clockNow+=milliseconds;},windowEvent:name=>windowEvents[name]?.(),
   holdNextRead:()=>{holdRead=true;},releaseRead:()=>{assert(releaseRead,'A read must be waiting');const release=releaseRead;releaseRead=null;release();},
   holdNextAction:()=>{holdAction=true;},releaseAction:()=>{assert(releaseAction,'An action must be waiting');const release=releaseAction;releaseAction=null;release();},
   emit:()=>{const sub=subscriptions.findLast(s=>!s.stopped);assert(sub,'A live subscription must exist');sub.onChange();},
   streamState:state=>subscriptions.findLast(s=>!s.stopped).onStatus({state}),
   flushPending:async()=>{for(const [id,timer] of [...timers])if(timer.delay===0){timers.delete(id);await timer.fn();await settle();}},
   poll:async()=>{assert(backgroundPoll,'A live refresh must be scheduled');const timer=timers.get(backgroundPoll);assert(timer,'The refresh timer must remain active');timers.delete(backgroundPoll);backgroundPoll=null;await timer.fn();},
   body:context.document.body,click:async(key,args={})=>events.click({target:{closest:selector=>selector==='[data-aa-ui]'?{dataset:{aaUi:key,...args}}:null}}),
   act:async(name,args)=>events.click({target:{closest:selector=>selector==='[data-aa-action]'?{dataset:{aaAction:name,aaArgs:JSON.stringify(args)}}:null}})};
}
(async()=>{
 const base={server_time:new Date().toISOString(),season:{id:1,name:'League',status:'live',currency:'₹',max_squad:8},
   auctions:[{id:1,name:'League'}],my_team_ids:[1],admin:false,bot_admin:false,max_overseas:4,clock_seconds:30,
   teams:[{id:1,name:'Mumbai',mine:true,purse_lakh:10000,opening_lakh:10000,size:0,overseas:0,max_bid_lakh:10000,warnings:[],squad:[]}],
   lot:{id:9,player_id:4,name:'Player',rating:90,bat_rating:88,bowl_rating:75,version:'Base',role:'Batsman',country:'India',
        number:1,set_name:'Marquee',base_lakh:20,next_lakh:20,bid_lakh:null,bids:0,rtm_stage:null,status:'on_block',
        deadline:new Date(Date.now()+30000).toISOString()},events:[],bids:[],counts:{},last_result:null};
 const botOwner=await boot({...base,admin:true,bot_admin:true,role_labels:['Bot Admin','Team Owner']});
 assert.match(botOwner.elements['aa-header'].innerHTML,/Bot Admin/);
 assert.match(botOwner.elements['aa-header'].innerHTML,/Team Owner/);
 const roleManager=await boot({...base,admin:true,bot_admin:false,role_labels:['Auction Manager','Team Owner']});
 assert.match(roleManager.elements['aa-header'].innerHTML,/Auction Manager<\/span>/);
 roleManager.setData({...base,role_labels:['Team Owner']});await roleManager.poll();
 assert.doesNotMatch(roleManager.elements['aa-header'].innerHTML,/Auction Manager/,'Revoked manager badge disappears with its controls');
 assert.match(roleManager.elements['aa-header'].innerHTML,/Team Owner/);
 const owner=await boot(base);
 assert.equal(owner.body.style.overflow,'hidden');
 assert.match(owner.elements['aa-body'].innerHTML,/PLACE BID: ₹0.20 Cr/);
 assert.match(owner.elements['aa-body'].innerHTML,/Batting/);
 assert.doesNotMatch(owner.elements['aa-header'].innerHTML,/data-aa-ui="admin"/);
 await owner.click('admin');assert.equal(owner.calls.filter(c=>c.body.view==='admin').length,0);
 await owner.act('admin_start',{});assert.equal(owner.calls.filter(c=>c.body.action==='admin_start').length,0);
 await owner.act('bid',{lot_id:9,expected_bids:0,stage:null,team_id:1,amount_lakh:20});
 const bid=owner.calls.find(c=>c.body.action==='bid');assert.equal(bid.body.amount_lakh,20);assert.equal(bid.body.expected_bids,0);assert.equal(bid.body.lot_id,9);
 await owner.click('tab',{tab:'squads'});assert.match(owner.elements['aa-body'].innerHTML,/Auction squads/);
 const retained={id:12,name:'Kept player',rating:90,role:'Batsman',price_lakh:100,acquisition:'retained'};
 const squad=[{...base.teams[0],size:1,squad:[retained]}];
 const paused=await boot({...base,season:{...base.season,status:'paused'},teams:squad});
 await paused.click('tab',{tab:'squads'});assert.doesNotMatch(paused.elements['aa-body'].innerHTML,/data-aa-action="release"/);
 await paused.act('release',{team_id:1,lot_id:12});assert.equal(paused.calls.filter(c=>c.body.action==='release').length,0);
 const playing=await boot({...base,teams:squad});await playing.click('tab',{tab:'squads'});
 assert.doesNotMatch(playing.elements['aa-body'].innerHTML,/data-aa-action="release"/);
 const spectator=await boot({...base,my_team_ids:[],season:{...base.season,status:'paused'},teams:[{...squad[0],mine:false}]});
 await spectator.click('tab',{tab:'squads'});assert.doesNotMatch(spectator.elements['aa-body'].innerHTML,/data-aa-action="release"/);
 await spectator.click('tab',{tab:'more'});assert.doesNotMatch(spectator.elements['aa-body'].innerHTML,/data-tool="(pool|preseason|trades|rules|admin)"/);
 await spectator.act('admin_start',{});assert.equal(spectator.calls.filter(c=>c.body.action==='admin_start').length,0);
 for(const name of ['bid','rtm','rtm_stand'])await spectator.act(name,{team_id:1,lot_id:9,amount_lakh:20});
 assert.equal(spectator.calls.filter(c=>c.url.endsWith('/action')).length,0,'Spectators never submit auction mutations');
 await owner.act('bid',{team_id:2,lot_id:9,amount_lakh:20});
 assert.equal(owner.calls.filter(c=>c.body.action==='bid').length,1,'Team users cannot submit bids for another franchise');
 for(const name of ['admin_pause','admin_resume','admin_settings','admin_team_purse','admin_team_add_player','admin_team_remove_player','admin_restart','admin_publish','retain','release','draft_pick','trade_open']) {
   await owner.act(name,{team_id:1});await spectator.act(name,{team_id:1});
   assert.equal(owner.calls.filter(c=>c.body.action===name).length,0,name+' is manager-only');
   assert.equal(spectator.calls.filter(c=>c.body.action===name).length,0,name+' is manager-only');
 }
 await owner.click('tab',{tab:'logs'});assert.match(owner.elements['aa-body'].innerHTML,/Live activity/);
 await owner.click('tab',{tab:'more'});assert.match(owner.elements['aa-body'].innerHTML,/More auction tools/);
 assert.doesNotMatch(owner.elements['aa-body'].innerHTML,/data-tool="preseason"|data-tool="trades"/);
 for(const tool of ['pool','rules'])assert.match(owner.elements['aa-body'].innerHTML,new RegExp('data-tool="'+tool+'"'));
 await owner.click('tool',{tool:'pool'});assert.equal(owner.elements['auction-arena'].open,true);assert.equal(owner.toolCalls.at(-1)[0],'pool');
 assert.match(owner.elements['aa-body'].innerHTML,/id="auction-app"/);
 owner.setToolBusy(true);const before=owner.calls.length;await owner.click('tab',{tab:'arena'});assert.equal(owner.calls.length,before,'Navigation waits while a tool mutation is saving');
 owner.setToolBusy(false);await owner.click('tab',{tab:'arena'});assert.match(owner.elements['aa-body'].innerHTML,/PLACE BID/);
 await owner.click('close');assert.equal(owner.elements['auction-arena'].open,false);assert.equal(owner.homes.at(-1),'home');
 assert.equal(owner.body.style.overflow,'auto','Leaving the auction restores the Mini App scroll setting');
 const admin=await boot({...base,admin:true,bot_admin:true});await admin.click('admin');
 assert.equal(admin.calls.filter(c=>c.body.view==='admin').length,1);assert.match(admin.elements['aa-body'].innerHTML,/AUCTION MANAGER/);
 await admin.click('manage-team',{team:'1'});assert.equal(admin.elements['auction-arena'].open,true);assert.equal(admin.toolCalls[0][0],'teams');assert.equal(admin.toolCalls[0][2],1);
 const delegated=await boot({...base,admin:true,bot_admin:false});await delegated.click('admin');
 assert.match(delegated.elements['aa-body'].innerHTML,/AUCTION MANAGER/);
 await delegated.act('admin_pause',{});assert.equal(delegated.calls.filter(c=>c.body.action==='admin_pause').length,1);
 const shortcut=await boot({...base,admin:true,bot_admin:false});
 await shortcut.workspace('preseason');assert.equal(shortcut.toolCalls.at(-1)[0],'preseason','Website shortcut waits for current manager access');
 const ownerShortcut=await boot(base);await ownerShortcut.workspace('preseason');
 assert.equal(ownerShortcut.toolCalls.length,0,'Website shortcuts cannot open manager tools as an owner');
 await ownerShortcut.workspace('squads');assert.match(ownerShortcut.elements['aa-body'].innerHTML,/Mumbai/,'Squad shortcuts open the requested section');
 const revoked=await boot({...base,admin:true,bot_admin:true},false);await revoked.click('admin');
 assert.doesNotMatch(revoked.elements['aa-body'].innerHTML,/AUCTION MANAGER/);
 const demoted=await boot({...base,admin:true,bot_admin:false});await demoted.click('admin');
 demoted.setEditing(true);demoted.setData({...base,admin:false,bot_admin:false});await demoted.poll();
 assert.doesNotMatch(demoted.elements['aa-header'].innerHTML,/data-aa-ui="admin"/);
 assert.doesNotMatch(demoted.elements['aa-body'].innerHTML,/AUCTION MANAGER|data-aa-action="admin_/,'Revocation removes the panel even while editing');
 assert.match(demoted.elements['aa-body'].innerHTML,/PLACE BID/,'A demoted owner retains bidding access');
 await demoted.act('admin_pause',{});assert.equal(demoted.calls.filter(c=>c.body.action==='admin_pause').length,0);
 const removedOwner=await boot(base);removedOwner.setEditing(true);
 removedOwner.setData({...base,my_team_ids:[],teams:[{...base.teams[0],mine:false}]});await removedOwner.poll();
 assert.match(removedOwner.elements['aa-body'].innerHTML,/Spectator view/);
 assert.doesNotMatch(removedOwner.elements['aa-body'].innerHTML,/data-aa-action="bid"|data-aa-form="bid"/);
 const denied=await boot({...base,admin:true,bot_admin:false});await denied.click('admin');denied.denyReads(403);await denied.poll();
 assert.doesNotMatch(denied.elements['aa-header'].innerHTML,/data-aa-ui="admin"/);
 assert.match(denied.elements['aa-body'].innerHTML,/Auction access denied/);
 await denied.act('admin_pause',{});assert.equal(denied.calls.filter(c=>c.body.action==='admin_pause').length,0);
 const sold=await boot({...base,lot:null,last_result:{id:9,name:'Player',role:'Batsman',status:'sold',sold_to_id:1,price_lakh:100,event_id:4}});
 assert.match(sold.elements['aa-body'].innerHTML,/SOLD · ₹1.00 Cr/);
 const unsold=await boot({...base,lot:null,last_result:{id:9,name:'Player',role:'Bowler',status:'unsold',event_id:5}});
 assert.match(unsold.elements['aa-body'].innerHTML,/aa-result-card unsold/);
 const exporter=await boot({...base,teams:[{...base.teams[0],name:'=SUM(1,2)',squad:[{name:'@Player',rating:90,role:'Batsman',country:'India',price_lakh:125,acquisition:'auction'}]}]});
 await exporter.click('tab',{tab:'more'});await exporter.click('download-results');
 assert.equal(exporter.downloads[0],'auction-1-results.csv');
 const csv=await exporter.blobs[0].text();
 assert(csv.includes('"Franchise","Player","Rating"'));
 assert(csv.includes('"\'=SUM(1,2)"'), 'Formula-like team names export as text');
 assert(csv.includes('"\'@Player"'), 'Formula-like player names export as text');
 assert(csv.includes('"125"'));assert(csv.includes('Purse remaining (lakh)'));

 const typing=await boot({...base,direct_bids:true});
 const originalInput=typing.input();assert(originalInput,'The custom bid input must be rendered');
 originalInput.value='37';typing.setEditing(true);
 const originalContext=typing.customContext(),beforeTyping=typing.bodyWrites();
 typing.setData({...base,direct_bids:true,teams:[...base.teams,{...base.teams[0],id:2,name:'Chennai',mine:false}],
   lot:{...base.lot,bid_lakh:25,next_lakh:30,bids:1,bidder_id:2}});
 await typing.poll();
 assert.equal(typing.bodyWrites(),beforeTyping,'Same-player price updates keep the focused form mounted');
 assert.equal(typing.input(),originalInput);assert.equal(typing.input().value,'37','A background bid never replaces a typed amount');
 assert.deepEqual(typing.customContext(),originalContext,'Typed bids retain the original context for server stale checks');
 assert.equal(typing.elements['aa-current-price'].textContent,'₹0.25 Cr');
 assert.equal(typing.elements['aa-current-bidder'].textContent,'Chennai');
 assert.match(typing.quickBid().textContent,/₹0.30 Cr/);
 assert.deepEqual(JSON.parse(typing.quickBid().dataset.aaArgs),{lot_id:9,stage:null,expected_bids:1,team_id:1,amount_lakh:30},'Quick bids display and submit the new explicit price');

 for(const [change,next] of [
   ['player',{...base,lot:{...base.lot,id:10,name:'Next player'}}],
   ['lot status',{...base,lot:{...base.lot,status:'rtm_offered'}}],
   ['RTM stage',{...base,lot:{...base.lot,rtm_stage:'final_offer'}}],
   ['auction status',{...base,season:{...base.season,status:'paused'}}]
 ]) {
   const changing=await boot({...base,direct_bids:true});changing.setEditing(true);
   const beforeChange=changing.bodyWrites();changing.setData({...next,direct_bids:true});await changing.poll();
   assert(changing.bodyWrites()>beforeChange,change+' changes must replace controls even while an input has focus');
 }
 const recovering=await boot({...base,direct_bids:true});recovering.setEditing(true);recovering.denyReads(503);
 await recovering.poll();assert.equal(recovering.quickBid().disabled,true,'Disconnected bidding is disabled');
 const beforeRecovery=recovering.bodyWrites();recovering.denyReads(null);await recovering.poll();
 assert(recovering.bodyWrites()>beforeRecovery,'A recovered connection refreshes controls despite input focus');
 assert.equal(recovering.quickBid().disabled,false,'Bidding becomes usable again after recovery');

 const slowResponse=await boot(base);slowResponse.holdNextRead();
 const waitingResponse=slowResponse.poll();await settle();slowResponse.advanceTime(7000);
 slowResponse.releaseRead();await waitingResponse;
 assert.equal(slowResponse.quickBid().disabled,true,'A seven-second-old response cannot enable bids just because it has arrived');
 assert.match(slowResponse.elements['aa-connection'].textContent,/Updates delayed/);
 const disconnected=await boot(base);assert.equal(disconnected.quickBid().disabled,false);
 disconnected.windowEvent('offline');
 assert.equal(disconnected.quickBid().disabled,true,'Going offline disables bidding immediately');
 assert.match(disconnected.elements['aa-connection'].textContent,/Updates delayed/);
 disconnected.windowEvent('online');await settle();
 assert.equal(disconnected.quickBid().disabled,false,'A successful reconnect refresh restores usable bids');

 const acknowledged=await boot(base);acknowledged.holdNextRead();let finished=false;
 const acknowledgedAction=acknowledged.act('bid',{lot_id:9,expected_bids:0,stage:null,team_id:1,amount_lakh:20}).then(()=>{finished=true;});
 await settle();assert.equal(finished,false,'State readback is still waiting');
 assert.equal(acknowledged.elements['aa-toast'].textContent,'Bid placed.','Committed bid feedback appears before a delayed readback');
 assert.equal(acknowledged.elements['aa-toast'].hidden,false);
 acknowledged.releaseRead();await acknowledgedAction;

 const streamed=await boot(base,true,{updates:true});assert.equal(streamed.subscriptions.length,1);
 streamed.streamState('live');assert.match(streamed.elements['aa-connection'].textContent,/Live connection/);
 const beforeBurst=streamed.calls.filter(c=>!c.url.endsWith('/action')).length;
 streamed.holdNextRead();streamed.emit();streamed.emit();streamed.emit();await settle();
 assert.equal(streamed.calls.filter(c=>!c.url.endsWith('/action')).length,beforeBurst+1,'A burst shares the in-flight state refresh');
 streamed.releaseRead();await settle();await streamed.flushPending();await settle();
 assert.equal(streamed.calls.filter(c=>!c.url.endsWith('/action')).length,beforeBurst+2,'A single follow-up catches changes received during a refresh');
 assert.equal(streamed.maxReads(),1,'Live notifications do not overlap state reads');
 streamed.holdNextAction();const duringMutation=streamed.calls.filter(c=>!c.url.endsWith('/action')).length;
 const pendingMutation=streamed.act('bid',{lot_id:9,expected_bids:0,stage:null,team_id:1,amount_lakh:20});await settle();
 streamed.emit();streamed.emit();await settle();
 assert.equal(streamed.calls.filter(c=>!c.url.endsWith('/action')).length,duringMutation,'Live notifications wait while a mutation is pending');
 streamed.releaseAction();await pendingMutation;
 assert.equal(streamed.maxReads(),1);
 await streamed.click('close');assert(streamed.subscriptions.every(s=>s.stopped),'Closing the room releases its live subscriptions');

})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
