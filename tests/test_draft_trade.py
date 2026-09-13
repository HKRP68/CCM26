"""``/dtrade`` — franchises swapping players once the draft is over.

The point of the command is what it *removes*: ``/trade``'s same-OVR rule. So
the first thing pinned here is that a 97 goes for a 74 and nobody is stopped —
and then, at much greater length, everything that has to hold instead, because
"any player for any player" is only a good rule if the squad it produces is
still a squad the draft could have built.

The rules worth pinning, and why:

  • **No rating rule, and it is visible.** The offer card says which side the
    deal favours and by how much. A lopsided trade is legal; a silent one is
    not, because the only brake left is the group watching.
  • **Counts match.** A squad's size is its slot count in the order sheet.
    Two-for-one is not a trade, it is a franchise playing a man short.
  • **The tier ceiling survives.** A slot takes its own tier or below, which is
    the rule that caps a team's Platinum count. A trade must not be the way
    around it — and equally, a Platinum slot spent on a Gold player must still
    be able to take Gold back, or the check is punishing teams for picking low.
  • **Never worse than it already was.** A squad short of a keeper because the
    clock passed a slot may still trade. It just may not get shorter.
  • **The offer is a row, not a dict in memory.** ``/trade`` loses its open
    trades to a redeploy; this one must not.
  • **A published league follows the trade**, or the squads people actually
    play with silently disagree with ``/dsquad``.
  • **Pick rows are never rewritten.** "R1 P1 was Mumbai's pick" stays true
    after Mumbai trades the player, which is also what keeps ``tier_slots``
    describing the order sheet — and what ``/dundo`` has to be taught about.
"""

import itertools
import os
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
# Everything that caches a reference to ``database`` / ``models`` and is
# reached from here. The handler modules are in the list because
# ``tests/test_player_draft.py`` imports ``handlers.draft`` against its OWN
# temporary database: left cached, its ``get_session`` and its ``DraftError``
# are a different object from the freshly imported ones, so ``_with_draft``
# would query the wrong file and fail to catch the refusals this module raises.
_MODULE_NAMES = ("database", "models", "config", "services.draft_service",
                 "services.draft_trade_service", "services.draft_scheduler",
                 "services.xlsx_reader", "handlers.draft",
                 "handlers.draft_trade")

_PID = itertools.count(1)


def _unload(names):
    """Drop these modules so the next import rebuilds them.

    Popping ``sys.modules`` is not enough on its own: ``from handlers import
    draft`` returns a **cached attribute on the package** when one exists,
    without consulting ``sys.modules`` at all, so the stale module — and, fatally,
    the ``get_session`` it bound at import time to a temporary database that no
    longer exists — would come straight back. The attribute has to go too.
    """
    for name in names:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            try:
                delattr(package, child)
            except AttributeError:
                pass


def _restore(saved):
    """Put back whatever ``_unload`` took away, package attributes included."""
    for name, module in saved.items():
        parent, _, child = name.rpartition(".")
        if module is None:
            _unload([name])
            continue
        sys.modules[name] = module
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            setattr(package, child, module)

def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    _unload(_MODULE_NAMES)

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
    _restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


POOL_HEADER = ["name", "rating", "tier", "icon_eligible", "gender",
               "indian_status", "category", "country", "bat_hand", "bowl_hand",
               "bowl_style", "bat_rating", "bowl_rating"]

# Nine players across three tiers, with ratings deliberately far apart inside a
# tier and across them, so an unequal swap is obviously unequal. Exactly two
# Platinum for two teams, and one spare of each shape left undrafted.
POOL_ROWS = [
    ["Virat Kohli", "97", "Platinum", "1", "Male", "Indian", "Batsman",
     "India", "R", "R", "Medium Pacer", "96", "30"],
    ["Jasprit Bumrah", "96", "Platinum", "1", "Male", "Indian", "Bowler",
     "India", "R", "R", "Fast", "20", "95"],
    ["Rashid Khan", "93", "Gold", "0", "Male", "Overseas", "Bowler",
     "Afghanistan", "R", "R", "Leg Spin", "40", "94"],
    ["Jos Buttler", "92", "Gold", "0", "Male", "Overseas", "wk", "England",
     "R", "R", "-", "91", "10"],
    ["Sanju Samson", "88", "Gold", "0", "Male", "Indian", "Wicket Keeper",
     "India", "R", "R", "-", "89", "5"],
    ["Yashasvi Jaiswal", "86", "Silver", "0", "Male", "Indian", "Batsman",
     "India", "L", "R", "Medium", "87", "10"],
    ["Tim David", "84", "Silver", "0", "Male", "Overseas", "Batsman",
     "Australia", "R", "R", "Medium", "85", "20"],
    ["Rinku Singh", "83", "Silver", "0", "Male", "Indian", "batter", "India",
     "L", "L", "Medium", "84", "15"],
    ["Mukesh Kumar", "74", "Silver", "0", "Male", "Indian", "bowl", "India",
     "R", "R", "Medium", "20", "75"],
]

