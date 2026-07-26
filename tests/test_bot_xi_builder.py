"""Tests for the bot's automatic Playing XI generation (/lpbot, /ciplbot).

``build_challenge_bot_xi`` is exercised against fake ``ChallengePlayer``-shaped
rows — it only ever reads ``id``, ``name``, ``is_overseas`` and the JSON blob in
``details_json``, so no DB is needed. The guarantee under test is the one a
player would notice: the bot always fields eleven, always legal, and always the
best it can from the squad it was given.
"""

import json
import unittest

from services import bot_xi_builder as bx
from services import xi_rules


class FakeChallengePlayer:
    def __init__(self, pid, name, category, rating, overseas=False,
                 bat_rating=None, bowl_rating=None):
        self.id = pid
        self.name = name
        self.is_overseas = overseas
        self.details_json = json.dumps({
            "category": category,
            "rating": rating,
            "bat_rating": bat_rating if bat_rating is not None else rating,
            "bowl_rating": bowl_rating if bowl_rating is not None else rating - 20,
        })


def _squad(size=18, overseas_count=0):
    """A realistic squad: keepers, batters, all-rounders and bowlers."""
    shape = (["Wicket Keeper"] * 2 + ["Batsman"] * 6
             + ["All-rounder"] * 4 + ["Bowler"] * 6)
    players = []
    for i in range(size):
        category = shape[i % len(shape)]
        players.append(FakeChallengePlayer(
            pid=i + 1, name=f"P{i + 1}", category=category,
            rating=90 - i, overseas=(i < overseas_count)))
    return players


def _validate(xi, lo=0, hi=11):
    return xi_rules.validate_challenge_xi(xi, lo, hi)


class ChallengeBotXITests(unittest.TestCase):
    def test_picks_exactly_eleven_and_passes_the_league_rules(self):
        xi = bx.build_challenge_bot_xi(_squad())
        self.assertEqual(len(xi), 11)
        ok, error = _validate(xi)
        self.assertTrue(ok, error)

    def test_no_duplicate_players(self):
        xi = bx.build_challenge_bot_xi(_squad())
        self.assertEqual(len({p.id for p in xi}), 11)

    def test_prefers_the_higher_rated_players(self):
        # The squad is rated 90 down to 73; the chosen XI should out-rate the
        # eleven weakest players by a clear margin.
        squad = _squad(size=18)
        xi = bx.build_challenge_bot_xi(squad)
        chosen = sum(bx._cp_rating(p) for p in xi) / 11
        weakest = sorted(squad, key=bx._cp_rating)[:11]
        self.assertGreater(chosen, sum(bx._cp_rating(p) for p in weakest) / 11)

    def test_respects_the_overseas_maximum(self):
        # The eight strongest players are overseas, so a naive "best eleven"
        # would blow straight past a limit of four.
        xi = bx.build_challenge_bot_xi(_squad(size=20, overseas_count=8),
                                       min_overseas=0, max_overseas=4)
        self.assertLessEqual(sum(1 for p in xi if xi_rules.challenge_is_overseas(p)), 4)
        ok, error = _validate(xi, 0, 4)
        self.assertTrue(ok, error)

    def test_respects_the_overseas_minimum(self):
        # Only the four weakest players are overseas, so meeting a minimum of
        # three means deliberately picking some of them.
        squad = _squad(size=20)
        for p in squad[-4:]:
            p.is_overseas = True
        xi = bx.build_challenge_bot_xi(squad, min_overseas=3, max_overseas=11)
        self.assertGreaterEqual(sum(1 for p in xi if xi_rules.challenge_is_overseas(p)), 3)

    def test_a_squad_that_is_too_small_yields_nothing(self):
        self.assertEqual(bx.build_challenge_bot_xi(_squad(size=9)), [])

    def test_batting_order_puts_the_specialist_bowlers_last(self):
        xi = bx.build_challenge_bot_xi(_squad())
        categories = [xi_rules.challenge_player_category(p) for p in xi]
        bowler_positions = [i for i, c in enumerate(categories) if c == "Bowler"]
        other_positions = [i for i, c in enumerate(categories) if c != "Bowler"]
        self.assertTrue(bowler_positions and other_positions,
                        "expected the XI to mix bowlers and non-bowlers")
        # Every specialist bowler bats below every non-bowler.
        self.assertGreater(min(bowler_positions), max(other_positions))

    def test_a_keeperless_squad_degrades_instead_of_crashing(self):
        squad = [FakeChallengePlayer(i + 1, f"B{i}", "Batsman", 80 - i)
                 for i in range(14)]
        xi = bx.build_challenge_bot_xi(squad)
        # No legal XI exists (no keeper, no bowling options), so it falls back
        # to the best eleven rather than returning a short or empty side.
        self.assertEqual(len(xi), 11)


