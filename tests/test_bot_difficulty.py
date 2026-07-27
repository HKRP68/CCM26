"""Tests for selectable bot difficulty on /lpbot and /ciplbot.

Difficulty has to mean something measurable, and it has to mean the *right*
thing. A weaker setting is not given a worse squad or a rigged engine — it
plays the identical game and simply captains it worse: it trusts the solver
less, reads the player less, and misjudges an over more often. These tests
assert that gradient rather than trusting the numbers in the table, plus the
plumbing that carries the player's choice from the button through to the match.
"""

import random
import unittest

from engine.approach_modifiers import BATTING_KEYS, BOWLING_KEYS
from services import bot_captain as bc
from tests.test_bot_captain import _state

SAMPLES = 1200


def _top_share(fn, state, n=SAMPLES):
    """How often the bot's single favourite plan comes up — its predictability."""
    counts = {}
    for _ in range(n):
        key = fn(state)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / n, counts


def _at(level, **kw):
    state = _state(bot_difficulty=level, **kw)
    state["bat_stats"]["100"] = {"runs": 8, "balls": 14}
    return state


class DifficultySettingTests(unittest.TestCase):
    def test_every_level_is_a_real_setting(self):
        for key in bc.DIFFICULTY_ORDER:
            self.assertIn(key, bc.DIFFICULTIES)
            self.assertTrue(bc.DIFFICULTIES[key]["label"])

    def test_unknown_and_missing_values_fall_back_to_normal(self):
        self.assertEqual(bc.normalize_difficulty("nonsense"), bc.DEFAULT_DIFFICULTY)
        self.assertEqual(bc.normalize_difficulty(None), bc.DEFAULT_DIFFICULTY)
        self.assertEqual(bc.normalize_difficulty("HARD"), "hard")

    def test_assign_sticks_on_the_state(self):
        state = {}
        self.assertEqual(bc.assign_difficulty(state, "hard"), "hard")
        self.assertEqual(state["bot_difficulty"], "hard")
        self.assertEqual(bc.difficulty_label(state), bc.DIFFICULTIES["hard"]["label"])

    def test_a_match_with_no_difficulty_still_plays(self):
        """Matches created before this existed must not break on resume."""
        state = _state()
        state.pop("bot_difficulty", None)
        self.assertIn(bc.pick_bowling_approach(state), BOWLING_KEYS)
        self.assertIn(bc.pick_batting_approach(state), BATTING_KEYS)


class DifficultyStrengthTests(unittest.TestCase):
    """The gradient a player should actually feel."""

    def test_harder_bots_commit_to_the_right_plan(self):
        """Easy spreads its bets; Hard backs the plan the solver likes.

        Predictability and strength are the same axis here: the strongest play
        in a given over is a fairly concentrated mix, so an Easy bot that keeps
        its options wide open is, by construction, easier to play against.
        """
        shares = {}
        for level in bc.DIFFICULTY_ORDER:
            random.seed(20260727)
            shares[level], _ = _top_share(bc.pick_bowling_approach, _at(level))
        self.assertLess(shares["easy"], shares["normal"])
        self.assertLess(shares["normal"], shares["hard"])

    def test_even_the_hardest_bot_still_mixes(self):
        """Unpredictability is not a difficulty setting — it is the whole point."""
        random.seed(20260727)
        share, counts = _top_share(bc.pick_bowling_approach, _at("hard"))
        self.assertLess(share, 0.9)
        self.assertGreaterEqual(len(counts), 4)

    def test_easy_still_plays_cricket(self):
        """Loose is not the same as random — Easy must not be nonsense."""
        random.seed(20260727)
        _share, counts = _top_share(bc.pick_bowling_approach, _at("easy"))
        self.assertTrue(set(counts).issubset(BOWLING_KEYS))
        # A uniform bot would sit at 20% each; Easy still has a preference.
        self.assertGreater(max(counts.values()) / SAMPLES, 0.22)

    def test_only_the_harder_bots_lean_on_reading_you(self):
        weights = []
        for level in bc.DIFFICULTY_ORDER:
            state = _at(level, is_bot_match=True, bot_user_id=99,
                        bat_team_id=7, bowl_team_id=99)
            for _ in range(8):
                bc.observe_over(state, {"batting_approach": "ultra",
                                        "bowling_approach": "balanced",
                                        "over_runs": 14, "over_wickets": 0})
            _predicted, weight = bc._predict_opponent(state, bot_batting=False)
            weights.append(weight)
        self.assertLess(weights[0], weights[1])
        self.assertLess(weights[1], weights[2])

    def test_every_level_always_returns_a_legal_plan(self):
        for level in bc.DIFFICULTY_ORDER:
            random.seed(3)
            for over in (1, 9, 18):
                state = _at(level, current_over=over)
                for _ in range(40):
                    self.assertIn(bc.pick_bowling_approach(state), BOWLING_KEYS)
                    self.assertIn(bc.pick_batting_approach(state), BATTING_KEYS)

    def test_difficulty_does_not_bend_the_rules(self):
        """An Easy bot is worse at captaincy, not exempt from the laws.

        Whatever the setting, the bowler it picks has to be one the shared
        eligibility check already approved — same quota, same no-back-to-back
        rule a human plays under.
        """
        from services import cipl_match as cm
        for level in bc.DIFFICULTY_ORDER:
            random.seed(9)
            state = _at(level, current_over=12)
            legal = {p["roster_id"] for p in cm.eligible_bowlers(state)}
            for _ in range(60):
                self.assertIn(bc.pick_bowler(state)["roster_id"], legal)


