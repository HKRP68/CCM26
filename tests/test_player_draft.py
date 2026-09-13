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
# Everything reached from here that caches a reference to ``database`` /
# ``models``. The handler modules are in the list because
# ``tests/test_draft_trade.py`` imports them against its OWN temporary
# database: left cached, ``handlers.draft``'s ``get_session`` and its
# ``DraftError`` are a different object from the freshly imported ones, so this
# module's command tests would query the wrong file and fail to catch the
# refusals it raises. Whichever of the two suites runs second was the one that
# broke; both now reload the whole set.
_MODULE_NAMES = ("database", "models", "config",
                 "services.draft_service", "services.draft_trade_service",
                 "services.draft_scheduler", "services.xlsx_reader",
                 "handlers.draft", "handlers.draft_trade")

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


class HomeCountryTests(DraftCase):
    """Who counts as *home*, which is decided by the pool's country column.

    It used to be decided by an ``indian_status`` label, which meant a sheet
    without that column flagged the entire pool as home — eleven "Indians" from
    four countries, and an overseas cap that never refused anything — and a
    draft whose home country wasn't India read labels that meant nothing
    against it.
    """

    # The bug, in one sheet: no indian_status column at all.
    NO_STATUS_HEADER = ["name", "rating", "tier", "category", "country"]
    NO_STATUS_ROWS = [
        ["Home One", "90", "Gold", "Batsman", "India"],
        ["Home Two", "88", "Gold", "Batsman", "IND"],
        ["Home Three", "86", "Gold", "Batsman", "Indian"],
        ["Away One", "89", "Gold", "Bowler", "Afghanistan"],
        ["Away Two", "87", "Gold", "Bowler", "England"],
    ]

    def load_countries(self, rows=None):
        return self.ds.import_pool(
            self.session, self.draft,
            [list(self.NO_STATUS_HEADER)] +
            [list(r) for r in (rows if rows is not None else self.NO_STATUS_ROWS)])

    def flags(self):
        from models import DraftPlayer
        return {p.name: p.is_indian for p in
                self.session.query(DraftPlayer)
                .filter(DraftPlayer.draft_id == self.draft.id).all()}

    def test_a_pool_with_no_status_column_is_flagged_by_country(self):
        self.load_countries()
        self.assertEqual(self.flags(), {"Home One": True, "Home Two": True,
                                        "Home Three": True, "Away One": False,
                                        "Away Two": False})

    def test_the_home_country_is_the_drafts_own_not_india(self):
        self.draft.home_country = "England"
        self.load_countries()
        self.assertEqual(self.flags(), {"Home One": False, "Home Two": False,
                                        "Home Three": False, "Away One": False,
                                        "Away Two": True})

    def test_the_country_column_beats_a_label_that_no_longer_applies(self):
        """An India-centric sheet re-used for an England draft."""
        self.draft.home_country = "England"
        self.load_pool(rows=[
            ["Buttler", "92", "Gold", "0", "M", "Indian", "Batsman", "England",
             "R", "R", "Medium", "92", "10"],
            ["Kohli", "97", "Gold", "0", "M", "Indian", "Batsman", "India",
             "R", "R", "Medium", "97", "10"],
        ])
        self.assertTrue(self.player("Buttler").is_indian)
        self.assertFalse(self.player("Kohli").is_indian)

    def test_spellings_of_one_country_fold_together(self):
        for spelling in ("India", "india", " IND ", "Ind.", "Indian", "bharat"):
            self.assertIs(self.ds.is_home_country(spelling, "India"), True,
                          spelling)
        for spelling in ("RSA", "South African", "south  africa"):
            self.assertIs(self.ds.is_home_country(spelling, "South Africa"),
                          True, spelling)
        self.assertIs(self.ds.is_home_country("England", "India"), False)

    def test_a_country_nobody_can_read_is_not_guessed_at(self):
        """``None`` is the answer that stops a blank cell flagging a whole pool."""
        for blank in ("", "   ", "Unknown", "N/A", "-", "TBD"):
            self.assertIsNone(self.ds.is_home_country(blank, "India"), blank)
        self.assertIsNone(self.ds.is_home_country("India", ""))

    def test_an_unreadable_country_falls_back_to_the_status_column(self):
        self.load_pool(rows=[
            ["No Country A", "80", "Gold", "0", "M", "Overseas", "Batsman",
             "Unknown", "R", "R", "Medium", "80", "10"],
            ["No Country B", "80", "Gold", "0", "M", "Domestic", "Batsman",
             "", "R", "R", "Medium", "80", "10"],
        ])
        self.assertFalse(self.player("No Country A").is_indian)
        self.assertTrue(self.player("No Country B").is_indian)

    def test_a_row_with_neither_is_counted_as_home(self):
        """What the column defaults to — and the only case left that guesses."""
        self.load_pool(rows=[
            ["Mystery", "80", "Gold", "0", "M", "", "Batsman", "",
             "R", "R", "Medium", "80", "10"]])
        self.assertTrue(self.player("Mystery").is_indian)

    def test_the_overseas_cap_counts_the_countries_not_the_labels(self):
        """The bug's real cost: a cap that never refused anything."""
        self.draft.max_overseas = 1
        self.load_countries()
        self.load_order(rows=[["1", "1", "Gold", "Solo", "Sam", "111"],
                              ["1", "2", "Gold", "Solo", "Sam", "111"]])
        self.draft.chat_id = -100_111
        self.ds.start(self.session, self.draft)
        self.pick("Away One", by=111)
        with self.assertRaises(self.ds.DraftError) as caught:
            self.ds.validate_pick(self.session, self.draft,
                                  self.ds.current_pick(self.session, self.draft),
                                  self.player("Away Two"))
        self.assertIn("overseas", str(caught.exception))


