"""Expansion teams: rolling a finished league into next season, and the picks
a brand-new side gets before the auction opens.

The IPL 2022 shape. Gujarat and Lucknow joined a league that already existed,
so they had nobody to retain — and rather than let them walk into the mega
auction with an empty list against eight sides holding three players each,
they were given three picks apiece out of the pool nobody had retained.

What is pinned here:

  • a season can start from a **league** and not only from a previous auction,
    which is what a finished tournament actually leaves behind;
  • who counts as an expansion side is *derived* — the sides the previous
    league records nobody for — not a checkbox somebody has to remember;
  • the snake order, as a sequence and not a set: the whole reason a draft
    snakes is that straight repetition hands the first seat the best player
    every round;
  • a pick is a retention with a different eligibility rule, so it obeys every
    cap retention obeys and the ledger agrees with the purse after each one;
  • **a player another side kept cannot be picked** — the rule the whole
    mechanism exists to enforce.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

_MODULE_NAMES = ("database", "models", "config",
                 "services.player_service", "services.player_query",
                 "services.auction_service", "services.retention_negotiation",
                 "services.auction_scheduler",
                 "handlers.auction")

_PID = itertools.count(1)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


# Eight, so two expansion sides with three picks each still leave a pool.
CATALOGUE = [
    ("Virat Kohli", 97, "Batsman", "India"),
    ("Jasprit Bumrah", 95, "Bowler", "India"),
    ("Rashid Khan", 93, "All Rounder", "Afghanistan"),
    ("Jos Buttler", 92, "Wicket Keeper", "England"),
    ("Sanju Samson", 88, "Wicket Keeper", "India"),
    ("Tim David", 84, "Batsman", "Australia"),
    ("Rinku Singh", 83, "Batsman", "India"),
    ("Mukesh Kumar", 74, "Bowler", "India"),
]

ALICE, BOB, CAROL, DAVE = 111, 222, 333, 444
NOW = datetime(2026, 3, 1, 12, 0, 0)


class ExpansionCase(unittest.TestCase):
    """A finished, published season one — the thing season two follows."""

    def setUp(self):
        from database import get_session
        from models import Player
        from services import auction_service as A

        self.session = get_session()
        self.A = A
        self.tag = next(_PID)

        self.players = []
        for name, rating, category, country in CATALOGUE:
            player = Player(name=name, rating=rating, category=category,
                            country=country, version="Base",
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style="Medium Pacer", bat_rating=rating,
                            bowl_rating=max(0, rating - 10), is_active=True)
            self.session.add(player)
            self.players.append(player)
        # One card whose name is unique across the whole module run. Every
        # test builds its own catalogue into the same database, so by the
        # tenth test there are ten "Virat Kohli"s and a command that looks a
        # player up BY NAME rightly refuses to guess between them.
        self.unique_name = f"Pick Target {self.tag}"
        unique = Player(name=self.unique_name, rating=91, category="Batsman",
                        country="India", version="Base", bat_hand="Right",
                        bowl_hand="Right", bowl_style="Medium Pacer",
                        bat_rating=91, bowl_rating=81, is_active=True)
        self.session.add(unique)
        self.players.append(unique)
        self.unique = unique
        self.session.flush()

        self.one = A.create_season(
            self.session, f"One {self.tag}", min_squad_size=1,
            max_squad_size=8, max_overseas=8, opening_purse_lakh=10_000,
            max_retentions=2)
        A.bind_chat(self.session, self.one, -7000 - self.tag)
        self.mumbai = A.create_franchise(self.session, self.one, "Mumbai",
                                         owner_tg_id=ALICE, sort_order=1)
        self.chennai = A.create_franchise(self.session, self.one, "Chennai",
                                          owner_tg_id=BOB, sort_order=2)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def publish_one(self):
        """Run season one out and publish it. Returns the league."""
        A = self.A
        A.add_players_to_pool(self.session, self.one, self.players)
        self.session.commit()
        lot = A.start(self.session, self.one, now=NOW)
        self.session.commit()
        buyers = (self.mumbai, self.chennai)
        guard = 0
        while lot is not None and guard < 40:
            A.place_bid(self.session, self.one, lot, buyers[guard % 2],
                        lot.base_price_lakh, now=NOW)
            A.sell_lot(self.session, self.one, lot, now=NOW)
            self.session.commit()
            lot = A.open_next_lot(self.session, self.one, now=NOW)
            self.session.commit()
            guard += 1
        league = A.publish_to_league(self.session, self.one)
        self.session.commit()
        return league

    def season_two(self, *, picks=3, extra=("Gujarat", "Lucknow")):
        """Season two from season one's league, plus the new sides."""
        A = self.A
        league = self.publish_one()
        two = A.season_from_league(self.session, league, f"Two {self.tag}",
                                   template=self.one)
        self.session.commit()
        # The carried sides need owners; start() refuses without them.
        order = 1
        for f in A.franchises(self.session, two.id):
            f.owner_tg_id = ALICE if f.name == "Mumbai" else BOB
            f.sort_order = order
            order += 1
        self.new_sides = []
        for tg, name in zip((CAROL, DAVE), extra):
            self.new_sides.append(
                A.create_franchise(self.session, two, name, owner_tg_id=tg,
                                   sort_order=order))
            order += 1
        two.expansion_picks = picks
        self.session.commit()
        A.deal_expansion_picks(self.session, two)
        # Picks come out of the un-retained pool, so retention has to be shut.
        A.lock_retention(self.session, two, quiet=True)
        A.add_players_to_pool(self.session, two, self.players)
        self.session.commit()
        self.two = two
        return two

    def assert_ledger_agrees(self, season, note=""):
        for f in self.A.franchises(self.session, season.id):
            self.assertEqual(
                self.A.ledger_total(self.session, f.id),
                int(f.purse_remaining_lakh or 0),
                f"{f.name}'s ledger and purse disagree {note}")
        self.assertEqual([], self.A.reconcile_purses(self.session, season),
                         f"reconcile_purses found drift {note}")


