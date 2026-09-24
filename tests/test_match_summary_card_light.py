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

    def test_it_renders_for_a_bowling_award(self):
        self._render(potm_stats="5-21", potm_runs=None, potm_balls=None,
                     potm_fours=None, potm_sixes=None, potm_sr=None,
                     potm_wickets=5, potm_conceded=21, potm_overs=4)

    def test_it_renders_for_an_all_rounder(self):
        self._render(potm_stats="🏏 52(31) | 🎳 4/27 (4)",
                     potm_wickets=4, potm_conceded=27, potm_overs="4")

    def test_it_renders_with_a_portrait(self):
        """The name block has two layouts: with a portrait the name keeps its
        place, without one it slides left into the empty photo band."""
        self._render(potm_photo_png=_png((300, 380)))

    def test_it_renders_with_the_award_winners_card(self):
        """The collectible card sits beside the winner's name, which is where
        someone reading the result looks for it."""
        self._render(potm_card_png=_png((1536, 1024)))

    def test_the_card_displaces_the_portrait_rather_than_crowding_it(self):
        """Both in the band would show the same player twice — the artwork
        already carries them."""
        self._render(potm_card_png=_png((1536, 1024)),
                     potm_photo_png=_png((300, 380)))

    def test_undecodable_card_bytes_do_not_cost_the_card(self):
        """The award's card is a bonus on a card that is already drawn."""
        self._render(potm_card_png=b"not an image at all")

    def test_the_name_moves_over_for_the_card(self):
        """The three name layouts are the point of the band being conditional:
        holding it open for artwork that is not coming leaves a dead gap."""
        self.assertGreater(card.POTM_NAME_X_CARD, card.POTM_NAME_X)
        self.assertGreater(card.POTM_NAME_X, card.POTM_NAME_X_BARE)

    def test_the_strip_is_deep_enough_for_a_landscape_card(self):
        """A collectible card is 3:2. The band has to fit one at a size that
        still reads as a card rather than as a smear."""
        band = card.POTM_CARD_BAND[1] - card.POTM_CARD_BAND[0]
        self.assertGreaterEqual(card.POTM_H - card.POTM_CARD_PAD * 2,
                                band / 1.5 * 0.9)

    def test_it_renders_with_figures_wide_enough_to_crowd_a_column(self):
        """Each showcase value is fitted to its own column, so a double-century
        or a ten-for shrinks rather than running into its divider."""
        self._render(potm_stats="148* (52)", potm_runs=148, potm_balls=52,
                     potm_wickets=10, potm_conceded=137, potm_overs="12.3")

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
    """The performance showcase.

    Columns 1 and 2 always carry the two numbers that describe a cricket
    performance — runs(balls) and wickets/conceded — because an all-rounder used
    to have one of them silently thrown away. The last three adapt, so a bowler
    is never shown FOURS and a batter is never shown ECONOMY.
    """

    def labels(self, metrics):
        return [label for _value, label in metrics]

    def values(self, metrics):
        return [value for value, _label in metrics]

    def test_an_all_rounder_shows_both_disciplines(self):
        metrics = card._potm_metrics("🏏 52(31) | 🎳 4/27 (4)", 52, 31, 4, 2, None,
                                     wickets=4, conceded=27, overs="4")
        self.assertEqual(self.labels(metrics),
                         ["BATTING", "BOWLING", "S/R", "OVERS", "ECON"])
        self.assertEqual(self.values(metrics)[:2], ["52(31)", "4/27"])

    def test_a_batter_gets_boundaries_after_the_two(self):
        metrics = card._potm_metrics("52* (31)", 52, 31, 4, 2, 167.7)
        self.assertEqual(self.labels(metrics),
                         ["BATTING", "BOWLING", "FOURS", "SIXES", "S/R"])
        self.assertEqual(self.values(metrics)[0], "52*(31)")
        self.assertEqual(self.values(metrics)[1], card.DASH)

    def test_a_bowler_gets_bowling_stats_after_the_two(self):
        metrics = card._potm_metrics("5-21", None, None, None, None, None,
                                     wickets=5, conceded=21, overs=4, dots=11)
        self.assertEqual(self.labels(metrics),
                         ["BATTING", "BOWLING", "OVERS", "ECON", "DOTS"])
        self.assertEqual(self.values(metrics)[0], card.DASH)
        self.assertEqual(self.values(metrics)[1], "5/21")
        self.assertEqual(self.values(metrics)[3], 5.25)

    def test_the_not_out_star_rides_on_the_batting_figure(self):
        metrics = card._potm_metrics("52* (31)", 52, 31, None, None, None)
        self.assertEqual(self.values(metrics)[0], "52*(31)")

    def test_nothing_at_all_degrades_to_dashes(self):
        metrics = card._potm_metrics(None, None, None, None, None, None)
        self.assertTrue(all(value == card.DASH for value in self.values(metrics)))
        self.assertEqual(self.labels(metrics)[:2], ["BATTING", "BOWLING"])

    # ── the stats strings the live callers actually send ──

    def test_the_playmatch_string_fills_both(self):
        metrics = card._potm_metrics("🏏 52(31) | 🎳 4/27 (4)",
                                     None, None, None, None, None)
        self.assertEqual(self.values(metrics)[:2], ["52(31)", "4/27"])
        self.assertEqual(self.values(metrics)[3], "4")   # overs, from "(4)"

    def test_the_cipl_string_fills_both(self):
        """This is the one the old guard threw away: it refused to read a
        bowling figure out of any string that also held a batting one."""
        metrics = card._potm_metrics("52(31) | 4/27 (4)",
                                     None, None, None, None, None)
        self.assertEqual(self.values(metrics)[:2], ["52(31)", "4/27"])

    def test_the_sim_string_parses(self):
        metrics = card._potm_metrics("52 runs, 3 wkts",
                                     None, None, None, None, None)
        self.assertEqual(self.values(metrics)[:2], ["52", "3"])

    def test_the_arena_string_parses(self):
        metrics = card._potm_metrics("12 runs • 2 wickets",
                                     None, None, None, None, None)
        self.assertEqual(self.values(metrics)[:2], ["12", "2"])

    def test_the_designer_preview_string_parses(self):
        metrics = card._potm_metrics("4/25 (4 OVERS)",
                                     None, None, None, None, None)
        self.assertEqual(self.values(metrics)[1], "4/25")
        self.assertEqual(self.labels(metrics)[2], "OVERS")

    def test_a_bowling_figure_is_not_read_as_runs_off_balls(self):
        """The lookbehind that stops "4/27 (4)" becoming 27 runs off 4 balls."""
        metrics = card._potm_metrics("🎳 4/27 (4)", None, None, None, None, None)
        self.assertEqual(self.values(metrics)[0], card.DASH)
        self.assertEqual(self.values(metrics)[1], "4/27")

    def test_payload_values_beat_the_string(self):
        metrics = card._potm_metrics("99 runs, 9 wkts", 52, 31, None, None, None,
                                     wickets=4, conceded=27)
        self.assertEqual(self.values(metrics)[:2], ["52(31)", "4/27"])


