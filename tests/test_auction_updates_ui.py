"""Exercise the authenticated notification transport without a network server."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for notification transport checks')
class AuctionUpdatesUITests(unittest.TestCase):
    def test_stream_parser_reconnection_and_cleanup(self):
        source = Path(__file__).resolve().parents[1] / 'static/js/auction_updates.js'
        script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const tick=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate);};
const encoder=new TextEncoder();
function boot(responses=[],extra={}) {
  const tasks=new Map(),events={},calls=[],streams=[],changes=[],statuses=[],access=[];
  let nextId=0,headers={'X-CSRF-Token':'first-token'};
  const context={
    document:{hidden:false,addEventListener(k,fn){events[k]=fn;},removeEventListener(k,fn){if(events[k]===fn)delete events[k];}},
    window:{addEventListener(k,fn){events[k]=fn;},removeEventListener(k,fn){if(events[k]===fn)delete events[k];}},
    navigator:{onLine:true},AbortController,TextDecoder,console,
    setTimeout(fn,delay){const id=++nextId;tasks.set(id,{fn,delay});return id;},clearTimeout(id){tasks.delete(id);},
    fetch:async(path,init)=>{
      calls.push({path,init});
      const next=responses.shift() || {};
      if(next.failure)throw new Error('Network unavailable');
      if(next.status)return {ok:false,status:next.status,json:async()=>next.data || {},headers:{get:name=>name==='Retry-After'?next.retry:null}};
      if(next.unsupported)return {ok:true,status:200,body:null};
      let controller;
      const stream=new ReadableStream({start(value){controller=value;},cancel(){}});
      init.signal.addEventListener('abort',()=>{try{controller.error(Object.assign(new Error('Aborted'),{name:'AbortError'}));}catch(_){}},{once:true});
      streams.push(controller);
      return {ok:true,status:200,body:stream,headers:{get:()=> 'text/event-stream; charset=utf-8'}};
    }
  };
  vm.createContext(context);vm.runInContext(source+'\nthis.transport=AuctionUpdates;',context);
  const stop=context.transport.watch({seasonId:7,headers:()=>headers,
    onChange:value=>changes.push(value),onStatus:value=>statuses.push(value),onAccess:value=>access.push(value),...extra});
  return {context,tasks,events,calls,streams,changes,statuses,access,stop,
    headers:value=>{headers=value;},
    send(value,index=streams.length-1){streams[index].enqueue(typeof value==='string'?encoder.encode(value):value);},
    async run(delay){const entry=[...tasks].find(([,task])=>task.delay===delay);assert(entry,'Expected timer '+delay+'; got '+JSON.stringify([...tasks.values()].map(v=>v.delay)));tasks.delete(entry[0]);entry[1].fn();await tick();}
  };
}
(async()=>{
  const live=boot();await tick();
  assert.equal(live.calls.length,1);assert.equal(live.calls[0].path,'/api/webapp/auction/updates');
  assert.equal(live.calls[0].init.credentials,'same-origin');
  assert.equal(live.calls[0].init.headers['X-CSRF-Token'],'first-token');
  assert.deepEqual(JSON.parse(live.calls[0].init.body),{season_id:7});
  // Split CRLF and UTF-8 bytes; include comments and multiline event data.
  const content=encoder.encode(': connection\r\nevent: ready\r\ndata: {"revision":"पहला",\r\ndata: "season_id":7}\r\n\r\n');
  for(const byte of content)live.send(Uint8Array.of(byte));
  await tick();assert.equal(live.changes.length,1,'Ready closes the initial-read/subscription race');
  assert.equal(live.changes[0].revision,'पहला');
  live.send('event: changed\ndata: {"revision":"second","season_id":7}\n\nevent: heartbeat\ndata: {"season_id":7}\n\n');
  await tick();assert.equal(live.changes.length,2,'Coalesced events dispatch individually; heartbeat does not fetch data');
  assert.equal(live.statuses.at(-1).state,'live');assert.equal(live.statuses.at(-1).event,'heartbeat');
  live.send('event: changed\ndata: {"revision":"other","season_id":8}\n\n');
  await tick();assert.equal(live.changes.length,2,'Another season cannot update this view');
  live.headers({'X-CSRF-Token':'rotated-token'});
  live.send('event: end\ndata: {}\n\n');await tick();await live.run(100);
  assert.equal(live.calls.length,2);assert.equal(live.calls[1].init.headers['X-CSRF-Token'],'rotated-token');
  live.stop();await tick();assert.equal(live.tasks.size,0);assert.equal(Object.keys(live.events).length,0);
  assert.equal(live.calls[1].init.signal.aborted,true);

  const unauthorized=boot([{status:401,data:{error:'login_required'}}]);await tick();
  assert.equal(unauthorized.access.length,1);assert.equal(unauthorized.tasks.size,0);
  unauthorized.events.online();await tick();assert.equal(unauthorized.calls.length,1,'Denied sessions do not reconnect');unauthorized.stop();
  const maintenance=boot([{status:503,data:{error:'maintenance'}}]);await tick();
  assert.equal(maintenance.access.length,1);assert.equal(maintenance.tasks.size,0);maintenance.stop();
  const invalid=boot([{status:400,data:{error:'invalid_input'}}]);await tick();
  assert.equal(invalid.access.length,1);assert.equal(invalid.tasks.size,0);invalid.stop();
  const transient=boot();await tick();
  transient.send('event: ready\ndata: {"season_id":7}\n\nevent: access\ndata: {"status":503,"error":"unavailable"}\n\n');
  await tick();assert.equal(transient.access.length,1,'A transient stream failure asks the view to refresh once');
  assert.equal(transient.calls[0].init.signal.aborted,true,'Failed stream is aborted before reconnecting');
  assert.equal(transient.calls.length,1);assert.equal(transient.statuses.at(-1).state,'fallback');
  await transient.run(1000);assert.equal(transient.calls.length,2,'Transient database failure can reconnect');
  transient.send('event: ready\ndata: {"season_id":7}\n\n');await tick();
  assert.equal(transient.changes.length,2);transient.stop();await tick();
  assert.equal(transient.tasks.size,0);assert.equal(transient.access.length,1);
  const saturated=boot([{status:503,retry:'8'}]);await tick();
  assert.equal(saturated.statuses.at(-1).state,'fallback');await saturated.run(8000);
  assert.equal(saturated.calls.length,2);saturated.stop();await tick();
  const failure=boot([{failure:true},{failure:true},{failure:true}]);await tick();
  await failure.run(1000);await failure.run(2000);assert.equal(failure.calls.length,3);failure.stop();

  const visibility=boot();await tick();visibility.context.document.hidden=true;visibility.events.visibilitychange();await tick();
  assert.equal(visibility.calls[0].init.signal.aborted,true);assert.equal(visibility.tasks.size,0);
  visibility.context.document.hidden=false;visibility.events.visibilitychange();await visibility.run(0);
  assert.equal(visibility.calls.length,2,'Returning to a visible tab reconnects once');
  visibility.context.navigator.onLine=false;visibility.events.offline();await tick();assert.equal(visibility.tasks.size,0);
  visibility.context.navigator.onLine=true;visibility.events.online();await visibility.run(0);
  assert.equal(visibility.calls.length,3);visibility.stop();await tick();

  const stalled=boot();await tick();await stalled.run(12000);
  assert.equal(stalled.calls[0].init.signal.aborted,true);assert.equal(stalled.statuses.at(-1).reason,'stream_timeout');
  stalled.stop();await tick();assert.equal(stalled.tasks.size,0);
  const oversized=boot();await tick();oversized.send('data: '+ 'x'.repeat(65537));await tick();
  assert.equal(oversized.statuses.at(-1).reason,'connection_error');oversized.stop();
  const unsupported=boot([{unsupported:true}]);await tick();assert.equal(unsupported.tasks.size,0);unsupported.stop();

  // Stop between response delivery and parsing, and inside a view callback.
  const stopped=boot();stopped.stop();await tick();assert.equal(stopped.changes.length,0);assert.equal(stopped.tasks.size,0);
  let stopOnLive;
  const callbackStop=boot([], {onStatus:value=>{if(value.state==='live')stopOnLive();}});
  stopOnLive=callbackStop.stop;await tick();
  callbackStop.send('event: ready\ndata: {"season_id":7}\n\n');await tick();
  assert.equal(callbackStop.changes.length,0,'A stopped watcher receives no later view callback');
  assert.equal(callbackStop.tasks.size,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(source)], text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
