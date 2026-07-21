"""Tests for the thriller-forward drama tuning and the LetsPlay rating/trait-aware
clutch finale (services/cipl_match).

Covers:
  * Change A — finish-profile weighting is thriller-forward (clusters at the wire).
  * Change B — the LetsPlay clutch hook scales with chase intent, and the death
    overs are resolved by ratings + traits (a strong finisher genuinely beats a
    rabbit; the Finisher trait lifts the six-rate) instead of a scripted value.
  * Scope — CIPL (no ``clutch_finale`` flag) still runs the scripted scenario
    finale; LetsPlay bypasses it.
"""

import random
import unittest

from services import cipl_match as cm


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _mk(rid, name, cat, bat, bowl, style="Fast", traits=None):
    d = {"roster_id": rid, "player_id": rid, "name": name, "rating": max(bat, bowl),
         "category": cat, "bat_rating": bat, "bowl_rating": bowl,
         "bowl_style": style, "bowl_hand": "Right", "bat_hand": "Right"}
    if traits:
        d["traits"] = traits
    return d


def _last_over_state(bat_rating=70, bowl_rating=75, striker_traits=None,
                     bowler_traits=None, need=12, clutch=True, pitch="Flat"):
    """A 2nd-innings state parked at the start of the final (20th) over, needing
    ``need`` runs off 6 balls, with the strike batter optionally carrying traits."""
    strike = _mk(1, "B1", "Batsman", bat_rating, 30, traits=striker_traits)
    bat_xi = [strike] + [_mk(i, "B%d" % i, "Batsman", bat_rating, 30)
                         for i in range(2, 8)] \
        + [_mk(i, "AR%d" % i, "All-rounder", 60, 70, "Off spin") for i in range(8, 12)]
    death = _mk(101, "W1", "Bowler", 30, bowl_rating, "Fast", traits=bowler_traits)
    bowl_xi = [death] + [_mk(100 + i, "W%d" % i, "Bowler", 30, 75, "Fast")
                         for i in range(2, 7)] \
        + [_mk(100 + i, "BAR%d" % i, "All-rounder", 60, 70, "Leg spin")
           for i in range(7, 12)]
    s = cm.build_cipl_state(1, 20, 10, 20, 111, 222, bat_xi, bowl_xi,
                            "A", "B", -100, pitch, False)
    if clutch:
        s["clutch_finale"] = True
    s["innings"] = 2
    s["current_over"] = 20
    s["current_ball"] = 0
    s["total_runs"] = 100
    s["target"] = 100 + need
    s["current_bowler"] = bowl_xi[0]
    s["bowling_approach"] = "balanced"
    s["batting_approach"] = "aggressive"
    return s


def _sim_last_over(**kw):
    """Return (sixes, made_target, runs) for one simulated final over."""
    s = _last_over_state(**kw)
    start = s["total_runs"]
    need = s["target"] - start
    summ = cm.simulate_over(s)
    got = s["total_runs"] - start
    return summ["over_timeline"].count("6"), (got >= need), got


def _monte(n=300, **kw):
    sixes = wins = runs = 0
    for k in range(n):
        random.seed(k)
        six, won, got = _sim_last_over(**kw)
        sixes += six
        wins += 1 if won else 0
        runs += got
    return sixes / n, wins / n, runs / n


# --------------------------------------------------------------------------- #
# Change A — thriller-forward finish profile
# --------------------------------------------------------------------------- #

class FinishProfileTests(unittest.TestCase):
    def test_default_scenario_probability_is_thriller_forward(self):
        # Default arm-rate raised from 0.30 → 0.55 (env still overrides).
        self.assertGreaterEqual(cm._scenario_probability(), 0.5)

    def test_finishes_cluster_at_the_wire(self):
        balls = 20 * 6
        last_ball = one_to_spare = super_over = 0
        random.seed(0)
        for _ in range(4000):
            stype, fb = cm._select_finish_profile(20)
            if stype == "super_over_thriller":
                super_over += 1
            elif fb == balls:
                last_ball += 1
            elif fb == balls - 1:
                one_to_spare += 1
        # Last-ball wins are now the single most common armed outcome, and
        # to-the-wire finishes (last ball / 1-to-spare / super over) dominate.
        self.assertGreater(last_ball, one_to_spare)
        self.assertGreater(last_ball + one_to_spare + super_over, 2000)

    def test_finish_profile_contract_unchanged(self):
        random.seed(1)
        for _ in range(200):
            stype, fb = cm._select_finish_profile(20)
            self.assertIn(stype, cm.SCENARIO_TYPES)
            if stype == "super_over_thriller":
                self.assertIsNone(fb)
            else:
                self.assertTrue(1 <= fb <= 120)


# --------------------------------------------------------------------------- #
# Change B — clutch hook mechanics
# --------------------------------------------------------------------------- #