class RowTextIsInkTests(unittest.TestCase):
    """Player names and the run / ball / wicket numbers are plain ink.

    Teams pick their own colour now, and a not-out score used to be drawn in it
    — which reads as the number saying something about the team rather than
    about the innings. The trailing "*" already marks a not-out, so the colour
    was only repeating it.

    This is the only test in the suite that asserts on rendered pixels, because
    it is the only way to state the requirement: the same match drawn for two
    wildly different teams must produce byte-identical table text.
    """

    def _card(self, colour):
        return card.generate_match_summary(**_payload(
            inn1_color=colour, inn2_color=colour,
            top_per_team={
                "inn1": {"team": "Rome Gladiators",
                         "batters": [
                             {"name": "Not Out Batter", "runs": 52, "balls": 31,
                              "out": False},
                             {"name": "Out Batter", "runs": 33, "balls": 22,
                              "out": True}],
                         "bowlers": [{"name": "Some Bowler", "wickets": 4,
                                      "runs": 27, "overs": "4"}]},
                "inn2": {"team": "Mumbai Marathas", "batters": [], "bowlers": []},
            }))

    def _table_strip(self, png):
        """The batters table's rows — clear of the crest panel on the left and
        of the team-coloured divider at the midpoint, both of which are
        *supposed* to follow the team colour."""
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert("RGB")
        return image.crop((card.CREST_X1_TOP + 20, card.INN1_Y + card.BAR_H + 4,
                           card.TABLE_DIV - 10, card.INN1_Y + card.INN_H - 4))

    def test_row_text_does_not_change_with_the_team_colour(self):
        """The same match drawn for a red team and a green one has to produce
        byte-identical table text."""
        red = self._table_strip(self._card("#aa001b"))
        green = self._table_strip(self._card("#15803d"))
        self.assertEqual(red.tobytes(), green.tobytes(),
                         "the table's text changed with the team colour")

    def test_a_not_out_score_carries_no_trace_of_the_team_colour(self):
        """Asserted as "nothing red survives" rather than as an exact pixel:
        two different numbers antialias differently, so only the *hue* is
        meaningful."""
        from PIL import Image
        image = Image.open(io.BytesIO(self._card("#aa001b"))).convert("RGB")
        top = card.INN1_Y + card.BAR_H + card.HEAD_ROW_H
        y0 = int(top) + 6
        y1 = int(top + card.ROW_PITCH) - 6
        box = image.crop((card.COL_L1_CX - 40, y0, card.COL_L1_CX + 40, y1))
        colours = [c for _count, c in box.getcolors(maxcolors=box.width * box.height)]
        reddest = max(colours, key=lambda p: p[0] - p[2])
        self.assertLess(reddest[0] - reddest[2], 40,
                        f"the not-out score still carries the team's red: {reddest}")

    def test_the_rows_take_no_team_colour_at_all(self):
        """The parameter is gone, so it cannot be reintroduced by accident."""
        import inspect
        self.assertNotIn("color",
                         inspect.signature(card._draw_rows).parameters)


