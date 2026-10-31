"""Exercise tournament loading and recovery with the actual Miniapp controller."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is needed for the Miniapp controller')
class TournamentMiniappUITests(unittest.TestCase):
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
  document:{getElementById:id => elements[id]},
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
})().catch(error => { console.error(error); process.exitCode=1; });
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