try:
    from handlers import botlevel
    _HAVE_TELEGRAM = True
except Exception:                              # pragma: no cover
    _HAVE_TELEGRAM = False


@unittest.skipUnless(_HAVE_TELEGRAM, "python-telegram-bot not importable")
class DifficultyPlumbingTests(unittest.TestCase):
    """The player's choice has to survive the whole setup flow."""

    def test_the_choice_is_remembered_and_reused(self):
        bot_data = {}
        self.assertIsNone(botlevel.remembered_level(bot_data, 5))
        self.assertEqual(botlevel.level_for_match(bot_data, 5),
                         bc.DEFAULT_DIFFICULTY)
        botlevel.remember_level(bot_data, 5, "hard")
        self.assertEqual(botlevel.level_for_match(bot_data, 5), "hard")
        # One player's choice is not another's.
        self.assertEqual(botlevel.level_for_match(bot_data, 6),
                         bc.DEFAULT_DIFFICULTY)

    def test_a_junk_choice_never_reaches_the_match(self):
        bot_data = {}
        botlevel.remember_level(bot_data, 5, "impossible")
        self.assertEqual(botlevel.level_for_match(bot_data, 5),
                         bc.DEFAULT_DIFFICULTY)

    def test_the_button_row_belongs_to_the_player_who_asked(self):
        """In a group the prompt is public, so the callback carries an owner."""
        markup = botlevel._rows("lp", "", "hard", owner_id=4242)
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(datas), len(bc.DIFFICULTY_ORDER))
        for data in datas:
            self.assertTrue(data.startswith("botdiff_"))
            self.assertTrue(data.endswith("_4242"))
            # Telegram hard-limits callback data to 64 bytes.
            self.assertLessEqual(len(data.encode()), 64)

    def test_the_current_choice_is_marked_on_the_row(self):
        markup = botlevel._rows("cipl", "ipl", "hard", owner_id=1)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        marked = [t for t in labels if t.startswith("•")]
        self.assertEqual(len(marked), 1)
        self.assertIn("Hard", marked[0])

    def test_mark_bot_match_records_the_chosen_difficulty(self):
        from handlers.cipl_play import mark_bot_match
        state = mark_bot_match({}, bot_user_id=99, difficulty="hard")
        self.assertEqual(state["bot_difficulty"], "hard")
        self.assertTrue(state["is_bot_match"])
        self.assertTrue(state["stats_disabled"])       # still unranked practice
        # No choice recorded anywhere → the safe middle setting.
        self.assertEqual(mark_bot_match({}, 99)["bot_difficulty"],
                         bc.DEFAULT_DIFFICULTY)


if __name__ == "__main__":
    unittest.main()