class PotmRowTests(unittest.TestCase):
    """The award is marked wherever the player appears — batters, bowlers, or
    both. The marking is pixels, so ``_draw_rows`` returns the rows it marked
    and that is what these assert on."""

    def _rows(self, rows, potm_name, *, x_name=308, cx1=686, cx2=813,
              max_name_w=282):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (s := card.s(card.CANVAS_W), card.s(card.CANVAS_H)),
                        (255, 255, 255, 255))
        del s
        draw = ImageDraw.Draw(img, "RGBA")
        return card._draw_rows(draw, None, rows, x_name=x_name, cx1=cx1,
                               cx2=cx2, top=300,
                               potm_name=potm_name, max_name_w=max_name_w)

    def test_a_batting_award_marks_the_batters_table(self):
        rows = card._normalise_batters([
            {"name": "Ben Stokes", "runs": 52, "balls": 31, "out": False},
            {"name": "Joe Root", "runs": 27, "balls": 17, "out": True},
        ])
        self.assertEqual(self._rows(rows, "Ben Stokes"), [0])

    def test_a_bowling_award_marks_the_bowlers_table(self):
        """The reason this matters: a bowler winning the award is common, and
        the bowlers table goes through the same call with different columns."""
        rows = card._normalise_bowlers([
            {"name": "Jasprit Bumrah", "wickets": 4, "runs": 27, "overs": "4"},
            {"name": "Sikandar Raza", "wickets": 1, "runs": 15, "overs": "2"},
        ])
        self.assertEqual(
            self._rows(rows, "Jasprit Bumrah", x_name=959, cx1=1375, cx2=1520,
                       max_name_w=320),
            [0])

    def test_an_all_rounder_is_marked_in_both_tables(self):
        bat = card._normalise_batters([{"name": "Ben Stokes", "runs": 52,
                                        "balls": 31, "out": False}])
        bowl = card._normalise_bowlers([{"name": "Ben Stokes", "wickets": 4,
                                         "runs": 27, "overs": "4"}])
        self.assertEqual(self._rows(bat, "Ben Stokes"), [0])
        self.assertEqual(
            self._rows(bowl, "Ben Stokes", x_name=959, cx1=1375, cx2=1520,
                       max_name_w=320),
            [0])

    def test_nobody_else_is_marked(self):
        rows = card._normalise_batters([
            {"name": "Joe Root", "runs": 27, "balls": 17, "out": True},
            {"name": "Ben Stokes", "runs": 52, "balls": 31, "out": False},
        ])
        self.assertEqual(self._rows(rows, "Ben Stokes"), [1])
        self.assertEqual(self._rows(rows, "Nobody At All"), [])
        self.assertEqual(self._rows(rows, None), [])

    def test_a_long_name_keeps_the_badge(self):
        """The badge used to be dropped when the name ran long, which lost it on
        exactly the row it exists to mark. The name is shortened instead."""
        rows = card._normalise_bowlers([
            {"name": "Ravichandran Ashwin Junior The Third", "wickets": 4,
             "runs": 27, "overs": "4"}])
        self.assertEqual(
            self._rows(rows, "Ravichandran Ashwin Junior The Third",
                       x_name=959, cx1=1375, cx2=1520, max_name_w=320),
            [0])

    def test_an_impact_substitute_can_win_it(self):
        rows = card._normalise_batters([
            {"name": "Super Sub", "runs": 70, "balls": 40, "out": False,
             "impact": True}])
        self.assertEqual(self._rows(rows, "Super Sub"), [0])

    def test_the_impact_suffix_does_not_break_the_match(self):
        """A caller that formats names for display appends ``-IP``; the award
        carries the bare name."""
        rows = card._normalise_batters([
            {"name": "Super Sub -IP", "runs": 70, "balls": 40, "out": False,
             "impact": True}])
        self.assertEqual(self._rows(rows, "Super Sub"), [0])

    def test_the_match_ignores_case_and_padding(self):
        rows = card._normalise_batters([{"name": "  ben   STOKES ", "runs": 52,
                                         "balls": 31, "out": False}])
        self.assertEqual(self._rows(rows, "Ben Stokes"), [0])

    def test_padding_rows_are_never_marked(self):
        """Empty rows render as em-dashes; an em-dash award would mark them."""
        rows = card._normalise_batters([])
        self.assertEqual(self._rows(rows, "—"), [])


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