class EngineXIValidationTests(unittest.TestCase):
    def _engine_xi(self, shape):
        xi = []
        for i, category in enumerate(shape):
            xi.append({
                "roster_id": -(bx.LPBOT_RID_BASE + i), "player_id": i + 1,
                "name": f"P{i}", "category": category,
                "rating": 70, "bat_rating": 70, "bowl_rating": 60,
                "traits": [],
            })
        return xi

    def test_the_lpbot_shape_is_a_legal_playing_xi(self):
        shape = []
        for category, count in bx.LPBOT_SHAPE:
            shape.extend([category] * count)
        self.assertEqual(len(shape), 11)
        ok, errors = bx.validate_engine_xi(self._engine_xi(shape))
        self.assertTrue(ok, errors)

    def test_an_illegal_shape_is_reported(self):
        ok, errors = bx.validate_engine_xi(self._engine_xi(["Batsman"] * 11))
        self.assertFalse(ok)
        self.assertTrue(errors)


class TraitTests(unittest.TestCase):
    def test_trait_dict_resolves_catalog_metadata(self):
        t = bx.trait_dict("bowl_death", 3)
        self.assertEqual(t["effect_key"], "bowl_death")
        self.assertEqual(t["level"], 3)
        self.assertTrue(t["display_name"])
        self.assertTrue(t["emoji"])

    def test_unknown_trait_key_is_none(self):
        self.assertIsNone(bx.trait_dict("not_a_real_trait", 3))

    def test_some_but_not_all_of_the_xi_carry_a_trait(self):
        shape = []
        for category, count in bx.LPBOT_SHAPE:
            shape.extend([category] * count)
        xi = [{"roster_id": -i, "player_id": i, "name": f"P{i}",
               "category": c, "rating": 70, "bat_rating": 70,
               "bowl_rating": 65, "traits": []}
              for i, c in enumerate(shape)]
        bx._assign_lpbot_traits(xi)
        with_traits = [p for p in xi if p["traits"]]
        self.assertEqual(len(with_traits), 5)
        self.assertLess(len(with_traits), len(xi))
        for p in with_traits:
            self.assertEqual(len(p["traits"]), 1)
            self.assertEqual(p["traits"][0]["level"], bx.LPBOT_TRAIT_LEVEL)


class BotTeamPickTests(unittest.TestCase):
    def test_never_picks_the_team_the_user_took(self):
        teams = ["A", "B", "C"]
        for _ in range(50):
            self.assertNotEqual(bx.pick_bot_team(teams, ("A",), lambda t: 15), "A")

    def test_prefers_a_team_with_a_full_squad(self):
        teams = ["A", "B", "C"]
        sizes = {"A": 15, "B": 4, "C": 15}
        for _ in range(50):
            self.assertNotEqual(
                bx.pick_bot_team(teams, ("A",), lambda t: sizes[t]), "B")

    def test_no_free_team_returns_none(self):
        self.assertIsNone(bx.pick_bot_team(["A"], ("A",), lambda t: 15))


if __name__ == "__main__":
    unittest.main()
