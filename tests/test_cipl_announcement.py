"""The pinned "Watch X 🆚 Y" card names the competition it is actually for.

This line used to be the literal "High-Voltage IPL Battle", so a /cbbl match, a
tournament final and a Challenge Draft all announced themselves as IPL. Each
kind of match now names itself — and a league keeps the exact wording it always
had, which is what the first test here pins.
"""

import unittest

from handlers.cipl_play import _competition_line, _match_start_announcement


class CompetitionLineTests(unittest.TestCase):

    def test_a_league_match_is_unchanged(self):
        """Byte-for-byte what /cipl has always posted — no regression."""
        self.assertEqual(_competition_line({"league_name": "IPL"}),
                         "⚡ <b>High-Voltage IPL Battle</b> ⚡")

    def test_another_league_names_itself_rather_than_ipl(self):
        self.assertEqual(_competition_line({"league_name": "BBL"}),
                         "⚡ <b>High-Voltage BBL Battle</b> ⚡")
        self.assertIn("Vitality Blast",
                      _competition_line({"league_name": "Vitality Blast"}))

    def test_a_tournament_names_the_tournament(self):
        line = _competition_line({"league_name": "IPL",
                                  "tournament_name": "Winter Championship 2026"})
        self.assertEqual(line, "🏆 <b>Winter Championship 2026</b>")
        self.assertNotIn("IPL", line)

    def test_a_cl_tour_names_the_series_and_its_position(self):
        self.assertEqual(
            _competition_line({"league_name": "IPL", "cl_tour_id": 4,
                               "cl_tour_match_no": 2, "cl_tour_match_count": 3}),
            "🏆 <b>IPL Tour</b> · Match 2/3")

    def test_a_cl_tour_without_numbers_still_reads(self):
        self.assertEqual(
            _competition_line({"league_name": "IPL", "cl_tour_id": 4}),
            "🏆 <b>IPL Tour</b>")

    def test_a_challenge_draft_names_the_mode(self):
        line = _competition_line({"league_name": "Challenge Draft",
                                  "mode_name": "Challenge Draft"})
        self.assertEqual(line, "🎯 <b>Challenge Draft</b>")
        self.assertNotIn("IPL", line)
        self.assertNotIn("High-Voltage", line)

    def test_the_mode_wins_over_everything_else(self):
        """A draft is never a league fixture, whatever else is on the state."""
        self.assertEqual(
            _competition_line({"mode_name": "Challenge Draft",
                               "league_name": "IPL", "tournament_name": "X",
                               "cl_tour_id": 9}),
            "🎯 <b>Challenge Draft</b>")

    def test_a_state_that_names_nothing_falls_back_rather_than_raising(self):
        """Old live matches were saved before these keys existed."""
        self.assertEqual(_competition_line({}),
                         "⚡ <b>High-Voltage IPL Battle</b> ⚡")

    def test_a_name_with_markup_in_it_is_escaped(self):
        """Tournament and team names are admin-entered free text, and this line
        is sent as HTML."""
        for state in ({"tournament_name": "Cup <b>2026</b>"},
                      {"league_name": "A & B"},
                      {"mode_name": "<i>Draft</i>"}):
            with self.subTest(state=state):
                line = _competition_line(state)
                self.assertNotIn("<b>2026", line)
                self.assertNotIn("<i>Draft", line)
                if "&" in str(state):
                    self.assertIn("&amp;", line)


class AnnouncementCardTests(unittest.TestCase):

    BASE = {
        "bat_team_name": "Shanka Draft XI", "bowl_team_name": "Rudra Draft XI",
        "bat_team_code": "SHANKA", "bowl_team_code": "RUDRA",
        "stadium": "Sheikh Zayed Cricket Stadium", "pitch_type": "Flat",
        "overs": 20,
    }

    def test_the_card_carries_the_competition_line(self):
        card = _match_start_announcement(dict(self.BASE,
                                              mode_name="Challenge Draft"))
        self.assertIn("🎯 <b>Challenge Draft</b>", card)
        self.assertNotIn("High-Voltage IPL Battle", card)

    def test_the_header_shows_the_short_codes_it_was_given(self):
        card = _match_start_announcement(dict(self.BASE))
        self.assertIn("🏆 <b>SHANKA</b> 🆚 <b>RUDRA</b>", card)

    def test_a_league_card_still_reads_as_it_always_did(self):
        card = _match_start_announcement(dict(self.BASE, league_name="IPL"))
        self.assertIn("⚡ <b>High-Voltage IPL Battle</b> ⚡", card)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
