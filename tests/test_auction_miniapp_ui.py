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
  const ids = ['auction-app', 'au-body', 'au-nav', 'au-season', 'au-status', 'au-connection'];
  const elements = Object.fromEntries(ids.map(id => [id, {
    dataset: {}, innerHTML: '', textContent: '', disabled: false,
    addEventListener() {}, querySelectorAll() { return []; }, contains() { return false; }
  }]));
  const context = vm.createContext({
    document: {getElementById: id => elements[id], addEventListener() {},
               querySelectorAll() { return []; }, activeElement: null},
    window: {addEventListener() {}}, currentScreen: 'auction',
    escapeHtml: v => String(v ?? ''), haptic() {}, toast() {}, tg: undefined,
    api: async () => ({ok: true, data}),
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {}
  });
  vm.runInContext(source, context);
  await vm.runInContext('AuctionApp.open()', context);
  await new Promise(setImmediate);
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
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