class HomeCountryResyncTests(DraftCase):
    """Fixing a draft that is already running, without re-uploading its pool.

    Replacing the pool is refused once picking has started, so a draft imported
    under the old rule needs a way to be corrected in place. ``/dhome`` and
    ``migrate_draft_home_country.py`` both call ``resync_home_status``.
    """

    def wrongly_flagged(self):
        """The pool as the old importer left it: everybody home."""
        self.load_pool()
        from models import DraftPlayer
        (self.session.query(DraftPlayer)
         .filter(DraftPlayer.draft_id == self.draft.id)
         .update({"is_indian": True}, synchronize_session=False))
        self.session.flush()

    def test_it_re_flags_the_pool_from_the_countries(self):
        self.wrongly_flagged()
        changed, unknown = self.ds.resync_home_status(self.session, self.draft)
        self.assertEqual((changed, unknown), (3, 0))
        self.assertFalse(self.player("Rashid Khan").is_indian)
        self.assertFalse(self.player("Jos Buttler").is_indian)
        self.assertFalse(self.player("Tim David").is_indian)
        self.assertTrue(self.player("Virat Kohli").is_indian)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.wrongly_flagged()
        self.ds.resync_home_status(self.session, self.draft)
        self.assertEqual(self.ds.resync_home_status(self.session, self.draft),
                         (0, 0))

    def test_a_player_with_no_readable_country_is_left_alone(self):
        """So a flag an admin fixed by hand survives the sweep."""
        self.load_pool(rows=[
            ["Stateless", "80", "Gold", "0", "M", "Overseas", "Batsman",
             "Unknown", "R", "R", "Medium", "80", "10"]])
        self.assertFalse(self.player("Stateless").is_indian)
        changed, unknown = self.ds.resync_home_status(self.session, self.draft)
        self.assertEqual((changed, unknown), (0, 1))
        self.assertFalse(self.player("Stateless").is_indian)

    def test_changing_the_home_country_re_flags_everyone(self):
        self.load_pool()
        country, changed, _unknown = self.ds.set_home_country(
            self.session, self.draft, " england ")
        self.assertEqual(country, "england")
        self.assertEqual(changed, 6)
        self.assertTrue(self.player("Jos Buttler").is_indian)
        self.assertFalse(self.player("Virat Kohli").is_indian)

    def test_a_blank_home_country_falls_back_rather_than_emptying(self):
        self.load_pool()
        country, _changed, _unknown = self.ds.set_home_country(
            self.session, self.draft, "   ")
        self.assertEqual(country, self.ds.DEFAULT_HOME_COUNTRY)

    def test_the_counts_report_what_a_resync_would_move(self):
        self.wrongly_flagged()
        home, overseas, unknown, wrong = self.ds.home_status_counts(
            self.session, self.draft)
        self.assertEqual((home, overseas, unknown, wrong), (8, 0, 0, 3))
        self.ds.resync_home_status(self.session, self.draft)
        self.assertEqual(self.ds.home_status_counts(self.session, self.draft),
                         (5, 3, 0, 0))

    def test_squads_already_picked_keep_their_players(self):
        """Re-flagging changes who counts as overseas, not who is on a team."""
        self.wrongly_flagged()
        self.load_order()
        self.draft.chat_id = -100_222
        self.ds.start(self.session, self.draft)
        self.pick("Virat Kohli", by=ALICE)
        self.pick("Jasprit Bumrah", by=BOB)
        self.pick("Rashid Khan", by=ALICE)
        mumbai = self.team("Mumbai Mavericks")
        self.assertEqual(self.ds.overseas_count(self.session, mumbai.id), 0)
        self.ds.resync_home_status(self.session, self.draft)
        self.assertEqual(self.ds.overseas_count(self.session, mumbai.id), 1)
        self.assertEqual({p.name for p in self.ds.squad(self.session, mumbai.id)},
                         {"Virat Kohli", "Rashid Khan"})

    def test_the_home_flag_follows_the_draft(self):
        self.assertEqual(self.ds.home_flag(self.draft), "🇮🇳")
        self.draft.home_country = "Australia"
        self.assertEqual(self.ds.home_flag(self.draft), "🇦🇺")
        self.draft.home_country = "Atlantis"
        self.assertEqual(self.ds.home_flag(self.draft), self.ds.DEFAULT_HOME_FLAG)


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


