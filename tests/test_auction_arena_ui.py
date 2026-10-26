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
async function boot(data,gate=true) {
 const elements={},events={},calls=[],toolCalls=[],homes=[],blobs=[],downloads=[];let toolBusy=false;
 function node(id) {
   const item={id,dataset:{},open:false,textContent:'',hidden:false,className:'',style:{},
     classList:{toggle(){}},setAttribute(){},contains(){return false;},
     querySelectorAll(){return [];},addEventListener(type,fn){if(id==='dialog')events[type]=fn;},
     showModal(){this.open=true;},close(){this.open=false;events.close();},click(){downloads.push(this.download);}};
   let html='';Object.defineProperty(item,'innerHTML',{get(){return html;},set(value){html=value;for(const m of value.matchAll(/id="([^"]+)"/g))if(!elements[m[1]])elements[m[1]]=node(m[1]);}});
   return item;
 }
 const context=vm.createContext({document:{hidden:false,activeElement:null,getElementById:id=>elements[id],
     createElement:tag=>{if(tag==='a')return node('link');const root=node('dialog');elements['auction-arena']=root;return root;},body:{style:{overflow:'auto'},append(){}},addEventListener(){}},
   window:{addEventListener(){},confirm(){return true;}},tg:undefined,
   Blob,URL:{createObjectURL(blob){blobs.push(blob);return 'blob:test';},revokeObjectURL(){}},
   navigate(screen){homes.push(screen);},
   escapeHtml:v=>String(v??'').replaceAll('<','&lt;').replaceAll('"','&quot;'),haptic(){},
   api:async(url,body)=>{calls.push({url,body});return body.view==='admin'?{ok:gate,data:{...data,bot_admin:gate}}:{ok:true,data:url.endsWith('/action')?{}:data};},
   AuctionApp:{openTools:async(...args)=>{toolCalls.push(args);},stopTools(){},setSeason(){},isBusy:()=>toolBusy,refreshTools:async()=>{}},
   setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){}});
 vm.runInContext(source,context);vm.runInContext('AuctionArena.open(1)',context);await new Promise(setImmediate);
 return {elements,calls,toolCalls,homes,blobs,downloads,setToolBusy:value=>{toolBusy=value;},body:context.document.body,click:async(key,args={})=>events.click({target:{closest:selector=>selector==='[data-aa-ui]'?{dataset:{aaUi:key,...args}}:null}}),
   act:async(name,args)=>events.click({target:{closest:selector=>selector==='[data-aa-action]'?{dataset:{aaAction:name,aaArgs:JSON.stringify(args)}}:null}})};
}
(async()=>{
 const base={server_time:new Date().toISOString(),season:{id:1,name:'League',status:'live',currency:'₹',max_squad:8},
   auctions:[{id:1,name:'League'}],my_team_ids:[1],admin:false,bot_admin:false,max_overseas:4,clock_seconds:30,
   teams:[{id:1,name:'Mumbai',mine:true,purse_lakh:10000,opening_lakh:10000,size:0,overseas:0,max_bid_lakh:10000,warnings:[],squad:[]}],
   lot:{id:9,player_id:4,name:'Player',rating:90,bat_rating:88,bowl_rating:75,version:'Base',role:'Batsman',country:'India',
        number:1,set_name:'Marquee',base_lakh:20,next_lakh:20,bid_lakh:null,bids:0,rtm_stage:null,status:'on_block',
        deadline:new Date(Date.now()+30000).toISOString()},events:[],bids:[],counts:{},last_result:null};
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
 await owner.click('tab',{tab:'logs'});assert.match(owner.elements['aa-body'].innerHTML,/Live activity/);
 await owner.click('tab',{tab:'more'});assert.match(owner.elements['aa-body'].innerHTML,/More auction tools/);
 for(const tool of ['pool','preseason','trades','rules'])assert.match(owner.elements['aa-body'].innerHTML,new RegExp('data-tool="'+tool+'"'));
 await owner.click('tool',{tool:'pool'});assert.equal(owner.elements['auction-arena'].open,true);assert.equal(owner.toolCalls.at(-1)[0],'pool');
 assert.match(owner.elements['aa-body'].innerHTML,/id="auction-app"/);
 owner.setToolBusy(true);const before=owner.calls.length;await owner.click('tab',{tab:'arena'});assert.equal(owner.calls.length,before,'Navigation waits while a tool mutation is saving');
 owner.setToolBusy(false);await owner.click('tab',{tab:'arena'});assert.match(owner.elements['aa-body'].innerHTML,/PLACE BID/);
 await owner.click('close');assert.equal(owner.elements['auction-arena'].open,false);assert.equal(owner.homes.at(-1),'home');
 assert.equal(owner.body.style.overflow,'auto','Leaving the auction restores the Mini App scroll setting');
 const admin=await boot({...base,admin:true,bot_admin:true});await admin.click('admin');
 assert.equal(admin.calls.filter(c=>c.body.view==='admin').length,1);assert.match(admin.elements['aa-body'].innerHTML,/VERIFIED BOT ADMIN/);
 await admin.click('manage-team',{team:'1'});assert.equal(admin.elements['auction-arena'].open,true);assert.equal(admin.toolCalls[0][0],'teams');assert.equal(admin.toolCalls[0][2],1);
 const revoked=await boot({...base,admin:true,bot_admin:true},false);await revoked.click('admin');
 assert.doesNotMatch(revoked.elements['aa-body'].innerHTML,/VERIFIED BOT ADMIN/);
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

})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
