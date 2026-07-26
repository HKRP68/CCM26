"""Tests for the AI captain that plays the bot's side in /lpbot and /ciplbot.

Pure state-in/decision-out, so these run under plain unittest with no Telegram
and no DB. The point of each test is a tactical guarantee a player would notice
if it broke: the bot never bowls an illegal over, it saves something for the
death, its batting intent tracks the match situation — and, just as importantly,
it never answers the same situation the same way twice in a row on autopilot.

Because every decision is now sampled from a weighted distribution, tactical
claims are asserted over a sample of runs ("this dominates", "this is more
aggressive than that") rather than on a single call.
"""

import random
import unittest

from engine.approach_modifiers import BATTING_KEYS, BOWLING_KEYS
from services import bot_captain as bc
from services import cipl_match as cm

BAT_INDEX = {k: i for i, k in enumerate(bc.BAT_LADDER)}
SAMPLES = 800


class SeededTestCase(unittest.TestCase):
    """Every decision is deliberately random, and the assertions below are about
    the shape of a sampled distribution. Seeding keeps them reproducible instead
    of leaving a small chance of a red CI run on an unlucky draw."""

    def setUp(self):
        random.seed(20260726)


def _player(rid, name, category, bowl_rating, bat_rating=50, traits=()):
    return {
        "roster_id": rid, "player_id": rid, "name": name,
        "category": category, "rating": max(bat_rating, bowl_rating),
        "bat_rating": bat_rating, "bowl_rating": bowl_rating,
        "bowl_style": "Fast", "bowl_hand": "Right", "bat_hand": "Right",
        "traits": [{"effect_key": k, "level": 3} for k in traits],
    }


def _bowl_xi():
    return [
        _player(1, "Ace", "Bowler", 90, 20, traits=("bowl_death",)),
        _player(2, "Newball", "Bowler", 86, 18, traits=("bowl_powerplay",)),
        _player(3, "Spinner", "Bowler", 80, 22),
        _player(4, "Fourth", "Bowler", 74, 25),
        _player(5, "Allround", "All-rounder", 70, 65),
        _player(6, "Opener", "Batsman", 20, 88),
        _player(7, "Keeper", "Wicket Keeper", 15, 80),
        _player(8, "Three", "Batsman", 18, 78),
        _player(9, "Four", "Batsman", 16, 72),
        _player(10, "Five", "Batsman", 14, 68),
        _player(11, "Six", "Batsman", 12, 60),
    ]


def _state(persona="tactician", **over):
    bat_xi = [_player(100 + i, f"Bat{i}", "Batsman", 20, 80 - i) for i in range(11)]
    bowl_xi = _bowl_xi()
    state = {
        "match_id": 1, "overs": 20, "ball_format": "T20",
        "innings": 1, "target": None,
        "current_over": 1, "current_ball": 0,
        "total_runs": 0, "total_wickets": 0,
        "wicket_limit": 10,
        "bat_xi": bat_xi, "bowl_xi": bowl_xi,
        "batting_order": list(bat_xi),
        "striker_idx": 0, "non_striker_idx": 1,
        "bat_stats": {str(p["roster_id"]): {"runs": 0, "balls": 0} for p in bat_xi},
        "bowl_stats": {str(p["roster_id"]): {"balls": 0, "runs": 0, "wickets": 0}
                       for p in bowl_xi},
        "prev_bowler_rid": None,
        "spell_rid": None, "spell_units": 0,
        # A fixed persona keeps the tactical assertions about tactics rather
        # than about which style happened to be drawn.
        "bot_persona": persona,
    }
    state.update(over)
    return state


def _sample_counts(fn, state, n=SAMPLES):
    counts = {}
    for _ in range(n):
        key = fn(state)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _mean_bat_index(state, n=SAMPLES):
    return sum(BAT_INDEX[bc.pick_batting_approach(state)] for _ in range(n)) / n


class PhaseTests(SeededTestCase):
    def test_t20_phases(self):
        self.assertEqual(bc.phase(_state(current_over=1)), "powerplay")
        self.assertEqual(bc.phase(_state(current_over=6)), "powerplay")
        self.assertEqual(bc.phase(_state(current_over=10)), "middle")
        self.assertEqual(bc.phase(_state(current_over=17)), "death")
        self.assertEqual(bc.phase(_state(current_over=20)), "death")

    def test_short_format_still_has_all_three_phases(self):
        seen = {bc.phase(_state(overs=5, current_over=o)) for o in range(1, 6)}
        self.assertEqual(seen, {"powerplay", "middle", "death"})


