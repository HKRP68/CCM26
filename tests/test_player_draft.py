"""The Tournament Draft: teams picking their squads, live, pick by pick.

What these cover is the part that is genuinely new — the pool, the order sheet,
the ceiling rule, the clock and the publish step. Everything downstream of
``publish_to_league`` is the Challenge League engine, already covered by its own
tests, and is exercised here only far enough to prove a drafted squad really is
a playable one (``cipl_match.cp_to_player_dict`` reads the blob we write).

The rules worth pinning, and why:

  • **Columns are matched by header name.** An importer that reads by position
    puts ratings in the country column the first time somebody reorders their
    spreadsheet, and says nothing.
  • **A slot's tier is a ceiling.** Gold into a Platinum slot is allowed;
    Platinum into a Gold slot is not. That single rule is also what caps a
    team's Platinum count at its number of Platinum slots.
  • **Only the owner and co-owners may pick**, and only for the team on the
    clock. Not admins — they have /dskip.
  • **Two co-owners can type /pick on the same tick**, and exactly one may win.
  • **Refusals must be reachable**, not retrospective: a pick is refused while
    there are still slots left to fix the problem, never after.
  • **Auto-pick must never be able to wedge the draft**, and must be
    deterministic enough to explain afterwards.
"""

import io
import itertools
import os
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.draft_service", "services.xlsx_reader")

_PID = itertools.count(1)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

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
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


POOL_HEADER = ["name", "rating", "tier", "icon_eligible", "gender",
               "indian_status", "category", "country", "bat_hand", "bowl_hand",
               "bowl_style", "bat_rating", "bowl_rating"]

# name, rating, tier, icon, gender, indian, category, country, ...
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
    ["Tim David", "84", "Silver", "0", "Male", "Overseas", "Batsman",
     "Australia", "R", "R", "Medium", "85", "20"],
    ["Rinku Singh", "83", "Silver", "0", "Male", "Indian", "batter", "India",
     "L", "L", "Medium", "84", "15"],
    ["Mukesh Kumar", "74", "Bronze", "0", "Male", "Indian", "bowl", "India",
     "R", "R", "Medium", "20", "75"],
]

ORDER_HEADER = ["Round No", "Pick Number", "Tier", "Team Name", "Owner Name",
                "Owner Tag ID"]
ORDER_ROWS = [
    ["1", "1", "Platinum", "Mumbai Mavericks", "Alice", "111"],
    ["1", "2", "Platinum", "Chennai Kings", "Bob", "222"],
    ["2", "1", "Gold", "Mumbai Mavericks", "Alice", "111"],
    ["2", "2", "Gold", "Chennai Kings", "Bob", "222"],
    ["3", "1", "Silver", "Mumbai Mavericks", "Alice", "111"],
    ["3", "2", "Silver", "Chennai Kings", "Bob", "222"],
]

ALICE, BOB, CAROL = 111, 222, 333


class DraftCase(unittest.TestCase):
    """A draft with a loaded pool and a two-team, three-round order."""

    max_overseas = 11
    role_minimums = None

    def setUp(self):
        from database import get_session
        from services import draft_service as ds

        self.session = get_session()
        self.ds = ds
        self.draft = ds.create_draft(
            self.session, "Test Draft", pick_seconds=900,
            max_overseas=self.max_overseas, role_minimums=self.role_minimums)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def load_pool(self, rows=None, header=True, **kwargs):
        body = [list(r) for r in (rows if rows is not None else POOL_ROWS)]
        sheet = ([list(POOL_HEADER)] + body) if header else body
        return self.ds.import_pool(self.session, self.draft, sheet, **kwargs)

    def load_order(self, rows=None, **kwargs):
        body = [list(r) for r in (rows if rows is not None else ORDER_ROWS)]
        return self.ds.import_order(self.session, self.draft,
                                    [list(ORDER_HEADER)] + body, **kwargs)

    def go_live(self):
        self.load_pool()
        self.load_order()
        self.draft.chat_id = -100_000 - self.draft.id
        pick = self.ds.start(self.session, self.draft)
        self.session.commit()
        return pick

    def player(self, name):
        from models import DraftPlayer
        return (self.session.query(DraftPlayer)
                .filter(DraftPlayer.draft_id == self.draft.id,
                        DraftPlayer.name == name).one())

    def slot(self, round_no, pick_no):
        from models import DraftPick
        return (self.session.query(DraftPick)
                .filter(DraftPick.draft_id == self.draft.id,
                        DraftPick.round_no == round_no,
                        DraftPick.pick_no == pick_no).one())

    def team(self, name):
        return self.ds.find_team(self.session, self.draft.id, name)

    def pick(self, name, by=ALICE):
        current = self.ds.current_pick(self.session, self.draft)
        return self.ds.make_pick(self.session, self.draft, current,
                                 self.player(name), by_tg_id=by)


# ══════════════════════════════════════════════════════════════════════
# Importing the sheets
# ══════════════════════════════════════════════════════════════════════

