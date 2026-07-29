"""Team Chemistry is scoped exactly like traits: your own roster XI, nothing else.

The over-by-over board is shared between /letsplay (personal rosters, traits
active) and Challenge League (league squads handed to both captains, no traits).
Chemistry describes a team you assembled, so scoring a squad you were *dealt*
would be meaningless — it follows the trait boundary rather than the board's.

Bot opponents are on the roster side of that line, deliberately: the generated
bot XI carries real traits and real cards, so it is scored like any other XI.
"""

import unittest

import handlers.cipl_play as cp
from services import cipl_match


def _card(country, category, version="Base"):
    return {"country": country, "category": category, "version": version,
            "name": f"{country[:3]}-{category[:3]}", "rating": 80}


def _roster_xi():
    return ([_card("India", "Batsman") for _ in range(5)]
            + [_card("Australia", "Bowler") for _ in range(3)]
            + [_card("England", "All-rounder") for _ in range(2)]
            + [_card("India", "Wicket Keeper")])


def _state(**over):
    state = {"bat_xi": _roster_xi(), "bowl_xi": _roster_xi(),
             "bat_team_code": "AAA", "bowl_team_code": "BBB",
             "bat_team_name": "Team A", "bowl_team_name": "Team B"}
    state.update(over)
    return state


class ChemistryScopeTests(unittest.TestCase):

    def test_a_letsplay_board_shows_both_sides(self):
        line = cp._chem_line(_state(is_letsplay=True))
        self.assertIn("CHEM", line)
        self.assertIn("AAA", line)
        self.assertIn("BBB", line)

    def test_a_challenge_league_board_shows_nothing(self):
        # Same board, same XIs — only the mode differs.
        self.assertEqual(cp._chem_line(_state()), "")
        self.assertEqual(cp._chem_line(_state(is_letsplay=False)), "")

    def test_a_bot_opponent_is_still_scored(self):
        # /lpbot builds a real XI from real cards and gives it real traits, so
        # chemistry applies to it too — the line is the trait line's twin.
        line = cp._chem_line(_state(is_letsplay=True, is_bot_match=True))
        self.assertIn("CHEM", line)

    def test_challenge_league_players_carry_no_chemistry_data(self):
        # Belt and braces behind the display gate: a Challenge League player
        # dict has no country/version at all, so nothing downstream can score
        # one even if a future caller forgets the mode check.
        class _CP:
            id = 7
            name = "Player"
            source_player_id = 3
            details_json = ('{"category":"Batsman","rating":80,'
                            '"country":"India","version":"Icon"}')

        player = cipl_match.cp_to_player_dict(_CP())
        self.assertNotIn("country", player)
        self.assertNotIn("version", player)

    def test_the_challenge_league_start_card_carries_no_chemistry(self):
        card = cp._match_start_announcement(_state(
            stadium="Wankhede", pitch_type="Hard", overs=20))
        self.assertNotIn("Chemistry", card)
        self.assertNotIn("🧪", card)


class ChemistryBlockTests(unittest.TestCase):
    """services.match_engine.build_chemistry_line — the /pm live scorecard."""

    def _block(self, bowl_xi):
        from services import match_engine
        return match_engine.build_chemistry_line(
            {"bat_xi": _roster_xi(), "bowl_xi": bowl_xi,
             "bat_team_name": "Team A", "bowl_team_name": "Team B"})

    def test_both_sides_or_nothing(self):
        # A half-rendered block invites a comparison that can't be made — and
        # the side that goes missing is exactly the unscorable synthetic one
        # (/vsbot builds its XI with no country data).
        synthetic = [{"category": c, "name": "bot"}
                     for c in (["Batsman"] * 5 + ["Bowler"] * 3
                               + ["All-rounder"] * 2 + ["Wicket Keeper"])]
        self.assertEqual(self._block(synthetic), "")
        self.assertEqual(self._block([]), "")
        self.assertIn("Team B", self._block(_roster_xi()))

    def test_team_names_are_escaped(self):
        from services import match_engine
        block = match_engine.build_chemistry_line(
            {"bat_xi": _roster_xi(), "bowl_xi": _roster_xi(),
             "bat_team_name": "A & B", "bowl_team_name": "<b>X</b>"})
        self.assertIn("A &amp; B", block)
        self.assertIn("&lt;b&gt;X&lt;/b&gt;", block)


if __name__ == "__main__":
    unittest.main()
