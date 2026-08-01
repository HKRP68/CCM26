"""The Career Player's ten attributes, what they cost, and the ratings they drive.

A career card is the one card a user grows themselves, so the arithmetic has to
be exactly what the player was promised:

  • every attribute starts at 78, and that means 78 Batting / 78 Bowling / 78 OVR
  • raising an attribute to N costs N gems — 78 → 79 is 79 💎
  • Batting is the mean of the five batting attributes, Bowling the mean of the
    five bowling ones, and OVR the mean of those two
  • nothing goes past 99, and nothing is bought without the gems to pay for it
"""

import itertools
import os
import sys
import tempfile
import unittest

# Telegram ids are unique, so hand each test its own rather than deriving
# one from id(self) — those collide once enough objects exist.
_TG_IDS = itertools.count(90_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.career_service")


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

    # The wizard only ever offers faces that exist and are active, and
    # create_career_player now rejects anything else — so the tests need a few
    # real CareerFace rows before they can create a career player.
    from database import get_session
    from models import CareerFace
    session = get_session()
    try:
        for slot in (1, 2, 3):
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


def _blank_player():
    """A career player object with every attribute at the starting value."""
    from models import Player
    from services import career_service as cs

    player = Player(name="Test Career", version=cs.CAREER_VERSION,
                    category=cs.CAREER_CATEGORY, country="India",
                    bat_hand="Right", bowl_hand="Right", bowl_style="Fast",
                    rating=cs.CAREER_START, is_career=True)
    for attr in cs.ALL_ATTRS:
        setattr(player, cs.attr_column(attr), cs.CAREER_START)
    cs.recompute_ratings(player)
    return player


class RatingDerivationTests(unittest.TestCase):
    def test_a_fresh_career_player_is_78_across_the_board(self):
        from services import career_service as cs
        player = _blank_player()
        self.assertEqual(
            (player.bat_rating, player.bowl_rating, player.rating),
            (cs.CAREER_START, cs.CAREER_START, cs.CAREER_START))

    def test_each_discipline_is_the_mean_of_its_five_attributes(self):
        from services import career_service as cs
        player = _blank_player()
        # Batting attributes at 80, 82, 84, 86, 88 -> mean 84.
        for attr, value in zip(cs.BAT_ATTRS, (80, 82, 84, 86, 88)):
            setattr(player, cs.attr_column(attr), value)
        cs.recompute_ratings(player)
        self.assertEqual(player.bat_rating, 84)
        self.assertEqual(player.bowl_rating, 78, "bowling must be untouched")

    def test_half_values_round_up_not_to_even(self):
        """Batting 83 with bowling 78 averages 80.5, which must show as 81.

        Python's built-in round() is banker's rounding and would give 80 here
        while giving 82 for 81.5 — arbitrary-looking on a card the owner is
        paying to improve.
        """
        from services import career_service as cs
        player = _blank_player()
        for attr in cs.BAT_ATTRS:
            setattr(player, cs.attr_column(attr), 83)
        cs.recompute_ratings(player)
        self.assertEqual((player.bat_rating, player.bowl_rating), (83, 78))
        self.assertEqual(player.rating, 81)

    def test_everything_maxed_is_99(self):
        from services import career_service as cs
        player = _blank_player()
        for attr in cs.ALL_ATTRS:
            setattr(player, cs.attr_column(attr), cs.CAREER_MAX)
        cs.recompute_ratings(player)
        self.assertEqual(
            (player.bat_rating, player.bowl_rating, player.rating),
            (cs.CAREER_MAX, cs.CAREER_MAX, cs.CAREER_MAX))

    def test_projection_does_not_mutate_the_player(self):
        from services import career_service as cs
        player = _blank_player()
        projected = cs.projected_ratings(player, "technique", 5)
        self.assertEqual(getattr(player, cs.attr_column("technique")),
                         cs.CAREER_START)
        self.assertGreaterEqual(projected["bat_rating"], player.bat_rating)


class CostTests(unittest.TestCase):
    def test_a_point_costs_its_own_new_value(self):
        from services import career_service as cs
        self.assertEqual(cs.upgrade_cost(79), 79)
        self.assertEqual(cs.upgrade_cost(99), 99)

    def test_taking_one_attribute_78_to_99_costs_1869(self):
        from services import career_service as cs
        total = sum(cs.upgrade_cost(v)
                    for v in range(cs.CAREER_START + 1, cs.CAREER_MAX + 1))
        self.assertEqual(total, 1869)

    def test_total_invested_is_zero_at_the_start(self):
        self.assertEqual(__import__("services.career_service", fromlist=["x"])
                         .total_invested(_blank_player()), 0)

    def test_maxing_every_attribute_costs_18690(self):
        from services import career_service as cs
        player = _blank_player()
        self.assertEqual(cs.cost_to_max(player), 18690)
        for attr in cs.ALL_ATTRS:
            setattr(player, cs.attr_column(attr), cs.CAREER_MAX)
        self.assertEqual(cs.total_invested(player), 18690)
        self.assertEqual(cs.cost_to_max(player), 0)

    def test_invested_matches_a_hand_computed_value(self):
        from services import career_service as cs
        player = _blank_player()
        # One attribute at 80 means the owner bought 79 and then 80.
        setattr(player, cs.attr_column("power"), 80)
        self.assertEqual(cs.total_invested(player), 79 + 80)

    def test_affordable_steps_stops_at_the_cap_and_the_balance(self):
        from services import career_service as cs
        player = _blank_player()
        steps, spent = cs.affordable_steps(player, "technique", 79 + 80)
        self.assertEqual((steps, spent), (2, 159))
        # A fortune still can't push past the 99 ceiling.
        steps, _spent = cs.affordable_steps(player, "technique", 10 ** 6)
        self.assertEqual(steps, cs.CAREER_MAX - cs.CAREER_START)


class UpgradeSpendTests(unittest.TestCase):
    """upgrade_attribute against real rows — gems really move, caps really hold."""

    def setUp(self):
        from database import get_session
        from models import User
        from services import career_service as cs

        self.session = get_session()
        self.user = User(telegram_id=next(_TG_IDS), username="career",
                         total_coins=0, total_gems=5000, roster_count=0)
        self.session.add(self.user)
        self.session.flush()
        result = cs.create_career_player(
            self.session, self.user, name=f"Career Tester {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertTrue(result["ok"], result.get("message"))
        self.player = result["player"]
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_one_point_charges_exactly_the_new_value(self):
        from services import career_service as cs
        before = self.user.total_gems
        result = cs.upgrade_attribute(self.session, self.user, "technique", 1)
        self.session.commit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["cost"], 79)
        self.assertEqual(self.user.total_gems, before - 79)
        self.assertEqual(getattr(self.player, cs.attr_column("technique")), 79)

    def test_several_points_charge_the_sum_of_each_new_value(self):
        from services import career_service as cs
        result = cs.upgrade_attribute(self.session, self.user, "power", 3)
        self.session.commit()
        self.assertEqual(result["cost"], 79 + 80 + 81)
        self.assertEqual(result["to"], 81)

    def test_ratings_follow_the_attributes(self):
        from services import career_service as cs
        for attr in cs.BAT_ATTRS:
            cs.upgrade_attribute(self.session, self.user, attr, 2)
        self.session.commit()
        self.assertEqual(self.player.bat_rating, 80)
        self.assertEqual(self.player.bowl_rating, 78)
        self.assertEqual(self.player.rating, 79)

    def test_a_spend_that_moves_the_ovr_says_so(self):
        """One attribute 78 → 83 lifts Batting to 79 and so the OVR to 79."""
        from services import career_service as cs
        result = cs.upgrade_attribute(self.session, self.user, "technique", 5)
        self.session.commit()
        self.assertTrue(result["rating_changed"])
        self.assertEqual(result["rating"], 79)
        self.assertEqual(result["rating"], self.player.rating)

    def test_a_spend_too_small_to_move_the_ovr_says_that_too(self):
        """A single point is only a fifth of a rating, so the card doesn't move."""
        from services import career_service as cs
        result = cs.upgrade_attribute(self.session, self.user, "technique", 1)
        self.session.commit()
        self.assertTrue(result["ok"])
        self.assertFalse(result["rating_changed"])

    def test_upgrading_clears_the_cached_card(self):
        from services import career_service as cs
        self.player.gen_card_file_id = "stale-file-id"
        cs.upgrade_attribute(self.session, self.user, "swing", 1)
        self.session.commit()
        self.assertIsNone(self.player.gen_card_file_id)

    def test_the_cap_holds(self):
        from services import career_service as cs
        setattr(self.player, cs.attr_column("timing"), cs.CAREER_MAX)
        self.session.commit()
        result = cs.upgrade_attribute(self.session, self.user, "timing", 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "maxed")

    def test_asking_past_the_cap_only_charges_up_to_it(self):
        from services import career_service as cs
        setattr(self.player, cs.attr_column("stamina"), 97)
        self.session.commit()
        result = cs.upgrade_attribute(self.session, self.user, "stamina", 50)
        self.session.commit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["to"], cs.CAREER_MAX)
        self.assertEqual(result["cost"], 98 + 99)

    def test_too_few_gems_is_refused_and_charges_nothing(self):
        from services import career_service as cs
        self.user.total_gems = 10
        self.session.commit()
        result = cs.upgrade_attribute(self.session, self.user, "pace", 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "insufficient_gems")
        self.assertEqual(self.user.total_gems, 10)
        self.assertEqual(getattr(self.player, cs.attr_column("pace")), 78)

    def test_an_unknown_attribute_is_refused(self):
        from services import career_service as cs
        result = cs.upgrade_attribute(self.session, self.user, "charisma", 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_attr")

    def test_zero_or_negative_steps_are_refused(self):
        from services import career_service as cs
        for steps in (0, -3):
            result = cs.upgrade_attribute(self.session, self.user, "swing", steps)
            self.assertFalse(result["ok"], f"steps={steps} should be refused")
            self.assertEqual(result["error"], "bad_steps")


class CreationTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import User

        self.session = get_session()
        self.user = User(telegram_id=next(_TG_IDS), username="dupe",
                         total_coins=0, total_gems=0, roster_count=0)
        self.session.add(self.user)
        self.session.flush()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_a_new_career_player_is_a_locked_all_rounder_at_78(self):
        from services import career_service as cs
        result = cs.create_career_player(
            self.session, self.user, name=f"Fresh Face {self.user.id}",
            country="Australia", bat_hand="Left", bowl_hand="Right",
            bowl_style="Off Spinner", face_slot=3)
        self.session.commit()
        player = result["player"]
        self.assertEqual(player.category, cs.CAREER_CATEGORY)
        self.assertEqual(player.version, cs.CAREER_VERSION)
        self.assertEqual(player.rating, cs.CAREER_START)
        self.assertEqual(player.career_face, "career_3")
        self.assertTrue(player.is_career)
        self.assertTrue(player.non_tradable, "must be barred from trading")
        self.assertTrue(player.restricted_from_buypl, "must be barred from /buypl")
        self.assertEqual(player.career_owner_user_id, self.user.id)

    def test_creation_adds_exactly_one_roster_row(self):
        from models import UserRoster
        from services import career_service as cs
        cs.create_career_player(
            self.session, self.user, name=f"Roster Row {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.session.commit()
        rows = (self.session.query(UserRoster)
                .filter(UserRoster.user_id == self.user.id).count())
        self.assertEqual(rows, 1)

    def test_a_second_career_player_is_refused(self):
        from services import career_service as cs
        cs.create_career_player(
            self.session, self.user, name=f"Only One {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.session.commit()
        again = cs.create_career_player(
            self.session, self.user, name=f"Second Try {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"], "exists")

    def test_a_name_another_player_already_uses_is_refused(self):
        from models import Player
        from services import career_service as cs

        self.session.add(Player(name="Taken Already", version="Base card",
                                rating=80, category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast"))
        self.session.flush()
        result = cs.create_career_player(
            self.session, self.user, name="taken already", country="India",
            bat_hand="Right", bowl_hand="Right", bowl_style="Fast", face_slot=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "name_taken")

    def test_a_face_slot_that_is_not_a_real_face_is_refused(self):
        """An unknown slot would be stored and then render on face 1 silently."""
        from services import career_service as cs
        for slot in (99, 0, -1, None, "banana"):
            result = cs.create_career_player(
                self.session, self.user, name=f"Bad Face {slot}",
                country="India", bat_hand="Right", bowl_hand="Right",
                bowl_style="Fast", face_slot=slot)
            self.assertFalse(result["ok"], f"slot={slot!r} should be refused")
            self.assertEqual(result["error"], "bad_face")

    def test_a_face_deactivated_mid_wizard_is_refused(self):
        from services import career_service as cs
        from services.career_face_service import set_face_fields

        set_face_fields(self.session, 2, is_active=False)
        self.session.flush()
        try:
            result = cs.create_career_player(
                self.session, self.user, name=f"Gone Face {self.user.id}",
                country="India", bat_hand="Right", bowl_hand="Right",
                bowl_style="Fast", face_slot=2)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "bad_face")
        finally:
            set_face_fields(self.session, 2, is_active=True)
            self.session.commit()

    def test_a_full_roster_is_refused(self):
        from config import MAX_ROSTER
        from models import Player, UserRoster
        from services import career_service as cs

        for index in range(MAX_ROSTER):
            player = Player(name=f"Filler {self.user.id}-{index}",
                            version="Base card", rating=70, category="Batsman",
                            country="India", bat_hand="Right",
                            bowl_hand="Right", bowl_style="Fast")
            self.session.add(player)
            self.session.flush()
            self.session.add(UserRoster(user_id=self.user.id,
                                        player_id=player.id,
                                        order_position=index + 1))
        self.session.flush()

        result = cs.create_career_player(
            self.session, self.user, name=f"No Room {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "roster_full")

    def test_the_roster_counter_is_derived_from_the_rows(self):
        """A drifted counter must not be compounded by the new card."""
        from models import Player, UserRoster
        from services import career_service as cs

        player = Player(name=f"Existing {self.user.id}", version="Base card",
                        rating=70, category="Batsman", country="India",
                        bat_hand="Right", bowl_hand="Right", bowl_style="Fast")
        self.session.add(player)
        self.session.flush()
        self.session.add(UserRoster(user_id=self.user.id, player_id=player.id,
                                    order_position=1))
        self.user.roster_count = 17   # drifted
        self.session.flush()

        result = cs.create_career_player(
            self.session, self.user, name=f"Counted {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.session.commit()
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(self.user.roster_count, 2)

    def test_the_database_refuses_a_second_card_even_without_the_check(self):
        """The partial unique index, not the pre-check, is the real guarantee.

        Two concurrent /cmucareer taps can both pass ``get_career_player`` and
        both reach the insert, so the index has to be what stops the second.
        """
        from sqlalchemy.exc import IntegrityError
        from models import Player
        from services import career_service as cs

        first = cs.create_career_player(
            self.session, self.user, name=f"Genuine {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.session.commit()
        self.assertTrue(first["ok"], first.get("message"))

        # Insert straight past the service-level check, as a racing session
        # that read the roster a moment earlier effectively would.
        self.session.add(Player(
            name=f"Sneaky {self.user.id}", version=cs.CAREER_VERSION, rating=78,
            category=cs.CAREER_CATEGORY, country="India", bat_hand="Right",
            bowl_hand="Right", bowl_style="Fast", is_career=True,
            career_owner_user_id=self.user.id, career_face="career_1"))
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()


if __name__ == "__main__":
    unittest.main()