class PoolImportTests(DraftCase):

    def test_a_full_sheet_loads(self):
        added, updated, errors = self.load_pool()
        self.assertEqual((added, updated, errors), (8, 0, []))

    def test_columns_are_matched_by_name_not_position(self):
        """The whole point of header matching: reordering must be harmless."""
        order = [POOL_HEADER.index(c) for c in
                 ("tier", "country", "name", "bowl_rating", "category",
                  "rating", "indian_status", "icon_eligible", "gender",
                  "bat_hand", "bowl_hand", "bowl_style", "bat_rating")]
        header = [POOL_HEADER[i] for i in order]
        rows = [[r[i] for i in order] for r in POOL_ROWS]
        added, _updated, errors = self.ds.import_pool(
            self.session, self.draft, [header] + rows)
        self.assertEqual((added, errors), (8, []))
        kohli = self.player("Virat Kohli")
        self.assertEqual((kohli.rating, kohli.tier, kohli.country),
                         (97, "Platinum", "India"))

    def test_a_headerless_sheet_falls_back_to_the_documented_order(self):
        added, _updated, errors = self.load_pool(header=False)
        self.assertEqual((added, errors), (8, []))
        self.assertEqual(self.player("Virat Kohli").rating, 97)

    def test_indian_status_accepts_the_three_shapes_people_type(self):
        self.load_pool(rows=[
            ["A", "80", "Gold", "0", "M", "Indian", "Batsman", "India",
             "R", "R", "Medium", "80", "10"],
            ["B", "80", "Gold", "0", "M", "Overseas", "Batsman", "England",
             "R", "R", "Medium", "80", "10"],
            ["C", "80", "Gold", "0", "M", "1", "Batsman", "India",
             "R", "R", "Medium", "80", "10"],
            ["D", "80", "Gold", "0", "M", "0", "Batsman", "England",
             "R", "R", "Medium", "80", "10"],
            ["E", "80", "Gold", "0", "M", "India", "Batsman", "India",
             "R", "R", "Medium", "80", "10"],
        ])
        got = {n: self.player(n).is_indian for n in "ABCDE"}
        self.assertEqual(got, {"A": True, "B": False, "C": True,
                               "D": False, "E": True})

    def test_roles_are_folded_onto_the_engines_four(self):
        self.load_pool()
        self.assertEqual(self.player("Jos Buttler").category, "Wicket Keeper")
        self.assertEqual(self.player("Rinku Singh").category, "Batsman")
        self.assertEqual(self.player("Mukesh Kumar").category, "Bowler")

    def test_bad_rows_are_reported_not_raised(self):
        """One broken row must not cost the admin the other 599."""
        added, _updated, errors = self.load_pool(rows=POOL_ROWS + [
            ["", "80", "Gold", "0", "M", "Indian", "Batsman", "India",
             "R", "R", "Medium", "80", "10"],
            ["Nobody", "80", "Titanium", "0", "M", "Indian", "Batsman",
             "India", "R", "R", "Medium", "80", "10"],
            ["Virat Kohli", "97", "Platinum", "1", "M", "Indian", "Batsman",
             "India", "R", "R", "Medium", "96", "30"],
        ])
        self.assertEqual(added, 8)
        self.assertEqual(len(errors), 3)
        self.assertIn("no player name", errors[0])
        self.assertIn("not on this draft's ladder", errors[1])
        self.assertIn("twice", errors[2])

    def test_reuploading_updates_in_place(self):
        self.load_pool()
        bumped = [list(r) for r in POOL_ROWS]
        bumped[0][1] = "99"
        added, updated, errors = self.load_pool(rows=bumped)
        self.assertEqual((added, updated, errors), (0, 8, []))
        self.assertEqual(self.player("Virat Kohli").rating, 99)

    def test_a_name_that_matches_a_card_links_to_it(self):
        """The link is what lets a pick post the player's real card image."""
        from models import Player
        card = Player(id=next(_PID) + 90_000, name="Virat Kohli", rating=97,
                      category="Batsman", country="India", bat_hand="Right",
                      bowl_hand="Right", bowl_style="Medium Pacer",
                      bat_rating=96, bowl_rating=30, is_active=True)
        self.session.add(card)
        self.session.flush()
        self.load_pool()
        self.assertEqual(self.player("Virat Kohli").source_player_id, card.id)
        self.assertIsNone(self.player("Tim David").source_player_id)

    def test_replacing_the_pool_is_refused_once_picking_has_started(self):
        self.go_live()
        self.pick("Virat Kohli")
        with self.assertRaises(self.ds.DraftError) as caught:
            self.load_pool(replace=True)
        self.assertIn("already started", str(caught.exception))


class OrderImportTests(DraftCase):

    def test_teams_are_created_from_the_sheet(self):
        picks, made, errors = self.load_order()
        self.assertEqual((picks, made, errors), (6, 2, []))
        team = self.team("Mumbai Mavericks")
        self.assertEqual((team.owner_name, team.owner_tg_id), ("Alice", ALICE))

    def test_the_running_order_is_rebuilt_from_round_and_pick(self):
        """A sheet in the wrong row order must still run R1 before R2."""
        from models import DraftPick
        scrambled = [ORDER_ROWS[3], ORDER_ROWS[0], ORDER_ROWS[5],
                     ORDER_ROWS[1], ORDER_ROWS[4], ORDER_ROWS[2]]
        self.load_order(rows=scrambled)
        rows = (self.session.query(DraftPick)
                .filter(DraftPick.draft_id == self.draft.id)
                .order_by(DraftPick.overall_no).all())
        self.assertEqual([(r.round_no, r.pick_no) for r in rows],
                         [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)])
        self.assertEqual([r.overall_no for r in rows], [1, 2, 3, 4, 5, 6])

    def test_a_duplicated_slot_is_reported(self):
        _picks, _made, errors = self.load_order(rows=ORDER_ROWS + [ORDER_ROWS[0]])
        self.assertEqual(len(errors), 1)
        self.assertIn("listed twice", errors[0])

    def test_tier_slots_become_the_teams_quota(self):
        self.load_order()
        slots = self.ds.tier_slots(self.session, self.draft.id,
                                   self.team("Mumbai Mavericks").id)
        self.assertEqual(slots, {"Platinum": 1, "Gold": 1, "Silver": 1})

    def test_the_order_is_frozen_once_picking_has_started(self):
        self.go_live()
        self.pick("Virat Kohli")
        with self.assertRaises(self.ds.DraftError) as caught:
            self.load_order()
        self.assertIn("already started", str(caught.exception))