class PersonaTests(SeededTestCase):
    def test_a_persona_is_drawn_once_and_then_sticks(self):
        state = _state(bot_persona=None)
        first = bc.assign_persona(state)
        self.assertIn(first, bc.PERSONAS)
        for _ in range(20):
            bc.pick_bowling_approach(state)
        self.assertEqual(state["bot_persona"], first)

    def test_different_matches_get_different_styles(self):
        drawn = {bc.assign_persona({}) for _ in range(60)}
        self.assertGreater(len(drawn), 1)

    def test_an_aggressor_bats_harder_than_a_grinder(self):
        hot = _mean_bat_index(_state(persona="aggressor", current_over=10))
        cold = _mean_bat_index(_state(persona="grinder", current_over=10))
        self.assertGreater(hot, cold)


class BowlerSelectionTests(SeededTestCase):
    def test_never_picks_the_previous_bowler(self):
        # Ace is the strongest bowler by a distance, so only the no-back-to-back
        # rule can keep them out of the next over.
        state = _state(current_over=2, prev_bowler_rid=1)
        for _ in range(30):
            self.assertNotEqual(bc.pick_bowler(state)["roster_id"], 1)

    def test_never_exceeds_the_per_bowler_quota(self):
        state = _state(current_over=10)
        quota = cm.max_bowler_overs(state)
        # Ace and Newball have already bowled out.
        for rid in (1, 2):
            state["bowl_stats"][str(rid)]["balls"] = quota * 6
        for _ in range(30):
            self.assertNotIn(bc.pick_bowler(state)["roster_id"], (1, 2))

    def test_prefers_a_death_specialist_at_the_death(self):
        state = _state(current_over=19)
        picks = [bc.pick_bowler(state)["roster_id"] for _ in range(SAMPLES)]
        # Ace carries bowl_death and the best rating — they should dominate.
        self.assertGreater(picks.count(1), picks.count(3))

    def test_holds_a_frontline_bowler_back_for_the_death(self):
        # Over 14 with two overs of death still to cover: Ace has exactly two
        # overs left, so those should be earmarked rather than spent now.
        state = _state(current_over=14)
        quota = cm.max_bowler_overs(state)
        state["bowl_stats"]["1"]["balls"] = (quota - 2) * 6
        state["bowl_stats"]["2"]["balls"] = (quota - 2) * 6
        picks = [bc.pick_bowler(state)["roster_id"] for _ in range(SAMPLES)]
        self.assertLess(picks.count(1), picks.count(3))

    def test_the_attack_is_not_a_fixed_rotation(self):
        """The same over, twice, must not always hand the ball to one bowler."""
        state = _state(current_over=8, prev_bowler_rid=3)
        picks = {bc.pick_bowler(state)["roster_id"] for _ in range(SAMPLES)}
        self.assertGreaterEqual(len(picks), 3)

    def test_takes_an_expensive_bowler_out_of_the_attack(self):
        cheap = _state(current_over=12, prev_bowler_rid=9)
        expensive = _state(current_over=12, prev_bowler_rid=9)
        # Same bowler, same over — but in one match they have been carted.
        expensive["bowl_stats"]["1"] = {"balls": 12, "runs": 34, "wickets": 0}
        cheap["bowl_stats"]["1"] = {"balls": 12, "runs": 8, "wickets": 0}
        hit = [bc.pick_bowler(expensive)["roster_id"] for _ in range(SAMPLES)]
        good = [bc.pick_bowler(cheap)["roster_id"] for _ in range(SAMPLES)]
        self.assertLess(hit.count(1), good.count(1))

    def test_always_returns_an_eligible_bowler(self):
        state = _state(current_over=8, prev_bowler_rid=3)
        eligible = {p["roster_id"] for p in cm.eligible_bowlers(state)}
        for _ in range(50):
            self.assertIn(bc.pick_bowler(state)["roster_id"], eligible)

    def test_no_bowlers_returns_none(self):
        state = _state(bowl_xi=[], bat_xi=[], batting_order=[])
        self.assertIsNone(bc.pick_bowler(state))


