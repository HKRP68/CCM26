"""One season following another: cloning, and who held whom across the join.

Season 3 used to mean re-typing every franchise, every purse and every rule,
and then remembering to point the new auction at last season's league by hand.
Forgetting that last step does not fail loudly — retention and Right To Match
simply find nobody — so it is the step worth doing automatically.

What is pinned here:

  • every rule is carried, and the list of rules cannot silently fall behind
    the model;
  • the franchises come with their owners, and are linked to last season's by
    something better than a name — renaming a side between seasons used to lose
    every Right To Match, in silence;
  • the purses start again and the ledger agrees with them from the first
    moment, which is the invariant the whole feature stands on;
  • what is deliberately NOT carried: the pool, the bound group, and every
    piece of state that belongs to a *run* rather than to the rules.
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
                 "services.auction_service", "services.auction_scheduler",
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


CATALOGUE = [
    ("Virat Kohli", 97, "Batsman", "India"),
    ("Jasprit Bumrah", 95, "Bowler", "India"),
    ("Rashid Khan", 93, "All Rounder", "Afghanistan"),
    ("Jos Buttler", 92, "Wicket Keeper", "England"),
]

ALICE, BOB, CAROL = 111, 222, 333
NOW = datetime(2026, 3, 1, 12, 0, 0)


class SeasonCase(unittest.TestCase):
    """A season whose every rule has been moved off its default.

    Deliberate: a clone test built on default values passes even when the clone
    copies nothing at all.
    """

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
        self.session.flush()

        self.season = A.create_season(
            self.session, f"Season {self.tag}",
            # Every one of these is off the default on purpose.
            bid_seconds=45, snipe_window_seconds=7, snipe_extend_seconds=12,
            max_extensions=3, opening_purse_lakh=7_500, currency_label="$",
            min_base_price_lakh=30, min_squad_size=1, max_squad_size=6,
            max_overseas=4, home_country="Australia",
            max_retentions=3, min_retentions=1,
            retention_max_spend_lakh=4_000,
            retention_min_rating=70, retention_max_rating=99,
            rtm_enabled=True, rtm_per_team=2, rtm_window_seconds=45,
            rtm_extra_lakh=200)
        A.bind_chat(self.session, self.season, -9000 - self.tag)
        self.mumbai = A.create_franchise(
            self.session, self.season, "Mumbai", owner_tg_id=ALICE,
            owner_name="Alice", city="Mumbai", short_name="MI", sort_order=1)
        self.chennai = A.create_franchise(
            self.session, self.season, "Chennai", owner_tg_id=BOB,
            owner_name="Bob", city="Chennai", short_name="CSK", sort_order=2)
        A.add_co_owner(self.session, self.chennai, CAROL)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def clone(self, name=None, **kw):
        fresh = self.A.clone_season(self.session, self.season,
                                    name or f"Next {self.tag}", **kw)
        self.session.commit()
        return fresh

    def run_and_publish(self):
        """Take the source season all the way to a published league.

        That league is the record of who held whom, and it is the only thing a
        next season can actually follow.
        """
        A = self.A
        # The fixture sets a retention minimum, to prove the rule carries.
        # ``start`` rightly refuses while nobody has met it, and retention is
        # not what this helper is about, so lift it here rather than retaining
        # two players in every test that wants a finished season.
        self.season.min_retentions = 0
        A.add_players_to_pool(self.session, self.season, self.players)
        self.session.commit()
        lot = A.start(self.session, self.season, now=NOW)
        self.session.commit()
        buyers = (self.mumbai, self.chennai)
        guard = 0
        while lot is not None and guard < 30:
            A.place_bid(self.session, self.season, lot, buyers[guard % 2],
                        lot.base_price_lakh, now=NOW)
            A.sell_lot(self.session, self.season, lot, now=NOW)
            self.session.commit()
            lot = A.open_next_lot(self.session, self.season, now=NOW)
            self.session.commit()
            guard += 1
        self.assertEqual(A.STATUS_COMPLETED, self.season.status)
        league = A.publish_to_league(self.session, self.season)
        self.session.commit()
        return league


# ══════════════════════════════════════════════════════════════════════
# The rules
# ══════════════════════════════════════════════════════════════════════

class RuleCarryTests(SeasonCase):

    def test_every_rule_is_carried(self):
        fresh = self.clone()
        for field in self.A.SEASON_RULE_FIELDS:
            self.assertEqual(getattr(self.season, field),
                             getattr(fresh, field),
                             f"{field} did not carry into the next season")

    def test_the_rule_list_has_not_fallen_behind_the_model(self):
        """The one way this feature rots: a rule column added later that
        nobody remembers to add to ``SEASON_RULE_FIELDS``. It would carry
        silently wrong — the new season quietly running on a default.
        """
        from models import AuctionSeason

        # Everything on the table that is neither identity, nor state from a
        # run, nor bookkeeping. What is left is, by definition, a rule.
        not_a_rule = {
            "id", "name", "status", "chat_id", "current_lot_id",
            "created_at", "updated_at",
            # State from a run
            "league_id", "published_at", "board_message_id",
            "announced_event_id", "board_rendered_bid_count",
            "retention_locked_at", "retention_deadline_at",
            # Which season/league this one follows — set by the clone itself,
            # never copied from the source.
            "previous_league_id", "previous_season_id",
        }
        columns = {c.name for c in AuctionSeason.__table__.columns}
        expected = columns - not_a_rule
        missing = sorted(expected - set(self.A.SEASON_RULE_FIELDS))
        self.assertEqual(
            [], missing,
            f"these look like rules but are not carried into a cloned season: "
            f"{missing}. Add them to SEASON_RULE_FIELDS, or to this test's "
            f"``not_a_rule`` set with a reason.")
        stale = sorted(set(self.A.SEASON_RULE_FIELDS) - columns)
        self.assertEqual([], stale,
                         f"SEASON_RULE_FIELDS names columns that do not "
                         f"exist: {stale}")

    def test_the_json_rule_blobs_carry_too(self):
        """They are the base-price ladder and the increment ladder — the two
        rules most likely to have been tuned by hand."""
        import json
        self.season.base_price_rules_json = json.dumps(
            [{"min_rating": 90, "base_lakh": 250}])
        self.session.commit()
        fresh = self.clone()
        self.assertEqual(self.season.base_price_rules_json,
                         fresh.base_price_rules_json)
        self.assertEqual(250, self.A.base_price_for(fresh, 93))

    def test_a_clone_starts_in_setup_however_the_source_ended(self):
        self.season.min_retentions = 0      # see run_and_publish
        self.A.add_players_to_pool(self.session, self.season, self.players)
        self.A.start(self.session, self.season, now=NOW)
        self.session.commit()
        self.assertEqual(self.A.STATUS_LIVE, self.season.status)
        fresh = self.clone()
        self.assertEqual(self.A.STATUS_SETUP, fresh.status)
        self.assertIsNone(fresh.current_lot_id)

    def test_none_of_the_run_comes_with_it(self):
        league = self.run_and_publish()
        self.assertIsNotNone(league)
        fresh = self.clone()
        self.assertIsNone(fresh.league_id)
        self.assertIsNone(fresh.published_at)
        self.assertIsNone(fresh.board_message_id)
        self.assertEqual(0, fresh.board_rendered_bid_count)
        self.assertIsNone(fresh.retention_locked_at)


# ══════════════════════════════════════════════════════════════════════
# The franchises
# ══════════════════════════════════════════════════════════════════════

class FranchiseCarryTests(SeasonCase):

    def test_the_field_carries_with_its_owners(self):
        fresh = self.clone()
        old = {f.name: f for f in self.A.franchises(self.session, self.season.id)}
        new = {f.name: f for f in self.A.franchises(self.session, fresh.id)}
        self.assertEqual(sorted(old), sorted(new))
        for name, f in new.items():
            for field in self.A.FRANCHISE_CARRY_FIELDS:
                self.assertEqual(getattr(old[name], field),
                                 getattr(f, field),
                                 f"{name}'s {field} did not carry")

    def test_co_owners_carry(self):
        """The tedious part of setting a season up, and the easiest to forget."""
        fresh = self.clone()
        chennai = [f for f in self.A.franchises(self.session, fresh.id)
                   if f.name == "Chennai"][0]
        self.assertIn(CAROL, self.A.co_owner_ids(chennai))
        self.assertTrue(self.A.may_bid_for(chennai, CAROL))

    def test_each_one_knows_which_franchise_it_continues(self):
        fresh = self.clone()
        old = {f.name: f for f in self.A.franchises(self.session, self.season.id)}
        for f in self.A.franchises(self.session, fresh.id):
            self.assertEqual(old[f.name].id, f.carried_from_id)

    def test_the_purses_start_again_and_the_ledger_agrees(self):
        """The invariant the whole feature stands on, from the first moment."""
        self.run_and_publish()
        self.session.refresh(self.mumbai)
        self.assertLess(self.mumbai.purse_remaining_lakh,
                        self.mumbai.purse_total_lakh,
                        "the source has to have spent something for this to "
                        "be testing anything")
        fresh = self.clone()
        for f in self.A.franchises(self.session, fresh.id):
            self.assertEqual(fresh.opening_purse_lakh, f.purse_total_lakh)
            self.assertEqual(fresh.opening_purse_lakh, f.purse_remaining_lakh)
            self.assertEqual(self.A.ledger_total(self.session, f.id),
                             f.purse_remaining_lakh,
                             f"{f.name}'s opening ledger row is missing")
        self.assertEqual([], self.A.reconcile_purses(self.session, fresh))

    def test_squads_cards_and_retentions_all_start_at_zero(self):
        self.run_and_publish()
        self.session.refresh(self.mumbai)
        self.assertGreater(self.mumbai.squad_size, 0)
        fresh = self.clone()
        for f in self.A.franchises(self.session, fresh.id):
            self.assertEqual(0, f.squad_size)
            self.assertEqual(0, f.retained_count)
            self.assertEqual(0, f.rtm_cards_used)
            self.assertEqual(fresh.rtm_per_team, f.rtm_cards_total,
                             "a new season deals the cards its own rules say")
            self.assertEqual(2, self.A.rtm_cards_left(f))

    def test_the_room_hears_one_line_not_one_per_franchise(self):
        fresh = self.clone()
        events = self.A.recent_events(self.session, fresh.id, limit=50)
        kinds = [e.kind for e in events]
        self.assertEqual(["season_cloned"], kinds,
                         "ten 'X joined' lines are ten messages for the "
                         "sweeper where one summary would do")
        self.assertIn(self.season.name, events[0].headline)


# ══════════════════════════════════════════════════════════════════════
# What is deliberately left behind
# ══════════════════════════════════════════════════════════════════════

class NotCarriedTests(SeasonCase):

    def test_the_pool_is_not_copied(self):
        """By now last season's players are on squads and the catalogue has
        moved on. The next season retains first and builds its pool after."""
        self.A.add_players_to_pool(self.session, self.season, self.players)
        self.session.commit()
        fresh = self.clone()
        self.assertEqual([], self.A.lots(self.session, fresh.id))
        self.assertEqual(0, self.A.pool_counts(self.session, fresh.id)
                         .get("total", 0))

    def test_the_group_is_not_taken_over(self):
        """The old season's pinned board is still in there."""
        chat = self.season.chat_id
        fresh = self.clone()
        self.assertIsNone(fresh.chat_id)
        self.session.refresh(self.season)
        self.assertEqual(chat, self.season.chat_id)
        self.assertEqual(self.season.id,
                         self.A.season_for_chat(self.session, chat).id)

    def test_but_a_free_group_can_be_bound_in_the_same_breath(self):
        free = -12345 - self.tag
        fresh = self.clone(chat_id=free)
        self.assertEqual(free, fresh.chat_id)

    def test_a_group_running_an_auction_is_still_refused(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.clone(chat_id=self.season.chat_id)
        self.assertIn("already running", str(caught.exception))
        self.assertIn("Finish or cancel", str(caught.exception))


class BindingHandoverTests(SeasonCase):
    """The group moves on to the next season — which it could not do before.

    ``season_for_chat`` resolves one chat to one auction, so a finished
    season's binding used to block its group forever. The refusal even read
    "cancel or finish it first" when finishing it was exactly what had
    happened.
    """

    def test_a_finished_season_hands_its_group_over(self):
        chat = self.season.chat_id
        self.run_and_publish()
        self.assertEqual(self.A.STATUS_COMPLETED, self.season.status)
        fresh = self.clone()
        self.A.bind_chat(self.session, fresh, chat)
        self.session.commit()
        self.assertEqual(chat, fresh.chat_id)
        self.session.refresh(self.season)
        self.assertIsNone(self.season.chat_id,
                          "two auctions on one chat make every command in "
                          "the group ambiguous")
        self.assertEqual(fresh.id,
                         self.A.season_for_chat(self.session, chat).id)

    def test_a_cancelled_one_does_too(self):
        chat = self.season.chat_id
        self.A.cancel(self.session, self.season)
        self.session.commit()
        fresh = self.clone()
        self.A.bind_chat(self.session, fresh, chat)
        self.session.commit()
        self.assertEqual(fresh.id,
                         self.A.season_for_chat(self.session, chat).id)

    def test_a_running_one_does_not(self):
        """Its pinned board is in there and the room is bidding into it."""
        self.season.min_retentions = 0
        self.A.add_players_to_pool(self.session, self.season, self.players)
        self.A.start(self.session, self.season, now=NOW)
        self.session.commit()
        fresh = self.clone()
        with self.assertRaises(self.A.AuctionError):
            self.A.bind_chat(self.session, fresh, self.season.chat_id)

    def test_nor_does_one_still_in_setup(self):
        fresh = self.clone()
        with self.assertRaises(self.A.AuctionError):
            self.A.bind_chat(self.session, fresh, self.season.chat_id)

    def test_the_handover_is_announced(self):
        chat = self.season.chat_id
        self.run_and_publish()
        fresh = self.clone()
        self.A.bind_chat(self.session, fresh, chat)
        self.session.commit()
        kinds = [e.kind for e in
                 self.A.recent_events(self.session, fresh.id, limit=20)]
        self.assertIn("chat_rebound", kinds)

    def test_rebinding_the_same_season_is_harmless(self):
        chat = self.season.chat_id
        self.A.bind_chat(self.session, self.season, chat)
        self.session.commit()
        self.assertEqual(chat, self.season.chat_id)


# ══════════════════════════════════════════════════════════════════════
# Following last season
# ══════════════════════════════════════════════════════════════════════

class FollowsTests(SeasonCase):

    def test_a_published_source_is_followed_automatically(self):
        """The most forgettable step in setting a season up, done for free."""
        league = self.run_and_publish()
        fresh = self.clone()
        self.assertEqual(league.id, fresh.previous_league_id)
        self.assertEqual(self.season.id, fresh.previous_season_id)

    def test_and_the_retention_picker_can_see_last_seasons_squads(self):
        self.run_and_publish()
        fresh = self.clone()
        held = self.A.previous_squad_map(self.session, fresh)
        self.assertTrue(held, "nobody was recorded as holding anybody")
        names = {f.name for f in held.values()}
        self.assertEqual({"Mumbai", "Chennai"}, names)

    def test_an_unpublished_source_leaves_nothing_to_follow_and_says_so(self):
        fresh = self.clone()
        self.assertIsNone(fresh.previous_league_id)
        self.assertEqual(self.season.id, fresh.previous_season_id)
        headline = self.A.recent_events(self.session, fresh.id, limit=5)[0].headline
        self.assertIn("never published", headline)

    def test_the_chain_reads_back(self):
        second = self.clone("Second")
        third = self.A.clone_season(self.session, second, "Third")
        self.session.commit()
        chain = self.A.season_chain(self.session, third)
        self.assertEqual([second.id, self.season.id], [s.id for s in chain])
        self.assertEqual([], self.A.season_chain(self.session, self.season))

    def test_a_broken_chain_does_not_hang_the_page(self):
        second = self.clone("Second")
        second.previous_season_id = second.id      # a hand-edited cycle
        self.session.commit()
        self.assertEqual([second.id],
                         [s.id for s in
                          self.A.season_chain(self.session, second)])


# ══════════════════════════════════════════════════════════════════════
# The rename, which used to lose everybody
# ══════════════════════════════════════════════════════════════════════

class RenameTests(SeasonCase):
    """Last season's team name matched this season's franchise name, and that
    was "the only link there is". Carrying the franchises gives a real one."""

    def test_a_renamed_franchise_still_holds_its_players(self):
        self.run_and_publish()
        fresh = self.clone()
        carried = {f.name: f for f in self.A.franchises(self.session, fresh.id)}
        before = self.A.previous_squad_map(self.session, fresh)
        self.assertTrue(before)

        # The rebrand that used to silently empty the Right To Match table.
        carried["Mumbai"].name = "Mumbai Champions"
        self.session.commit()

        after = self.A.previous_squad_map(self.session, fresh)
        self.assertEqual(set(before), set(after),
                         "a rename lost players who were still held")
        renamed = [f for f in after.values() if f.name == "Mumbai Champions"]
        self.assertTrue(renamed, "the renamed franchise holds nobody")
        self.assertEqual(
            {p: f.id for p, f in before.items()},
            {p: f.id for p, f in after.items()},
            "a rename moved players to a different franchise")

    def test_and_rtm_works_after_one(self):
        """The end the rename actually matters for."""
        self.run_and_publish()
        fresh = self.clone()
        carried = {f.name: f for f in self.A.franchises(self.session, fresh.id)}
        carried["Mumbai"].name = "Mumbai Champions"
        self.session.commit()

        self.A.add_players_to_pool(self.session, fresh, self.players)
        self.session.commit()
        self.A.link_previous_season(self.session, fresh, fresh.previous_league_id)
        self.session.commit()

        held = [lot for lot in self.A.lots(self.session, fresh.id)
                if lot.previous_franchise_id]
        self.assertTrue(held, "no lot knows who held it last season")
        owners = {lot.previous_franchise_id for lot in held}
        # ``carried`` was keyed before the rename, so this row IS the renamed
        # franchise — the point being that its identity survived the rename.
        self.assertIn(carried["Mumbai"].id, owners)
        self.assertIn(carried["Chennai"].id, owners)

    def test_a_hand_linked_season_still_matches_by_name(self):
        """The fallback has to keep working: a season linked to a league by
        hand has no carry links at all."""
        league = self.run_and_publish()
        from services import auction_service as A
        fresh = A.create_season(self.session, f"By hand {self.tag}",
                                opening_purse_lakh=7_500)
        A.create_franchise(self.session, fresh, "Mumbai", owner_tg_id=ALICE)
        A.create_franchise(self.session, fresh, "Chennai", owner_tg_id=BOB)
        self.session.commit()
        A.link_previous_season(self.session, fresh, league.id)
        self.session.commit()
        held = A.previous_squad_map(self.session, fresh)
        self.assertTrue(held)
        self.assertEqual({"Mumbai", "Chennai"},
                         {f.name for f in held.values()})


# ══════════════════════════════════════════════════════════════════════
# /aclone
# ══════════════════════════════════════════════════════════════════════

class CommandTests(SeasonCase):

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
            effective_chat=SimpleNamespace(id=self.season.chat_id,
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
        self.season = self.session.merge(self.season)
        return self.replies

    def _cloned(self):
        from models import AuctionSeason
        return (self.session.query(AuctionSeason)
                .filter(AuctionSeason.previous_season_id == self.season.id)
                .first())

    def test_aclone_starts_the_next_season(self):
        from handlers import auction as H
        self._run(H.aclone_handler, CAROL, ("Season", "99"))
        fresh = self._cloned()
        self.assertIsNotNone(fresh)
        self.assertEqual("Season 99", fresh.name)
        self.assertEqual(2, len(self.A.franchises(self.session, fresh.id)))
        self.assertIn("Season 99", self.replies[-1])

    def test_it_says_to_bind_the_group_later(self):
        """The likeliest support question, answered before it is asked."""
        from handlers import auction as H
        self._run(H.aclone_handler, CAROL, ("Season", "99"))
        self.assertIn("/abind", self.replies[-1])

    def test_it_warns_when_there_is_no_published_season_to_follow(self):
        from handlers import auction as H
        self._run(H.aclone_handler, CAROL, ("Season", "99"))
        self.assertIn("never published", self.replies[-1])

    def test_it_confirms_when_there_is(self):
        from handlers import auction as H
        self.run_and_publish()
        self._run(H.aclone_handler, CAROL, ("Season", "99"))
        self.assertIn("Right To Match", self.replies[-1])
        self.assertIsNotNone(self._cloned().previous_league_id)

    def test_a_name_is_required(self):
        from handlers import auction as H
        self._run(H.aclone_handler, CAROL)
        self.assertIn("Usage", self.replies[-1])
        self.assertIsNone(self._cloned())

    def test_a_stranger_cannot_start_next_season(self):
        from handlers import auction as H
        self._run(H.aclone_handler, ALICE, ("Season", "99"))
        self.assertIn("admin", self.replies[-1].lower())
        self.assertIsNone(self._cloned())


if __name__ == "__main__":
    unittest.main()