# Two Platinum slots a team, then a Gold and a Silver. The second Platinum slot
# is the point: both teams spend one below its ceiling, which is legal and
# common, and which leaves each of them room to take a Platinum player back in
# a trade. A sheet where every slot is spent at its own tier can only ever swap
# like for like, which would test the rule this command exists to remove.
ORDER_HEADER = ["Round No", "Pick Number", "Tier", "Team Name", "Owner Name",
                "Owner Tag ID"]
ORDER_ROWS = [
    ["1", "1", "Platinum", "Mumbai Mavericks", "Alice", "111"],
    ["1", "2", "Platinum", "Chennai Kings", "Bob", "222"],
    ["2", "1", "Platinum", "Mumbai Mavericks", "Alice", "111"],
    ["2", "2", "Platinum", "Chennai Kings", "Bob", "222"],
    ["3", "1", "Gold", "Mumbai Mavericks", "Alice", "111"],
    ["3", "2", "Gold", "Chennai Kings", "Bob", "222"],
    ["4", "1", "Silver", "Mumbai Mavericks", "Alice", "111"],
    ["4", "2", "Silver", "Chennai Kings", "Bob", "222"],
]

# The squads the default ``finish()`` produces, in pick order:
#   Mumbai   Kohli (P97 bat) · Jaiswal (S86 bat) · Rashid (G93 bowl, overseas)
#            · Rinku (S83 bat)
#   Chennai  Bumrah (P96 bowl) · Tim David (S84 bat, overseas)
#            · Buttler (G92 keeper, overseas) · Mukesh (S74 bowl)
#   undrafted: Sanju Samson (G88 keeper)
DEFAULT_PICKS = ["Virat Kohli", "Jasprit Bumrah", "Yashasvi Jaiswal",
                 "Tim David", "Rashid Khan", "Jos Buttler", "Rinku Singh",
                 "Mukesh Kumar"]

ALICE, BOB, CAROL = 111, 222, 333


class TradeCase(unittest.TestCase):
    """A finished two-team draft, ready to trade."""

    max_overseas = 11
    role_minimums = None

    def setUp(self):
        from database import get_session
        from services import draft_service as ds
        from services import draft_trade_service as dts

        self.session = get_session()
        self.ds = ds
        self.dts = dts
        self.draft = ds.create_draft(
            self.session, f"Trade Draft {next(_PID)}", pick_seconds=900,
            max_overseas=self.max_overseas, role_minimums=self.role_minimums)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def load(self, pool_rows=None, order_rows=None):
        self.ds.import_pool(
            self.session, self.draft,
            [list(POOL_HEADER)] + [list(r) for r in
                                   (pool_rows if pool_rows is not None
                                    else POOL_ROWS)])
        self.ds.import_order(
            self.session, self.draft,
            [list(ORDER_HEADER)] + [list(r) for r in
                                    (order_rows if order_rows is not None
                                     else ORDER_ROWS)])
        self.draft.chat_id = -100_000 - self.draft.id
        self.ds.start(self.session, self.draft)
        self.session.commit()

    def player(self, name):
        from models import DraftPlayer
        return (self.session.query(DraftPlayer)
                .filter(DraftPlayer.draft_id == self.draft.id,
                        DraftPlayer.name == name).one())

    def team(self, name):
        return self.ds.find_team(self.session, self.draft.id, name)

    def take(self, name, by=ALICE):
        current = self.ds.current_pick(self.session, self.draft)
        return self.ds.make_pick(self.session, self.draft, current,
                                 self.player(name), by_tg_id=by)

    def finish(self, picks=None):
        """Run the whole order out so the draft completes.

        ``picks`` is in slot order — Mumbai, Chennai, Mumbai, … — and defaults
        to ``DEFAULT_PICKS``, the squad shape every rule test below leans on.
        """
        picks = picks or list(DEFAULT_PICKS)
        for index, name in enumerate(picks):
            self.take(name, by=ALICE if index % 2 == 0 else BOB)
        self.session.commit()
        self.assertEqual(self.draft.status, self.ds.STATUS_COMPLETED)

    def squad_names(self, team_name):
        return sorted(p.name for p in
                      self.dts.tradable_squad(self.session,
                                              self.team(team_name).id))

    def offer(self, a_names, b_names, a_team="Mumbai Mavericks",
              b_team="Chennai Kings", by=ALICE):
        """Build an offer all the way to the confirmation step."""
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team(a_team), self.team(b_team),
                                    by_tg_id=by)
        for name in a_names:
            self.dts.toggle(self.session, trade, "a", self.player(name).id)
        self.dts.finish_side(self.session, trade, "a")
        for name in b_names:
            self.dts.toggle(self.session, trade, "b", self.player(name).id)
        self.dts.finish_side(self.session, trade, "b")
        return trade

    def swap(self, a_names, b_names, **kwargs):
        trade = self.offer(a_names, b_names, **kwargs)
        result = self.dts.execute(self.session, trade, by_tg_id=BOB)
        self.session.commit()
        return trade, result


# ══════════════════════════════════════════════════════════════════════
# The window
# ══════════════════════════════════════════════════════════════════════

