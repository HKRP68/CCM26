"""Admin support actions on a Career Player: rename it, or delete it outright.

A career card is generated, not chosen — the owner never picks the name and can
never release the card — so the two things support has to be able to do are fix
a name and take the card away so its owner can build a new one. Both are only
correct if they leave everything they don't touch alone:

  • renaming changes the name and nothing else, and can never collide with
    another player's name — the same rule creation enforces
  • deleting really removes the row, its roster slot and its match record, frees
    the name, refunds the gems that were sunk into it, and leaves the owner able
    to run the /cmucareer wizard again from the first step
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG_IDS = itertools.count(94_001)
_NAME_SEQ = itertools.count(1)


def unique_name(prefix):
    """A unique career name built only from letters.

    Career names come from the name pool and never contain digits, so a test
    fixture must not either — a "Career Owner 12" could be renamed to itself and
    still be refused by the name rules, which would be the test's fault and not
    the code's.
    """
    number = next(_NAME_SEQ)
    suffix = ""
    while True:
        number, remainder = divmod(number - 1, 26)
        suffix = chr(ord("A") + remainder) + suffix
        if number == 0:
            return f"{prefix} {suffix}"

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.career_service",
                 "services.player_service")


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

    from database import get_session
    from models import CareerFace
    session = get_session()
    try:
        for slot in (1, 2):
            session.add(CareerFace(slot=slot, label=f"Face {slot}",
                                   sort_order=slot, is_active=True))
        session.commit()
    finally:
        session.close()


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


class CareerAdminActionTestCase(unittest.TestCase):
    """One user with one freshly created career player."""

    def setUp(self):
        from database import get_session
        from models import User
        from services import career_service as cs

        self.session = get_session()
        self.user = User(telegram_id=next(_TG_IDS), username="careerowner",
                         total_coins=1000, total_gems=5000, roster_count=0)
        self.session.add(self.user)
        self.session.flush()
        result = cs.create_career_player(
            self.session, self.user, name=unique_name("Career Owner"),
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertTrue(result["ok"], result.get("message"))
        self.player = result["player"]
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()


class RenameTests(CareerAdminActionTestCase):
    def test_a_rename_changes_the_name_and_nothing_else(self):
        from services import career_service as cs

        before = {attr: getattr(self.player, cs.attr_column(attr))
                  for attr in cs.ALL_ATTRS}
        face, country, rating = (self.player.career_face, self.player.country,
                                 self.player.rating)

        result = cs.rename_career_player(self.session, self.player, "Rahul Nair")
        self.session.commit()

        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["to"], "Rahul Nair")
        self.assertEqual(self.player.name, "Rahul Nair")
        self.assertEqual({attr: getattr(self.player, cs.attr_column(attr))
                          for attr in cs.ALL_ATTRS}, before)
        self.assertEqual(
            (self.player.career_face, self.player.country, self.player.rating),
            (face, country, rating))
        self.assertTrue(self.player.is_career)
        self.assertEqual(self.player.career_owner_user_id, self.user.id)

    def test_the_owner_still_owns_the_same_roster_row(self):
        from services import career_service as cs

        entry_before = cs.career_roster_entry(self.session, self.user.id)
        cs.rename_career_player(self.session, self.player, "Ishan Bose")
        self.session.commit()
        entry_after = cs.career_roster_entry(self.session, self.user.id)
        self.assertIsNotNone(entry_after)
        self.assertEqual(entry_after.id, entry_before.id)

    def test_whitespace_is_tidied_up(self):
        from services import career_service as cs
        result = cs.rename_career_player(self.session, self.player,
                                         "   Arjun    Menon  ")
        self.session.commit()
        self.assertTrue(result["ok"])
        self.assertEqual(self.player.name, "Arjun Menon")

    def test_a_name_another_player_already_carries_is_refused(self):
        from models import Player
        from services import career_service as cs

        self.session.add(Player(name="Virat Kohli", version="Base", rating=90,
                                category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Medium Pacer"))
        self.session.flush()

        original = self.player.name
        result = cs.rename_career_player(self.session, self.player, "virat kohli")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_name")
        self.assertEqual(self.player.name, original)

    def test_saving_the_same_name_is_reported_rather_than_treated_as_a_clash(self):
        """The player's own name must not read as somebody else's."""
        from services import career_service as cs
        result = cs.rename_career_player(self.session, self.player,
                                         self.player.name)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unchanged")

    def test_a_capitalisation_fix_is_a_real_change_not_a_self_clash(self):
        """"mccullum" → "McCullum" is exactly what this action exists for."""
        from services import career_service as cs

        first = cs.rename_career_player(self.session, self.player, "mccullum")
        self.session.commit()
        self.assertTrue(first["ok"], first.get("message"))

        second = cs.rename_career_player(self.session, self.player, "McCullum")
        self.session.commit()
        self.assertTrue(second["ok"], second.get("message"))
        self.assertEqual(self.player.name, "McCullum")

    def test_a_case_only_clash_with_another_player_is_still_refused(self):
        from models import Player
        from services import career_service as cs

        self.session.add(Player(name="Rohit Sharma", version="Base", rating=88,
                                category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Off Spinner"))
        self.session.flush()
        result = cs.rename_career_player(self.session, self.player,
                                         "ROHIT SHARMA")
        self.assertFalse(result["ok"])

    def test_names_that_are_not_names_are_refused(self):
        from services import career_service as cs
        for bad in ("", "   ", "A", "9 Lives", "Rahul <b>Nair</b>",
                    "Sanju 🏏", "x" * (cs.NAME_MAX + 1), ".Leading Dot"):
            with self.subTest(name=bad):
                result = cs.rename_career_player(self.session, self.player, bad)
                self.assertFalse(result["ok"], f"{bad!r} should be refused")
                self.assertTrue(result["message"])

    def test_the_punctuation_real_names_carry_is_allowed(self):
        from services import career_service as cs
        for good in ("M.S. Dhoni", "de Villiers", "O'Keefe", "Jean-Paul Duminy"):
            with self.subTest(name=good):
                result = cs.rename_career_player(self.session, self.player, good)
                self.assertTrue(result["ok"], result.get("message"))
                self.session.flush()

    def test_renaming_drops_the_cached_card(self):
        from services import career_service as cs
        self.player.gen_card_file_id = "cached-file-id"
        self.session.flush()
        cs.rename_career_player(self.session, self.player, "Kabir Rao")
        self.assertIsNone(self.player.gen_card_file_id)

    def test_an_ordinary_player_cannot_be_renamed_through_this_door(self):
        from models import Player
        from services import career_service as cs

        ordinary = Player(name="Ordinary Card", version="Base", rating=80,
                          category="Batsman", country="India", bat_hand="Right",
                          bowl_hand="Right", bowl_style="Fast")
        self.session.add(ordinary)
        self.session.flush()
        result = cs.rename_career_player(self.session, ordinary, "Renamed Card")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_career")
        self.assertEqual(ordinary.name, "Ordinary Card")


class DeleteTests(CareerAdminActionTestCase):
    def test_the_player_row_and_the_roster_slot_both_go(self):
        from models import Player, UserRoster
        from services import career_service as cs

        player_id = self.player.id
        result = cs.delete_career_player(self.session, self.player)
        self.session.commit()

        self.assertTrue(result["ok"])
        self.assertIsNone(self.session.query(Player).get(player_id))
        self.assertIsNone(cs.get_career_player(self.session, self.user.id))
        self.assertEqual(
            self.session.query(UserRoster)
            .filter(UserRoster.player_id == player_id).count(), 0)

    def test_the_roster_count_comes_back_down(self):
        from services import career_service as cs
        self.assertEqual(self.user.roster_count, 1)
        cs.delete_career_player(self.session, self.player)
        self.session.commit()
        self.assertEqual(self.user.roster_count, 0)

    def test_the_match_record_goes_with_the_card(self):
        from models import PlayerGameStats
        from services import career_service as cs

        player_id = self.player.id
        self.session.add(PlayerGameStats(user_id=self.user.id,
                                         player_id=player_id, runs=250,
                                         bat_inns=6))
        self.session.flush()

        cs.delete_career_player(self.session, self.player)
        self.session.commit()
        self.assertEqual(
            self.session.query(PlayerGameStats)
            .filter(PlayerGameStats.player_id == player_id).count(), 0)

    def test_the_invested_gems_come_back_by_default(self):
        from services import career_service as cs

        cs.upgrade_attribute(self.session, self.user, "technique", 3)
        self.session.commit()
        invested = cs.total_invested(self.player)
        self.assertEqual(invested, 79 + 80 + 81)
        balance = self.user.total_gems

        result = cs.delete_career_player(self.session, self.player)
        self.session.commit()
        self.assertEqual(result["refunded"], invested)
        self.assertEqual(self.user.total_gems, balance + invested)

    def test_the_refund_can_be_withheld(self):
        from services import career_service as cs

        cs.upgrade_attribute(self.session, self.user, "power", 2)
        self.session.commit()
        balance = self.user.total_gems

        result = cs.delete_career_player(self.session, self.player,
                                         refund_gems=False)
        self.session.commit()
        self.assertEqual(result["refunded"], 0)
        self.assertEqual(self.user.total_gems, balance)

    def test_coins_are_never_paid_out_for_a_card_that_cannot_be_sold(self):
        from services import career_service as cs
        coins = self.user.total_coins
        cs.delete_career_player(self.session, self.player)
        self.session.commit()
        self.assertEqual(self.user.total_coins, coins)

    def test_the_weekly_streak_belongs_to_the_user_and_survives(self):
        from services import career_service as cs
        self.user.career_weekly_streak = 3
        self.user.career_weekly_best_streak = 5
        self.session.flush()
        cs.delete_career_player(self.session, self.player)
        self.session.commit()
        self.assertEqual(self.user.career_weekly_streak, 3)
        self.assertEqual(self.user.career_weekly_best_streak, 5)

    def test_the_owner_can_create_a_new_career_player_afterwards(self):
        """The whole point: deletion must leave /cmucareer able to start again."""
        from services import career_service as cs

        cs.delete_career_player(self.session, self.player)
        self.session.commit()

        again = cs.create_career_player(
            self.session, self.user, name=unique_name("Second Innings"),
            country="India", bat_hand="Left", bowl_hand="Left",
            bowl_style="Leg Spinner", face_slot=2)
        self.session.commit()
        self.assertTrue(again["ok"], again.get("message"))
        self.assertEqual(cs.get_career_player(self.session, self.user.id).id,
                         again["player"].id)
        self.assertEqual(self.user.roster_count, 1)
        self.assertEqual(again["player"].rating, cs.CAREER_START)

    def test_the_deleted_name_is_free_to_be_used_again(self):
        from services import career_service as cs
        from services.career_name_service import name_is_taken

        name = self.player.name
        cs.delete_career_player(self.session, self.player)
        self.session.commit()
        self.assertFalse(name_is_taken(self.session, name))

    def test_the_rest_of_the_squad_is_renumbered_not_left_with_a_hole(self):
        from models import Player, UserRoster
        from services import career_service as cs

        # Two ordinary cards either side of the career card's slot 1.
        for index, name in enumerate(("Squad One", "Squad Two"), start=2):
            card = Player(name=unique_name(name), version="Base",
                          rating=80, category="Batsman", country="India",
                          bat_hand="Right", bowl_hand="Right", bowl_style="Fast")
            self.session.add(card)
            self.session.flush()
            self.session.add(UserRoster(user_id=self.user.id, player_id=card.id,
                                        order_position=index))
        self.user.roster_count = 3
        self.session.flush()

        cs.delete_career_player(self.session, self.player)
        self.session.commit()

        positions = sorted(
            row.order_position for row in
            self.session.query(UserRoster)
            .filter(UserRoster.user_id == self.user.id).all())
        self.assertEqual(positions, [1, 2])
        self.assertEqual(self.user.roster_count, 2)

    def test_an_ordinary_player_cannot_be_deleted_through_this_door(self):
        from models import Player
        from services import career_service as cs

        ordinary = Player(name=unique_name("Not Career"), version="Base",
                          rating=80, category="Batsman", country="India",
                          bat_hand="Right", bowl_hand="Right", bowl_style="Fast")
        self.session.add(ordinary)
        self.session.flush()
        result = cs.delete_career_player(self.session, ordinary)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_career")
        self.assertIsNotNone(self.session.query(Player).get(ordinary.id))


if __name__ == "__main__":
    unittest.main()
