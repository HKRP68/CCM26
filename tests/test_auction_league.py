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
                        if st.get("awaiting_noms"):
                            picks = [c["id"] for c in AL.nomination_choices(st)[:3]]
                            AL.submit_nominations(st, picks)
                            continue
                        break
                    if lot["status"] not in ("open",) + AL.RTM_WAITING:
                        continue
                lot = st["lot"]
                if lot is None:
                    continue
                if lot["status"] in ("rtm_intent", "rtm"):
                    AL.user_rtm(st, rng.random() < 0.5)
                    continue
                if lot["status"] == "rtm_raise":
                    opts = AL.rtm_raise_options(st)
                    AL.user_final_raise(st, opts[0] if opts and rng.random() < 0.5 else None)
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

    def test_bid_by_bid_one_raise_at_a_time(self):
        st = new_state()
        AL.apply_retentions(st, [])
        # Find a player an AI side opens on (pass the ones nobody wants).
        for _ in range(300):
            lot = AL.open_next_lot(st)
            if lot["status"] == "open" and lot["leader"] is not None:
                break
            if st.get("lot"):
                AL.user_pass(st)
        # One AI bid at base, and then it is your call.
        self.assertEqual((lot["bids"], lot["price"]), (1, lot["base"]))
        self.assertTrue(AL.user_may_bid(st)[0])
        out = AL.user_bid(st)
        if out == "user":
            # Your raise, then exactly one AI answer.
            self.assertEqual(lot["bids"], 3)
            self.assertNotEqual(lot["leader"], st["user_team"])

    def test_fast_mode_lets_the_ai_settle_first(self):
        slow, fast = new_state(seed=21), new_state(seed=21)
        fast["fast"] = True
        for st in (slow, fast):
            AL.apply_retentions(st, [])
        a, b = AL.open_next_lot(slow), AL.open_next_lot(fast)
        self.assertEqual(a["pid"], b["pid"])
        self.assertGreaterEqual(b["bids"], a["bids"])

    def test_simulate_lot_decides_exactly_one_player(self):
        st = new_state()
        AL.apply_retentions(st, [])
        AL.open_next_lot(st)
        first = st["lot"]["pid"] if st.get("lot") else st["last_lot"]["pid"]
        lot = AL.simulate_lot(st)
        self.assertEqual(lot["pid"], first)
        self.assertIn(lot["status"], ("sold", "unsold"))
        self.assertIsNone(st["lot"])
        self.assertFalse(st.get("autopilot"))

    def test_ipl_like_prices_and_purses_spent(self):
        lefts, tops = [], []
        for seed in range(4):
            st = new_state(seed=seed)
            AL.apply_retentions(st, [])
            AL.simulate_to_last(st)
            AL.finish_auction(st)
            prices = [e["price"] for e in st["sold_log"]
                      if e["how"] in (AL.HOW_AUCTION, AL.HOW_RTM)]
            self.assertLessEqual(max(prices), AL.RECORD_PRICE)
            tops.append(max(prices))
            lefts += [t["purse"] for n, t in st["teams"].items()
                      if n != st["user_team"]]
        # Stars go big, as at the IPL…
        self.assertTrue(all(t >= 1500 for t in tops), tops)
        # …and the AI spends its purse down to a few crore.
        lefts.sort()
        self.assertLessEqual(lefts[len(lefts) // 2], 400, lefts)

    def test_prices_follow_rating(self):
        bands, pairs, inverted = {}, 0, 0
        for seed in range(6):
            st = new_state(seed=seed)
            AL.apply_retentions(st, [])
            AL.simulate_to_last(st)
            AL.finish_auction(st)
            sold = [(AL.card(st, e["pid"])["rating"], e["price"]) for e in st["sold_log"]
                    if e["how"] in (AL.HOW_AUCTION, AL.HOW_RTM)]
            for r, p in sold:
                bands.setdefault(r // 3 * 3, []).append(p)
            for a in sold:
                for b in sold:
                    if a[0] >= b[0] + 3:
                        pairs += 1
                        inverted += a[1] < b[1]
            for name in st["team_order"]:
                if name != st["user_team"]:
                    self.assertGreaterEqual(len(st["teams"][name]["squad"]), 15, name)
        # A clearly better player almost never goes for less…
        self.assertLess(inverted / pairs, 0.02)
        # …and every rating band's median sits above the band below it.
        medians = [sorted(v)[len(v) // 2] for k, v in sorted(bands.items())
                   if k >= 75 and len(v) >= 8]
        self.assertEqual(medians, sorted(medians))
        self.assertEqual(len(set(medians)), len(medians))

    def test_base_price_ladder_is_monotonic(self):
        prices = [AL.base_price(r) for r in range(60, 100)]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(AL.base_price(95), 200)
        self.assertEqual(AL.base_price(65), 30)

    def test_jump_bid(self):
        st = new_state()
        AL.apply_retentions(st, [])
        for _ in range(300):
            lot = AL.open_next_lot(st)
            if lot["status"] == "open" and lot["leader"] is not None:
                break
            if st.get("lot"):
                AL.user_pass(st)
        jump = AL.user_jump_price(st)
        self.assertEqual(jump, lot["price"] + AL.jump_amount(lot["price"]))
        with self.assertRaises(AL.AuctionLeagueError):
            AL.user_bid(st, to_price=lot["price"])        # stale / below next
        AL.user_bid(st, to_price=jump)
        self.assertIn([st["user_team"], jump], lot["trail"])

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


def card_set(spec, start=1, overseas_every=0):
    """Cards from ``[(role, rating, bat), …]``."""
    out = []
    for i, (role, rating, bat) in enumerate(spec):
        out.append(AL.make_card(start + i, f"C{start + i}", category=role, rating=rating,
                                bat_rating=bat, bowl_rating=rating if role in
                                ("Bowler", "All-rounder") else 30,
                                is_overseas=bool(overseas_every) and i % overseas_every == 0))
    return out


class PlayingXI(unittest.TestCase):
    def test_shape_and_batting_order(self):
        cards = card_set([("Batsman", 90 - i, 90 - i) for i in range(6)]
                         + [("Wicket Keeper", 85, 80), ("Wicket Keeper", 80, 75)]
                         + [("All-rounder", 88 - i, 70 - i) for i in range(4)]
                         + [("Bowler", 92 - i, 30 + i) for i in range(5)])
        xi = AL.ai_playing_xi(cards)
        counts = {r: sum(1 for c in xi if c["category"] == r) for r in AL.ROLES}
        self.assertEqual(counts, {"Batsman": 4, "Wicket Keeper": 1,
                                  "All-rounder": 3, "Bowler": 3})
        bats = [c["bat_rating"] for c in xi]
        self.assertEqual(bats, sorted(bats, reverse=True))
        # The best of each role is picked.
        self.assertIn(92, [c["rating"] for c in xi if c["category"] == "Bowler"])

    def test_short_role_still_legal(self):
        from services import xi_rules
        cards = card_set([("Batsman", 85, 85)] * 7 + [("Wicket Keeper", 80, 80)]
                         + [("All-rounder", 82, 70)] + [("Bowler", 84, 30)] * 6)
        xi = AL.ai_playing_xi(cards)
        self.assertEqual(len(xi), 11)
        shims = [AL.LeagueCard(c) for c in xi]
        ok, err = xi_rules.validate_challenge_xi(shims, 0, 11)
        self.assertTrue(ok, err)

    def test_overseas_limit(self):
        cards = card_set([("Batsman", 90 - i, 90 - i) for i in range(6)]
                         + [("Wicket Keeper", 85, 80)]
                         + [("All-rounder", 88 - i, 70) for i in range(4)]
                         + [("Bowler", 92 - i, 30) for i in range(5)], overseas_every=2)
        xi = AL.ai_playing_xi(cards, overseas_max=4)
        self.assertLessEqual(sum(1 for c in xi if c["is_overseas"]), 4)
        self.assertEqual(len(xi), 11)


class SeasonFeatures(unittest.TestCase):
    def _season(self, seed=3):
        st = new_state(seed=seed)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        return st

    def test_every_fixture_has_venue_and_pitch(self):
        st = self._season()
        from services.match_constants import PITCH_TYPES
        self.assertTrue(all(t.get("ground") for t in st["teams"].values()))
        for fx in st["fixtures"]:
            self.assertIn(fx["pitch"], PITCH_TYPES)
            self.assertEqual(fx["venue"], st["teams"][fx["home"]]["ground"])
        self.assertGreater(len({fx["pitch"] for fx in st["fixtures"]}), 1)

    def test_balancing_trades_narrow_the_league(self):
        before, after = [], []
        for seed in range(6):
            st = new_state(seed=seed)
            AL.apply_retentions(st, [])
            AL.simulate_to_last(st)
            AL.autofill(st)
            before.append(AL.ai_spread(st))
            news = AL.balance_squads(st)
            after.append(AL.ai_spread(st))
            self.assertLessEqual(len(news), AL.AI_BALANCE_TRADES)
            self.assertTrue(all(AL.squad_is_legal(st, n) for n in st["team_order"]))
            self.assertLessEqual(after[-1], before[-1])
        self.assertLess(sum(after) / len(after), sum(before) / len(before))

    def test_trade_window_rules(self):
        st = self._season()
        t = AL.trade_window(st)
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t["offers"]), AL.AI_OFFERS_MAX)
        me = st["user_team"]
        mine = AL.squad_card_dicts(st, me)
        # An unfair ask (their best for your worst) is refused and can't be repeated.
        team = next(n for n in st["team_order"] if n != me)
        theirs = AL.squad_card_dicts(st, team)
        worst, best = mine[-1], theirs[0]
        if best["rating"] > worst["rating"] + 1:
            ok, _why = AL.propose_trade(st, worst["id"], team, best["id"])
            self.assertFalse(ok)
            with self.assertRaises(AL.AuctionLeagueError):
                AL.propose_trade(st, worst["id"], team, best["id"])
        # Offers can be rejected; a closed window refuses everything.
        for o in list(t["offers"]):
            self.assertEqual(AL.answer_offer(st, o["id"], False)["status"], "rejected")
        AL.close_trade_window(st)
        with self.assertRaises(AL.AuctionLeagueError):
            AL.propose_trade(st, mine[0]["id"], team, theirs[0]["id"])

    def test_fair_trade_accepted_and_capped(self):
        st = self._season(seed=5)
        me = st["user_team"]
        done = 0
        for team in st["team_order"]:
            if team == me or done >= AL.TRADE_MAX:
                continue
            for mine in AL.squad_card_dicts(st, me):
                hit = None
                for theirs in AL.squad_card_dicts(st, team):
                    if (theirs["category"] == mine["category"]
                            and theirs["rating"] <= mine["rating"] - 2):
                        ok, _ = AL.propose_trade(st, mine["id"], team, theirs["id"])
                        if ok:
                            hit = theirs
                            break
                if hit:
                    done += 1
                    self.assertTrue(any(e["pid"] == hit["id"] for e in st["teams"][me]["squad"]))
                    break
        self.assertTrue(all(AL.squad_is_legal(st, n) for n in st["team_order"]))
        if done >= AL.TRADE_MAX:
            mine = AL.squad_card_dicts(st, me)[0]
            other = next(n for n in st["team_order"] if n != me)
            with self.assertRaises(AL.AuctionLeagueError):
                AL.propose_trade(st, mine["id"], other, AL.squad_card_dicts(st, other)[-1]["id"])

    def test_stats_awards_and_qualification(self):
        st = self._season()
        AL.close_trade_window(st)
        guard = 0
        while st["phase"] == AL.PHASE_SEASON and guard < 100:
            guard += 1
            AL.sim_until_user(st)
            fx = AL.next_user_fixture(st)
            if fx is not None:
                AL.simulate_fixture(st, fx)
        self.assertEqual(st["phase"], AL.PHASE_COMPLETED)
        bat = st["stats"]["bat"]
        self.assertTrue(any(r.get("sixes") for r in bat.values()))
        self.assertEqual(sum(r["inns"] for r in st["stats"]["bowl"].values()) > 0, True)
        for board in AL.STAT_BOARDS:
            AL.stat_board(st, board)
        totals = AL.stat_board(st, "totals")
        self.assertEqual(len(st["records"]["totals"]), 2 * len(st["fixtures"]))
        self.assertGreaterEqual(totals["high"][0]["runs"], totals["high"][-1]["runs"])
        marks = AL.qualification(st)
        self.assertEqual(sorted(v for v in marks.values()), ["E"] * 6 + ["Q"] * 4)
        self.assertEqual(len(AL.recent_form(st, st["user_team"])), 5)
        aw = AL.season_awards(st)
        for key in ("orange", "purple", "mvp", "sixes"):
            self.assertIn(key, aw)
        self.assertEqual(len(aw["xi"]), 11)
        top = AL.stat_board(st, "orange", 1)[0]
        self.assertEqual(aw["orange"][0], top[0])

    def test_qualification_midseason_is_certain_only(self):
        st = self._season()
        marks = AL.qualification(st)
        self.assertTrue(all(v is None for v in marks.values()))


if __name__ == "__main__":
    unittest.main()


def _rtm_lot(st, holder, winner, value=None):
    """Put a player whose former side is ``holder`` on the block, sold to
    ``winner`` at base — ready for ``_hammer``'s Right To Match."""
    AL.apply_retentions(st, [])
    lot = AL.open_next_lot(st)
    while lot is None or st.get("lot") is None:
        lot = AL.open_next_lot(st)
    c = AL.card(st, lot["pid"])
    c["team"] = holder
    st["teams"][holder]["rtm"] = 1
    lot["leader"], lot["price"] = winner, lot["base"]
    lot["values"] = {k: (value if value is not None else lot["base"]) for k in lot["values"]}
    return lot


class RightToMatch(unittest.TestCase):
    def test_user_holder_calls_and_matches_the_raise(self):
        st = new_state(seed=5)
        lot = _rtm_lot(st, "Team A", "Team B", value=0)
        lot["values"]["Team B"] = lot["base"] * 3
        self.assertEqual(AL._hammer(st, random.Random(1)), "rtm")
        self.assertEqual(lot["status"], "rtm_intent")
        AL.user_rtm(st, True)
        self.assertEqual(lot["status"], "rtm")
        self.assertGreater(lot["price"], lot["base"])     # the buyer's final raise
        raised = lot["price"]
        AL.user_rtm(st, True)
        self.assertEqual(st["last_lot"]["winner"], "Team A")
        self.assertEqual(st["last_lot"]["price"], raised)
        self.assertEqual(st["teams"]["Team A"]["rtm"], 0)

    def test_user_holder_lets_him_go(self):
        st = new_state(seed=5)
        lot = _rtm_lot(st, "Team A", "Team B")
        AL._hammer(st, random.Random(1))
        AL.user_rtm(st, False)
        self.assertEqual(st["last_lot"]["winner"], "Team B")
        self.assertEqual(st["last_lot"]["price"], lot["base"])
        self.assertEqual(st["teams"]["Team A"]["rtm"], 1)

    def test_user_buyer_gets_one_final_raise(self):
        st = new_state(seed=5)
        lot = _rtm_lot(st, "Team B", "Team A", value=10 ** 6)
        lot["pid"]  # noqa: B018
        AL.card(st, lot["pid"])["rating"] = 95       # a star: the AI calls RTM
        self.assertEqual(AL._hammer(st, random.Random(1)), "rtm")
        self.assertEqual(lot["status"], "rtm_raise")
        opts = AL.rtm_raise_options(st)
        self.assertTrue(opts and all(p > lot["base"] for p in opts))
        # Raise beyond what Team B values him at: it can't match.
        lot["values"]["Team B"] = lot["base"]
        AL.user_final_raise(st, opts[-1])
        self.assertEqual(st["last_lot"]["winner"], "Team A")
        self.assertEqual(st["last_lot"]["price"], opts[-1])
        with self.assertRaises(AL.AuctionLeagueError):
            AL.user_final_raise(st, None)

    def test_ai_holder_matches_ai_buyer(self):
        st = new_state(seed=5)
        lot = _rtm_lot(st, "Team B", "Team C", value=10 ** 6)
        AL.card(st, lot["pid"])["rating"] = 95
        lot["values"]["Team C"] = lot["base"] * 2
        AL._hammer(st, random.Random(1))
        done = st["last_lot"]
        self.assertEqual(done["status"], "sold")
        self.assertEqual(done["winner"], "Team B")
        self.assertEqual(done["how"], AL.HOW_RTM)
        self.assertGreaterEqual(done["price"], lot["base"])


class AcceleratedRound(unittest.TestCase):
    def _to_noms(self, seed=4):
        st = new_state(seed=seed)
        AL.apply_retentions(st, [])
        st["nominations_by_hand"] = True
        for _ in range(5000):
            if st.get("lot") is None and AL.open_next_lot(st) is None:
                break
            if st.get("lot"):
                lot = st["lot"]
                if lot["status"] in ("rtm_intent", "rtm"):
                    AL.user_rtm(st, False)
                elif lot["status"] == "rtm_raise":
                    AL.user_final_raise(st, None)
                else:
                    AL.user_pass(st)
        return st

    def test_only_nominated_players_return(self):
        st = self._to_noms()
        self.assertTrue(st.get("awaiting_noms"))
        self.assertFalse(AL.auction_finished(st))
        unsold = list(st["unsold"])
        mine = [c["id"] for c in AL.nomination_choices(st)[:2]]
        ai = set()
        for name in st["team_order"]:
            if name != st["user_team"]:
                ai.update(AL.ai_nominations(st, name))
        with self.assertRaises(AL.AuctionLeagueError):
            AL.submit_nominations(st, unsold[:AL.ACCEL_NOMS_USER + 1])
        AL.submit_nominations(st, mine)
        listed = st["sets"][-1]["pids"]
        self.assertTrue(st["sets"][-1]["accelerated"])
        self.assertEqual(set(listed), (set(mine) | ai) & set(unsold))
        self.assertFalse(st.get("awaiting_noms"))
        AL.simulate_to_last(st)
        self.assertTrue(AL.auction_finished(st))
        AL.finish_auction(st)
        assert_all_legal(self, st)


class PitchAndForm(unittest.TestCase):
    def _cards(self):
        spec = ([("Batsman", 88 - i, 88 - i) for i in range(5)]
                + [("Wicket Keeper", 84, 80)]
                + [("All-rounder", 84 - i, 70) for i in range(4)]
                + [("Bowler", 86 - i, 30) for i in range(6)])
        cards = card_set(spec)
        for c in cards:
            if c["category"] in ("All-rounder", "Bowler"):
                c["bowl_style"] = "Fast Medium"
        # Two bowlers a little below the best three quicks spin.
        for c in [c for c in cards if c["category"] == "Bowler"][3:5]:
            c["bowl_style"] = "Right Arm Off Break"
        return cards

    def test_spinners_picked_on_dusty_pace_on_green(self):
        cards = self._cards()
        spin_ids = {c["id"] for c in cards if AL.is_spinner(c)}
        for pitch, want in (("Dusty", 2), ("Green", 0), ("Flat", 0)):
            xi = AL.ai_playing_xi(cards, pitch=pitch)
            counts = {r: sum(1 for c in xi if c["category"] == r) for r in AL.ROLES}
            self.assertEqual(counts, {"Batsman": 4, "Wicket Keeper": 1,
                                      "All-rounder": 3, "Bowler": 3}, pitch)
            self.assertEqual(sum(1 for c in xi if c["id"] in spin_ids), want, pitch)

    def test_form_moves_ratings(self):
        st = new_state(seed=6)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        team = st["team_order"][1]
        squad = AL.squad_card_dicts(st, team)
        hot, cold = squad[0], squad[1]
        st["form"] = {str(hot["id"]): [200] * 3, str(cold["id"]): [0] * 3}
        self.assertEqual(AL.form_level(st, hot["id"]), 2)
        self.assertEqual(AL.form_level(st, cold["id"]), -2)
        st["form"][str(hot["id"])] = [200, 200]      # not enough matches yet
        self.assertEqual(AL.form_level(st, hot["id"]), 0)
        st["form"][str(hot["id"])] = [200] * 3
        by_id = {c["id"]: c for c in AL.match_cards(st, team)}
        self.assertEqual(by_id[hot["id"]]["rating"], min(99, hot["rating"] + 2))
        self.assertEqual(by_id[cold["id"]]["rating"], cold["rating"] - 2)

    def test_injuries_heal_and_replacements_keep_xi_legal(self):
        st = new_state(seed=6)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        team = st["team_order"][2]
        keepers = [c["id"] for c in AL.squad_card_dicts(st, team)
                   if c["category"] == "Wicket Keeper"]
        st["injuries"] = {str(p): {"team": team, "left": 1, "fx": 0} for p in keepers}
        made = AL.sign_injury_replacements(st, team)
        self.assertTrue(made)
        self.assertTrue(AL._can_field_xi(AL.match_cards(st, team)))
        self.assertTrue(all(p not in [c["id"] for c in AL.match_cards(st, team)]
                            for p in keepers))
        fx = {"no": 99}
        rng = random.Random(0)
        rng.random = lambda: 1.0        # nobody new gets hurt
        news = AL.note_match(st, fx, {team: []}, {}, rng)
        self.assertEqual({n["kind"] for n in news}, {"fit"})
        self.assertFalse(st["injuries"])

    def test_season_with_form_and_injuries_runs(self):
        st = new_state(seed=8)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        for _ in range(200):
            fx = next(iter(AL.pending_fixtures(st)), None)
            if fx is None:
                break
            AL.simulate_fixture(st, fx)
        self.assertTrue(st["form"])
        for name in st["teams"]:
            self.assertTrue(AL._can_field_xi(AL.match_cards(st, name)))


class NewsTicker(unittest.TestCase):
    def _lot(self, st, rating=90):
        if st["phase"] != AL.PHASE_AUCTION:
            AL.apply_retentions(st, [])
        lot = AL.open_next_lot(st)
        while st.get("lot") is None:
            lot = AL.open_next_lot(st)
        AL.card(st, lot["pid"])["rating"] = rating
        return lot

    def test_record_war_steal_and_unsold(self):
        st = new_state(seed=2)
        lot = self._lot(st)
        lot.update(leader="Team B", price=2000, bids=20, values={},
                   trail=[(f"Team {x}", 100) for x in "BCDEFG"])
        AL._sell(st, "Team B", 2000, AL.HOW_AUCTION)
        texts = AL.headlines(st)
        self.assertTrue(any(t.startswith("💰") for t in texts))
        self.assertTrue(any(t.startswith("🔥") for t in texts))
        mark = AL.news_mark(st)
        lot = self._lot(st, rating=91)
        AL._sell(st, "Team C", lot["base"], AL.HOW_AUCTION)
        self.assertTrue(AL.headlines(st, since=mark)[0].startswith("💎"))
        mark = AL.news_mark(st)
        lot = self._lot(st, rating=92)
        lot["leader"] = None
        AL._hammer(st, random.Random(0))
        self.assertIn("unsold", AL.headlines(st, since=mark)[0])

    def test_full_auction_news_is_bounded_and_ranked(self):
        st = new_state(seed=3)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        self.assertLessEqual(len(st["news"]), AL.NEWS_KEPT)
        self.assertTrue(any("done" in t for t in AL.headlines(st)))     # set wrap-ups
        best = AL.headlines(st, limit=3, best=True)
        self.assertEqual(len(best), 3)
        self.assertFalse(any("done —" in t for t in best))


def play_season(st):
    guard = 0
    while st["phase"] == AL.PHASE_SEASON and guard < 100:
        guard += 1
        AL.sim_until_user(st)
        fx = AL.next_user_fixture(st)
        if fx is not None:
            AL.simulate_fixture(st, fx)


class MultiSeason(unittest.TestCase):
    def _finished(self, seed=3):
        st = new_state(seed=seed)
        AL.apply_retentions(st, [])
        AL.simulate_to_last(st)
        AL.finish_auction(st)
        play_season(st)
        self.assertEqual(st["phase"], AL.PHASE_COMPLETED)
        return st

    def test_next_season_carries_squads_and_history(self):
        prev = self._finished()
        with self.assertRaises(AL.AuctionLeagueError):
            AL.next_season_state(new_state())
        nxt = AL.next_season_state(prev, seed=11)
        self.assertEqual(nxt["phase"], AL.PHASE_RETENTION)
        self.assertEqual(AL.season_no(nxt), 2)
        self.assertEqual(len(nxt["career"]), 1)
        self.assertEqual(nxt["career"][0]["season"], 1)
        self.assertEqual(nxt["career"][0]["champion"], prev["champion"])
        # Last season's squads are now each side's own players (retention, RTM).
        for name, t in prev["teams"].items():
            own = {c["id"] for c in AL.original_squad(nxt, name)}
            self.assertEqual(own, {e["pid"] for e in t["squad"]})
            self.assertEqual(nxt["teams"][name]["personality"], t["personality"])
        self.assertEqual(set(nxt["pool"]), set(prev["pool"]))
        # Ratings moved by at most two, and only for those who played.
        nudges = AL.rating_nudges(prev)
        self.assertTrue(any(d > 0 for d in nudges.values()))
        self.assertTrue(any(d < 0 for d in nudges.values()))
        for pid, c in nxt["pool"].items():
            old = prev["pool"][pid]["rating"]
            self.assertLessEqual(abs(c["rating"] - old), AL.NUDGE_MAX)
            if int(pid) not in nudges:
                self.assertEqual(c["rating"], old)
        # Retain your best three from last season's squad and run season 2.
        mine = [c["id"] for c in AL.original_squad(nxt, nxt["user_team"])[:3]]
        AL.apply_retentions(nxt, mine)
        AL.simulate_to_last(nxt)
        AL.finish_auction(nxt)
        assert_all_legal(self, nxt)
        play_season(nxt)
        rec = AL.career_record(nxt)
        self.assertEqual(rec["seasons"], 2)
        self.assertEqual([r["season"] for r in AL.career_history(nxt)], [1, 2])
        self.assertEqual(rec["p"], sum(r["p"] for r in AL.career_history(nxt)))
