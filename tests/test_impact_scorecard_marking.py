"""Impact substitutes are marked on the summary-card image.

The card flattens each row dict to a tuple before drawing, which is exactly
where a flag gets silently dropped — the batter/bowler dicts carry `impact`,
but `_normalise_*` used to keep only (name, value, value). If the flag stops
surviving that hop the row quietly renders as an ordinary one, and nothing else
in the suite would notice.

The drawing itself is pixels, so these assert on the tuple contract and then
render one card end-to-end to prove the wider tuple did not break the renderer.
"""

import unittest

from services import match_summary_card as card


class NormaliseCarriesTheFlagTests(unittest.TestCase):
    def test_batters_keep_the_impact_flag(self):
        rows = card._normalise_batters([
            {"name": "SuperSub", "runs": 44, "balls": 21, "out": False,
             "impact": True},
            {"name": "Regular", "runs": 30, "balls": 25, "out": True},
        ])
        # (name, value, value, is_impact, is_not_out)
        self.assertEqual(rows[0], ("SuperSub", "44*", "21", True, True))
        self.assertEqual(rows[1], ("Regular", "30", "25", False, False))

    def test_bowlers_keep_the_impact_flag(self):
        rows = card._normalise_bowlers([
            {"name": "BenchPace", "wickets": 3, "runs": 24, "overs": "4.0",
             "impact": True},
            {"name": "Regular", "wickets": 1, "runs": 40, "overs": "4.0"},
        ])
        self.assertEqual(rows[0], ("BenchPace", "3-24", "4.0", True, False))
        self.assertEqual(rows[1][3], False)

    def test_padding_rows_are_not_marked(self):
        # Fewer than four batters pads with placeholders; a padded row must not
        # come out green.
        rows = card._normalise_batters([{"name": "Only", "runs": 1, "balls": 1}])
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r[3] is False for r in rows[1:]))

    def test_a_missing_flag_reads_as_not_impact(self):
        rows = card._normalise_batters([{"name": "NoFlag", "runs": 5, "balls": 4}])
        self.assertIs(rows[0][3], False)


class RendersTests(unittest.TestCase):
    def test_the_card_still_renders_with_the_wider_tuple(self):
        png = card.generate_match_summary(
            inn1_team="Alpha", inn1_runs=180, inn1_wickets=5, inn1_overs="20.0",
            inn2_team="Beta", inn2_runs=175, inn2_wickets=8, inn2_overs="20.0",
            winner_name="Alpha", win_margin_text="5 runs", overs_total=20,
            top_per_team={
                "inn1": {"batters": [{"name": "SuperSub", "runs": 70,
                                      "balls": 40, "out": False,
                                      "impact": True}],
                         "bowlers": [{"name": "BenchPace", "wickets": 3,
                                      "runs": 24, "overs": "4.0",
                                      "impact": True}]},
                "inn2": {"batters": [{"name": "Rohit", "runs": 80, "balls": 45,
                                      "out": True}],
                         "bowlers": [{"name": "Bumrah", "wickets": 2,
                                      "runs": 30, "overs": "4.0"}]},
            })
        self.assertTrue(png, "card failed to render")
        self.assertGreater(len(png), 1000)

    def test_a_caller_still_passing_three_tuples_does_not_crash(self):
        # _draw_rows tolerates the old shape so an un-updated caller degrades to
        # "no green tint" rather than an IndexError mid-render.
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (600, 400), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img, "RGBA")
        card._draw_rows(draw, None, [("Old", "10", "8"), ("Shape", "20", "9")],
                        x_name=40, cx1=300, cx2=420, top=20,
                        potm_name=None, max_name_w=200)


class ScorecardRowMarkingTests(unittest.TestCase):
    """The shared scorecard builder feeds both the HTML analysis and the Mini
    App scorecard tab, so marking there covers both surfaces at once."""

    def test_display_name_is_what_the_builder_stores(self):
        from services import impact_player
        self.assertEqual(
            impact_player.display_name({"name": "Sub", "impact_replacement": True}),
            "Sub" + impact_player.IMPACT_SUFFIX)

    def test_the_suffix_is_a_single_shared_constant(self):
        from services import impact_player
        self.assertEqual(impact_player.IMPACT_SUFFIX, " -IP")


if __name__ == "__main__":
    unittest.main()