class WindowTests(TradeCase):

    def test_a_live_draft_cannot_trade(self):
        """Trading mid-draft is a different feature — and it would break the
        reachability checks that make role minimums enforceable."""
        self.load()
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.require_open(self.draft)
        self.assertIn("Trading opens when the draft finishes",
                      str(caught.exception))

    def test_a_finished_draft_can(self):
        self.load()
        self.finish()
        self.assertTrue(self.dts.require_open(self.draft) is None)

    def test_a_cancelled_draft_cannot(self):
        self.load()
        self.ds.set_status(self.session, self.draft, self.ds.STATUS_CANCELLED)
        with self.assertRaises(self.ds.DraftError):
            self.dts.require_open(self.draft)

    def test_an_admin_can_close_the_window(self):
        self.load()
        self.finish()
        self.dts.set_window(self.session, self.draft, False)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.require_open(self.draft)
        self.assertIn("trade window is closed", str(caught.exception))

    def test_closing_the_window_kills_offers_in_flight(self):
        """Otherwise a half-built offer wakes up hours later against squads
        that have moved on."""
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.dts.set_window(self.session, self.draft, False)
        self.assertEqual(trade.status, self.dts.STATUS_CANCELLED)

    def test_a_legacy_null_reads_as_open(self):
        """The column arrived after the table, so an old row has NULL in it —
        which must not read as "this draft may never trade"."""
        self.load()
        self.finish()
        self.draft.trades_open = None
        self.assertTrue(self.dts.trades_open(self.draft))
        self.assertIsNone(self.dts.require_open(self.draft))


# ══════════════════════════════════════════════════════════════════════
# The rule that is gone
# ══════════════════════════════════════════════════════════════════════

class NoRatingRuleTests(TradeCase):

    def test_a_97_goes_for_a_74(self):
        """The whole command in one test: /trade would refuse this outright."""
        self.load()
        self.finish()
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        self.assertIn("Mukesh Kumar", self.squad_names("Mumbai"))
        self.assertIn("Virat Kohli", self.squad_names("Chennai"))

    def test_tiers_need_not_match_either(self):
        """Platinum out, Silver back. Both teams have a Platinum slot free once
        Kohli leaves, and a Silver player fits a Platinum slot."""
        self.load()
        self.finish()
        _trade, (out, back) = self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        self.assertEqual(([p.tier for p in out], [p.tier for p in back]),
                         (["Platinum"], ["Silver"]))

    def test_the_offer_card_says_who_gains_and_by_how_much(self):
        """No rule means the only brake is the room reading the numbers."""
        self.load()
        self.finish()
        trade = self.offer(["Virat Kohli"], ["Mukesh Kumar"])
        card = self.dts.render_offer(self.session, self.draft, trade)
        self.assertIn("97 OVR ⇄ 74 OVR", card)
        self.assertIn("+23 OVR", card)
        self.assertIn("heavily", card)
        # Mumbai sent the better player, so the card names Chennai as the
        # side that gained — in front of the whole group, before either tap.
        self.assertIn("<b>Chennai Kings</b> gains", card)

    def test_a_near_even_trade_is_called_near_even(self):
        self.load()
        self.finish()
        trade = self.offer(["Virat Kohli"], ["Jasprit Bumrah"])
        # 97 vs 96 — a shade, not a robbery.
        self.assertIn("a shade", self.dts.render_offer(self.session,
                                                       self.draft, trade))


# ══════════════════════════════════════════════════════════════════════
# The rules that replace it
# ══════════════════════════════════════════════════════════════════════

