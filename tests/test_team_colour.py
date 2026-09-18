"""A team's own colour on the scorecards.

Two things here are worth locking down, and neither is the storage:

  • **what counts as a colour.** People type ``#aa001b``, ``aa001b``, ``#abc``
    and ``crimson``, and all four have to work or the command feels broken.
  • **what happens when two teams clash.** Team colours are chosen
    independently, so two sides can pick the same red — and two identical
    innings blocks read as a rendering fault, not as a coincidence. Innings 1
    keeps what it chose and innings 2 is pushed away from it.
"""

import unittest

from services import card_identity as ci
from handlers.team import COLOUR_NAMES


class NormaliseTests(unittest.TestCase):
    def test_a_full_hex_is_accepted(self):
        self.assertEqual(ci.normalise_hex("#aa001b"), "#aa001b")

    def test_the_hash_is_optional(self):
        self.assertEqual(ci.normalise_hex("aa001b"), "#aa001b")

    def test_shorthand_expands(self):
        self.assertEqual(ci.normalise_hex("#abc"), "#aabbcc")
        self.assertEqual(ci.normalise_hex("abc"), "#aabbcc")

    def test_case_is_normalised(self):
        self.assertEqual(ci.normalise_hex("#AA001B"), "#aa001b")

    def test_padding_is_ignored(self):
        self.assertEqual(ci.normalise_hex("  #aa001b  "), "#aa001b")

    def test_black_is_a_colour_like_any_other(self):
        """It is the obvious value for a validator to mishandle, because it is
        every fallback's default."""
        self.assertEqual(ci.normalise_hex("#000000"), "#000000")
        self.assertEqual(ci.normalise_hex("000"), "#000000")

    def test_junk_is_refused(self):
        for value in ("", None, "zzz", "#12345", "#aabbccdd", "rgb(1,2,3)",
                      "crimson", "#", "123456789"):
            with self.subTest(value=value):
                self.assertIsNone(ci.normalise_hex(value))


class ColourNameTests(unittest.TestCase):
    def test_every_name_maps_to_a_real_colour(self):
        for name, value in COLOUR_NAMES.items():
            with self.subTest(name=name):
                self.assertEqual(ci.normalise_hex(value), value.lower())

    def test_the_names_are_lowercase_so_lookup_is_predictable(self):
        self.assertTrue(all(name == name.lower() for name in COLOUR_NAMES))


class RoundTripTests(unittest.TestCase):
    def test_hex_to_rgb_and_back(self):
        self.assertEqual(ci.hex_to_rgb("#aa001b"), (170, 0, 27))
        self.assertEqual(ci.rgb_to_hex((170, 0, 27)), "#aa001b")

    def test_an_unreadable_value_takes_the_default(self):
        self.assertIsNone(ci.hex_to_rgb("nonsense"))
        self.assertEqual(ci.hex_to_rgb("nonsense", (1, 2, 3)), (1, 2, 3))

    def test_out_of_range_channels_are_clamped(self):
        self.assertEqual(ci.rgb_to_hex((-20, 300, 27)), "#00ff1b")


class SeparationTests(unittest.TestCase):
    def _apart(self, a, b):
        return ci._distance(ci.hex_to_rgb(a), b) >= ci.COLOUR_MIN_DISTANCE

    def test_distinct_colours_are_left_alone(self):
        red, blue = ci.hex_to_rgb("#aa001b"), ci.hex_to_rgb("#0065b3")
        self.assertEqual(ci.separate_colours(red, blue), blue)

    def test_a_near_identical_colour_is_pushed_apart(self):
        first, second = ci.hex_to_rgb("#aa001b"), ci.hex_to_rgb("#b21226")
        moved = ci.separate_colours(first, second)
        self.assertNotEqual(tuple(moved), tuple(second))
        self.assertTrue(self._apart("#aa001b", moved))

    def test_two_identical_colours_end_up_distinguishable(self):
        same = ci.hex_to_rgb("#aa001b")
        moved = ci.separate_colours(same, same)
        self.assertTrue(self._apart("#aa001b", moved))

    def test_a_dark_clash_lightens(self):
        """Both sides near-black: darkening further would make them both
        invisible, so the second has to move the other way."""
        first, second = ci.hex_to_rgb("#111111"), ci.hex_to_rgb("#131313")
        moved = ci.separate_colours(first, second)
        self.assertGreater(sum(moved), sum(second))
        self.assertTrue(self._apart("#111111", moved))

    def test_a_light_clash_darkens(self):
        first, second = ci.hex_to_rgb("#eeeeee"), ci.hex_to_rgb("#efefef")
        moved = ci.separate_colours(first, second)
        self.assertLess(sum(moved), sum(second))
        self.assertTrue(self._apart("#eeeeee", moved))

    def test_a_missing_colour_is_not_an_error(self):
        self.assertIsNone(ci.separate_colours(ci.hex_to_rgb("#aa001b"), None))
        self.assertEqual(ci.separate_colours(None, (1, 2, 3)), (1, 2, 3))

    def test_separation_is_bounded(self):
        """It must terminate rather than shifting forever on a pathological
        pair."""
        black = (0, 0, 0)
        moved = ci.separate_colours(black, black, fallback=(0, 201, 167))
        self.assertIsNotNone(moved)


class SwatchTests(unittest.TestCase):
    """The confirmation shows the colour rather than quoting a hex code, so the
    swatch has to render for anything the command will accept."""

    def test_a_swatch_renders_for_every_named_colour(self):
        from handlers.team import _swatch_png
        for name, value in COLOUR_NAMES.items():
            with self.subTest(name=name):
                png = _swatch_png(value, "Test XI")
                self.assertTrue(png and png[:4] == b"\x89PNG")

    def test_a_swatch_survives_an_absurd_team_name(self):
        from handlers.team import _swatch_png
        png = _swatch_png("#aa001b", "A" * 200)
        self.assertTrue(png and png[:4] == b"\x89PNG")

    def test_a_swatch_survives_no_team_name(self):
        from handlers.team import _swatch_png
        self.assertTrue(_swatch_png("#aa001b", None))


if __name__ == "__main__":
    unittest.main()
