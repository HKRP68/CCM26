"""The match summary card — the light "poster" design.

Nothing here can assert that the card *looks* right; that was settled by
diffing renders against the reference poster. What these lock down is the
contract around the pixels, which is where the card has actually broken before:

  • the bundled fonts resolve. The body face silently fell back to DejaVu for
    months because the candidate list named four filenames that do not exist,
    and no test noticed.
  • an archived payload still renders. scorecard_delivery drops payload keys
    the generator no longer takes, so old rows must survive both a missing key
    and an unknown one.
  • user-supplied crest bytes never cost the card.
  • a bowling Player of the Match gets bowling numbers.
"""

import io
import os
import unittest

from services import match_summary_card as card


def _png(size=(320, 320), color=(30, 90, 200, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _payload(**overrides):
    base = dict(
        inn1_team="Rome Gladiators", inn1_runs=156, inn1_wickets=7,
        inn1_overs="20",
        inn2_team="Mumbai Marathas", inn2_runs=158, inn2_wickets=4,
        inn2_overs="16.5",
        winner_name="Mumbai Marathas", win_margin_text="by 6 wickets",
        overs_total=20, match_no=98, stadium="Singapore National Stadium",
        potm_name="Ben Stokes", potm_team="Mumbai Marathas",
        potm_stats="52* (31)", potm_runs=52, potm_balls=31, potm_fours=4,
        potm_sixes=2, potm_sr=167.7,
        top_per_team={
            "inn1": {"team": "Rome Gladiators",
                     "batters": [{"name": "Shimron Hetmyer", "runs": 33,
                                  "balls": 22, "out": True}],
                     "bowlers": [{"name": "Ben Stokes", "wickets": 4,
                                  "runs": 27, "overs": "4"}]},
            "inn2": {"team": "Mumbai Marathas",
                     "batters": [{"name": "Ben Stokes", "runs": 52,
                                  "balls": 31, "out": False}],
                     "bowlers": [{"name": "James Anderson", "wickets": 1,
                                  "runs": 20, "overs": "4"}]},
        },
    )
    base.update(overrides)
    return base


class FontTests(unittest.TestCase):
    """Every family must resolve to a bundled file. A family that silently
    falls through to DejaVu changes how the whole card reads, and the card
    still renders, so nothing else would catch it."""

    def test_every_family_resolves_to_a_bundled_file(self):
        for family, candidates in card._FAMILIES.items():
            with self.subTest(family=family):
                found = card._first_existing(candidates)
                self.assertIsNotNone(
                    found, f"no bundled font on disk for the {family!r} family")
                self.assertTrue(os.path.isfile(found))

    def test_the_headline_face_is_the_wide_grotesque(self):
        """A condensed face lands ~40% narrow at the same cap height, which is
        what the reference is not."""
        found = card._first_existing(card._FAMILIES["headline"])
        self.assertIn("ArchivoBlack", os.path.basename(found))

    def test_font_sizes_scale_with_the_supersample(self):
        self.assertEqual(card._font(40, family="body").size, 40 * card.SCALE)


class RenderTests(unittest.TestCase):
    def _render(self, **overrides):
        png = card.generate_match_summary(**_payload(**overrides))
        self.assertTrue(png, "card failed to render")
        self.assertEqual(png[:4], b"\x89PNG")
        return png

    def test_it_renders_at_the_reference_size(self):
        from PIL import Image
        image = Image.open(io.BytesIO(self._render()))
        self.assertEqual(image.size, (card.CANVAS_W, card.CANVAS_H))

    def test_it_renders_with_both_crests(self):
        self._render(inn1_logo_png=_png(), inn2_logo_png=_png((200, 260)))

    def test_undecodable_crest_bytes_do_not_cost_the_card(self):
        self._render(inn1_logo_png=b"not an image at all")

    def test_it_renders_with_admin_supplied_colours(self):
        self._render(inn1_color="#aa001b", inn2_color="#0065b3")

    def test_a_nonsense_colour_falls_back_rather_than_raising(self):
        self._render(inn1_color="not-a-colour", inn2_color="#xyz")

    def test_a_very_long_team_name_still_renders(self):
        self._render(inn1_team="Himanshu's Absolutely Enormous Super Kings XI")

    def test_it_renders_with_nothing_but_the_required_arguments(self):
        png = card.generate_match_summary(
            inn1_team="A", inn1_runs=1, inn1_wickets=0, inn1_overs="1",
            inn2_team="B", inn2_runs=2, inn2_wickets=1, inn2_overs="1",
            winner_name="B", win_margin_text="by 9 wickets", overs_total=1)
        self.assertTrue(png and png[:4] == b"\x89PNG")


class ArchivedPayloadTests(unittest.TestCase):
    """``scorecard_delivery._accepted_kwargs`` filters a stored payload against
    the generator's signature, so a row written months ago must still redraw."""

    def test_a_payload_without_the_showcase_keys_still_renders(self):
        payload = _payload()
        for key in ("potm_runs", "potm_balls", "potm_fours", "potm_sixes",
                    "potm_sr"):
            payload.pop(key)
        self.assertTrue(card.generate_match_summary(**payload))

    def test_an_unknown_key_is_dropped_rather_than_raising(self):
        from services.scorecard_delivery import _accepted_kwargs
        payload = _payload(some_key_from_2024="gone")
        kwargs = _accepted_kwargs(card.generate_match_summary, payload)
        self.assertNotIn("some_key_from_2024", kwargs)
        self.assertTrue(card.generate_match_summary(**kwargs))


class ShowcaseTests(unittest.TestCase):
    def test_a_batting_award_shows_batting_numbers(self):
        metrics = card._potm_metrics("52* (31)", 52, 31, 4, 2, None)
        self.assertEqual([label for _value, label in metrics],
                         ["RUNS", "BALLS", "FOURS", "SIXES", "STRIKE RATE"])
        self.assertEqual(metrics[0][0], "52*")
        self.assertEqual(metrics[4][0], 167.7)

    def test_a_bowling_award_shows_bowling_numbers(self):
        """Five dashes under RUNS/BALLS/FOURS/SIXES is not a performance."""
        metrics = card._potm_metrics("5-21", None, None, None, None, None,
                                     wickets=5, conceded=21, overs=4)
        self.assertEqual([label for _value, label in metrics],
                         ["WICKETS", "RUNS", "OVERS", "ECONOMY", "DOTS"])
        self.assertEqual(metrics[3][0], 5.25)

    def test_the_numbers_are_recovered_from_an_archived_stats_string(self):
        metrics = card._potm_metrics("🏏 52(31)", None, None, None, None, None)
        self.assertEqual(metrics[0][0], "52")
        self.assertEqual(metrics[1][0], "31")

    def test_bowling_figures_are_recovered_from_an_archived_stats_string(self):
        metrics = card._potm_metrics("🎳 4/27 (4)", None, None, None, None, None)
        self.assertEqual(metrics[0][1], "WICKETS")
        self.assertEqual(metrics[0][0], "4")

    def test_an_all_rounder_who_batted_keeps_the_batting_showcase(self):
        metrics = card._potm_metrics("🏏 52(31) | 🎳 4/27 (4)",
                                     None, None, None, None, None)
        self.assertEqual(metrics[0][1], "RUNS")
        self.assertEqual(metrics[0][0], "52")

    def test_nothing_at_all_degrades_to_dashes(self):
        metrics = card._potm_metrics(None, None, None, None, None, None)
        self.assertTrue(all(value == card.DASH for value, _label in metrics))


class FlourishTests(unittest.TestCase):
    def test_the_default_matches_the_reference(self):
        """The poster says "Game Changer!", so an untouched install must too —
        the performance-aware line is opt-in."""
        import inspect
        default = (inspect.signature(card.generate_match_summary)
                   .parameters["dynamic_flourish"].default)
        self.assertIs(default, False)

    def test_the_dynamic_line_names_what_the_player_did(self):
        self.assertEqual(card._flourish("5-21", None, 5), "Five For!")
        self.assertEqual(card._flourish("101 (55)", 101, None), "Century!")
        self.assertEqual(card._flourish("52 (31)", 52, None), "Match Winner!")
        self.assertEqual(card._flourish("12 (9)", 12, None), "Game Changer!")

    def test_rubbish_input_does_not_raise(self):
        self.assertEqual(card._flourish(None, "not a number", "nope"),
                         "Game Changer!")


if __name__ == "__main__":
    unittest.main()