class XlsxTests(unittest.TestCase):
    """The reader must accept what the site's own exporter writes."""

    def _build(self, rows):
        import html as html_lib
        import pathlib
        import zipfile
        src = pathlib.Path(__file__).resolve().parent.parent / "admin.py"
        text = src.read_text()
        start = text.index("def _build_players_xlsx(rows):")
        end = text.index("\ndef _users_csv_export", start)
        namespace = {"io": io, "zipfile": zipfile, "html_lib": html_lib}
        exec(text[start:end], namespace)  # noqa: S102 - reading our own source
        return namespace["_build_players_xlsx"](rows).getvalue()

    def test_round_trip(self):
        from services.xlsx_reader import read_rows
        data = self._build([
            {"name": "Virat Kohli", "rating": 97, "tier": "Platinum",
             "icon_eligible": True},
            {"name": "Tim David", "rating": 84, "tier": "Silver",
             "icon_eligible": False},
        ])
        rows = read_rows(data)
        self.assertEqual(rows[0], ["name", "rating", "tier", "icon_eligible"])
        self.assertEqual(rows[1], ["Virat Kohli", "97", "Platinum", "1"])
        self.assertEqual(rows[2][3], "0")

    def test_junk_is_a_readable_refusal_not_a_traceback(self):
        from services.xlsx_reader import read_rows, XlsxError
        for junk in (b"", b"this is not a zip"):
            with self.assertRaises(XlsxError):
                read_rows(junk)


# ══════════════════════════════════════════════════════════════════════
# The ceiling rule
# ══════════════════════════════════════════════════════════════════════

class TierCeilingTests(DraftCase):

    def test_a_slot_takes_its_own_tier(self):
        pick = self.go_live()
        self.assertTrue(self.ds.validate_pick(self.session, self.draft, pick,
                                              self.player("Virat Kohli")))

    def test_a_slot_takes_anything_below_it(self):
        pick = self.go_live()
        for name in ("Rashid Khan", "Tim David", "Mukesh Kumar"):
            self.assertTrue(self.ds.validate_pick(self.session, self.draft,
                                                  pick, self.player(name)))

    def test_a_slot_refuses_anything_above_it(self):
        self.go_live()
        gold = self.slot(2, 1)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.validate_pick(self.session, self.draft, gold,
                                  self.player("Virat Kohli"))
        message = str(caught.exception)
        self.assertIn("Platinum", message)
        self.assertIn("Gold slot", message)

    def test_the_ceiling_is_what_caps_a_teams_tier_count(self):
        """A Platinum player fits only a Platinum slot, so one slot = one max.

        This is why per-tier quotas need no separate rule: a team with a single
        Platinum slot can never end up holding two Platinum players.
        """
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)              # Mumbai's only Platinum
        self.pick("Jasprit Bumrah", by=BOB)             # Chennai's only Platinum
        gold = self.ds.current_pick(self.session, self.draft)
        self.assertEqual(gold.tier, "Gold")
        remaining_platinum = [p for p in
                              self.ds.available(self.session, self.draft.id)
                              if p.tier == "Platinum"]
        self.assertEqual(remaining_platinum, [])

    def test_picking_below_spends_the_slot_with_no_refund(self):
        self.go_live()
        team = self.team("Mumbai Mavericks")
        self.pick("Mukesh Kumar", by=ALICE)             # Bronze into Platinum
        slots = self.ds.tier_slots(self.session, self.draft.id, team.id)
        squad = self.ds.squad(self.session, team.id)
        self.assertEqual(len(squad), 1)
        self.assertEqual(sum(slots.values()), 3)
        self.assertEqual(len(self.ds.pending_picks(self.session, self.draft.id,
                                                   team.id)), 2)


# ══════════════════════════════════════════════════════════════════════
# Who may pick
# ══════════════════════════════════════════════════════════════════════

class PermissionTests(DraftCase):

    def test_the_owner_may_pick(self):
        self.go_live()
        self.assertTrue(self.ds.may_pick_for(self.team("Mumbai Mavericks"), ALICE))

    def test_a_co_owner_may_pick(self):
        self.go_live()
        team = self.team("Mumbai Mavericks")
        self.ds.add_co_owner(self.session, team, CAROL)
        self.assertTrue(self.ds.may_pick_for(team, CAROL))

    def test_another_teams_owner_may_not(self):
        self.go_live()
        self.assertFalse(self.ds.may_pick_for(self.team("Mumbai Mavericks"), BOB))

    def test_a_stranger_may_not(self):
        self.go_live()
        self.assertFalse(self.ds.may_pick_for(self.team("Mumbai Mavericks"), 999))

    def test_team_for_actor_finds_only_your_own_team(self):
        self.go_live()
        self.assertEqual(
            self.ds.team_for_actor(self.session, self.draft.id, BOB).name,
            "Chennai Kings")
        self.assertIsNone(
            self.ds.team_for_actor(self.session, self.draft.id, 999))

    def test_the_owner_cannot_be_added_as_their_own_co_owner(self):
        self.go_live()
        with self.assertRaises(self.ds.DraftError):
            self.ds.add_co_owner(self.session, self.team("Mumbai Mavericks"),
                                 ALICE)