class CountTests(TradeCase):

    def test_two_for_one_is_refused(self):
        """A squad's size is its slot count; a short squad cannot field an XI."""
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        for name in ("Virat Kohli", "Rashid Khan"):
            self.dts.toggle(self.session, trade, "a", self.player(name).id)
        self.dts.finish_side(self.session, trade, "a")
        self.dts.toggle(self.session, trade, "b",
                        self.player("Jasprit Bumrah").id)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.finish_side(self.session, trade, "b")
        self.assertIn("player-for-player", str(caught.exception))

    def test_two_for_two_is_an_ordinary_trade(self):
        """Package deals are the point — /trade can only ever do one for one."""
        self.load()
        self.finish()
        self.swap(["Virat Kohli", "Rinku Singh"],
                  ["Jasprit Bumrah", "Mukesh Kumar"])
        self.assertEqual(self.squad_names("Mumbai"),
                         ["Jasprit Bumrah", "Mukesh Kumar", "Rashid Khan",
                          "Yashasvi Jaiswal"])
        self.assertEqual(self.squad_names("Chennai"),
                         ["Jos Buttler", "Rinku Singh", "Tim David",
                          "Virat Kohli"])

    def test_an_empty_side_is_refused(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        with self.assertRaises(self.ds.DraftError):
            self.dts.finish_side(self.session, trade, "a")

    def test_ticking_twice_un_ticks(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        kohli = self.player("Virat Kohli").id
        self.assertEqual(self.dts.toggle(self.session, trade, "a", kohli),
                         [kohli])
        self.assertEqual(self.dts.toggle(self.session, trade, "a", kohli), [])

    def test_a_player_from_the_wrong_squad_cannot_be_ticked(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.toggle(self.session, trade, "a",
                            self.player("Jasprit Bumrah").id)
        self.assertIn("not on that squad", str(caught.exception))


class TierQuotaTests(TradeCase):
    """The ceiling rule is what caps a team's tier counts. A trade must not be
    the back door around it — and must not punish a team for picking low."""

    def test_a_squad_cannot_outgrow_the_slots_that_bought_it(self):
        """Mumbai has 2 Platinum + 1 Gold slot, so it can hold three
        Gold-or-better players and no more. Taking Bumrah and Buttler for two
        Silver players would make four."""
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        for name in ("Rinku Singh", "Yashasvi Jaiswal"):
            self.dts.toggle(self.session, trade, "a", self.player(name).id)
        self.dts.finish_side(self.session, trade, "a")
        for name in ("Jasprit Bumrah", "Jos Buttler"):
            self.dts.toggle(self.session, trade, "b", self.player(name).id)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.finish_side(self.session, trade, "b")
        self.assertIn("Gold-or-better", str(caught.exception))

    def test_platinum_for_platinum_is_fine(self):
        self.load()
        self.finish()
        self.swap(["Virat Kohli"], ["Jasprit Bumrah"])
        self.assertIn("Jasprit Bumrah", self.squad_names("Mumbai"))

    def test_a_high_slot_spent_low_can_still_take_its_own_tier_back(self):
        """Mumbai's second Platinum slot went on Jaiswal, a Silver player. It
        is still a Platinum slot, so trading back up into it is legal —
        otherwise the check punishes exactly the team that picked below its
        ceiling, which the draft explicitly allows."""
        self.load()
        self.finish()
        self.swap(["Yashasvi Jaiswal"], ["Jasprit Bumrah"])
        self.assertIn("Jasprit Bumrah", self.squad_names("Mumbai"))

    def test_swapping_both_top_players_keeps_the_shape(self):
        """Two-for-two across tiers: the counts per tier come out unchanged."""
        self.load()
        self.finish()
        self.swap(["Virat Kohli", "Rashid Khan"],
                  ["Jasprit Bumrah", "Jos Buttler"])
        self.assertEqual(self.squad_names("Mumbai"),
                         ["Jasprit Bumrah", "Jos Buttler", "Rinku Singh",
                          "Yashasvi Jaiswal"])


class OverseasCapTests(TradeCase):
    max_overseas = 1

    # One overseas player a side, which is all the cap allows:
    #   Mumbai   Kohli · Rashid (overseas) · Samson · Rinku
    #   Chennai  Bumrah · Buttler (overseas) · Jaiswal · Mukesh
    PICKS = ["Virat Kohli", "Jasprit Bumrah", "Rashid Khan", "Jos Buttler",
             "Sanju Samson", "Yashasvi Jaiswal", "Rinku Singh", "Mukesh Kumar"]

    def test_the_cap_refuses_the_trade_that_would_break_it(self):
        self.load()
        self.finish(self.PICKS)
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.dts.toggle(self.session, trade, "a", self.player("Rinku Singh").id)
        self.dts.finish_side(self.session, trade, "a")
        self.dts.toggle(self.session, trade, "b",
                        self.player("Jos Buttler").id)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.finish_side(self.session, trade, "b")
        self.assertIn("overseas", str(caught.exception))

    def test_overseas_for_overseas_is_always_fine(self):
        self.load()
        self.finish(self.PICKS)
        self.swap(["Rashid Khan"], ["Jos Buttler"])
        self.assertIn("Jos Buttler", self.squad_names("Mumbai"))


class RoleMinimumTests(TradeCase):
    """The minimum is set *after* the draft here, on purpose.

    During a draft ``validate_pick`` makes a minimum unreachable-proof, so a
    squad can only end up short of one by a route the trade window then has to
    cope with — a slot the clock passed, or an admin who set the rule late.
    Setting it late is the reproducible version of that, and it is also what
    produces the case that matters most: a squad that is *already* short."""

    def short_of_a_keeper(self, picks=None):
        self.load()
        self.finish(picks)
        self.ds.set_role_minimums(self.draft, {"Wicket Keeper": 1})
        self.session.commit()

    def test_a_trade_may_not_break_a_minimum(self):
        self.short_of_a_keeper()
        # Chennai holds the only drafted keeper (Buttler) and must keep one.
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Chennai"), self.team("Mumbai"))
        self.dts.toggle(self.session, trade, "a",
                        self.player("Jos Buttler").id)
        self.dts.finish_side(self.session, trade, "a")
        self.dts.toggle(self.session, trade, "b",
                        self.player("Rashid Khan").id)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.finish_side(self.session, trade, "b")
        self.assertIn("Wicket Keeper", str(caught.exception))

    def test_a_squad_that_is_already_short_may_still_trade(self):
        """Mumbai has no keeper at all. Freezing it out of the trade window for
        that would lock out exactly the squad that most needs to fix itself."""
        self.short_of_a_keeper()
        self.assertIn("Wicket Keeper",
                      self.ds.role_gap(self.session, self.draft,
                                       self.team("Mumbai").id))
        self.swap(["Virat Kohli"], ["Jasprit Bumrah"])
        self.assertIn("Jasprit Bumrah", self.squad_names("Mumbai"))

    def test_the_trade_that_fixes_a_shortfall_is_allowed(self):
        """Chennai drafted both keepers, so it can spare one."""
        self.short_of_a_keeper(
            ["Virat Kohli", "Jasprit Bumrah", "Yashasvi Jaiswal",
             "Sanju Samson", "Rashid Khan", "Jos Buttler", "Rinku Singh",
             "Mukesh Kumar"])
        self.swap(["Rashid Khan"], ["Jos Buttler"])
        self.assertEqual(
            self.ds.role_gap(self.session, self.draft,
                             self.team("Mumbai").id), {})


# ══════════════════════════════════════════════════════════════════════
# tier_overflow, on its own
# ══════════════════════════════════════════════════════════════════════

class TierOverflowTests(TradeCase):
    """The fit check is Hall's condition on a nested ladder. It is worth
    testing directly: it is the one rule here that is not obvious by reading."""

    def fake(self, *tiers):
        from types import SimpleNamespace
        return [SimpleNamespace(tier=t) for t in tiers]

    def test_an_exact_fit_is_zero(self):
        self.assertEqual(
            self.dts.tier_overflow(self.draft, {"Platinum": 1, "Gold": 1},
                                   self.fake("Platinum", "Gold")), 0)

    def test_picking_below_the_ceiling_still_fits(self):
        self.assertEqual(
            self.dts.tier_overflow(self.draft, {"Platinum": 2},
                                   self.fake("Gold", "Bronze")), 0)

    def test_a_player_above_every_slot_overflows(self):
        self.assertEqual(
            self.dts.tier_overflow(self.draft, {"Gold": 2},
                                   self.fake("Platinum", "Gold")), 1)

    def test_it_names_the_tier_that_overflows(self):
        overflow, where = self.dts.tier_overflow_at(
            self.draft, {"Platinum": 1, "Gold": 1},
            self.fake("Platinum", "Platinum"))
        self.assertEqual((overflow, where), (1, "Platinum"))

    def test_the_prefix_is_what_matters_not_each_tier_alone(self):
        """One Gold slot and two Gold players is legal if a Platinum slot is
        spare — the Platinum slot takes one of them."""
        self.assertEqual(
            self.dts.tier_overflow(self.draft, {"Platinum": 1, "Gold": 1},
                                   self.fake("Gold", "Gold")), 0)

    def test_an_off_ladder_tier_is_counted_not_ignored(self):
        """A hand-edited row must not be able to slip past the check."""
        self.assertEqual(
            self.dts.tier_overflow(self.draft, {"Platinum": 1},
                                   self.fake("Platinum", "Mystery")), 1)


# ══════════════════════════════════════════════════════════════════════
# Permissions and offer lifecycle
# ══════════════════════════════════════════════════════════════════════

class PermissionTests(TradeCase):

    def test_an_owner_drives_their_own_side(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.assertEqual(self.dts.side_for(trade, ALICE), "a")
        self.assertEqual(self.dts.side_for(trade, BOB), "b")

    def test_a_stranger_drives_neither(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.assertIsNone(self.dts.side_for(trade, CAROL))

    def test_a_co_owner_may_trade(self):
        """A franchise run by two people does not stop being run by two people
        when the draft ends."""
        self.load()
        self.finish()
        self.ds.add_co_owner(self.session, self.team("Mumbai"), CAROL)
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.assertEqual(self.dts.side_for(trade, CAROL), "a")

    def test_a_team_cannot_trade_with_itself(self):
        self.load()
        self.finish()
        with self.assertRaises(self.ds.DraftError):
            self.dts.open_trade(self.session, self.draft,
                                self.team("Mumbai"), self.team("Mumbai"))

    def test_one_open_offer_per_team(self):
        self.load()
        self.finish()
        self.dts.open_trade(self.session, self.draft,
                            self.team("Mumbai"), self.team("Chennai"))
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.open_trade(self.session, self.draft,
                                self.team("Chennai"), self.team("Mumbai"))
        self.assertIn("already has an open trade", str(caught.exception))

    def test_a_cancelled_offer_frees_both_teams(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.dts.cancel(self.session, trade, by_tg_id=ALICE)
        self.assertIsNotNone(
            self.dts.open_trade(self.session, self.draft,
                                self.team("Chennai"), self.team("Mumbai")))


class LifecycleTests(TradeCase):

    def test_an_offer_survives_a_restart(self):
        """The reason it is a row and not a dict: this host redeploys often and
        a trade window is open for days. /trade loses every open trade."""
        from database import get_session
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        self.dts.toggle(self.session, trade, "a",
                        self.player("Virat Kohli").id)
        self.dts.finish_side(self.session, trade, "a")
        trade_id = trade.id
        self.session.commit()

        # A whole new session, as a restarted process would have.
        fresh = get_session()
        try:
            reloaded = self.dts.get_trade(fresh, trade_id)
            self.assertEqual(reloaded.status, self.dts.STATUS_BUILDING_B)
            self.assertEqual(
                [p.name for p in self.dts.players_for(fresh, reloaded, "a")],
                ["Virat Kohli"])
        finally:
            fresh.close()

    def test_an_expired_offer_is_swept(self):
        from datetime import datetime, timedelta
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        trade.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.session.flush()
        self.assertTrue(self.dts.is_expired(trade))
        self.assertEqual(self.dts.expire_stale(self.session, self.draft), 1)
        self.assertEqual(trade.status, self.dts.STATUS_EXPIRED)

    def test_working_on_an_offer_pushes_the_deadline_out(self):
        self.load()
        self.finish()
        trade = self.dts.open_trade(self.session, self.draft,
                                    self.team("Mumbai"), self.team("Chennai"))
        trade.expires_at = None
        self.dts.toggle(self.session, trade, "a",
                        self.player("Virat Kohli").id)
        self.assertIsNotNone(trade.expires_at)

    def test_a_completed_trade_is_on_the_record(self):
        self.load()
        self.finish()
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        log = self.dts.render_log(self.session, self.draft)
        self.assertIn("Mumbai Mavericks", log)
        self.assertIn("Virat Kohli", log)
        self.assertEqual(self.dts.trade_count(self.session, self.draft), 1)

    def test_the_log_says_so_when_there_is_nothing_in_it(self):
        self.load()
        self.finish()
        self.assertIn("none yet",
                      self.dts.render_log(self.session, self.draft))

    def test_ownership_is_re_read_when_the_players_actually_move(self):
        """An offer sits on screen while the world moves. Ticking checks the
        squad, but the tap that swaps them has to check again — otherwise a
        stale card hands over a player who has already gone."""
        self.load()
        self.finish()
        trade = self.offer(["Virat Kohli"], ["Mukesh Kumar"])
        # Kohli leaves Mumbai behind the offer's back.
        self.player("Virat Kohli").picked_by_team_id = self.team("Chennai").id
        self.session.flush()
        with self.assertRaises(self.ds.DraftError) as caught:
            self.dts.execute(self.session, trade, by_tg_id=ALICE)
        self.assertIn("no longer on", str(caught.exception))


# ══════════════════════════════════════════════════════════════════════
# What a trade must not disturb
# ══════════════════════════════════════════════════════════════════════

class HistoryTests(TradeCase):

    def test_the_pick_rows_are_not_rewritten(self):
        """"R1 P1 was Mumbai's pick of Kohli" stays true after Mumbai trades
        him. It is also where tier_slots reads the team's quota from, which
        must keep describing the order sheet rather than the squad."""
        from models import DraftPick
        self.load()
        self.finish()
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        first = (self.session.query(DraftPick)
                 .filter(DraftPick.draft_id == self.draft.id,
                         DraftPick.overall_no == 1).one())
        self.assertEqual(first.team_id, self.team("Mumbai").id)
        self.assertEqual(first.draft_player_id, self.player("Virat Kohli").id)

    def test_the_slot_quota_is_unchanged_by_a_trade(self):
        self.load()
        self.finish()
        before = self.ds.tier_slots(self.session, self.draft.id,
                                    self.team("Mumbai").id)
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        self.assertEqual(before,
                         self.ds.tier_slots(self.session, self.draft.id,
                                            self.team("Mumbai").id))

    def test_undo_refuses_to_reach_through_a_trade(self):
        """Undo frees the player from the team that picked them. After a trade
        that is somebody else's player, and undoing would take him off a squad
        that never made the pick."""
        self.load()
        self.finish()
        # Mukesh is Chennai's R4 P2 pick — the last one — and goes to Mumbai.
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.undo_last(self.session, self.draft)
        self.assertIn("traded", str(caught.exception))

    def test_undo_still_works_on_a_pick_nobody_traded(self):
        self.load()
        self.finish()
        last, player = self.ds.undo_last(self.session, self.draft)
        self.assertEqual(player.name, "Mukesh Kumar")
        self.assertEqual(last.status, "pending")


class PublishedLeagueTests(TradeCase):
    """A published draft is a live Challenge League, and the league is what the
    match engine reads. A trade that only moved DraftPlayer rows would show in
    /dsquad and change nothing anybody actually plays with."""

    def publish(self):
        league = self.ds.publish_to_league(self.session, self.draft)
        self.session.commit()
        return league

    def challenge_team_of(self, league, player_name):
        from models import ChallengePlayer, ChallengeTeam
        cp = (self.session.query(ChallengePlayer)
              .join(ChallengeTeam, ChallengeTeam.id == ChallengePlayer.team_id)
              .filter(ChallengeTeam.league_id == league.id,
                      ChallengePlayer.name == player_name).one())
        return (self.session.query(ChallengeTeam)
                .filter(ChallengeTeam.id == cp.team_id).one().name)

    def test_a_trade_moves_the_published_player_too(self):
        self.load()
        self.finish()
        league = self.publish()
        self.assertEqual(self.challenge_team_of(league, "Virat Kohli"),
                         "Mumbai Mavericks")
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        self.assertEqual(self.challenge_team_of(league, "Virat Kohli"),
                         "Chennai Kings")
        self.assertEqual(self.challenge_team_of(league, "Mukesh Kumar"),
                         "Mumbai Mavericks")

    def test_nobody_is_left_behind_on_the_old_team(self):
        """Republishing alone could not fix this: publish_to_league adds and
        updates, so the player would arrive at the new team and stay at the old
        one as well."""
        from models import ChallengePlayer, ChallengeTeam
        self.load()
        self.finish()
        league = self.publish()
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        rows = (self.session.query(ChallengePlayer)
                .join(ChallengeTeam, ChallengeTeam.id == ChallengePlayer.team_id)
                .filter(ChallengeTeam.league_id == league.id,
                        ChallengePlayer.name == "Virat Kohli").all())
        self.assertEqual(len(rows), 1)

    def test_squad_sizes_survive_the_move(self):
        from models import ChallengePlayer, ChallengeTeam
        self.load()
        self.finish()
        league = self.publish()
        self.swap(["Virat Kohli", "Rashid Khan"],
                  ["Jasprit Bumrah", "Jos Buttler"])
        for team in self.ds.teams(self.session, self.draft.id):
            ct = (self.session.query(ChallengeTeam)
                  .filter(ChallengeTeam.league_id == league.id,
                          ChallengeTeam.name == team.name).one())
            self.assertEqual(
                self.session.query(ChallengePlayer)
                .filter(ChallengePlayer.team_id == ct.id).count(), 4)

    def test_an_unpublished_draft_is_simply_skipped(self):
        self.load()
        self.finish()
        self.assertEqual(self.dts.sync_league(self.session, self.draft), 0)


# ══════════════════════════════════════════════════════════════════════
# The commands
# ══════════════════════════════════════════════════════════════════════

class CommandTests(TradeCase):
    """``handlers/draft_trade.py``, driven with a stub Update — the gates and
    the button authorisation, which is where a two-party flow goes wrong."""

    def setUp(self):
        super().setUp()
        try:
            from handlers import draft_trade as handler
        except Exception as exc:        # python-telegram-bot not installed
            self.skipTest(f"handlers.draft_trade unavailable: {exc}")
        self.handler = handler
        self.replies = []
        self.markups = []
        self.edits = []
        self.alerts = []

    def _update(self, user_id, args=(), chat_type="supergroup"):
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            self.markups.append(kwargs.get("reply_markup"))
            return SimpleNamespace(message_id=1)

        return (SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.draft.chat_id,
                                           type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text)),
            SimpleNamespace(args=list(args), bot=SimpleNamespace()))

    def run_command(self, command, user_id, args=(), chat_type="supergroup"):
        import asyncio
        self.session.commit()
        update, context = self._update(user_id, args, chat_type)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(command(update, context))
        finally:
            loop.close()
        self.session.expire_all()
        return "\n".join(self.replies)

    def press(self, callback_data, user_id):
        """Press one dt_ button; returns ``(edited text, alert text)``."""
        import asyncio
        from types import SimpleNamespace
        self.session.commit()
        edits, alerts = [], []

        async def answer(text=None, **kwargs):
            if text:
                alerts.append(text)

        async def edit_message_text(text, **kwargs):
            edits.append(text)
            self.markups.append(kwargs.get("reply_markup"))

        query = SimpleNamespace(
            data=callback_data, answer=answer,
            edit_message_text=edit_message_text,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(reply_text=None))
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=self.draft.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=user_id))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                self.handler.trade_button_callback(
                    update, SimpleNamespace(args=[])))
        finally:
            loop.close()
        self.session.expire_all()
        return "\n".join(edits), "\n".join(alerts)

    def last_markup(self):
        for markup in reversed(self.markups):
            if markup is not None:
                return markup
        return None

    @staticmethod
    def data_for(markup, label):
        for row in markup.inline_keyboard:
            for button in row:
                if label in button.text:
                    return button.callback_data
        return None

    # ── the gates ──

    def test_dtrade_is_refused_while_the_draft_is_live(self):
        self.load()
        body = self.run_command(self.handler.dtrade_handler, ALICE,
                                ["Chennai"])
        self.assertIn("Trading opens when the draft finishes", body)

    def test_dtrade_is_refused_outside_the_bound_group(self):
        self.load()
        self.finish()
        body = self.run_command(self.handler.dtrade_handler, ALICE,
                                ["Chennai"], chat_type="private")
        self.assertIn("only work in the group", body)

    def test_dtrade_refuses_someone_with_no_team(self):
        self.load()
        self.finish()
        body = self.run_command(self.handler.dtrade_handler, CAROL,
                                ["Chennai"])
        self.assertIn("don't own a team", body)

    def test_dtrade_with_no_argument_explains_itself(self):
        self.load()
        self.finish()
        body = self.run_command(self.handler.dtrade_handler, ALICE)
        self.assertIn("no rating rule", body.lower())
        self.assertIn("Chennai Kings", body)

    def test_dtrade_posts_the_tick_list(self):
        self.load()
        self.finish()
        body = self.run_command(self.handler.dtrade_handler, ALICE,
                                ["Chennai"])
        self.assertIn("tick the players", body)
        markup = self.last_markup()
        self.assertIsNotNone(self.data_for(markup, "Virat Kohli"))
        self.assertIsNotNone(self.data_for(markup, "Cancel"))

    # ── the buttons ──

    def open_offer(self):
        self.load()
        self.finish()
        self.run_command(self.handler.dtrade_handler, ALICE, ["Chennai"])
        return self.last_markup()

    def test_a_stranger_cannot_press_anything(self):
        markup = self.open_offer()
        _text, alert = self.press(self.data_for(markup, "Virat Kohli"), CAROL)
        self.assertIn("don't own a team", alert)

    def test_the_other_franchise_cannot_tick_for_you(self):
        """Both sides can reach these buttons — that is the point of the shared
        prefix — so the turn check is the only thing keeping them apart."""
        markup = self.open_offer()
        _text, alert = self.press(self.data_for(markup, "Virat Kohli"), BOB)
        self.assertIn("Mumbai Mavericks", alert)

    def test_ticking_redraws_the_card_with_the_player_on_it(self):
        markup = self.open_offer()
        text, _alert = self.press(self.data_for(markup, "Virat Kohli"), ALICE)
        self.assertIn("Ticked", text)
        self.assertIn("Virat Kohli", text)

    def test_the_whole_flow_moves_the_players(self):
        markup = self.open_offer()
        self.press(self.data_for(markup, "Virat Kohli"), ALICE)
        self.press(self.data_for(self.last_markup(), "Done"), ALICE)
        # Now it is Chennai's turn, on the same message.
        self.press(self.data_for(self.last_markup(), "Mukesh Kumar"), BOB)
        self.press(self.data_for(self.last_markup(), "Done"), BOB)
        offer = self.last_markup()
        self.press(self.data_for(offer, "Mumbai"), ALICE)
        text, _alert = self.press(self.data_for(offer, "Chennai"), BOB)
        self.assertIn("TRADE COMPLETE", text)
        self.assertIn("Mukesh Kumar", self.squad_names("Mumbai"))
        self.assertIn("Virat Kohli", self.squad_names("Chennai"))

    def test_one_side_confirming_does_not_move_anybody(self):
        markup = self.open_offer()
        self.press(self.data_for(markup, "Virat Kohli"), ALICE)
        self.press(self.data_for(self.last_markup(), "Done"), ALICE)
        self.press(self.data_for(self.last_markup(), "Mukesh Kumar"), BOB)
        self.press(self.data_for(self.last_markup(), "Done"), BOB)
        text, _alert = self.press(
            self.data_for(self.last_markup(), "Mumbai"), ALICE)
        self.assertIn("waiting for", text)
        self.assertIn("Virat Kohli", self.squad_names("Mumbai"))

    def test_you_cannot_press_the_other_teams_confirm_button(self):
        markup = self.open_offer()
        self.press(self.data_for(markup, "Virat Kohli"), ALICE)
        self.press(self.data_for(self.last_markup(), "Done"), ALICE)
        self.press(self.data_for(self.last_markup(), "Mukesh Kumar"), BOB)
        self.press(self.data_for(self.last_markup(), "Done"), BOB)
        offer = self.last_markup()
        _text, alert = self.press(self.data_for(offer, "Chennai"), ALICE)
        self.assertIn("Chennai Kings", alert)

    def test_either_side_may_cancel(self):
        markup = self.open_offer()
        text, _alert = self.press(self.data_for(markup, "Cancel"), BOB)
        self.assertIn("Trade cancelled", text)

    def test_a_button_for_a_trade_that_is_gone_says_so(self):
        self.open_offer()
        _text, alert = self.press("dt_tog_999999_1", ALICE)
        self.assertIn("gone", alert)

    # ── the log and the lock ──

    def test_dtrades_lists_what_has_been_done(self):
        self.load()
        self.finish()
        self.swap(["Virat Kohli"], ["Mukesh Kumar"])
        body = self.run_command(self.handler.dtrades_handler, CAROL)
        self.assertIn("Virat Kohli", body)
        self.assertIn("Mukesh Kumar", body)

    def test_dtradecancel_calls_off_your_own_offer(self):
        self.open_offer()
        body = self.run_command(self.handler.dtradecancel_handler, BOB)
        self.assertIn("Called off", body)

    def test_dtradecancel_with_nothing_open_says_so(self):
        self.load()
        self.finish()
        body = self.run_command(self.handler.dtradecancel_handler, ALICE)
        self.assertIn("no open trade", body)

    def test_dtradelock_refuses_a_non_admin(self):
        self.load()
        self.finish()
        with _admin_ids("999"):
            body = self.run_command(self.handler.dtradelock_handler, ALICE,
                                    ["on"])
        self.assertIn("Only bot admins", body)
        self.assertTrue(self.dts.trades_open(self.draft))

    def test_dtradelock_closes_and_reopens_the_window(self):
        self.load()
        self.finish()
        with _admin_ids("999"):
            self.run_command(self.handler.dtradelock_handler, 999, ["on"])
        self.session.expire_all()
        self.assertFalse(self.dts.trades_open(self.draft))
        self.replies.clear()
        with _admin_ids("999"):
            self.run_command(self.handler.dtradelock_handler, 999, ["off"])
        self.session.expire_all()
        self.assertTrue(self.dts.trades_open(self.draft))

    def test_dtradelock_with_no_argument_reports_the_state(self):
        self.load()
        self.finish()
        with _admin_ids("999"):
            body = self.run_command(self.handler.dtradelock_handler, 999)
        self.assertIn("trade window is <b>open</b>", body)


import contextlib as _contextlib


@_contextlib.contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _admin_ids(raw):
    """``services.admin_ids`` reads these on every check, so the env is enough."""
    return _env(BOT_ADMIN_IDS=raw, ADMIN_IDS=raw, ADMIN_USER_IDS=raw,
                SUDO_USERS=raw, OWNER_IDS=raw, ADMIN_CHAT_ID=raw)


if __name__ == "__main__":
    unittest.main()
