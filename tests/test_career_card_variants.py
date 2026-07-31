"""Career Player faces are card-template variants, and must not disturb the rest.

Each face the admin uploads is a complete blank card with the player's face
already on it, keyed ``career_<slot>``. That reuses the existing per-rarity
template machinery, so the risks worth pinning are: the career card picks its
own face, its layout is independent of Base/Star/Legend, it does not draw a
cutout over its baked-in face, and the three rarity cards are untouched.
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
                 "services.card_template_service")


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


class VariantKeyTests(unittest.TestCase):
    def test_career_keys_are_recognised(self):
        from services import card_template_service as cts
        self.assertTrue(cts.is_career_variant("career_1"))
        self.assertTrue(cts.is_career_variant("career_12"))
        self.assertFalse(cts.is_career_variant("base"))
        self.assertFalse(cts.is_career_variant("career"))
        self.assertFalse(cts.is_career_variant("career_x"))

    def test_the_slot_number_round_trips(self):
        from services import card_template_service as cts
        self.assertEqual(cts.career_variant_slot("career_7"), 7)
        self.assertIsNone(cts.career_variant_slot("legend"))

    def test_normalising_accepts_rarities_and_faces_but_nothing_else(self):
        from services import card_template_service as cts
        for variant in ("base", "star", "legend", "career_3"):
            self.assertEqual(cts.normalise_template_variant(variant), variant)
        self.assertEqual(cts.normalise_template_variant("nonsense"), "base")
        self.assertEqual(cts.normalise_template_variant(None), "base")

    def test_the_template_file_stem_matches_what_storage_mirrors(self):
        from services import card_template_service as cts
        self.assertEqual(cts.template_stem("base"), "template")
        self.assertEqual(cts.template_stem("career_2"), "template_career_2")


class PlayerVariantSelectionTests(unittest.TestCase):
    def _player(self, **kwargs):
        from models import Player
        defaults = dict(name="X", version="Base card", rating=80,
                        category="Batsman", country="India", bat_hand="Right",
                        bowl_hand="Right", bowl_style="Fast")
        defaults.update(kwargs)
        return Player(**defaults)

    def test_a_career_player_renders_on_its_own_face(self):
        from services.card_template_service import player_template_variant
        player = self._player(is_career=True, career_face="career_4",
                              version="Career")
        self.assertEqual(player_template_variant(player), "career_4")

    def test_the_career_face_wins_over_the_version_label(self):
        """A career card labelled "Legend" must still use its face, not legend."""
        from services.card_template_service import player_template_variant
        player = self._player(is_career=True, career_face="career_1",
                              version="Legend card")
        self.assertEqual(player_template_variant(player), "career_1")

    def test_a_career_player_with_a_broken_face_falls_back_to_base(self):
        from services.card_template_service import player_template_variant
        player = self._player(is_career=True, career_face="", version="Career")
        self.assertEqual(player_template_variant(player), "base")

    def test_ordinary_rarity_selection_is_unchanged(self):
        from services.card_template_service import player_template_variant
        self.assertEqual(
            player_template_variant(self._player(version="Base card")), "base")
        self.assertEqual(
            player_template_variant(self._player(version="Star Card")), "star")
        self.assertEqual(
            player_template_variant(self._player(version="Legend card")), "legend")


class VariantSettingsTests(unittest.TestCase):
    def test_rarity_layouts_keep_the_v71_defaults(self):
        from services import card_template_service as cts
        settings = cts.normalise_variant_settings(variants=["base", "star",
                                                            "legend"])
        for key, value in cts.DEFAULT_TEMPLATE_SETTINGS.items():
            if key == "show_portrait":
                continue
            self.assertEqual(settings["base"][key], value,
                             f"base layout drifted on {key}")

    def test_a_career_face_does_not_draw_the_cutout(self):
        """The face is baked into the artwork; a cutout would cover it."""
        from services import card_template_service as cts
        settings = cts.normalise_variant_settings(variants=["career_1"])
        self.assertFalse(settings["career_1"]["show_portrait"])

    def test_the_rarity_cards_still_draw_the_cutout(self):
        from services import card_template_service as cts
        settings = cts.normalise_variant_settings(variants=["base"],
                                                  shared_show_portrait=True)
        self.assertTrue(settings["base"]["show_portrait"])

    def test_a_career_face_starts_with_light_text_on_its_dark_artwork(self):
        from services import card_template_service as cts
        settings = cts.normalise_variant_settings(variants=["career_1"])
        self.assertEqual(settings["career_1"]["name_color"],
                         cts.CAREER_COLOR_SETTINGS["name_color"])
        self.assertNotEqual(settings["career_1"]["name_color"],
                            cts.DEFAULT_COLOR_SETTINGS["name_color"])

    def test_the_rarity_cards_keep_the_original_palette(self):
        from services import card_template_service as cts
        settings = cts.normalise_variant_settings(variants=["base"])
        for key, value in cts.DEFAULT_COLOR_SETTINGS.items():
            self.assertEqual(settings["base"][key], value)

    def test_a_new_face_inherits_the_first_face_rather_than_the_base_card(self):
        from services import card_template_service as cts
        saved = {"career_1": {"name_x": 321, "name_y": 654}}
        settings = cts.normalise_variant_settings(
            saved, variants=["career_1", "career_2"])
        self.assertEqual(settings["career_1"]["name_x"], 321)
        self.assertEqual(settings["career_2"]["name_x"], 321,
                         "face 2 should start from face 1's tuning")
        self.assertNotEqual(settings["base"]["name_x"], 321,
                            "the base card must not have moved")

    def test_saving_one_face_leaves_the_others_alone(self):
        from services import card_template_service as cts
        saved = {"career_2": {"name_x": 999}}
        settings = cts.normalise_variant_settings(
            saved, variants=["career_1", "career_2"])
        self.assertEqual(settings["career_2"]["name_x"], 999)
        self.assertEqual(settings["career_1"]["name_x"],
                         cts.DEFAULT_TEMPLATE_SETTINGS["name_x"])
        self.assertEqual(settings["base"]["name_x"],
                         cts.DEFAULT_TEMPLATE_SETTINGS["name_x"])

    def test_out_of_range_numbers_are_clamped(self):
        from services import card_template_service as cts
        settings = cts.normalise_variant_settings(
            {"career_1": {"name_font_size": 100000}}, variants=["career_1"])
        low, high = cts.SETTING_LIMITS["name_font_size"]
        self.assertEqual(settings["career_1"]["name_font_size"], high)


class ColourParsingTests(unittest.TestCase):
    def test_hex_colours_normalise(self):
        from services.card_template_service import normalise_color
        self.assertEqual(normalise_color("#AABBCC"), "#aabbcc")
        self.assertEqual(normalise_color("aabbcc"), "#aabbcc")
        self.assertEqual(normalise_color("#abc"), "#aabbcc")

    def test_rubbish_falls_back_instead_of_raising(self):
        from services.card_template_service import normalise_color
        self.assertEqual(normalise_color("not a colour", "#123456"), "#123456")
        self.assertEqual(normalise_color(None, "#123456"), "#123456")

    def test_conversion_to_rgba_is_what_pillow_wants(self):
        from services.card_template_service import color_to_rgba
        self.assertEqual(color_to_rgba("#ffffff"), (255, 255, 255, 255))
        self.assertEqual(color_to_rgba("#005632"), (0, 86, 50, 255))


class FaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import CareerFace
        self.session = get_session()
        self.session.query(CareerFace).delete()
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_faces_take_the_lowest_free_slot(self):
        from services.career_face_service import add_face, next_slot
        first, _ = add_face(self.session, "One")
        second, _ = add_face(self.session, "Two")
        self.session.commit()
        self.assertEqual((first.slot, second.slot), (1, 2))
        self.assertEqual(next_slot(self.session), 3)

    def test_deleting_a_face_frees_its_slot_again(self):
        from services.career_face_service import add_face, next_slot, remove_face
        add_face(self.session, "One")
        add_face(self.session, "Two")
        self.session.commit()
        remove_face(self.session, 1)
        self.session.commit()
        self.assertEqual(next_slot(self.session), 1)

    def test_the_variant_key_follows_the_slot(self):
        from services.career_face_service import variant_for
        self.assertEqual(variant_for(3), "career_3")

    def test_only_active_faces_become_variants(self):
        from services.card_template_service import career_variants
        from services.career_face_service import add_face, set_face_fields
        add_face(self.session, "One")
        add_face(self.session, "Two")
        self.session.commit()
        self.assertEqual(career_variants(self.session),
                         ["career_1", "career_2"])
        set_face_fields(self.session, 2, is_active=False)
        self.session.commit()
        self.assertEqual(career_variants(self.session), ["career_1"])

    def test_a_face_with_no_artwork_is_not_offered_to_players(self):
        """A half-finished design must never reach the wizard."""
        from services.career_face_service import add_face, selectable_faces
        add_face(self.session, "No Artwork Yet")
        self.session.commit()
        self.assertEqual(selectable_faces(self.session), [])

    def test_all_variants_lists_rarities_first_then_faces(self):
        from services.card_template_service import all_template_variants
        from services.career_face_service import add_face
        add_face(self.session, "One")
        self.session.commit()
        self.assertEqual(all_template_variants(self.session),
                         ["base", "star", "legend", "career_1"])


if __name__ == "__main__":
    unittest.main()