# ══════════════════════════════════════════════════════════════════════
# The squad rules
# ══════════════════════════════════════════════════════════════════════

class OverseasCapTests(DraftCase):
    max_overseas = 1

    def test_the_cap_refuses_the_pick_that_would_break_it(self):
        self.go_live()
        self.pick("Rashid Khan", by=ALICE)          # Mumbai's 1 overseas
        gold = self.slot(2, 1)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.validate_pick(self.session, self.draft, gold,
                                  self.player("Jos Buttler"))
        self.assertIn("overseas", str(caught.exception))

    def test_a_home_player_is_never_blocked_by_it(self):
        self.go_live()
        self.pick("Rashid Khan", by=ALICE)
        self.assertTrue(self.ds.validate_pick(self.session, self.draft,
                                              self.slot(2, 1),
                                              self.player("Sanju Samson")))


class RoleMinimumTests(DraftCase):
    role_minimums = {"Wicket Keeper": 1}

    # A Silver keeper, so a Silver slot can actually satisfy the minimum — the
    # scenario has to be about the count running out, not about the tier.
    POOL = POOL_ROWS + [
        ["Dhruv Jurel", "78", "Silver", "0", "Male", "Indian", "Wicket Keeper",
         "India", "R", "R", "-", "79", "5"],
    ]

    def _three_non_keepers_deep(self):
        """Mumbai one slot from home, still owing a keeper."""
        self.load_pool(rows=self.POOL)
        self.load_order()
        self.draft.chat_id = -100_321
        self.ds.start(self.session, self.draft)
        self.pick("Virat Kohli", by=ALICE)      # R1P1 Mumbai  Platinum
        self.pick("Jasprit Bumrah", by=BOB)     # R1P2 Chennai Platinum
        self.pick("Rashid Khan", by=ALICE)      # R2P1 Mumbai  Gold
        self.pick("Sanju Samson", by=BOB)       # R2P2 Chennai Gold
        return self.ds.current_pick(self.session, self.draft)   # R3P1, Silver

    def test_a_non_keeper_is_allowed_while_a_keeper_slot_remains(self):
        self.load_pool(rows=self.POOL)
        self.load_order()
        self.draft.chat_id = -100_322
        self.ds.start(self.session, self.draft)
        self.pick("Virat Kohli", by=ALICE)      # Mumbai: 2 slots, 1 keeper owed
        self.pick("Jasprit Bumrah", by=BOB)
        gold = self.ds.current_pick(self.session, self.draft)
        self.assertTrue(self.ds.validate_pick(self.session, self.draft, gold,
                                              self.player("Rashid Khan")))

    def test_a_pick_is_refused_while_it_can_still_be_fixed(self):
        """Reachability, not the finished squad.

        Mumbai has three slots and owes a keeper. Once two have gone on
        non-keepers the last one must be the keeper — and the refusal has to
        arrive at that pick, while it is still possible to obey it.
        """
        last = self._three_non_keepers_deep()
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.validate_pick(self.session, self.draft, last,
                                  self.player("Rinku Singh"))
        message = str(caught.exception)
        self.assertIn("Wicket Keeper", message)
        self.assertIn("0 pick(s) left", message)

    def test_the_pick_that_satisfies_the_minimum_is_allowed(self):
        last = self._three_non_keepers_deep()
        self.assertTrue(self.ds.validate_pick(self.session, self.draft, last,
                                              self.player("Dhruv Jurel")))

    def test_an_all_rounder_counts_towards_a_bowler_minimum(self):
        """The XI rules already treat them that way; refusing here would be a
        rule the squad sheet never stated."""
        self.draft.role_minimums_json = '{"Bowler": 1}'
        self.go_live()
        team = self.team("Mumbai Mavericks")
        self.assertEqual(self.ds.role_gap(self.session, self.draft, team.id),
                         {"Bowler": 1})
        pick = self.ds.current_pick(self.session, self.draft)
        allrounder = self.player("Tim David")
        allrounder.category = "All-rounder"
        self.session.flush()
        self.ds.make_pick(self.session, self.draft, pick, allrounder,
                          by_tg_id=ALICE)
        self.assertEqual(self.ds.role_gap(self.session, self.draft, team.id), {})


# ══════════════════════════════════════════════════════════════════════
# Making the pick
# ══════════════════════════════════════════════════════════════════════