class RandomGrantTests(DraftCase):
    """``/dautopick`` — the owner granting an absent team a pick of its tier.

    The clock's ``auto_pick`` is deliberately deterministic so its choice can be
    explained afterwards. Granting picks to offline teams with that same rule
    hands every one of them the middle of the band, so this one is random —
    which makes the rules it must *still* obey the thing worth pinning: the
    allotted tier, the overseas cap, the role minimums, and never wedging.
    """

    def test_it_picks_from_the_allotted_tier(self):
        self.go_live()
        gold = self.slot(2, 1)
        for _ in range(20):
            chosen = self.ds.random_pick(self.session, self.draft, gold)
            self.assertEqual(chosen.tier, "Gold")

    def test_it_is_actually_random_within_the_tier(self):
        """Otherwise it is just a second, slower ``auto_pick``."""
        self.go_live()
        gold = self.slot(2, 1)
        seen = {self.ds.random_pick(self.session, self.draft, gold).name
                for _ in range(60)}
        self.assertGreater(len(seen), 1)
        self.assertEqual(seen, {"Rashid Khan", "Jos Buttler", "Sanju Samson"})

    def test_it_never_returns_an_illegal_player(self):
        self.draft.max_overseas = 0
        self.go_live()
        gold = self.slot(2, 1)
        for _ in range(20):
            chosen = self.ds.random_pick(self.session, self.draft, gold)
            self.assertTrue(chosen.is_indian)

    def test_it_obeys_a_role_minimum_that_is_about_to_become_unreachable(self):
        """One slot left and a keeper still owed: chance gets no say."""
        self.ds.set_role_minimums(self.draft, {"Wicket Keeper": 1})
        self.load_pool(rows=[
            ["Keeper G", "80", "Gold", "0", "M", "Indian", "wk", "India",
             "R", "R", "-", "80", "10"],
            ["Bat A", "82", "Gold", "0", "M", "Indian", "Batsman", "India",
             "R", "R", "Medium", "82", "10"],
            ["Bat B", "81", "Gold", "0", "M", "Indian", "Batsman", "India",
             "R", "R", "Medium", "81", "10"],
            ["Bat C", "79", "Gold", "0", "M", "Indian", "Batsman", "India",
             "R", "R", "Medium", "79", "10"],
        ])
        self.load_order(rows=[["1", "1", "Gold", "Solo", "Sam", "111"],
                              ["2", "1", "Gold", "Solo", "Sam", "111"]])
        self.draft.chat_id = -100_777
        self.ds.start(self.session, self.draft)
        self.pick("Bat A", by=111)
        last = self.ds.current_pick(self.session, self.draft)
        for _ in range(20):
            chosen = self.ds.random_pick(self.session, self.draft, last)
            self.assertEqual(chosen.name, "Keeper G")

    def test_it_steps_down_the_ladder_when_the_slot_tier_is_empty(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        self.pick("Jasprit Bumrah", by=BOB)         # Platinum now exhausted
        platinum_slot = self.slot(1, 1)
        platinum_slot.status = "pending"            # re-open it artificially
        self.session.flush()
        chosen = self.ds.random_pick(self.session, self.draft, platinum_slot)
        self.assertEqual(chosen.tier, "Gold")

    def test_it_ignores_the_owners_queue(self):
        """The queue is the clock's courtesy to an owner who said what they want.

        This command is for the owner who said nothing, and a granted pick that
        quietly followed a queue would be indistinguishable from one they made.
        """
        self.go_live()
        mumbai = self.team("Mumbai Mavericks")
        self.ds.queue_add(self.session, self.draft, mumbai,
                          self.player("Jasprit Bumrah"))
        platinum = self.ds.current_pick(self.session, self.draft)
        names = {self.ds.random_pick(self.session, self.draft, platinum).name
                 for _ in range(40)}
        self.assertEqual(names, {"Virat Kohli", "Jasprit Bumrah"})

    def test_a_granted_pick_is_recorded_as_an_auto_pick(self):
        """The board must never read as the owner's own choice."""
        pick = self.go_live()
        resolved, player = self.ds.resolve_random(self.session, self.draft, pick,
                                                  by_tg_id=999)
        self.assertTrue(resolved.is_auto)
        self.assertEqual(resolved.status, "done")
        self.assertEqual(resolved.picked_by_tg_id, 999)
        self.assertIsNotNone(player)
        self.assertEqual(player.picked_by_team_id, resolved.team_id)

    def test_it_moves_the_clock_on(self):
        pick = self.go_live()
        self.ds.resolve_random(self.session, self.draft, pick)
        nxt = self.ds.current_pick(self.session, self.draft)
        self.assertEqual((nxt.round_no, nxt.pick_no), (1, 2))

    def test_nothing_legal_left_passes_the_slot_instead_of_wedging(self):
        self.draft.max_overseas = 0
        self.load_pool(rows=[
            ["Only Overseas", "80", "Gold", "0", "M", "Overseas", "Batsman",
             "England", "R", "R", "Medium", "80", "10"]])
        self.load_order(rows=[["1", "1", "Gold", "Solo", "Sam", "111"]])
        self.draft.chat_id = -100_666
        pick = self.ds.start(self.session, self.draft)
        self.assertIsNone(self.ds.random_pick(self.session, self.draft, pick))
        resolved, player = self.ds.resolve_random(self.session, self.draft, pick)
        self.assertIsNone(player)
        self.assertEqual(resolved.status, "skipped")

    def test_a_seeded_run_is_reproducible(self):
        """``rng`` is injectable so a grant can be replayed in a test."""
        import random
        self.go_live()
        gold = self.slot(2, 1)
        first = self.ds.random_pick(self.session, self.draft, gold,
                                    rng=random.Random(7)).name
        second = self.ds.random_pick(self.session, self.draft, gold,
                                     rng=random.Random(7)).name
        self.assertEqual(first, second)


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
# The pool browser
# ══════════════════════════════════════════════════════════════════════

class SearchTests(DraftCase):
    """``/dsearch`` — "is he still there, and if not, who took him".

    The board's Available tab lists the best of what's left; this is the other
    question, asked all evening, about one player or one shape of player.
    """

    def rows(self, **kwargs):
        return self.ds.search_pool(self.session, self.draft, **kwargs)

    def names(self, **kwargs):
        return [p.name for p in self.rows(**kwargs)]

    def test_it_shows_taken_players_too_marked_as_taken(self):
        """Hiding them would leave "who got Kohli" unanswerable."""
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        body, _page, _pages = self.ds.render_search(
            self.session, self.draft, self.rows())
        self.assertIn("🔴", body)
        self.assertIn("🟢", body)
        self.assertIn("Virat Kohli", body)

    def test_a_taken_player_is_shown_with_who_holds_him(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        body, _page, _pages = self.ds.render_search(
            self.session, self.draft, self.rows(query="kohli"))
        team = self.team("Mumbai Mavericks")
        self.assertIn(team.short_name or team.name, body)

    def test_available_players_come_first(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)       # the pool's best rating
        self.assertNotEqual(self.names()[0], "Virat Kohli")
        self.assertEqual(self.names()[-1], "Virat Kohli")

    def test_the_filters_stack(self):
        self.load_pool()
        self.assertEqual(self.names(tier="Gold", role="Bowler"),
                         ["Rashid Khan"])

    def test_availability_narrows_both_ways(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        self.assertEqual(self.names(availability=self.ds.AVAIL_GONE),
                         ["Virat Kohli"])
        self.assertNotIn("Virat Kohli",
                         self.names(availability=self.ds.AVAIL_FREE))

    def test_home_and_overseas_are_filters_of_their_own(self):
        self.load_pool()
        self.assertEqual(set(self.names(home=False)),
                         {"Rashid Khan", "Jos Buttler", "Tim David"})
        self.assertNotIn("Rashid Khan", self.names(home=True))

    def test_a_query_matches_a_country_as_well_as_a_name(self):
        """"Who is left from Australia" is a question people actually ask."""
        self.load_pool()
        self.assertEqual(self.names(query="australia"), ["Tim David"])
        self.assertEqual(self.names(query="kohli"), ["Virat Kohli"])

    def test_the_query_language_is_just_words_in_any_order(self):
        self.load_pool()
        parsed = self.ds.parse_search_query(self.draft, "bowler plat available")
        self.assertEqual(parsed, ("Platinum", "Bowler", self.ds.AVAIL_FREE,
                                  None, ""))
        parsed = self.ds.parse_search_query(self.draft, "gold rashid")
        self.assertEqual(parsed, ("Gold", None, self.ds.AVAIL_ANY, None,
                                  "rashid"))
        parsed = self.ds.parse_search_query(self.draft, "overseas taken wk")
        self.assertEqual(parsed, (None, "Wicket Keeper", self.ds.AVAIL_GONE,
                                  False, ""))

    def test_an_unknown_word_is_a_name_not_a_filter(self):
        self.load_pool()
        _tier, _role, _avail, _home, name = self.ds.parse_search_query(
            self.draft, "bumrah")
        self.assertEqual(name, "bumrah")
        self.assertEqual(self.names(query=name), ["Jasprit Bumrah"])

    def test_a_page_past_the_end_is_clamped_not_empty(self):
        """A stale button must not hand somebody a blank list."""
        self.load_pool()
        _body, page, pages = self.ds.render_search(
            self.session, self.draft, self.rows(), page=99)
        self.assertEqual(page, pages - 1)

    def test_every_player_appears_on_exactly_one_page(self):
        self.load_pool()
        rows = self.rows()
        seen = []
        pages = max(1, -(-len(rows) // self.ds.SEARCH_PAGE))
        for page in range(pages):
            body, _p, _n = self.ds.render_search(self.session, self.draft, rows,
                                                 page=page)
            seen += [p.name for p in rows if p.name in body]
        self.assertEqual(sorted(seen), sorted(p.name for p in rows))

    def test_nothing_matching_says_so_rather_than_showing_an_empty_list(self):
        self.load_pool()
        body, _page, _pages = self.ds.render_search(
            self.session, self.draft, self.rows(query="nobody"))
        self.assertIn("Nothing in the pool matches", body)

    def test_one_players_card_says_where_he_stands(self):
        self.go_live()
        body = self.ds.render_search_one(self.session, self.draft,
                                         self.player("Virat Kohli"))
        self.assertIn("Still available", body)
        self.assertIn("Platinum", body)
        self.pick("Virat Kohli", by=ALICE)
        body = self.ds.render_search_one(self.session, self.draft,
                                         self.player("Virat Kohli"))
        self.assertIn("Picked by", body)
        self.assertIn("Mumbai Mavericks", body)

    def test_a_players_own_country_flag_is_shown_not_just_a_plane(self):
        """A column of ✈️ says "not from here"; 🇦🇫 says who they are."""
        self.load_pool()
        self.assertEqual(self.ds.pool_flag(self.player("Virat Kohli"),
                                           self.draft), "🇮🇳")
        self.assertEqual(self.ds.pool_flag(self.player("Rashid Khan"),
                                           self.draft), "🇦🇫")
        self.assertEqual(self.ds.pool_flag(self.player("Tim David"),
                                           self.draft), "🇦🇺")

    def test_an_unknown_country_falls_back_to_home_or_overseas(self):
        self.load_pool(rows=[
            ["Nowhere Man", "80", "Gold", "0", "M", "Overseas", "Batsman",
             "Unknown", "R", "R", "Medium", "80", "10"]])
        self.assertEqual(self.ds.pool_flag(self.player("Nowhere Man"),
                                           self.draft), self.ds.OVERSEAS_FLAG)

    def test_the_tier_summary_counts_what_is_left(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        summary = dict((tier, (free, total)) for tier, free, total
                       in self.ds.tier_summary(self.session, self.draft))
        self.assertEqual(summary["Platinum"], (1, 2))
        self.assertEqual(summary["Gold"], (3, 3))


# ══════════════════════════════════════════════════════════════════════
# The commands themselves
# ══════════════════════════════════════════════════════════════════════

import contextlib as _contextlib
import os as _os


@_contextlib.contextmanager
def _env(**values):
    saved = {key: _os.environ.get(key) for key in values}
    _os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = old


def _owner_ids(raw):
    """``services.admin_ids`` reads these on every check, so the env is enough."""
    return _env(OWNER_IDS=raw, BOT_OWNER_IDS=raw)


def _admin_ids(raw):
    return _env(BOT_ADMIN_IDS=raw, ADMIN_IDS=raw, ADMIN_USER_IDS=raw,
                SUDO_USERS=raw, OWNER_IDS=raw, ADMIN_CHAT_ID=raw)


class CommandTests(DraftCase):
    """``handlers/draft.py``, driven with a stub Update.

    Only the commands whose *gate* is the point — ``/dautopick`` is the one
    draft command an ordinary bot admin may not run — plus enough of ``/dhome``
    to prove its reply builds against a real pool. Every rule underneath is
    tested through the service, which is where the rules live.
    """

    def setUp(self):
        super().setUp()
        try:
            from handlers import draft as handler
        except Exception as exc:        # python-telegram-bot not installed
            self.skipTest(f"handlers.draft unavailable: {exc}")
        self.handler = handler
        self.replies = []
        self.replies_with_markup = []
        self.pressed_keyboard = None
        self.announced = []
        self.pinned = []
        self.unpinned = []

    def _update(self, user_id, args=(), chat_type="supergroup"):
        from types import SimpleNamespace

        async def reply_text(text, **kwargs):
            self.replies.append(text)
            self.replies_with_markup.append((text, kwargs.get("reply_markup")))
            return SimpleNamespace(message_id=1)

        async def send_message(chat_id=None, text="", **kwargs):
            self.announced.append(text)
            return SimpleNamespace(message_id=100 + len(self.announced))

        async def pin_chat_message(chat_id=None, message_id=None, **kwargs):
            self.pinned.append(message_id)
            return True

        async def unpin_chat_message(chat_id=None, message_id=None, **kwargs):
            self.unpinned.append(message_id)
            return True

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.draft.chat_id,
                                           type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=SimpleNamespace(reply_text=reply_text))
        context = SimpleNamespace(
            args=list(args),
            bot=SimpleNamespace(send_message=send_message,
                                pin_chat_message=pin_chat_message,
                                unpin_chat_message=unpin_chat_message))
        return update, context

    def run_command(self, command, user_id, args=(), chat_type="supergroup"):
        """Run one command handler and return everything it replied.

        The handler opens its own session, so this commits first and expires
        after — otherwise the test would be reading its own stale rows.
        """
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

    def last_keyboard(self):
        for _text, keyboard in reversed(self.replies_with_markup):
            if keyboard is not None:
                return keyboard
        return None

    @staticmethod
    def button(keyboard, label):
        """The button whose label contains ``label``, ignoring the ✅ marker."""
        for row in keyboard.inline_keyboard:
            for button in row:
                if label in button.text:
                    return button
        return None

    def press(self, callback_data, user_id, expect_alert=False):
        """Press one inline button; returns the edited text (or the alert)."""
        import asyncio
        from types import SimpleNamespace
        self.session.commit()
        edits, alerts = [], []

        async def answer(text=None, **kwargs):
            if text:
                alerts.append(text)

        async def edit_message_text(text, **kwargs):
            edits.append(text)
            self.pressed_keyboard = kwargs.get("reply_markup")

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
                self.handler.search_callback(update, SimpleNamespace(args=[])))
        finally:
            loop.close()
        self.session.expire_all()
        return "\n".join(alerts if expect_alert else edits)

    # ── /dautopick is owner-only ──

    def test_dautopick_refuses_a_plain_admin(self):
        """A bot admin has /dskip. Granting a team a player is the owner's call."""
        self.go_live()
        with _admin_ids("111,222"), _owner_ids("999"):
            body = self.run_command(self.handler.dautopick_handler, 111)
        self.assertIn("Only the bot owner", body)
        self.assertEqual(self.slot(1, 1).status, "pending")

    def test_dautopick_grants_the_owner_a_pick(self):
        self.go_live()
        with _owner_ids("999"):
            self.run_command(self.handler.dautopick_handler, 999)
        first = self.slot(1, 1)
        self.assertEqual(first.status, "done")
        self.assertTrue(first.is_auto)
        self.assertEqual(first.picked_by_tg_id, 999)
        self.assertIsNotNone(first.draft_player_id)
        # The room is told, and told that it was an auto-pick.
        self.assertIn("AUTO-PICK", "\n".join(self.announced))

    def test_dautopick_outside_the_bound_group_is_refused(self):
        self.go_live()
        with _owner_ids("999"):
            body = self.run_command(self.handler.dautopick_handler, 999,
                                    chat_type="private")
        self.assertIn("only work in the group", body)
        self.assertEqual(self.slot(1, 1).status, "pending")

    def test_dautopick_says_so_when_the_draft_is_not_live(self):
        self.go_live()
        self.ds.pause(self.session, self.draft)
        with _owner_ids("999"):
            body = self.run_command(self.handler.dautopick_handler, 999)
        self.assertIn("Paused", body)
        self.assertEqual(self.slot(1, 1).status, "pending")

    # ── /dsearch ──

    def test_dsearch_lists_the_pool_with_buttons(self):
        self.go_live()
        body = self.run_command(self.handler.dsearch_handler, 111)
        self.assertIn("Pool", body)
        self.assertIn("🟢", body)
        labels = [b.text for row in self.last_keyboard().inline_keyboard
                  for b in row]
        self.assertIn("💎 Platinum", labels)
        self.assertIn("🟢 Available", labels)

    def test_dsearch_filters_from_the_words_typed(self):
        self.go_live()
        body = self.run_command(self.handler.dsearch_handler, 111,
                                ["gold", "bowler"])
        self.assertIn("Rashid Khan", body)
        self.assertNotIn("Virat Kohli", body)

    def test_dsearch_on_one_name_answers_about_that_player(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        body = self.run_command(self.handler.dsearch_handler, 111, ["kohli"])
        self.assertIn("Picked by", body)
        self.assertIn("Mumbai Mavericks", body)

    def test_a_filter_button_redraws_the_same_message(self):
        """The point of the buttons: nobody retypes /dsearch to turn a page."""
        self.go_live()
        self.run_command(self.handler.dsearch_handler, 111)
        keyboard = self.last_keyboard()
        gold = self.button(keyboard, "🥇 Gold")
        edited = self.press(gold.callback_data, 111)
        self.assertIn("Rashid Khan", edited)
        self.assertNotIn("Virat Kohli", edited)

    def test_a_page_button_walks_the_list(self):
        self.go_live()
        # One more than a page holds, so there is a second page to turn to.
        self.load_pool(rows=[
            [f"Extra {n}", "70", "Bronze", "0", "M", "Indian", "Batsman",
             "India", "R", "R", "Medium", "70", "10"]
            for n in range(self.ds.SEARCH_PAGE)])
        self.run_command(self.handler.dsearch_handler, 111)
        nxt = self.button(self.last_keyboard(), "▶️")
        self.assertIsNotNone(nxt, "a pool over one page long should paginate")
        second = self.press(nxt.callback_data, 111)
        self.assertIn("Page 2", second)

    def test_a_button_from_an_older_pool_is_refused_not_crashed(self):
        self.go_live()
        self.assertIn("out of date", self.press("dr_srch_nonsense", 111,
                                                expect_alert=True))

    def test_the_taken_filter_shows_only_what_has_gone(self):
        self.go_live()
        self.pick("Virat Kohli", by=ALICE)
        self.run_command(self.handler.dsearch_handler, 111)
        taken = self.button(self.last_keyboard(), "🔴 Taken")
        body = self.press(taken.callback_data, 111)
        self.assertIn("Virat Kohli", body)
        self.assertNotIn("Rashid Khan", body)

    # ── whose buttons are whose ──
    #
    # Every draft keyboard belongs to the person whose command posted it. The
    # lock lives in the callback data, so these assert against
    # ``services.button_access`` rather than against this module's stubs — the
    # button has to refuse a stranger on its own, with nothing remembered about
    # the message it is attached to.

    @staticmethod
    def _pressed_by(callback_data, user_id):
        from types import SimpleNamespace
        from services import button_access
        return button_access.check_callback_owner(SimpleNamespace(
            callback_query=SimpleNamespace(
                data=callback_data,
                from_user=SimpleNamespace(id=user_id),
                message=SimpleNamespace(chat_id=-1, message_id=1))))

    def _assert_owned_by(self, keyboard, user_id):
        from services import button_access
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        self.assertTrue(buttons, "expected a keyboard with buttons")
        for button in buttons:
            with self.subTest(callback_data=button.callback_data):
                self.assertEqual(
                    button_access.owner_from_callback_data(button.callback_data),
                    user_id)
                self.assertTrue(self._pressed_by(button.callback_data, user_id))
                self.assertFalse(self._pressed_by(button.callback_data, CAROL))
                # 64 bytes is Telegram's hard limit for callback data; the
                # owner id now shares that budget with a player's name.
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_the_board_buttons_belong_to_whoever_asked_for_the_board(self):
        self.go_live()
        self.run_command(self.handler.dboard_handler, ALICE)
        self._assert_owned_by(self.last_keyboard(), ALICE)

    def test_the_pool_browser_belongs_to_whoever_ran_dsearch(self):
        self.go_live()
        self.run_command(self.handler.dsearch_handler, ALICE)
        self._assert_owned_by(self.last_keyboard(), ALICE)

    def test_a_filter_press_leaves_the_browser_still_yours(self):
        """Ownership has to survive a redraw, or it lapses on the first tap."""
        self.go_live()
        self.run_command(self.handler.dsearch_handler, ALICE)
        gold = self.button(self.last_keyboard(), "🥇 Gold")
        self.press(gold.callback_data, ALICE)
        self._assert_owned_by(self.pressed_keyboard, ALICE)

    def test_the_pick_buttons_belong_to_whoever_typed_the_name(self):
        """A pick is irreversible, so only the hands that typed it may finish."""
        self.go_live()
        self.load_pool(rows=[["Virat Sharma", "95", "Platinum", "0", "Male",
                             "Indian", "Batsman", "India", "R", "R", "Medium",
                             "94", "20"]])
        body = self.run_command(self.handler.pick_handler, ALICE, ["virat"])
        self.assertIn("more than one player", body)
        self._assert_owned_by(self.last_keyboard(), ALICE)

    def test_a_stranger_is_told_which_command_to_run_instead(self):
        from services import button_access
        self.go_live()
        self.run_command(self.handler.dboard_handler, ALICE)
        board = self.last_keyboard().inline_keyboard[0][0].callback_data

        message = button_access.blocked_message_for(board)
        self.assertIn("/dboard", message)
        self.assertNotEqual(message, button_access.BLOCKED_BUTTON_MESSAGE)

    def test_a_board_posted_before_this_shipped_is_not_bricked(self):
        """Untagged callback data still parses — old messages keep working."""
        self.assertEqual(self.handler._board_view("dr_view_pool"), "pool")
        state = self.handler._decode_search(self.draft,
                                            "dr_srch_~~a~~0~")
        self.assertIsNotNone(state)
        self.assertIsNone(state["owner"])
        self.assertIsNone(state["tier"])
        self.assertEqual(state["page"], 0)

    # ── the pinned pick ──

    def test_a_pick_is_pinned_and_the_previous_pin_dropped(self):
        self.go_live()
        with _owner_ids("999"):
            self.run_command(self.handler.dautopick_handler, 999)
            first = list(self.pinned)
            self.assertEqual(len(first), 1)
            self.run_command(self.handler.dautopick_handler, 999)
        self.assertEqual(len(self.pinned), 2)
        self.assertEqual(self.unpinned, [first[0]])
        self.session.refresh(self.draft)
        self.assertEqual(self.draft.pinned_message_id, self.pinned[-1])

    def test_dpin_off_stops_pinning_and_clears_the_pin(self):
        self.go_live()
        with _owner_ids("999"), _admin_ids("999"):
            self.run_command(self.handler.dautopick_handler, 999)
            pinned = list(self.pinned)
            body = self.run_command(self.handler.dpin_handler, 999, ["off"])
            self.assertIn("no longer be pinned", body)
            self.assertEqual(self.unpinned, pinned)
            self.run_command(self.handler.dautopick_handler, 999)
        self.assertEqual(self.pinned, pinned)      # nothing new was pinned
        self.session.refresh(self.draft)
        self.assertFalse(self.draft.pin_picks)

    def test_dpin_reports_the_state_and_turns_back_on(self):
        self.go_live()
        with _admin_ids("111"):
            self.assertIn("<b>on</b>",
                          self.run_command(self.handler.dpin_handler, 111))
            self.replies.clear()
            self.run_command(self.handler.dpin_handler, 111, ["off"])
            self.replies.clear()
            self.assertIn("will be pinned",
                          self.run_command(self.handler.dpin_handler, 111, ["on"]))
        self.session.refresh(self.draft)
        self.assertTrue(self.draft.pin_picks)

    def test_undo_drops_the_pin_for_the_pick_it_rolled_back(self):
        self.go_live()
        with _owner_ids("999"), _admin_ids("999"):
            self.run_command(self.handler.dautopick_handler, 999)
            pinned = list(self.pinned)
            self.run_command(self.handler.dundo_handler, 999)
        self.assertEqual(self.unpinned, pinned)
        self.session.refresh(self.draft)
        self.assertIsNone(self.draft.pinned_message_id)

    # ── /dhome ──

    def test_dhome_reports_the_pool_then_sets_the_country(self):
        self.go_live()
        with _admin_ids("111"):
            body = self.run_command(self.handler.dhome_handler, 111)
            self.assertIn("India", body)
            self.assertIn("5 home", body)
            self.replies.clear()
            body = self.run_command(self.handler.dhome_handler, 111, ["England"])
        self.assertIn("England", body)
        self.assertEqual(self.draft.home_country, "England")
        self.assertTrue(self.player("Jos Buttler").is_indian)
        self.assertFalse(self.player("Virat Kohli").is_indian)

    def test_dhome_sync_fixes_a_pool_flagged_under_the_old_rule(self):
        """The live-draft repair path: no re-upload, same home country."""
        from models import DraftPlayer
        self.go_live()
        (self.session.query(DraftPlayer)
         .filter(DraftPlayer.draft_id == self.draft.id)
         .update({"is_indian": True}, synchronize_session=False))
        with _admin_ids("111"):
            body = self.run_command(self.handler.dhome_handler, 111, ["sync"])
        self.assertIn("Re-flagged", body)
        self.assertEqual(self.draft.home_country, "India")
        self.assertFalse(self.player("Rashid Khan").is_indian)

    def test_dhome_refuses_a_non_admin(self):
        self.go_live()
        with _admin_ids("111"):
            body = self.run_command(self.handler.dhome_handler, 222, ["England"])
        self.assertIn("Only bot admins", body)
        self.assertEqual(self.draft.home_country, "India")


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