class BowlingApproachTests(SeededTestCase):
    def test_always_a_valid_key(self):
        for over in range(1, 21):
            for _ in range(10):
                self.assertIn(bc.pick_bowling_approach(_state(current_over=over)),
                              BOWLING_KEYS)

    def test_the_same_situation_produces_different_plans(self):
        """The complaint this whole design answers: the bot must not bowl the
        same plan every time the scoreboard looks the same."""
        state = _state(current_over=9)
        seen = _sample_counts(bc.pick_bowling_approach, state)
        self.assertGreaterEqual(len(seen), 4)
        # ...and no single plan may swallow the over.
        self.assertLess(max(seen.values()) / SAMPLES, 0.8)

    def test_attacks_a_brand_new_batter(self):
        """A wicket is worth more than a dot while the batter is still looking
        for the pace, so the attacking plans come out far more often than they
        do against the same batter once they are set."""
        fresh = _sample_counts(bc.pick_bowling_approach, _state(current_over=9))
        settled = _state(current_over=9)
        settled["bat_stats"]["100"] = {"runs": 14, "balls": 14}
        set_in = _sample_counts(bc.pick_bowling_approach, settled)
        self.assertGreater(fresh.get("aggressive", 0), set_in.get("aggressive", 0))
        self.assertGreater(fresh.get("aggressive", 0), fresh.get("defensive", 0) * 2)

    def test_mixes_it_up_at_the_death(self):
        state = _state(current_over=18)
        state["bat_stats"]["100"] = {"runs": 24, "balls": 20}
        counts = _sample_counts(bc.pick_bowling_approach, state)
        # Between them the two death plans should own the over.
        self.assertGreater(counts.get("mixed", 0) + counts.get("variation", 0),
                           SAMPLES * 0.45)
        self.assertGreater(counts.get("mixed", 0), counts.get("balanced", 0))

    def test_varies_against_a_set_batter(self):
        """Someone timing it needs a change of pace, not more of the same."""
        state = _state(current_over=11)
        state["bat_stats"]["100"] = {"runs": 40, "balls": 20}   # SR 200
        counts = _sample_counts(bc.pick_bowling_approach, state)
        self.assertGreater(counts.get("variation", 0), counts.get("aggressive", 0) * 2)
        self.assertGreater(counts.get("variation", 0) + counts.get("mixed", 0),
                           SAMPLES * 0.45)

    def test_squeezes_when_the_chase_has_run_away(self):
        state = _state(innings=2, target=250, total_runs=60, current_over=14)
        state["bat_stats"]["100"] = {"runs": 20, "balls": 15}
        counts = _sample_counts(bc.pick_bowling_approach, state)
        # They have to swing at everything — deny the boundary rather than
        # hand them one hunting a wicket that is coming anyway.
        self.assertGreater(counts.get("defensive", 0), counts.get("aggressive", 0) * 2)
        self.assertGreater(counts.get("defensive", 0) + counts.get("mixed", 0),
                           SAMPLES * 0.4)


class OpponentReadTests(SeededTestCase):
    """The bot watches what the human does and starts countering it."""

    def _remember(self, state, *, bat=(), bowl=(), runs=8, wkts=0):
        """Play ``bat``/``bowl`` picks into the bot's memory of the match."""
        for key in bat:
            bc.observe_over(state, {"batting_approach": key,
                                    "bowling_approach": "balanced",
                                    "over_runs": runs, "over_wickets": wkts})
        for key in bowl:
            bc.observe_over(state, {"batting_approach": "balanced",
                                    "bowling_approach": key,
                                    "over_runs": runs, "over_wickets": wkts})

    def test_memory_is_ignored_outside_a_bot_match(self):
        state = _state(current_over=10)
        self._remember(state, bat=("ultra",) * 4)
        self.assertNotIn("bot_memory", state)

    def test_a_repeated_batting_plan_gets_countered(self):
        """Four Ultra Attack overs running and the bot starts choking the six off."""
        base = _state(current_over=10, is_bot_match=True, bot_user_id=99,
                      bat_team_id=7, bowl_team_id=99)
        read = _state(current_over=10, is_bot_match=True, bot_user_id=99,
                      bat_team_id=7, bowl_team_id=99)
        self._remember(read, bat=("ultra",) * 4, runs=16)
        naive = _sample_counts(bc.pick_bowling_approach, base)
        wise = _sample_counts(bc.pick_bowling_approach, read)
        self.assertGreater(wise.get("variation", 0), naive.get("variation", 0))

    def test_a_blocking_opponent_is_attacked(self):
        state = _state(current_over=10, is_bot_match=True, bot_user_id=99,
                       bat_team_id=7, bowl_team_id=99)
        state["bat_stats"]["100"] = {"runs": 8, "balls": 14}   # nobody new in
        plain = _sample_counts(bc.pick_bowling_approach, dict(state))
        self._remember(state, bat=("defensive", "rotate", "defensive", "rotate"), runs=3)
        against_a_blocker = _sample_counts(bc.pick_bowling_approach, state)
        self.assertGreater(against_a_blocker.get("aggressive", 0),
                           plain.get("aggressive", 0))

    def test_the_bot_stops_repeating_its_own_plan(self):
        state = _state(current_over=10, is_bot_match=True, bot_user_id=99,
                       bat_team_id=7, bowl_team_id=99)
        state["bat_stats"]["100"] = {"runs": 8, "balls": 14}
        before = _sample_counts(bc.pick_bowling_approach, dict(state))
        bc.observe_over(state, {"batting_approach": "balanced",
                                "bowling_approach": "aggressive",
                                "over_runs": 14, "over_wickets": 0})
        after = _sample_counts(bc.pick_bowling_approach, state)
        self.assertLess(after.get("aggressive", 0), before.get("aggressive", 0))

    def test_observe_over_survives_junk(self):
        state = _state(current_over=3, is_bot_match=True, bot_user_id=99,
                       bat_team_id=99, bowl_team_id=7)
        bc.observe_over(state, None)
        bc.observe_over(state, {"over_runs": None, "over_wickets": None})
        bc.observe_over(None, {"over_runs": 4})
        self.assertIn(bc.pick_batting_approach(state), BATTING_KEYS)