class ClutchHookTests(unittest.TestCase):
    BASE = {"Dot": 30.0, "Single": 25.0, "Double": 8.0, "Three": 1.0,
            "Four": 12.0, "Six": 8.0, "Wicket": 6.0, "Extras": 2.0}

    def test_high_intent_boosts_boundaries_and_cuts_dots(self):
        # Need 18 off 6 → all-out intent.
        hook = cm._make_clutch_hook(18, 6, 5, is_final_ball=False)
        out = hook(dict(self.BASE))
        self.assertGreater(out["Six"], self.BASE["Six"])
        self.assertGreater(out["Four"], self.BASE["Four"])
        self.assertLess(out["Dot"], self.BASE["Dot"])
        self.assertGreater(out["Wicket"], self.BASE["Wicket"])  # risk of going big

    def test_cruise_plays_safe(self):
        # Need 2 off 12 → cruising; protect wickets, milk singles.
        hook = cm._make_clutch_hook(2, 12, 5, is_final_ball=False)
        out = hook(dict(self.BASE))
        self.assertLess(out["Six"], self.BASE["Six"])
        self.assertLess(out["Wicket"], self.BASE["Wicket"])
        self.assertGreater(out["Dot"], self.BASE["Dot"])

    def test_final_ball_is_six_or_bust(self):
        hi = cm._make_clutch_hook(6, 1, 5, is_final_ball=True)(dict(self.BASE))
        # Final ball spread is maximal: strongest six + hardest dot suppression.
        self.assertGreaterEqual(hi["Six"] / self.BASE["Six"], 1.8)
        self.assertLessEqual(hi["Dot"] / self.BASE["Dot"], 0.6)

    def test_weights_never_negative(self):
        hook = cm._make_clutch_hook(30, 6, 1, is_final_ball=True)
        for v in hook(dict(self.BASE)).values():
            self.assertGreaterEqual(v, 0.0)


# --------------------------------------------------------------------------- #
# Change B — the death overs respect ratings and traits
# --------------------------------------------------------------------------- #

class ClutchFinaleRespectsRatingsAndTraits(unittest.TestCase):
    def test_strong_batter_beats_rabbit_at_the_death(self):
        weak_sixes, weak_wins, weak_runs = _monte(bat_rating=35)
        strong_sixes, strong_wins, strong_runs = _monte(bat_rating=95)
        self.assertGreater(strong_wins, weak_wins + 0.2)
        self.assertGreater(strong_sixes, weak_sixes)
        self.assertGreater(strong_runs, weak_runs)

    def test_finisher_trait_lifts_the_six_rate(self):
        fin = [{"effect_key": "bat_finisher", "level": 5,
                "display_name": "Finisher", "emoji": "🔥"}]
        plain_sixes, plain_wins, _ = _monte(bat_rating=70)
        fin_sixes, fin_wins, _ = _monte(bat_rating=70, striker_traits=fin)
        self.assertGreater(fin_sixes, plain_sixes)
        self.assertGreaterEqual(fin_wins, plain_wins)

    def test_death_bowler_trait_denies_runs(self):
        death = [{"effect_key": "bowl_death", "level": 5,
                  "display_name": "Death Specialist", "emoji": "🎯"}]
        plain_sixes, plain_wins, plain_runs = _monte(bat_rating=80, bowl_rating=80)
        deny_sixes, deny_wins, deny_runs = _monte(
            bat_rating=80, bowl_rating=80, bowler_traits=death)
        self.assertLess(deny_wins, plain_wins + 1e-9)
        self.assertLessEqual(deny_runs, plain_runs)


# --------------------------------------------------------------------------- #
# Scope — CIPL keeps its scripted finale, LetsPlay bypasses it
# --------------------------------------------------------------------------- #

class ClutchFinaleScopeTests(unittest.TestCase):
    def _armed(self, clutch):
        s = _last_over_state(need=18, clutch=clutch)
        # Arm a scripted scenario finishing on the very last ball.
        s["scenario"] = {"type": "controlled_finish", "active": True,
                         "finish_ball": 120, "finale_script": None,
                         "finale_ball_index": 0, "convergence_logged": False,
                         "endgame_checked_overs": []}
        return s

    def test_cipl_still_runs_scripted_override(self):
        # No clutch flag → the scenario engine's scripted finale drives the over.
        calls = [0]
        orig = cm.ScenarioEngine.get_override_outcome

        def wrap(self, b, bo):
            calls[0] += 1
            return orig(self, b, bo)

        cm.ScenarioEngine.get_override_outcome = wrap
        try:
            random.seed(5)
            s = self._armed(clutch=False)
            cm.simulate_over(s)
        finally:
            cm.ScenarioEngine.get_override_outcome = orig
        self.assertGreater(calls[0], 0)
        self.assertGreater(s["scenario"]["finale_ball_index"], 0)

    def test_letsplay_bypasses_scripted_override(self):
        calls = [0]
        orig = cm.ScenarioEngine.get_override_outcome

        def wrap(self, b, bo):
            calls[0] += 1
            return orig(self, b, bo)

        cm.ScenarioEngine.get_override_outcome = wrap
        try:
            random.seed(5)
            s = self._armed(clutch=True)
            cm.simulate_over(s)
        finally:
            cm.ScenarioEngine.get_override_outcome = orig
        self.assertEqual(calls[0], 0)
        self.assertEqual(s["scenario"]["finale_ball_index"], 0)


if __name__ == "__main__":
    unittest.main()
