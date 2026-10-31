"""Exercise tournament loading and recovery with the actual Miniapp controller."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is needed for the Miniapp controller')
class TournamentMiniappRecoveryTests(unittest.TestCase):
    def test_loading_recovery_and_selection_races(self):
        source = Path(__file__).resolve().parents[1] / 'templates/webapp.html'
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('const TN_SUBS =');
const end = source.indexOf('\nasync function loadTnStats()', start);
const elements = Object.fromEntries(['tn-content', 'tn-switch', 'tab-tournament'].map(id => [id, {style:{}, innerHTML:''}]));
const pending = [];
const context = vm.createContext({
  document:{hidden:false,getElementById:id => elements[id],addEventListener:() => {}},
  navigator:{onLine:true},window:{addEventListener:() => {}},
  setTimeout:() => 1,clearTimeout:() => {},
  sessionStorage:{getItem:() => null}, currentScreen:'tournament',
  console:{error:() => {}}, escapeHtml:String, haptic:() => {}, navigate:() => {},
  api:() => new Promise((resolve,reject) => pending.push({resolve,reject})),
  renderTournament:() => { elements['tn-content'].innerHTML = 'Loaded'; },
  loadTnStats:() => {},
});
vm.runInContext(source.slice(start,end), context);
const run = code => vm.runInContext(code,context);
const payload = id => ({ok:true,data:{live:true,tournament:{id},table:[],fixtures:[]}});
(async () => {
  run('tnLive = [{id:1,name:"One"},{id:2,name:"Two"}]');
  const first = run('loadTournament()');
  pending.shift().resolve(payload(1)); await first;
  assert.equal(elements['tn-content'].innerHTML, 'Loaded');
  const old = run('loadTournament(true)');
  const oldRequest = pending.shift();
  const newer = run('tnId=2; tnData=null; loadTournament()');
  pending.shift().resolve(payload(2)); await newer;
  oldRequest.resolve(payload(1)); await old;
  assert.equal(run('tnId'),2);
  const failed = run('tnData=null; loadTournament()');
  pending.shift().reject(new Error('Disconnected')); await failed;
  assert.match(elements['tn-content'].innerHTML,/Try again/);
  const retry = run('loadTournament(true)');
  pending.shift().resolve(payload(2)); await retry;
  assert.equal(elements['tn-content'].innerHTML,'Loaded');
  context.renderTournament = () => { throw new Error('Render failed'); };
  const broken = run('tnData=null; loadTournament()');
  pending.shift().resolve(payload(2)); await broken;
  assert.match(elements['tn-content'].innerHTML,/Try again/);
  assert.doesNotMatch(elements['tn-content'].innerHTML,/Loading tournament/);
  // The server may select another live tournament when the requested one ends.
  // A render failure after that accepted fallback still needs recovery UI.
  const fallback = run('tnData=null; loadTournament()');
  pending.shift().resolve(payload(1)); await fallback;
  assert.equal(run('tnId'),1);
  assert.match(elements['tn-content'].innerHTML,/Try again/);
  assert.doesNotMatch(elements['tn-content'].innerHTML,/Loading tournament/);
})().catch(error => { console.error(error); process.exitCode=1; });
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("node"), "Node is needed for the Mini App controller")
class TournamentMiniappUITests(unittest.TestCase):
    def test_results_refresh_without_navigation_and_ignore_old_responses(self):
        source = Path(__file__).resolve().parents[1] / "templates/webapp.html"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(process.argv[1], 'utf8');
const start = template.indexOf('const TN_SUBS =');
const controller = template.slice(start, template.indexOf('function tnLogo(team)', start));
const navigation = template.slice(template.indexOf('function navigate(screen)'),
  template.indexOf('async function refreshDashboard()'));
const tick = () => new Promise(setImmediate);

function boot() {
  const timers = new Map(), handlers = {}, elements = {}, calls = [], writes = [], waiting = [];
  let timerId = 0, holdReads = false, holdStats = false, failReads = false;
  const live = [1, 2].map(id => ({id, name: `Cup ${id}`, kind: 'challenge'}));
  const payloads = new Map(live.map(t => [t.id, {
    live: true, live_tournaments: live, tournament: {...t, matches_played: 0}, table: [],
    fixtures: [{id: t.id * 10, status: 'live', result_text: null}],
  }]));
  const stats = new Map([[1, 0], [2, 0]]);
  function element(id) {
    if (!elements[id]) {
      let html = '';
      elements[id] = {style: {}, classList: {add() {}, remove() {}, toggle() {}},
        addEventListener() {}, get innerHTML() { return html; },
        set innerHTML(value) { html = value; writes.push({id, value}); }};
    }
    return elements[id];
  }
  const context = vm.createContext({
    currentScreen: 'home', navigator: {onLine: true}, console: {error() {}},
    document: {hidden: false, getElementById: element, querySelectorAll: () => [],
      querySelector: () => null, addEventListener(name, fn) { handlers[name] = fn; }},
    window: {scrollTo() {}, addEventListener(name, fn) { handlers[name] = fn; }},
    sessionStorage: {getItem: () => null, setItem() {}}, haptic() {},
    escapeHtml: value => String(value ?? ''), syncCommunityCards() {}, showNewsBackButton() {},
    refreshDashboard() {},
    renderTournament() {
      const data = vm.runInContext('tnData', context);
      element('tn-content').innerHTML = `${data.tournament.id}:${data.fixtures[0].status}`;
    },
    api: async (path, body) => {
      const id = body.tournament_id || 1;
      calls.push({path, id});
      const isStats = path.endsWith('/stats');
      if (!isStats && failReads) throw new Error('Disconnected');
      const data = isStats ? {tournament_id: id, value: stats.get(id)} : payloads.get(id);
      const response = {ok: true, data: JSON.parse(JSON.stringify(data))};
      if (isStats ? holdStats : holdReads) {
        return new Promise(resolve => waiting.push({id, isStats, resolve: () => resolve(response)}));
      }
      return response;
    },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(controller + '\n' + navigation, context);
  vm.runInContext(`updateTournamentTab({live: true, tournaments: ${JSON.stringify(live)}})`, context);
  return {context, timers, calls, writes, handlers, waiting, payloads, stats, elements,
    run: code => vm.runInContext(code, context),
    holdReads: value => {holdReads = value;}, holdStats: value => {holdStats = value;},
    failReads: value => {failReads = value;},
    async poll() {
      assert.equal(timers.size, 1, 'exactly one scheduled tournament refresh');
      const [id, timer] = [...timers][0];
      assert.equal(timer.delay, 5000);
      timers.delete(id);
      await timer.fn(); await tick();
    },
  };
}

(async () => {
  const app = boot();
  app.run("navigate('tournament')"); await tick();
  assert.equal(app.elements['tn-content'].innerHTML, '1:live');
  assert.equal(app.run('tnStats.value'), 0);

  app.payloads.get(1).fixtures[0] = {id: 10, status: 'completed', result_text: 'Team A won'};
  app.stats.set(1, 8);
  const writesBefore = app.writes.length;
  await app.poll();
  assert.equal(app.elements['tn-content'].innerHTML, '1:completed');
  assert.equal(app.run('tnStats.value'), 8, 'new results update cached stats too');
  assert(!app.writes.slice(writesBefore).some(w => w.value.includes('spinner')),
    'background updates keep the current results visible');
  const statsCalls = app.calls.filter(c => c.path.endsWith('/stats')).length;
  await app.poll();
  assert.equal(app.calls.filter(c => c.path.endsWith('/stats')).length, statsCalls,
    'unchanged results reuse the stats cache');

  app.failReads(true);
  await app.poll();
  assert.equal(app.elements['tn-content'].innerHTML, '1:completed',
    'transient background failures retain the last results');
  assert.equal(app.run('tnLoading'), false, 'a rejected refresh releases loading state');
  app.failReads(false);
  await app.poll();
  assert.equal(app.elements['tn-content'].innerHTML, '1:completed');

  app.context.document.hidden = true;
  app.handlers.visibilitychange();
  assert.equal(app.timers.size, 0);
  const callsBeforeResume = app.calls.length;
  app.context.document.hidden = false;
  app.handlers.visibilitychange(); await tick();
  assert.equal(app.calls.length, callsBeforeResume + 1, 'returning to the app refreshes immediately');
  app.context.navigator.onLine = false; app.handlers.offline();
  assert.equal(app.timers.size, 0);
  app.context.navigator.onLine = true; app.handlers.online(); await tick();
  assert.equal(app.timers.size, 1);
  app.run("navigate('home')");
  assert.equal(app.timers.size, 0, 'leaving the tournament stops polling');

  // A slow old tournament response cannot replace the newly selected cup.
  const switched = boot(); switched.holdReads(true);
  switched.run("navigate('tournament')");
  switched.run('tnSelect(2)');
  assert.equal(switched.waiting.length, 2);
  switched.waiting.find(w => w.id === 1).resolve(); await tick();
  assert.equal(switched.run('tnId'), 2);
  assert.equal(switched.run('tnData'), null);
  switched.waiting.find(w => w.id === 2).resolve(); await tick();
  assert.equal(switched.elements['tn-content'].innerHTML, '2:live');
  assert.equal(switched.run('tnLoading'), false);
  assert.equal(switched.timers.size, 1);

  // Leaving while a request is pending prevents its completion rearming a timer.
  const left = boot(); left.holdReads(true);
  left.run("navigate('tournament')");
  left.run("navigate('home')");
  left.waiting[0].resolve(); await tick();
  assert.equal(left.run('tnData'), null);
  assert.equal(left.timers.size, 0);

  // A slow stats response from before the final ball cannot overwrite new stats.
  const staleStats = boot(); staleStats.holdStats(true);
  staleStats.run("navigate('tournament')"); await tick();
  staleStats.payloads.get(1).fixtures[0].status = 'completed';
  staleStats.stats.set(1, 15);
  await staleStats.poll();
  assert.equal(staleStats.waiting.length, 2);
  staleStats.waiting[1].resolve(); await tick();
  assert.equal(staleStats.run('tnStats.value'), 15);
  staleStats.waiting[0].resolve(); await tick();
  assert.equal(staleStats.run('tnStats.value'), 15);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