# ══════════════════════════════════════════════════════════════════════
# Starting a season from a league
# ══════════════════════════════════════════════════════════════════════

class FromLeagueTests(ExpansionCase):

    def test_a_franchise_per_team_and_it_follows_the_league(self):
        league = self.publish_one()
        two = self.A.season_from_league(self.session, league, "Two")
        self.session.commit()
        names = {f.name for f in self.A.franchises(self.session, two.id)}
        self.assertEqual({"Mumbai", "Chennai"}, names)
        self.assertEqual(league.id, two.previous_league_id)
        self.assertEqual(self.A.STATUS_SETUP, two.status)

    def test_and_the_retention_picker_sees_last_seasons_squads(self):
        """The step this exists to stop anyone forgetting."""
        league = self.publish_one()
        two = self.A.season_from_league(self.session, league, "Two")
        self.session.commit()
        held = self.A.previous_squad_map(self.session, two)
        self.assertTrue(held, "nobody is recorded as holding anybody")
        self.assertEqual({"Mumbai", "Chennai"},
                         {f.name for f in held.values()})

    def test_the_new_franchises_have_no_owners_and_start_says_so(self):
        """A ChallengeTeam has no owner to carry, so this is the flow's
        weakest point. It must fail loudly, by name."""
        league = self.publish_one()
        two = self.A.season_from_league(self.session, league, "Two")
        self.A.bind_chat(self.session, two, -7700 - self.tag)
        self.A.add_players_to_pool(self.session, two, self.players)
        self.session.commit()
        for f in self.A.franchises(self.session, two.id):
            self.assertIsNone(f.owner_tg_id)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.start(self.session, two, now=NOW)
        self.assertIn("no owner", str(caught.exception))
        self.assertIn("Mumbai", str(caught.exception))

    def test_rules_come_from_the_template_when_given(self):
        league = self.publish_one()
        self.one.bid_seconds = 47
        self.session.commit()
        two = self.A.season_from_league(self.session, league, "Two",
                                        template=self.one)
        self.session.commit()
        self.assertEqual(47, two.bid_seconds)
        self.assertEqual(self.one.id, two.previous_season_id)

    def test_the_group_is_not_taken_from_the_old_season(self):
        league = self.publish_one()
        two = self.A.season_from_league(self.session, league, "Two")
        self.session.commit()
        self.assertIsNone(two.chat_id)
        self.assertEqual(self.one.id,
                         self.A.season_for_chat(self.session,
                                                self.one.chat_id).id)

    def test_a_second_press_finds_the_season_that_exists(self):
        """Or an admin clicking twice quietly gets two rival fields."""
        league = self.publish_one()
        self.assertIsNone(
            self.A.season_following_league(self.session, league.id))
        two = self.A.season_from_league(self.session, league, "Two")
        self.session.commit()
        found = self.A.season_following_league(self.session, league.id)
        self.assertIsNotNone(found)
        self.assertEqual(two.id, found.id)


