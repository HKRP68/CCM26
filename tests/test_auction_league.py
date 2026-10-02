"""Auction League (/auctionleague) — the solo retain → auction → season career.

The promises pinned here:

  • Retention costs exactly 18 / 14 / 10 Cr and the AI keeps only its stars.
  • Sets run marquee first, then rating bands per role.
  • **Every squad the auction ends with is legal** — role minimums, at most
    18, the overseas cap, purses never negative, nobody sold twice — over many
    seeds, whether you bid by hand or hand it to the autopilot.
  • /simset stops at the end of a set; /simtolast runs to the end.
  • The season is a single round robin plus the four IPL playoff matches,
    paired from the table.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import auction_league_service as AL  # noqa: E402

ROLE_PLAN = (["Batsman"] * 7 + ["Wicket Keeper"] * 2 + ["All-rounder"] * 4
             + ["Bowler"] * 7)


def make_league(n_teams=10, per_team=20, seed=1, home=True):
    rng = random.Random(seed)
    teams, pid = [], 1
    for t in range(n_teams):
        players = []
        for i, role in enumerate(ROLE_PLAN[:per_team]):
            rating = rng.randint(66, 96)
            players.append(AL.make_card(
                pid, f"P{pid}", category=role, rating=rating,
                bat_rating=rating if role != "Bowler" else rating - 25,
                bowl_rating=rating if role in ("Bowler", "All-rounder") else 30,
                is_overseas=home and i % 4 == 0, source_player_id=pid))
            pid += 1
        teams.append({"name": f"Team {chr(65 + t)}", "short": f"T{chr(65 + t)}",
                      "players": players})
    league = {"id": 1, "name": "IPL", "key": "ipl",
              "home_country": "India" if home else None,
              "overseas_min": 0, "overseas_max": 4}
    return league, teams


def new_state(seed=7, **kw):
    league, teams = make_league(seed=seed)
    return AL.new_state(league, teams, "Team A", seed=seed, **kw)


def assert_all_legal(tc, st):
    seen = set()
    for name, t in st["teams"].items():
        tc.assertGreaterEqual(t["purse"], 0, name)
        tc.assertLessEqual(len(t["squad"]), AL.SQUAD_MAX, name)
        tc.assertTrue(AL.squad_is_legal(st, name),
                      f"{name}: {AL.role_counts(st, name)} size {len(t['squad'])}")
        for e in t["squad"]:
            tc.assertNotIn(e["pid"], seen)
            seen.add(e["pid"])


class MoneyAndRetention(unittest.TestCase):
    def test_money(self):
        self.assertEqual(AL.money(12000), "₹120 Cr")
        self.assertEqual(AL.money(150), "₹1.5 Cr")
        self.assertEqual(AL.money(75), "₹75 L")
        self.assertEqual(AL.increment(95), 5)
        self.assertEqual(AL.increment(150), 10)
        self.assertEqual(AL.increment(450), 20)
        self.assertEqual(AL.increment(900), 25)

    def test_retention_costs(self):
        st = new_state(ai_retain=False)
        own = [c["id"] for c in AL.original_squad(st, "Team A")[:3]]
        AL.apply_retentions(st, own)
        me = AL.user_team(st)
        self.assertEqual(me["purse"], 12000 - 1800 - 1400 - 1000)
        self.assertEqual(me["rtm"], 0)
        self.assertEqual(st["phase"], AL.PHASE_AUCTION)
        for name, t in st["teams"].items():
            if name != "Team A":
                self.assertEqual(t["squad"], [])
                self.assertEqual(t["rtm"], 1)

    def test_retention_rules(self):
        st = new_state()
        other = AL.original_squad(st, "Team B")[0]["id"]
        with self.assertRaises(AL.AuctionLeagueError):
            AL.apply_retentions(st, [other])
        with self.assertRaises(AL.AuctionLeagueError):
            AL.apply_retentions(st, [c["id"] for c in AL.original_squad(st, "Team A")[:4]])

    def test_ai_retains_only_stars(self):
        st = new_state(ai_retain=True)
        made = AL.apply_retentions(st, [])
        ratings = sorted((c["rating"] for c in st["pool"].values()), reverse=True)
        cut = ratings[int(len(ratings) * AL.AI_RETAIN_TOP_FRACTION) - 1]
        kept = 0
        for name, pids in made.items():
            self.assertLessEqual(len(pids), 3)
            for pid in pids:
                self.assertGreaterEqual(AL.card(st, pid)["rating"], cut)
                self.assertEqual(AL.card(st, pid)["team"], name)
            kept += len(pids)
        self.assertGreater(kept, 0)


class Sets(unittest.TestCase):
    def test_marquee_then_bands(self):
        st = new_state()
        AL.apply_retentions(st, [])
        sets = st["sets"]
        self.assertTrue(sets[0]["name"].startswith("⭐ Marquee"))
        for pid in sets[0]["pids"]:
            self.assertGreaterEqual(AL.card(st, pid)["rating"], AL.MARQUEE_MIN_RATING)
        self.assertTrue(sets[1]["name"].startswith("Batsmen 90"))
        listed = [p for s in sets for p in s["pids"]]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed), set(int(k) for k in st["pool"]) - AL.taken_pids(st))


class Auction(unittest.TestCase):
    def test_sim_to_last_legal_over_many_seeds(self):
        for seed in range(12):
            st = new_state(seed=seed, ai_retain=bool(seed % 2))
            AL.apply_retentions(st, [c["id"] for c in AL.original_squad(st, "Team A")[:seed % 4]])
            AL.simulate_to_last(st)
            self.assertIsNone(st["lot"])
            self.assertTrue(AL.auction_finished(st))
            AL.finish_auction(st)
            assert_all_legal(self, st)
            cap = st["overseas_cap"]
            for name in st["teams"]:
                self.assertLessEqual(AL.overseas_count(st, name), cap)

    def test_simset_stops_at_set_end(self):
        st = new_state()
        AL.apply_retentions(st, [])
        first = st["sets"][0]
        AL.simulate_set(st)
        self.assertIsNone(st["lot"])
        for pid in first["pids"]:
            self.assertTrue(pid in AL.taken_pids(st) or pid in st["unsold"])
        # Nothing from set 2 has been touched.
        second = st["sets"][1]["pids"]
        self.assertFalse(any(p in AL.taken_pids(st) for p in second))
        lot = AL.open_next_lot(st)
        self.assertEqual(lot["set_no"], 2)

    def test_simset_upto(self):
        st = new_state()
        AL.apply_retentions(st, [])
        AL.simulate_set(st, upto=3)
        third = st["sets"][2]["pids"]
        self.assertTrue(all(p in AL.taken_pids(st) or p in st["unsold"] for p in third))
        fourth = st["sets"][3]["pids"]
        self.assertFalse(any(p in AL.taken_pids(st) for p in fourth))

    def test_manual_bidding_always_legal(self):
        for seed in range(6):
            st = new_state(seed=seed)
            AL.apply_retentions(st, [])
            rng = random.Random(seed)
            steps = 0
            while steps < 3000:
                steps += 1
                if st.get("lot") is None:
                    lot = AL.open_next_lot(st)
                    if lot is None:
                        break
                    if lot["status"] != "open" and lot["status"] != "rtm":
                        continue
                lot = st["lot"]
                if lot is None:
                    continue
                if lot["status"] == "rtm":
                    AL.user_rtm(st, rng.random() < 0.5)
                    continue
                ok, _price = AL.user_may_bid(st)
                if ok and rng.random() < 0.55:
                    AL.user_bid(st)
                else:
                    AL.user_pass(st)
            self.assertTrue(AL.auction_finished(st))
            AL.finish_auction(st)
            assert_all_legal(self, st)

    def test_user_wins_when_ai_drops(self):
        st = new_state()
        AL.apply_retentions(st, [])
        lot = AL.open_next_lot(st)
        # Make every AI uninterested, then bid once: sold to you at base.
        lot["values"] = {k: 0 for k in lot["values"]}
        lot["leader"], lot["price"], lot["status"] = None, None, "open"
        out = AL.user_bid(st)
        self.assertEqual(out, "done")
        self.assertEqual(st["last_lot"]["winner"], "Team A")
        self.assertEqual(st["last_lot"]["price"], lot["base"])

    def test_cannot_overspend(self):
        st = new_state()
        AL.apply_retentions(st, [])
        AL.user_team(st)["purse"] = 400   # 11 slots × 30L reserve = 330
        lot = AL.open_next_lot(st)
        c = AL.card(st, lot["pid"])
        self.assertLessEqual(AL.max_bid(st, "Team A", c), 400 - 30 * 10)


class Season(unittest.TestCase):
    def _season(self, seed=3):
        st = new_state(seed=seed)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        return st

    def test_fixtures_round_robin(self):
        st = self._season()
        fx = st["fixtures"]
        self.assertEqual(len(fx), 45)
        pairs = {frozenset((f["home"], f["away"])) for f in fx}
        self.assertEqual(len(pairs), 45)
        mine = [f for f in fx if "Team A" in (f["home"], f["away"])]
        self.assertEqual(len(mine), 9)

    def test_full_season_with_playoffs(self):
        st = self._season()
        guard = 0
        while st["phase"] == AL.PHASE_SEASON and guard < 100:
            guard += 1
            AL.sim_until_user(st)
            fx = AL.next_user_fixture(st)
            if fx is not None:
                AL.simulate_fixture(st, fx)  # stand-in for your real match
        self.assertEqual(st["phase"], AL.PHASE_COMPLETED)
        stages = [f["stage"] for f in st["fixtures"]]
        self.assertEqual(stages.count("q1"), 1)
        self.assertEqual(stages.count("elim"), 1)
        self.assertEqual(stages.count("q2"), 1)
        self.assertEqual(stages.count("final"), 1)
        self.assertEqual(len(stages), 49)
        top = AL.standings(st)[:4]
        q1 = next(f for f in st["fixtures"] if f["stage"] == "q1")
        el = next(f for f in st["fixtures"] if f["stage"] == "elim")
        self.assertEqual({q1["home"], q1["away"]}, set(top[:2]))
        self.assertEqual({el["home"], el["away"]}, set(top[2:]))
        self.assertIn(st["champion"], top)
        table = st["table"]
        self.assertEqual(sum(r["p"] for r in table.values()), 90)
        orange, purple = AL.cap_tables(st)
        self.assertTrue(orange and purple)

    def test_record_user_match_idempotent(self):
        st = self._season()
        AL.sim_until_user(st)
        fx = AL.next_user_fixture(st)
        opp = fx["away"] if fx["home"] == "Team A" else fx["home"]
        ms = {"inn1_bat_team": "Team A", "inn1_runs": 180, "inn1_wickets": 6,
              "inn1_balls": 120, "bat_team_name": opp, "total_runs": 150,
              "total_wickets": 10, "balls": 110, "match_id": 99,
              "inn1_bat_stats": {}, "bat_stats": {}, "inn1_bowl_stats": {},
              "bowl_stats": {}}
        self.assertTrue(AL.record_user_match(st, fx["no"], ms, "Team A", match_id=99))
        self.assertFalse(AL.record_user_match(st, fx["no"], ms, "Team A", match_id=99))
        row = st["table"]["Team A"]
        self.assertEqual((row["w"], row["pts"]), (1, 2))
        # The all-out chase is charged the full 20 overs.
        self.assertEqual(st["table"][opp]["bf"], 120)

    def test_sim_restores_global_rng(self):
        st = self._season()
        AL.sim_until_user(st)  # the AI games before yours
        fx = AL.pending_fixtures(st)[0]
        random.seed(1234)
        expected = random.Random(1234).random()
        AL.simulate_fixture(st, fx)
        self.assertEqual(random.random(), expected)

    def test_reward_needs_no_concede(self):
        st = self._season()
        st["phase"] = AL.PHASE_COMPLETED
        st["champion"] = "Team A"
        self.assertEqual(AL.season_reward(st), AL.REWARD_CHAMPION)
        st["user_conceded"] = 1
        self.assertEqual(AL.season_reward(st), (0, 0))


if __name__ == "__main__":
    unittest.main()