class PickTests(DraftCase):

    def test_a_pick_claims_the_player_and_the_slot_and_moves_the_clock(self):
        first = self.go_live()
        done = self.pick("Virat Kohli", by=ALICE)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.picked_by_tg_id, ALICE)
        self.assertFalse(done.is_auto)
        self.assertEqual(self.player("Virat Kohli").picked_by_team_id,
                         self.team("Mumbai Mavericks").id)
        nxt = self.ds.current_pick(self.session, self.draft)
        self.assertNotEqual(nxt.id, first.id)
        self.assertEqual((nxt.round_no, nxt.pick_no), (1, 2))
        self.assertIsNotNone(self.draft.pick_deadline_at)
        self.assertFalse(self.draft.warn_sent)

    def test_the_same_player_cannot_be_taken_twice(self):
        """Two co-owners hammering /pick on the same tick: one may win."""
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        second = self.ds.current_pick(self.session, self.draft)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.make_pick(self.session, self.draft, second,
                              self.player("Virat Kohli"), by_tg_id=BOB)
        self.assertIn("already gone", str(caught.exception))

    def test_a_slot_that_is_no_longer_pending_is_refused(self):
        self.go_live()
        done = self.pick("Virat Kohli", by=ALICE)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.make_pick(self.session, self.draft, done,
                              self.player("Tim David"), by_tg_id=ALICE)
        self.assertIn("already been made", str(caught.exception))

    def test_the_draft_completes_when_the_last_slot_is_filled(self):
        self.go_live()
        for name, who in (("Virat Kohli", ALICE), ("Jasprit Bumrah", BOB),
                          ("Rashid Khan", ALICE), ("Sanju Samson", BOB),
                          ("Tim David", ALICE), ("Rinku Singh", BOB)):
            self.pick(name, by=who)
        self.assertEqual(self.draft.status, self.ds.STATUS_COMPLETED)
        self.assertIsNone(self.ds.current_pick(self.session, self.draft))
        self.assertIsNone(self.draft.pick_deadline_at)