# ══════════════════════════════════════════════════════════════════════
# Who is an expansion side
# ══════════════════════════════════════════════════════════════════════

class WhoIsNewTests(ExpansionCase):

    def test_only_the_sides_with_no_previous_squad(self):
        two = self.season_two()
        new = {f.name for f in
               self.A.expansion_franchises(self.session, two)}
        self.assertEqual({"Gujarat", "Lucknow"}, new)

    def test_the_allowance_goes_to_them_and_nobody_else(self):
        two = self.season_two(picks=3)
        by_name = {f.name: f for f in self.A.franchises(self.session, two.id)}
        self.assertEqual(3, by_name["Gujarat"].draft_picks_total)
        self.assertEqual(3, by_name["Lucknow"].draft_picks_total)
        self.assertEqual(0, by_name["Mumbai"].draft_picks_total,
                         "a side with a squad does not get free players")
        self.assertEqual(0, by_name["Chennai"].draft_picks_total)

    def test_a_first_season_has_no_expansion_sides_at_all(self):
        """Every side is new in season one. Handing the whole field free
        players is not what this is for."""
        self.assertEqual([], self.A.expansion_franchises(self.session,
                                                         self.one))

    def test_a_side_that_has_picked_keeps_what_it_has_left(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        two.expansion_picks = 2
        self.A.deal_expansion_picks(self.session, two)
        self.session.commit()
        self.session.refresh(gujarat)
        self.assertEqual(3, gujarat.draft_picks_total,
                         "re-dealing must not strip a side mid-draft")


# ══════════════════════════════════════════════════════════════════════
# The snake
# ══════════════════════════════════════════════════════════════════════

class SnakeOrderTests(ExpansionCase):

    def test_the_running_order_snakes(self):
        """A B B A A B — not A B A B A B. Straight repetition would hand the
        first seat the best player available in every single round."""
        two = self.season_two(picks=3)
        names = [f.name for f in self.A.pick_schedule(self.session, two)]
        self.assertEqual(
            ["Gujarat", "Lucknow", "Lucknow", "Gujarat", "Gujarat", "Lucknow"],
            names)

    def test_the_turn_walks_that_order(self):
        two = self.season_two(picks=3)
        seen = []
        for player in self.players[:6]:
            turn = self.A.pick_turn(self.session, two)
            seen.append(turn.name)
            self.A.draft_pick(self.session, two, turn, player)
            self.session.commit()
        self.assertEqual(
            ["Gujarat", "Lucknow", "Lucknow", "Gujarat", "Gujarat", "Lucknow"],
            seen)
        self.assertIsNone(self.A.pick_turn(self.session, two),
                          "every allowance is spent")

    def test_unequal_allowances_still_work(self):
        two = self.season_two(picks=3)
        gujarat, lucknow = self.A.expansion_franchises(self.session, two)
        self.A.set_franchise_picks(self.session, two, lucknow, 1)
        self.session.commit()
        names = [f.name for f in self.A.pick_schedule(self.session, two)]
        self.assertEqual(["Gujarat", "Lucknow", "Gujarat", "Gujarat"], names)

    def test_it_is_refused_when_it_is_not_your_turn(self):
        two = self.season_two(picks=3)
        gujarat, lucknow = self.A.expansion_franchises(self.session, two)
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, lucknow, self.players[0])
        self.assertIn("Gujarat's pick", str(caught.exception))

    def test_and_when_you_have_none_left(self):
        two = self.season_two(picks=1)
        gujarat, lucknow = self.A.expansion_franchises(self.session, two)
        self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.A.draft_pick(self.session, two, lucknow, self.players[1])
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, gujarat, self.players[2])
        self.assertIn("used all", str(caught.exception))

    def test_a_skip_spends_the_turn_and_moves_on(self):
        """Or a side that does not want its pick stalls everyone."""
        two = self.season_two(picks=2)
        gujarat, lucknow = self.A.expansion_franchises(self.session, two)
        self.A.skip_pick(self.session, two, gujarat)
        self.session.commit()
        self.session.refresh(gujarat)
        self.assertEqual(1, gujarat.draft_picks_used)
        self.assertEqual([], self.A.drafted(self.session, gujarat.id))
        self.assertEqual("Lucknow",
                         self.A.pick_turn(self.session, two).name)


