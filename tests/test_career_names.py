"""Career Player names must be new — never a name a card already carries.

The wizard combines a first name and a surname from a per-country pool, so the
same two letters can be picked by many users. The generator therefore has to
check every candidate against the whole ``players`` table, and the letter
buttons must only offer letters that can actually produce a name.
"""

import os
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.career_name_service")


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
    import models  # noqa: F401

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


class NamePoolTests(unittest.TestCase):
    COUNTRY = "Testland"

    def setUp(self):
        from database import get_session
        from models import CareerNamePool
        from services.career_name_service import FIRST, SURNAME, add_names

        self.session = get_session()
        self.session.query(CareerNamePool).delete()
        add_names(self.session, self.COUNTRY, FIRST, ["Arjun", "Kabir", "Rohan"])
        add_names(self.session, self.COUNTRY, SURNAME, ["Verma", "Iyer"])
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_names_are_filed_under_their_first_letter(self):
        from models import CareerNamePool
        rows = {r.value: r.letter for r in
                self.session.query(CareerNamePool).all()}
        self.assertEqual(rows["Arjun"], "A")
        self.assertEqual(rows["Verma"], "V")

    def test_adding_the_same_name_twice_is_a_no_op(self):
        from models import CareerNamePool
        from services.career_name_service import FIRST, add_names
        added, skipped = add_names(self.session, self.COUNTRY, FIRST, ["Arjun"])
        self.session.commit()
        self.assertEqual((added, skipped), (0, 1))
        count = (self.session.query(CareerNamePool)
                 .filter(CareerNamePool.country == self.COUNTRY,
                         CareerNamePool.name_kind == FIRST).count())
        self.assertEqual(count, 3)

    def test_blank_entries_are_skipped(self):
        from services.career_name_service import FIRST, add_names
        added, skipped = add_names(self.session, self.COUNTRY, FIRST,
                                   ["", "   ", "\n"])
        self.assertEqual(added, 0)
        self.assertEqual(skipped, 3)

    def test_only_letters_with_names_are_offered(self):
        from services.career_name_service import (available_initials,
                                                  available_surname_letters)
        self.assertEqual(available_initials(self.session, self.COUNTRY),
                         ["A", "K", "R"])
        self.assertEqual(available_surname_letters(self.session, self.COUNTRY),
                         ["I", "V"])

    def test_a_country_needs_both_kinds_to_be_offered(self):
        from services.career_name_service import (FIRST, add_names,
                                                  available_countries)
        add_names(self.session, "Halfland", FIRST, ["Solo"])
        self.session.commit()
        countries = available_countries(self.session, require_flag=False)
        self.assertIn(self.COUNTRY, countries)
        self.assertNotIn("Halfland", countries,
                         "a country with no surnames can't build a name")

    def test_generated_names_use_the_chosen_letters(self):
        from services.career_name_service import generate_name
        name = generate_name(self.session, self.COUNTRY, "A", "V")
        self.assertEqual(name, "Arjun Verma")

    def test_a_name_already_in_use_is_never_returned(self):
        from models import Player
        from services.career_name_service import generate_name

        self.session.add(Player(name="Arjun Verma", version="Base card",
                                rating=80, category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast"))
        self.session.flush()
        # "A" + "V" now has exactly one combination and it is taken.
        self.assertIsNone(generate_name(self.session, self.COUNTRY, "A", "V"))

    def test_generation_falls_through_to_a_free_combination(self):
        from models import Player
        from services.career_name_service import generate_name

        self.session.add(Player(name="Arjun Verma", version="Base card",
                                rating=80, category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast"))
        self.session.flush()
        # "A" + "I" is still free, so the generator must find it.
        self.assertEqual(generate_name(self.session, self.COUNTRY, "A", "I"),
                         "Arjun Iyer")

    def test_the_taken_check_ignores_case(self):
        from models import Player
        from services.career_name_service import name_is_taken

        self.session.add(Player(name="Kabir Iyer", version="Base card",
                                rating=80, category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast"))
        self.session.flush()
        self.assertTrue(name_is_taken(self.session, "kabir iyer"))
        self.assertTrue(name_is_taken(self.session, "KABIR IYER"))
        self.assertFalse(name_is_taken(self.session, "Kabir Verma"))

    def test_repeated_generation_never_collides_with_an_existing_player(self):
        """The property that matters: 200 draws, never a duplicate name."""
        from models import Player
        from services.career_name_service import generate_name, name_is_taken

        for index in range(200):
            name = generate_name(self.session, self.COUNTRY, "K", "V")
            if name is None:
                break
            self.assertFalse(name_is_taken(self.session, name))
            # Claim it, so the next draw has to find something else.
            self.session.add(Player(name=name, version="Career", rating=78,
                                    category="All-rounder", country="Testland",
                                    bat_hand="Right", bowl_hand="Right",
                                    bowl_style="Fast", is_career=True))
            self.session.flush()
        else:
            self.fail("the single K+V combination should have run out")

    def test_surprise_me_returns_a_usable_pair(self):
        from services.career_name_service import generate_name, random_letters
        initial, surname_letter = random_letters(self.session, self.COUNTRY)
        self.assertIn(initial, ["A", "K", "R"])
        self.assertIn(surname_letter, ["I", "V"])
        self.assertIsNotNone(
            generate_name(self.session, self.COUNTRY, initial, surname_letter))

    def test_an_unknown_country_yields_nothing_rather_than_raising(self):
        from services.career_name_service import (available_initials,
                                                  generate_name)
        self.assertEqual(available_initials(self.session, "Nowhere"), [])
        self.assertIsNone(generate_name(self.session, "Nowhere", "A", "B"))

    def test_the_summary_flags_unusable_countries(self):
        from services.career_name_service import FIRST, add_names, pool_summary
        add_names(self.session, "Halfland", FIRST, ["Solo"])
        self.session.commit()
        summary = {row["country"]: row for row in pool_summary(self.session)}
        self.assertTrue(summary[self.COUNTRY]["usable"])
        self.assertFalse(summary["Halfland"]["usable"])
        self.assertEqual(summary[self.COUNTRY]["firsts"], 3)
        self.assertEqual(summary[self.COUNTRY]["surnames"], 2)


class SeedSplitTests(unittest.TestCase):
    """The mining step that turns real player names into pool entries."""

    def test_a_simple_name_splits_into_first_and_surname(self):
        from seed_career_names import split_name
        self.assertEqual(split_name("Sachin Tendulkar"),
                         ("Sachin", "Tendulkar"))

    def test_a_middle_name_is_dropped(self):
        from seed_career_names import split_name
        self.assertEqual(split_name("Kusal Janith Perera"),
                         ("Kusal", "Perera"))

    def test_surname_particles_stay_attached(self):
        from seed_career_names import split_name
        self.assertEqual(split_name("Rassie van der Dussen"),
                         ("Rassie", "van der Dussen"))

    def test_a_single_word_name_is_rejected(self):
        from seed_career_names import split_name
        self.assertEqual(split_name("Ashwin"), (None, None))
        self.assertEqual(split_name(""), (None, None))


if __name__ == "__main__":
    unittest.main()