class NameMatchTests(DraftCase):

    def test_an_exact_name_wins_outright(self):
        self.go_live()
        found, candidates = self.ds.find_available(self.session, self.draft.id,
                                                   "virat kohli")
        self.assertEqual(found.name, "Virat Kohli")
        self.assertEqual(candidates, [])

    def test_a_unique_partial_resolves(self):
        self.go_live()
        found, _c = self.ds.find_available(self.session, self.draft.id, "bumr")
        self.assertEqual(found.name, "Jasprit Bumrah")

    def test_an_ambiguous_name_is_handed_back_not_guessed(self):
        """Guessing 'Kohli' wrong costs a team its pick and needs an admin."""
        self.load_pool(rows=POOL_ROWS + [
            ["Virat Kohli Jr", "70", "Bronze", "0", "M", "Indian", "Batsman",
             "India", "R", "R", "Medium", "70", "10"]])
        self.load_order()
        self.draft.chat_id = -100_123
        self.ds.start(self.session, self.draft)
        found, candidates = self.ds.find_available(self.session, self.draft.id,
                                                   "virat")
        self.assertIsNone(found)
        self.assertEqual(sorted(c.name for c in candidates),
                         ["Virat Kohli", "Virat Kohli Jr"])

    def test_an_exact_name_still_wins_over_a_longer_one(self):
        self.load_pool(rows=POOL_ROWS + [
            ["Virat Kohli Jr", "70", "Bronze", "0", "M", "Indian", "Batsman",
             "India", "R", "R", "Medium", "70", "10"]])
        found, _c = self.ds.find_available(self.session, self.draft.id,
                                           "Virat Kohli")
        self.assertEqual(found.name, "Virat Kohli")

    def test_a_drafted_player_stops_matching(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        found, candidates = self.ds.find_available(self.session, self.draft.id,
                                                   "virat kohli")
        self.assertIsNone(found)
        self.assertEqual(candidates, [])


# ══════════════════════════════════════════════════════════════════════
# The clock
# ══════════════════════════════════════════════════════════════════════

class AutoPickTests(DraftCase):

    def test_it_takes_the_middle_of_the_band_not_the_best_of_it(self):
        """Deterministic on purpose: the result has to be explainable after.

        Gold is Rashid 93, Buttler 92, Samson 88 — mean 91, so Buttler wins.
        """
        self.go_live()
        gold = self.slot(2, 1)
        chosen = self.ds.auto_pick(self.session, self.draft, gold)
        self.assertEqual(chosen.name, "Jos Buttler")

    def test_it_is_stable_across_calls(self):
        self.go_live()
        gold = self.slot(2, 1)
        first = self.ds.auto_pick(self.session, self.draft, gold)
        second = self.ds.auto_pick(self.session, self.draft, gold)
        self.assertEqual(first.id, second.id)

    def test_it_drains_the_owners_queue_first(self):
        """Being asleep must not mean losing the player you already asked for."""
        pick = self.go_live()
        team = self.team("Mumbai Mavericks")
        self.ds.queue_add(self.session, self.draft, team,
                          self.player("Mukesh Kumar"))
        self.assertEqual(self.ds.auto_pick(self.session, self.draft, pick).name,
                         "Mukesh Kumar")

    def test_a_queued_player_who_is_gone_is_skipped_over(self):
        self.go_live()
        team = self.team("Mumbai Mavericks")
        for name in ("Jasprit Bumrah", "Mukesh Kumar"):
            self.ds.queue_add(self.session, self.draft, team, self.player(name))
        self.pick("Jasprit Bumrah", by=ALICE)       # takes the first one legitimately
        second = self.ds.current_pick(self.session, self.draft)
        second.team_id = team.id                     # put Mumbai back on the clock
        self.session.flush()
        self.assertEqual(self.ds.auto_pick(self.session, self.draft, second).name,
                         "Mukesh Kumar")

    def test_a_drafted_player_leaves_every_queue(self):
        """Otherwise an owner's queue silently fills with names nobody can have."""
        self.go_live()
        mumbai, chennai = self.team("Mumbai Mavericks"), self.team("Chennai Kings")
        for team in (mumbai, chennai):
            self.ds.queue_add(self.session, self.draft, team,
                              self.player("Virat Kohli"))
        self.pick("Virat Kohli", by=ALICE)
        self.assertEqual(self.ds.queue_ids(chennai), [])
        self.assertEqual(self.ds.queue_ids(mumbai), [])

    def test_it_steps_down_the_ladder_when_the_slot_tier_is_empty(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        self.pick("Jasprit Bumrah", by=BOB)         # Platinum now exhausted
        platinum_slot = self.slot(1, 1)
        platinum_slot.status = "pending"            # re-open it artificially
        self.session.flush()
        chosen = self.ds.auto_pick(self.session, self.draft, platinum_slot)
        self.assertEqual(chosen.tier, "Gold")

    def test_it_never_returns_an_illegal_player(self):
        self.draft.max_overseas = 0
        self.go_live()
        gold = self.slot(2, 1)
        chosen = self.ds.auto_pick(self.session, self.draft, gold)
        self.assertTrue(chosen.is_indian)
        self.assertEqual(chosen.name, "Sanju Samson")

    def test_nothing_legal_left_passes_the_slot_instead_of_wedging(self):
        self.draft.max_overseas = 0
        self.load_pool(rows=[
            ["Only Overseas", "80", "Gold", "0", "M", "Overseas", "Batsman",
             "England", "R", "R", "Medium", "80", "10"]])
        self.load_order(rows=[["1", "1", "Gold", "Solo", "Sam", "111"]])
        self.draft.chat_id = -100_555
        pick = self.ds.start(self.session, self.draft)
        self.assertIsNone(self.ds.auto_pick(self.session, self.draft, pick))
        resolved, player = self.ds.resolve_expired(self.session, self.draft, pick)
        self.assertIsNone(player)
        self.assertEqual(resolved.status, "skipped")
        self.assertTrue(resolved.is_auto)
        self.assertEqual(self.draft.status, self.ds.STATUS_COMPLETED)

    def test_an_auto_pick_is_flagged_as_one(self):
        pick = self.go_live()
        resolved, player = self.ds.resolve_expired(self.session, self.draft, pick)
        self.assertTrue(resolved.is_auto)
        self.assertEqual(resolved.status, "done")
        self.assertIsNotNone(player)


class ClockTests(DraftCase):

    def test_starting_puts_the_first_slot_on_a_deadline(self):
        pick = self.go_live()
        self.assertEqual((pick.round_no, pick.pick_no), (1, 1))
        self.assertEqual(self.draft.status, self.ds.STATUS_LIVE)
        self.assertIsNotNone(self.draft.pick_deadline_at)
        self.assertAlmostEqual(self.ds.seconds_left(self.draft), 900, delta=5)

    def test_pausing_drops_the_deadline_rather_than_freezing_it(self):
        """A frozen deadline auto-picks the instant the draft resumes."""
        self.go_live()
        self.ds.pause(self.session, self.draft)
        self.assertIsNone(self.draft.pick_deadline_at)
        self.assertIsNone(self.ds.seconds_left(self.draft))
        self.ds.resume(self.session, self.draft)
        self.assertAlmostEqual(self.ds.seconds_left(self.draft), 900, delta=5)

    def test_resuming_keeps_the_same_slot_on_the_clock(self):
        pick = self.go_live()
        self.ds.pause(self.session, self.draft)
        self.assertEqual(self.ds.resume(self.session, self.draft).id, pick.id)

    def test_the_deadline_survives_a_restart(self):
        """The whole reason the clock is a column and not a job."""
        from database import get_session
        self.go_live()
        deadline, pick_id = self.draft.pick_deadline_at, self.draft.current_pick_id
        self.session.commit()
        fresh = get_session()
        try:
            reloaded = self.ds.draft_for_chat(fresh, self.draft.chat_id)
            self.assertEqual(reloaded.pick_deadline_at, deadline)
            self.assertEqual(reloaded.current_pick_id, pick_id)
            self.assertEqual(self.ds.current_pick(fresh, reloaded).id, pick_id)
        finally:
            fresh.close()

    def test_current_pick_heals_a_stale_pointer(self):
        self.go_live()
        self.draft.current_pick_id = 999_999
        self.session.flush()
        self.assertEqual(
            (self.ds.current_pick(self.session, self.draft).round_no,
             self.ds.current_pick(self.session, self.draft).pick_no), (1, 1))

    def test_the_clock_is_clamped_to_something_sane(self):
        self.assertEqual(self.ds.clamp_pick_seconds(1), self.ds.MIN_PICK_SECONDS)
        self.assertEqual(self.ds.clamp_pick_seconds(10 ** 9),
                         self.ds.MAX_PICK_SECONDS)
        self.assertEqual(self.ds.clamp_pick_seconds("abc"), 900)

    def test_starting_without_a_pool_or_an_owner_is_refused(self):
        self.load_order()
        self.draft.chat_id = -100_777
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.start(self.session, self.draft)
        self.assertIn("player pool", str(caught.exception))

    def test_a_team_with_no_owner_id_blocks_the_start(self):
        self.load_pool()
        self.load_order(rows=[["1", "1", "Gold", "Orphan FC", "", ""]])
        self.draft.chat_id = -100_778
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.start(self.session, self.draft)
        self.assertIn("Orphan FC", str(caught.exception))


class UndoTests(DraftCase):

    def test_undo_frees_the_player_and_re_arms_the_slot(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        last, player = self.ds.undo_last(self.session, self.draft)
        self.assertEqual(player.name, "Virat Kohli")
        self.assertIsNone(self.player("Virat Kohli").picked_by_team_id)
        self.assertEqual(last.status, "pending")
        self.assertIsNone(last.draft_player_id)
        self.assertEqual(self.draft.current_pick_id, last.id)
        self.assertIsNotNone(self.draft.pick_deadline_at)

    def test_undo_reopens_a_completed_draft(self):
        """Otherwise a slot sits on the clock in a draft nothing can pick in."""
        self.go_live()
        for name, who in (("Virat Kohli", ALICE), ("Jasprit Bumrah", BOB),
                          ("Rashid Khan", ALICE), ("Sanju Samson", BOB),
                          ("Tim David", ALICE), ("Rinku Singh", BOB)):
            self.pick(name, by=who)
        self.assertEqual(self.draft.status, self.ds.STATUS_COMPLETED)
        self.ds.undo_last(self.session, self.draft)
        self.assertEqual(self.draft.status, self.ds.STATUS_LIVE)
        self.assertEqual(
            self.ds.current_pick(self.session, self.draft).overall_no, 6)

    def test_undo_with_nothing_to_undo_says_so(self):
        self.go_live()
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.undo_last(self.session, self.draft)
        self.assertIn("No picks", str(caught.exception))

    def test_a_passed_slot_can_be_undone_too(self):
        pick = self.go_live()
        self.ds.skip_pick(self.session, self.draft, pick)
        last, player = self.ds.undo_last(self.session, self.draft)
        self.assertIsNone(player)
        self.assertEqual(last.status, "pending")


# ══════════════════════════════════════════════════════════════════════
# Publishing — the draft's output has to be a playable league
# ══════════════════════════════════════════════════════════════════════

class PublishTests(DraftCase):

    def _finish(self):
        self.go_live()
        for name, who in (("Virat Kohli", ALICE), ("Jasprit Bumrah", BOB),
                          ("Rashid Khan", ALICE), ("Sanju Samson", BOB),
                          ("Tim David", ALICE), ("Rinku Singh", BOB)):
            self.pick(name, by=who)
        self.assertEqual(self.draft.status, self.ds.STATUS_COMPLETED)

    def test_an_unfinished_draft_is_refused(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.publish_to_league(self.session, self.draft)
        self.assertIn("Finish the draft first", str(caught.exception))

    def test_every_squad_reaches_the_league(self):
        from models import ChallengePlayer, ChallengeTeam
        self._finish()
        league = self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        teams = (self.session.query(ChallengeTeam)
                 .filter(ChallengeTeam.league_id == league.id).all())
        self.assertEqual(sorted(t.name for t in teams),
                         ["Chennai Kings", "Mumbai Mavericks"])
        players = (self.session.query(ChallengePlayer)
                   .filter(ChallengePlayer.team_id.in_([t.id for t in teams]))
                   .all())
        self.assertEqual(len(players), 6)
        self.assertEqual(self.draft.league_id, league.id)
        self.assertIsNotNone(self.draft.published_at)

    def test_the_leagues_overseas_rule_is_inherited(self):
        self.draft.max_overseas = 2
        self.draft.home_country = "India"
        self._finish()
        league = self.ds.publish_to_league(self.session, self.draft)
        self.assertEqual(league.home_country, "India")
        self.assertEqual(league.max_overseas, 2)

    def test_is_overseas_inverts_is_indian(self):
        from models import ChallengePlayer
        self._finish()
        self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        rows = {cp.name: cp.is_overseas
                for cp in self.session.query(ChallengePlayer).all()}
        self.assertFalse(rows["Virat Kohli"])
        self.assertTrue(rows["Rashid Khan"])
        self.assertTrue(rows["Tim David"])

    def test_the_engine_can_read_what_we_wrote(self):
        """The published blob has to satisfy cipl_match.cp_to_player_dict.

        That function — not the master players table — is where the match engine
        gets every rating and handedness from, so a drifted key set would give a
        drafted squad silently wrong numbers rather than an error.
        """
        from models import ChallengePlayer
        from services.cipl_match import cp_to_player_dict
        self._finish()
        self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        cp = (self.session.query(ChallengePlayer)
              .filter(ChallengePlayer.name == "Rashid Khan").one())
        data = cp_to_player_dict(cp)
        self.assertEqual(data["name"], "Rashid Khan")
        self.assertEqual(data["rating"], 93)
        self.assertEqual(data["bat_rating"], 40)
        self.assertEqual(data["bowl_rating"], 94)
        self.assertEqual(data["category"], "Bowler")
        self.assertEqual(data["roster_id"], cp.id)

    def test_the_draft_only_keys_ride_along(self):
        import json
        from models import ChallengePlayer
        self._finish()
        self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        cp = (self.session.query(ChallengePlayer)
              .filter(ChallengePlayer.name == "Virat Kohli").one())
        blob = json.loads(cp.details_json)
        self.assertEqual(blob["tier"], "Platinum")
        self.assertTrue(blob["icon_eligible"])
        self.assertEqual(blob["gender"], "Male")

    def test_publishing_twice_re_syncs_rather_than_duplicating(self):
        """So a correction made with /dundo can simply be republished."""
        from models import ChallengeLeague, ChallengePlayer
        self._finish()
        first = self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        before = self.session.query(ChallengePlayer).count()
        second = self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.session.query(ChallengePlayer).count(), before)
        self.assertEqual(
            self.session.query(ChallengeLeague)
            .filter(ChallengeLeague.id == first.id).count(), 1)

    def test_a_passed_slot_is_simply_absent_from_the_squad(self):
        from models import ChallengePlayer, ChallengeTeam
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        for _ in range(5):
            current = self.ds.current_pick(self.session, self.draft)
            self.ds.skip_pick(self.session, self.draft, current)
        league = self.ds.publish_to_league(self.session, self.draft)
        self.session.flush()
        mumbai = (self.session.query(ChallengeTeam)
                  .filter(ChallengeTeam.league_id == league.id,
                          ChallengeTeam.name == "Mumbai Mavericks").one())
        self.assertEqual(
            [cp.name for cp in self.session.query(ChallengePlayer)
             .filter(ChallengePlayer.team_id == mumbai.id).all()],
            ["Virat Kohli"])


# ══════════════════════════════════════════════════════════════════════
# What the group actually sees
# ══════════════════════════════════════════════════════════════════════

class RenderTests(DraftCase):

    def test_the_pick_card_carries_the_squad_and_the_next_turn(self):
        self.go_live()
        done = self.pick("Virat Kohli", by=ALICE)
        nxt = self.ds.current_pick(self.session, self.draft)
        body = self.ds.render_pick(self.session, self.draft, done,
                                   self.player("Virat Kohli"), next_pick=nxt,
                                   next_mention="@bob")
        self.assertIn("MUMBAI MAVERICKS", body)
        self.assertIn("R1 P1", body)
        self.assertIn("Virat Kohli", body)
        self.assertIn("Updated Squad", body)
        self.assertIn("R1 P2", body)
        self.assertIn("@bob", body)

    def test_spending_a_high_slot_on_a_lower_tier_is_said_out_loud(self):
        """It is legal and irreversible, so it must not be discovered later."""
        self.go_live()
        done = self.pick("Mukesh Kumar", by=ALICE)
        body = self.ds.render_pick(self.session, self.draft, done,
                                   self.player("Mukesh Kumar"))
        self.assertIn("Platinum slot used on a Bronze player", body)

    def test_a_same_tier_pick_gets_no_warning(self):
        self.go_live()
        done = self.pick("Virat Kohli", by=ALICE)
        body = self.ds.render_pick(self.session, self.draft, done,
                                   self.player("Virat Kohli"))
        self.assertNotIn("slot used on", body)

    def test_names_are_escaped(self):
        """Player and team names are admin-supplied and land inside HTML."""
        self.load_pool(rows=[
            ["<b>Bold</b> Player", "80", "Gold", "0", "M", "Indian", "Batsman",
             "India", "R", "R", "Medium", "80", "10"]])
        self.load_order(rows=[["1", "1", "Gold", "A & B <XI>", "Sam", "111"]])
        self.draft.chat_id = -100_999
        pick = self.ds.start(self.session, self.draft)
        done = self.ds.make_pick(self.session, self.draft, pick,
                                 self.player("<b>Bold</b> Player"),
                                 by_tg_id=111)
        body = self.ds.render_pick(self.session, self.draft, done,
                                   self.player("<b>Bold</b> Player"))
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt; Player", body)
        self.assertIn("A &amp; B &lt;XI&gt;", body)
        self.assertNotIn("<b>Bold</b>", body)

    def test_the_squad_readout_shows_slot_progress_and_the_overseas_count(self):
        self.draft.max_overseas = 2
        self.go_live()
        self.pick("Rashid Khan", by=ALICE)
        body = self.ds.render_squad(self.session, self.draft,
                                    self.team("Mumbai Mavericks"))
        self.assertIn("Squad 1/3", body)
        self.assertIn("Platinum — 0/1", body)
        self.assertIn("1/2 overseas", body)

    def test_the_squad_readout_names_an_unmet_minimum(self):
        self.draft.role_minimums_json = '{"Wicket Keeper": 1}'
        self.go_live()
        body = self.ds.render_squad(self.session, self.draft,
                                    self.team("Mumbai Mavericks"))
        self.assertIn("Still needs", body)
        self.assertIn("Wicket Keeper", body)

    def test_the_board_names_who_is_on_the_clock(self):
        self.go_live()
        body = self.ds.render_board(self.session, self.draft, mention="@alice")
        self.assertIn("Test Draft", body)
        self.assertIn("Picks 0/6", body)
        self.assertIn("MUMBAI MAVERICKS", body)
        self.assertIn("@alice", body)

    def test_an_auto_pick_is_marked_on_the_board(self):
        pick = self.go_live()
        self.ds.resolve_expired(self.session, self.draft, pick)
        self.assertIn("⏱", self.ds.render_board(self.session, self.draft))

    def test_the_bowling_line_reads_like_cricket(self):
        self.load_pool()
        self.assertEqual(self.ds.bowling_line(self.player("Jasprit Bumrah")),
                         "Right-arm Fast")
        self.assertEqual(self.ds.bowling_line(self.player("Jos Buttler")), "")