# ══════════════════════════════════════════════════════════════════════
# What a pick may take
# ══════════════════════════════════════════════════════════════════════

class EligibilityTests(ExpansionCase):

    def test_a_retained_player_cannot_be_picked(self):
        """The rule the whole mechanism exists for: picks come out of the
        pool nobody kept."""
        A = self.A
        league = self.publish_one()
        two = A.season_from_league(self.session, league, "Two",
                                   template=self.one)
        self.session.commit()
        order = 1
        for f in A.franchises(self.session, two.id):
            f.owner_tg_id = ALICE
            f.sort_order = order
            order += 1
        gujarat = A.create_franchise(self.session, two, "Gujarat",
                                     owner_tg_id=CAROL, sort_order=order)
        two.expansion_picks = 2
        self.session.commit()

        # Mumbai keep Kohli BEFORE the picks open.
        mumbai = [f for f in A.franchises(self.session, two.id)
                  if f.name == "Mumbai"][0]
        kept = A.retain(self.session, two, mumbai, self.players[0])
        self.session.commit()
        self.assertEqual(A.ACQ_RETAINED, kept.acquisition)

        A.deal_expansion_picks(self.session, two)
        A.lock_retention(self.session, two, quiet=True)
        self.session.commit()

        with self.assertRaises(A.AuctionError) as caught:
            A.draft_pick(self.session, two, gujarat, self.players[0])
        self.assertIn("already been retained by", str(caught.exception))
        self.assertIn("Mumbai", str(caught.exception))

    def test_nor_one_another_new_side_already_took(self):
        two = self.season_two(picks=2)
        gujarat, lucknow = self.A.expansion_franchises(self.session, two)
        self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, lucknow, self.players[0])
        self.assertIn("already been drafted by", str(caught.exception))
        self.assertIn("Gujarat", str(caught.exception))

    def test_picks_are_refused_while_retention_is_still_open(self):
        A = self.A
        league = self.publish_one()
        two = A.season_from_league(self.session, league, "Two",
                                   template=self.one)
        self.session.commit()
        gujarat = A.create_franchise(self.session, two, "Gujarat",
                                     owner_tg_id=CAROL, sort_order=9)
        two.expansion_picks = 2
        self.session.commit()
        A.deal_expansion_picks(self.session, two)
        self.session.commit()
        self.assertFalse(A.retention_locked(two))
        with self.assertRaises(A.AuctionError) as caught:
            A.draft_pick(self.session, two, gujarat, self.players[0])
        self.assertIn("/aretlock on", str(caught.exception))

    def test_and_once_the_auction_has_opened(self):
        two = self.season_two(picks=2)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        self.A.bind_chat(self.session, two, -7800 - self.tag)
        self.A.start(self.session, two, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.assertIn("before the auction opens", str(caught.exception))


# ══════════════════════════════════════════════════════════════════════
# The money
# ══════════════════════════════════════════════════════════════════════

class PickMoneyTests(ExpansionCase):

    def test_a_pick_costs_the_retention_ladder_by_default(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        self.assertEqual(self.A.retention_price_for(two, 1),
                         lot.sold_price_lakh)

    def test_and_the_ladder_steps_down_pick_by_pick(self):
        two = self.season_two(picks=3)
        gujarat, lucknow = self.A.expansion_franchises(self.session, two)
        first = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.A.draft_pick(self.session, two, lucknow, self.players[1])
        self.A.draft_pick(self.session, two, lucknow, self.players[2])
        second = self.A.draft_pick(self.session, two, gujarat,
                                   self.players[3])
        self.session.commit()
        self.assertEqual(self.A.retention_price_for(two, 1),
                         first.sold_price_lakh)
        self.assertEqual(self.A.retention_price_for(two, 2),
                         second.sold_price_lakh)

    def test_a_typed_price_is_honoured(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0],
                                price_lakh=650)
        self.session.commit()
        self.assertEqual(650, lot.sold_price_lakh)

    def test_the_purse_pays_and_the_ledger_agrees(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        before = gujarat.purse_remaining_lakh
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        self.session.refresh(gujarat)
        self.assertEqual(before - lot.sold_price_lakh,
                         gujarat.purse_remaining_lakh)
        self.assertEqual(1, gujarat.squad_size)
        self.assertEqual(1, gujarat.draft_picks_used)
        kinds = [r.kind for r in self.A.ledger(self.session, gujarat.id)]
        self.assertIn(self.A.LEDGER_DRAFT, kinds)
        self.assertNotIn(self.A.LEDGER_RETENTION, kinds)
        self.assert_ledger_agrees(two, "after a pick")

    def test_more_than_the_purse_is_refused(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, gujarat, self.players[0],
                              price_lakh=99_999)
        self.assertIn("more than the purse", str(caught.exception))
        self.assert_ledger_agrees(two, "after a refused pick")

    def test_the_reachability_rule_refuses_too(self):
        """The same rule that refuses a bid leaving a squad unfillable."""
        two = self.season_two(picks=3)
        two.min_squad_size = 6
        self.session.commit()
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, gujarat, self.players[0],
                              price_lakh=gujarat.purse_remaining_lakh)
        self.assertIn("unable to fill its squad", str(caught.exception))

    def test_the_overseas_cap_refuses(self):
        two = self.season_two(picks=3)
        two.max_overseas = 0
        self.session.commit()
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        # Rashid Khan is Afghan; the season's home country is India.
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, gujarat, self.players[2])
        self.assertIn("overseas limit", str(caught.exception))

    def test_the_squad_cap_refuses(self):
        two = self.season_two(picks=3)
        two.max_squad_size = 0
        self.session.commit()
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.assertIn("squad limit", str(caught.exception))