class BattingApproachTests(SeededTestCase):
    def test_always_a_valid_key(self):
        for over in range(1, 21):
            for _ in range(10):
                self.assertIn(bc.pick_batting_approach(_state(current_over=over)),
                              BATTING_KEYS)

    def test_the_same_situation_produces_different_intents(self):
        seen = _sample_counts(bc.pick_batting_approach, _state(current_over=10))
        self.assertGreaterEqual(len(seen), 4)
        self.assertLess(max(seen.values()) / SAMPLES, 0.8)

    def test_intent_rises_with_the_required_rate(self):
        def intent(runs):
            return _mean_bat_index(_state(innings=2, target=runs + 1,
                                          total_runs=0, current_over=8))

        # 60 needed off 78 balls (RRR ~4.6) vs 200 off 78 (RRR ~15).
        self.assertLess(intent(60), intent(200))

    def test_wickets_lost_pulls_the_intent_back(self):
        calm = _state(innings=2, target=140, total_runs=60, current_over=10,
                      total_wickets=1)
        rocky = dict(calm, total_wickets=8)
        self.assertLess(_mean_bat_index(rocky), _mean_bat_index(calm))

    def test_a_desperate_chase_overrides_caution(self):
        # 120 needed off 30 balls with 8 down — blocking cannot win this.
        state = _state(innings=2, target=121, total_runs=0, current_over=16,
                       total_wickets=8)
        for _ in range(SAMPLES):
            self.assertGreaterEqual(BAT_INDEX[bc.pick_batting_approach(state)],
                                    BAT_INDEX["aggressive"])

    def test_a_tailender_plays_within_themselves(self):
        base = _state(current_over=10)
        base["batting_order"][0]["bat_rating"] = 25
        base["batting_order"][0]["rating"] = 30
        star = _state(current_over=10)
        star["batting_order"][0]["bat_rating"] = 92
        star["batting_order"][0]["rating"] = 92
        self.assertLess(_mean_bat_index(base), _mean_bat_index(star))

    def test_the_weak_link_in_the_attack_gets_targeted(self):
        part_timer = _state(current_over=10)
        part_timer["current_bowler"] = _player(6, "Opener", "Batsman", 20, 88)
        spearhead = _state(current_over=10)
        spearhead["current_bowler"] = _player(1, "Ace", "Bowler", 90, 20)
        self.assertGreater(_mean_bat_index(part_timer), _mean_bat_index(spearhead))

    def test_the_last_pair_does_not_throw_the_innings_away(self):
        state = _state(current_over=12, total_wickets=9)
        for _ in range(SAMPLES):
            self.assertLessEqual(BAT_INDEX[bc.pick_batting_approach(state)],
                                 BAT_INDEX["balanced"])


class TossTests(SeededTestCase):
    def test_bot_elects_bat_or_bowl_and_uses_both(self):
        picks = {bc.elect_toss_decision() for _ in range(200)}
        self.assertEqual(picks, {"bat", "bowl"})


if __name__ == "__main__":
    unittest.main()