# ══════════════════════════════════════════════════════════════════════
# What a pick IS, to everything downstream
# ══════════════════════════════════════════════════════════════════════

class PickShapeTests(ExpansionCase):

    def test_it_is_a_drafted_lot_not_a_retained_one(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        self.assertEqual(self.A.ACQ_DRAFTED, lot.acquisition)
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual([], self.A.retained(self.session, gujarat.id))
        self.assertEqual([lot.id],
                         [x.id for x in
                          self.A.drafted(self.session, gujarat.id)])
        self.assertEqual(lot.sold_price_lakh,
                         self.A.pick_spent(self.session, gujarat.id))
        self.assertEqual(0, self.A.retention_spent(self.session, gujarat.id))

    def test_the_board_does_not_count_it_as_auction_progress(self):
        """Nobody bid and no clock ran. Counting it would have the board
        announce lots resolved before the first one ever opened."""
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        before = self.A.pool_counts(self.session, two.id)
        self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        after = self.A.pool_counts(self.session, two.id)
        self.assertEqual(before.get("total", 0) - 1, after.get("total", 0))
        self.assertEqual(0, after.get("sold", 0))
        self.assertEqual(1, after.get("drafted", 0))
        self.assertEqual(0, after.get("retained", 0),
                         "a pick is not a retention")

    def test_he_is_on_the_squad_and_reads_as_a_pick(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        self.assertIn(lot.id,
                      [x.id for x in self.A.squad(self.session, gujarat.id)])
        body = self.A.render_squad(self.session, two, gujarat)
        self.assertIn(lot.name, body)
        self.assertIn("🆕", body)

    def test_it_does_not_rewrite_who_held_him_last_season(self):
        """A retention records the holder because it IS one. A pick takes
        somebody nobody kept, so the real record must survive."""
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        target = [lot for lot in self.A.lots(self.session, two.id)
                  if lot.previous_franchise_id][0]
        was = target.previous_franchise_id
        self.assertNotEqual(was, gujarat.id)
        player = [p for p in self.players if p.id == target.player_id][0]

        # Walk the turn round to Gujarat if it is not already theirs.
        while self.A.pick_turn(self.session, two).id != gujarat.id:
            self.A.skip_pick(self.session, two,
                             self.A.pick_turn(self.session, two))
            self.session.commit()
        lot = self.A.draft_pick(self.session, two, gujarat, player)
        self.session.commit()
        self.assertEqual(was, lot.previous_franchise_id)

    def test_a_picked_player_survives_publishing(self):
        from models import ChallengePlayer, ChallengeTeam
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()

        # Finish the auction so it can publish.
        self.A.bind_chat(self.session, two, -7900 - self.tag)
        current = self.A.start(self.session, two, now=NOW)
        self.session.commit()
        guard = 0
        while current is not None and guard < 40:
            self.A.pass_lot(self.session, two, current)
            self.session.commit()
            current = self.A.open_next_lot(self.session, two, now=NOW)
            self.session.commit()
            guard += 1
        league = self.A.publish_to_league(self.session, two)
        self.session.commit()

        team = (self.session.query(ChallengeTeam)
                .filter(ChallengeTeam.league_id == league.id,
                        ChallengeTeam.name == gujarat.name).first())
        self.assertIsNotNone(team, "the expansion side has no published team")
        rows = (self.session.query(ChallengePlayer)
                .filter(ChallengePlayer.team_id == team.id).all())
        self.assertIn(lot.name, [r.name for r in rows])

        from services.cipl_match import cp_to_player_dict
        picked = [r for r in rows if r.name == lot.name][0]
        as_player = cp_to_player_dict(picked)
        self.assertEqual(lot.name, as_player["name"])
        self.assertTrue(as_player.get("rating"))


# ══════════════════════════════════════════════════════════════════════
# Undo
# ══════════════════════════════════════════════════════════════════════

class UndoPickTests(ExpansionCase):

    def test_undo_gives_back_the_money_and_the_pick(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        before = gujarat.purse_remaining_lakh
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        self.A.undo_pick(self.session, two, lot)
        self.session.commit()
        self.session.refresh(gujarat)
        self.assertEqual(before, gujarat.purse_remaining_lakh)
        self.assertEqual(0, gujarat.draft_picks_used)
        self.assertEqual(0, gujarat.squad_size)
        self.assertEqual(self.A.LOT_QUEUED, lot.status)
        self.assert_ledger_agrees(two, "after an undone pick")

    def test_and_he_can_be_picked_again(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        self.A.undo_pick(self.session, two, lot)
        self.session.commit()
        self.assertEqual("Gujarat", self.A.pick_turn(self.session, two).name)
        again = self.A.draft_pick(self.session, two, gujarat,
                                  self.players[0])
        self.session.commit()
        self.assertEqual(self.A.ACQ_DRAFTED, again.acquisition)

    def test_undo_sale_refuses_a_pick_and_says_what_to_use(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.undo_sale(self.session, two, lot)
        self.assertIn("expansion pick", str(caught.exception))
        self.assertIn("pick back", str(caught.exception))

    def test_unretain_refuses_a_pick_too(self):
        two = self.season_two(picks=3)
        gujarat = self.A.expansion_franchises(self.session, two)[0]
        lot = self.A.draft_pick(self.session, two, gujarat, self.players[0])
        self.session.commit()
        with self.assertRaises(self.A.AuctionError):
            self.A.unretain(self.session, two, gujarat, lot)

    def test_undo_refuses_an_ordinary_sale(self):
        two = self.season_two(picks=3)
        self.A.bind_chat(self.session, two, -7950 - self.tag)
        lot = self.A.start(self.session, two, now=NOW)
        mumbai = [f for f in self.A.franchises(self.session, two.id)
                  if f.name == "Mumbai"][0]
        self.A.place_bid(self.session, two, lot, mumbai,
                         lot.base_price_lakh, now=NOW)
        sold = self.A.sell_lot(self.session, two, lot, now=NOW)
        self.session.commit()
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.undo_pick(self.session, two, sold)
        self.assertIn("not an expansion pick", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


# ══════════════════════════════════════════════════════════════════════
# The commands
# ══════════════════════════════════════════════════════════════════════

class CommandTests(ExpansionCase):

    def setUp(self):
        super().setUp()
        self.replies = []
        self._prev_admins = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)

    def tearDown(self):
        if self._prev_admins is None:
            os.environ.pop("BOT_ADMIN_IDS", None)
        else:
            os.environ["BOT_ADMIN_IDS"] = self._prev_admins
        super().tearDown()

    def _run(self, handler, user_id, args=()):
        import asyncio
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            return SimpleNamespace(message_id=1)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.two.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        context = SimpleNamespace(args=list(args), bot=SimpleNamespace())
        self.session.commit()
        self.session.close()
        asyncio.run(handler(update, context))
        from database import get_session
        self.session = get_session()
        self.two = self.session.merge(self.two)
        return self.replies

    def _bind(self, picks=3):
        two = self.season_two(picks=picks)
        self.A.bind_chat(self.session, two, -8200 - self.tag)
        self.session.commit()
        return two

    def _side(self, name):
        return [f for f in self.A.franchises(self.session, self.two.id)
                if f.name == name][0]

    def test_apicks_shows_the_order_and_whose_turn(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apicks_handler, CAROL)
        body = self.replies[-1]
        self.assertIn("Gujarat", body)
        self.assertIn("Lucknow", body)
        self.assertIn("It is <b>Gujarat</b>'s pick", body)

    def test_a_new_side_reads_its_own_pick_order(self):
        """The board is the *new side's* business — their turn is the one
        coming up — so it answers anyone, not just the admin running it."""
        from handlers import auction as H
        self._bind()
        self._run(H.apicks_handler, ALICE)
        body = self.replies[-1]
        self.assertNotIn("Only auction admins", body)
        self.assertIn("Gujarat", body)
        self.assertIn("It is <b>Gujarat</b>'s pick", body)

    def test_apick_signs_a_player(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apick_handler, CAROL,
                  ("Gujarat", "|", self.unique_name))
        self.assertIn("draft", self.replies[-1])
        gujarat = self._side("Gujarat")
        taken = self.A.drafted(self.session, gujarat.id)
        self.assertEqual(1, len(taken))
        self.assertEqual(self.unique_name, taken[0].name)
        self.assertIn("Next: <b>Lucknow</b>", self.replies[-1])

    def test_apick_honours_a_typed_price(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apick_handler, CAROL,
                  ("Gujarat", "|", self.unique_name, "|", "6"))
        gujarat = self._side("Gujarat")
        self.assertEqual(600,
                         self.A.drafted(self.session,
                                        gujarat.id)[0].sold_price_lakh)

    def test_apick_out_of_turn_is_refused_and_says_whose_it_is(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apick_handler, CAROL,
                  ("Lucknow", "|", self.unique_name))
        # Escaped, because the reply is HTML: "Gujarat&#x27;s pick".
        self.assertIn("It is Gujarat", self.replies[-1])
        self.assertIn("pick, not Lucknow", self.replies[-1])
        self.assertEqual([], self.A.drafted(self.session,
                                            self._side("Lucknow").id))

    def test_a_bare_apick_says_whose_turn_it_is(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apick_handler, CAROL)
        self.assertIn("Usage", self.replies[-1])
        self.assertIn("Gujarat", self.replies[-1])

    def test_apickset_deals_the_picks(self):
        from handlers import auction as H
        self._bind(picks=0)
        self._run(H.apickset_handler, CAROL, ("2",))
        self.assertIn("Gujarat", self.replies[-1])
        self.assertEqual(2, self._side("Gujarat").draft_picks_total)
        self.assertEqual(0, self._side("Mumbai").draft_picks_total)

    def test_apickset_can_give_one_side_its_own_number(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apickset_handler, CAROL, ("Lucknow", "|", "1"))
        self.assertEqual(1, self._side("Lucknow").draft_picks_total)
        self.assertEqual(3, self._side("Gujarat").draft_picks_total)

    def test_apickskip_passes_the_turn(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apickskip_handler, CAROL)
        self.assertIn("Next: <b>Lucknow</b>", self.replies[-1])
        self.assertEqual(1, self._side("Gujarat").draft_picks_used)

    def test_apickundo_gives_it_back(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apick_handler, CAROL, ("Gujarat", "|", self.unique_name))
        self._run(H.apickundo_handler, CAROL, (self.unique_name,))
        self.assertIn("undone", self.replies[-1])
        gujarat = self._side("Gujarat")
        self.assertEqual(0, gujarat.draft_picks_used)
        self.assertEqual([], self.A.drafted(self.session, gujarat.id))

    def test_a_stranger_cannot_pick(self):
        from handlers import auction as H
        self._bind()
        self._run(H.apick_handler, ALICE, ("Gujarat", "|", self.unique_name))
        self.assertIn("admin", self.replies[-1].lower())
        self.assertEqual([], self.A.drafted(self.session,
                                            self._side("Gujarat").id))

    def test_apicks_says_so_when_nobody_is_new(self):
        """A first season, or a season nobody joined."""
        from handlers import auction as H
        self.A.add_players_to_pool(self.session, self.one, self.players)
        self.session.commit()
        self.two = self.one          # the chat the command will resolve to
        self._run(H.apicks_handler, CAROL)
        self.assertIn("nobody is new", self.replies[-1])
