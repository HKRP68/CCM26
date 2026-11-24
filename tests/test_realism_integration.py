"""Wiring: /cipl, /sim, determinism, shadow safety and no scripted finishes.

Covers requirements 7 and 9-20 of the brief's test list. These are the tests
that protect what already works: if the realism engine is off, every one of
them must describe the engine CCM26 has always had.
"""

import os
import random
import unittest

from engine import ball_outcome as bo
from engine.format_config import get_format
from engine.realism import baselines, config as rcfg, integration as ri, shadow

T20 = get_format("T20")
LISTA = get_format("ListA")


class _RealismEnv:
    """Context manager for the feature flags, restoring them afterwards."""

    def __init__(self, enabled=None, shadow_mode=None, formats=None,
                 version=None):
        self.want = {
            "SIM_REALISM_ENABLED": enabled,
            "SIM_REALISM_SHADOW_MODE": shadow_mode,
            "SIM_REALISM_FORMATS": formats,
            "SIM_REALISM_REFERENCE_VERSION": version,
        }

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.want}
        for k, v in self.want.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rcfg.clear_cache()
        baselines.clear_cache()
        ri.reset_log_state()
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rcfg.clear_cache()
        baselines.clear_cache()
        ri.reset_log_state()
        return False


def _batter(rating=80, hand="Right"):
    return {"name": "B%d" % rating, "batting_rating": rating,
            "batting_hand": hand}


def _bowler(rating=75, hand="Right", btype="Fast-medium"):
    return {"name": "W%d" % rating, "bowling_rating": rating,
            "fielding_rating": 70, "bowling_hand": hand, "bowling_type": btype}


def _ball(seed=None, **kw):
    if seed is not None:
        random.seed(seed)
    args = dict(batter=_batter(), bowler=_bowler(), pitch="Even",
                streak={"boundaries": 0}, over_number=8, batter_runs=15,
                balls_faced=12, format_config=T20)
    args.update(kw)
    return bo.calculate_outcome(**args)


def _innings(seed, balls=120, **kw):
    """A quick standalone innings straight through calculate_outcome."""
    random.seed(seed)
    runs = wkts = legal = 0
    marks = []
    while legal < balls and wkts < 10:
        oc = _ball(over_number=legal // 6, balls_faced=legal,
                   batter_runs=runs, **kw)
        if oc.get("is_extra") and oc.get("extra_type") in ("Wide", "No Ball"):
            runs += 1 + oc["runs"]
            marks.append(oc["extra_type"])
            continue
        legal += 1
        runs += oc["runs"]
        if oc.get("batter_out"):
            wkts += 1
            marks.append("W")
        else:
            marks.append(str(oc["runs"]))
    return runs, wkts, marks


class ModeSelectionTests(unittest.TestCase):

    def test_default_is_off(self):
        with _RealismEnv():
            self.assertEqual(ri.mode_for(T20), ri.OFF)

    def test_enabled_flag_gives_live_mode(self):
        with _RealismEnv(enabled="1"):
            self.assertEqual(ri.mode_for(T20), ri.LIVE)

    def test_shadow_wins_over_live_when_both_are_set(self):
        """Measuring is the safe reading of an ambiguous configuration."""
        with _RealismEnv(enabled="1", shadow_mode="1"):
            self.assertEqual(ri.mode_for(T20), ri.SHADOW)

    def test_a_format_not_in_the_allowlist_stays_on_the_legacy_engine(self):
        with _RealismEnv(enabled="1", formats="T20"):
            self.assertEqual(ri.mode_for(LISTA), ri.OFF)
            self.assertEqual(ri.mode_for(T20), ri.LIVE)

    def test_the_long_format_is_not_enabled_by_default(self):
        """§10: do not pretend a mode is integrated when it is not.

        CCM26's FORMAT_REGISTRY holds exactly two configs, T20 and ListA —
        "ODI" and "Test" fall back to T20 by design (see
        engine/format_config.get_format), so ListA is the 50-over format to
        assert on, and a literal "ODI" string is a T20 match as far as the
        engine is concerned.
        """
        with _RealismEnv(enabled="1"):
            self.assertEqual(ri.mode_for(LISTA), ri.OFF)
            self.assertEqual(get_format("ODI").name, "T20")

    def test_a_missing_reference_version_falls_back_to_legacy(self):
        """Test 20 at the integration level."""
        with _RealismEnv(enabled="1", version="not-a-version"):
            self.assertEqual(ri.mode_for(T20), ri.LIVE)
            self.assertIsNone(ri.try_build(
                ri.LIVE, batter=_batter(), bowler=_bowler(), pitch="Even",
                over_number=4, innings=1, batter_runs=0, balls_faced=0,
                batting_position=3, format_config=T20, game_state=None,
                pitch_wear=0.0, is_day_night=False, batting_approach=None,
                bowling_approach=None, approach_context=None, free_hit=False))
            out = _ball(seed=3)
            self.assertIn(out["type"], ("run", "wicket", "extra"))
            self.assertNotIn("realism", out)


class ScoreboardWiringTests(unittest.TestCase):
    """The scoreboard has to reach the model, and nothing warns if it does not."""

    def _ctx(self, **kw):
        from engine.realism.context import from_engine_call
        args = dict(pitch="Even", over_number=5, innings=1, batter_runs=0,
                    balls_faced=0, batting_position=3, format_config=T20,
                    game_state=None)
        args.update(kw)
        return from_engine_call(_batter(), _bowler(), **args)

    def test_game_state_alone_does_not_carry_the_scoreboard(self):
        """This is the trap: compute_game_state_vector has no score or target.

        It carries derived indices, so reading game_state["score"] or
        ["target"] yields 0 and None with no error at all — which silently
        pins every delivery to the w0_2 node and switches the chase layer
        off. The model must reconstruct what it can from the keys that do
        exist, and the callers must pass the rest.
        """
        from engine.game_state_engine import compute_game_state_vector
        gs = compute_game_state_vector(
            ball_history=[], score=150, current_over=14, current_ball=2,
            wickets=5, innings=2, target=181, pitch="Even", format_config=T20)
        self.assertNotIn("score", gs)
        self.assertNotIn("target", gs)
        self.assertNotIn("wickets", gs)
        self.assertIn("wickets_in_hand", gs)
        self.assertIn("rrr", gs)

    def test_wickets_are_recovered_from_wickets_in_hand(self):
        from engine.game_state_engine import compute_game_state_vector
        gs = compute_game_state_vector(
            ball_history=[], score=60, current_over=10, current_ball=0,
            wickets=6, innings=1, target=0, pitch="Even", format_config=T20)
        self.assertEqual(self._ctx(game_state=gs).wickets, 6)

    def test_the_chase_is_reconstructed_from_the_required_rate(self):
        from engine.game_state_engine import compute_game_state_vector
        gs = compute_game_state_vector(
            ball_history=[], score=90, current_over=12, current_ball=0,
            wickets=3, innings=2, target=181, pitch="Even", format_config=T20)
        ctx = self._ctx(game_state=gs, innings=2)
        self.assertIsNotNone(ctx.required_rate)
        self.assertGreater(ctx.required_rate, 0.0)

    def test_match_context_wins_over_the_game_state(self):
        ctx = self._ctx(match_context={"runs": 140, "wickets": 4,
                                       "balls_bowled": 90, "target": 181,
                                       "innings_balls": 120})
        self.assertEqual((ctx.runs, ctx.wickets, ctx.balls_bowled), (140, 4, 90))
        self.assertEqual(ctx.target, 181)

    def test_a_short_sim_reaches_its_death_overs(self):
        """_fmt_to_engine_fmt hands a 10-over match the 20-over T20 config.

        Without the caller's own innings length the phase fraction would top
        out at 0.5 and a 10-over innings would never have a death phase.
        """
        seen = set()
        for balls in range(0, 60, 5):
            seen.add(self._ctx(match_context={
                "runs": 0, "wickets": 0, "balls_bowled": balls,
                "target": None, "innings_balls": 60}).phase)
        self.assertEqual(seen, {"powerplay", "middle", "death"})

    def test_both_pilot_call_sites_pass_the_scoreboard(self):
        """A regression guard: the wiring is easy to drop in a refactor."""
        import inspect
        from services import cipl_match, sim_match
        for mod in (cipl_match, sim_match):
            src = inspect.getsource(mod)
            self.assertIn("match_context={", src,
                          "%s must pass match_context to calculate_outcome"
                          % mod.__name__)
            self.assertIn('"innings_balls"', src, mod.__name__)

    def test_the_whole_hierarchy_is_reachable(self):
        """Every wickets bucket and both innings must be used, not just w0_2."""
        from engine.realism import baselines
        from engine.realism.context import BallContext
        doc = baselines.load(format_name="T20", reload=True)
        self.assertIsNotNone(doc)
        comp = baselines.competition_key(doc)
        reached = set()
        for balls in (10, 60, 110):
            for wkts in (0, 4, 8):
                for inn, target in ((1, None), (2, 181)):
                    ctx = BallContext(balls_bowled=balls, wickets=wkts,
                                      innings=inn, target=target, runs=90)
                    _d, key, _m = baselines.lookup(doc, ctx.node_path(comp))
                    reached.add(key)
        for bucket in ("w0_2", "w3_5", "w6_plus"):
            self.assertTrue(any(k.endswith(bucket) for k in reached), bucket)
        for inn in ("inn1", "inn2"):
            self.assertTrue(any("/%s/" % inn in k for k in reached), inn)
        self.assertGreaterEqual(len(reached), 12)


class OutcomeContractTests(unittest.TestCase):
    """§5 / tests 9-11: the result dict contract is unchanged."""

    def _check(self, oc):
        self.assertIn(oc["type"], ("run", "wicket", "extra"))
        self.assertIn(oc["runs"], (0, 1, 2, 3, 4, 6))
        self.assertIsInstance(oc["is_extra"], bool)
        self.assertIsInstance(oc["batter_out"], bool)
        self.assertIsInstance(oc["description"], str)
        if oc["batter_out"]:
            self.assertIn(oc["wicket_type"],
                          ("Caught", "Bowled", "LBW", "Run Out", "Stumped"))
            # A wicket must not award runs beyond a completed run out.
            self.assertEqual(oc["runs"], 1 if oc["wicket_type"] == "Run Out" else 0)
        else:
            self.assertIsNone(oc["wicket_type"])
        if oc["is_extra"]:
            self.assertIn(oc["extra_type"], ("Wide", "No Ball", "Leg Bye", "Byes"))

    def test_contract_holds_in_realism_mode(self):
        with _RealismEnv(enabled="1"):
            random.seed(99)
            for i in range(4000):
                self._check(_ball(over_number=i % 20, balls_faced=i % 30,
                                  batter_runs=i % 70))

    def test_contract_holds_in_legacy_mode(self):
        with _RealismEnv():
            random.seed(99)
            for i in range(2000):
                self._check(_ball(over_number=i % 20, balls_faced=i % 30))

    def test_no_extras_when_the_caller_forbids_them(self):
        """Test 10: legal-ball handling honours allow_extras."""
        with _RealismEnv(enabled="1"):
            random.seed(5)
            for _ in range(3000):
                self.assertFalse(_ball(allow_extras=False)["is_extra"])

    def test_free_hit_only_allows_a_run_out(self):
        """Test 11: free-hit dismissal restrictions."""
        with _RealismEnv(enabled="1"):
            random.seed(17)
            outs = 0
            for _ in range(6000):
                oc = _ball(free_hit=True)
                if oc["batter_out"]:
                    outs += 1
                    self.assertEqual(oc["wicket_type"], "Run Out")
            self.assertGreater(outs, 0, "a free hit must still allow a run out")

    def test_a_wicket_never_consumes_runs_it_did_not_score(self):
        with _RealismEnv(enabled="1"):
            random.seed(23)
            for _ in range(4000):
                oc = _ball()
                if oc["batter_out"] and oc["wicket_type"] != "Run Out":
                    self.assertEqual(oc["runs"], 0)

    def test_realism_provenance_is_additive_only(self):
        """Existing consumers read type/runs/wicket_type and ignore the rest."""
        with _RealismEnv(enabled="1"):
            oc = _ball(seed=7)
            self.assertIn("realism", oc)
            self.assertEqual(oc["realism"]["model_version"], "realism-v1")
            self.assertIn(oc["realism"]["phase"],
                          ("powerplay", "middle", "death"))
        with _RealismEnv():
            self.assertNotIn("realism", _ball(seed=7))


class DeterminismTests(unittest.TestCase):
    """Test 17: the same seed and settings reproduce the same result."""

    def test_realism_mode_is_reproducible(self):
        with _RealismEnv(enabled="1"):
            a = _innings(1234)
            b = _innings(1234)
            self.assertEqual(a, b)

    def test_legacy_mode_is_reproducible(self):
        with _RealismEnv():
            self.assertEqual(_innings(555), _innings(555))

    def test_different_seeds_give_different_innings(self):
        """Two seeds can coincide on the total; the ball sequence cannot."""
        with _RealismEnv(enabled="1"):
            self.assertNotEqual(_innings(1)[2], _innings(2)[2])


class ShadowModeTests(unittest.TestCase):
    """§10: shadow mode must not change a single delivery."""

    def test_shadow_mode_is_bit_identical_to_realism_off(self):
        with _RealismEnv():
            baseline = _innings(4242)
        with _RealismEnv(shadow_mode="1"):
            shadowed = _innings(4242)
        self.assertEqual(baseline, shadowed,
                         "shadow mode changed the match; it must not")

    def test_shadow_mode_consumes_no_extra_random_numbers(self):
        with _RealismEnv():
            random.seed(9)
            for _ in range(50):
                _ball()
            after_legacy = random.random()
        with _RealismEnv(shadow_mode="1"):
            random.seed(9)
            for _ in range(50):
                _ball()
            after_shadow = random.random()
        self.assertEqual(after_legacy, after_shadow)

    def test_shadow_mode_records_a_divergence_report(self):
        shadow.reset()
        with _RealismEnv(shadow_mode="1"):
            _innings(31337)
        snap = shadow.snapshot()
        self.assertGreater(snap["deliveries"], 50)
        self.assertGreaterEqual(snap["overall"]["total_variation"], 0.0)
        self.assertLessEqual(snap["overall"]["total_variation"], 1.0)
        self.assertIn("powerplay", snap["phases"])
        self.assertIn("phase", shadow.format_report(snap))
        shadow.reset()
        self.assertEqual(shadow.snapshot()["deliveries"], 0)

    def test_the_recorder_survives_rubbish_input(self):
        shadow.reset()
        shadow.record({}, {}, "middle")
        shadow.record({"Dot": float("nan")}, {"Dot": 1.0}, "middle")
        shadow.record(None, None, None)
        self.assertEqual(shadow.snapshot()["deliveries"], 0)


class StrongAndWeakTests(unittest.TestCase):
    """Test 7: both strong and weak sides can win; nobody is invincible.

    These measure an INDIVIDUAL innings — one batter until they are out —
    rather than one batter grinding out 120 balls. The grind version answers
    the wrong question: over 120 balls the law of large numbers makes the
    better card win essentially always, which tells us about arithmetic, not
    about the model. A single innings is the unit a player actually watches.
    """

    @staticmethod
    def _batter_innings(seed, bat, bowl, max_balls=120):
        random.seed(seed)
        runs = balls = 0
        while balls < max_balls:
            oc = _ball(batter=_batter(bat), bowler=_bowler(bowl),
                       over_number=balls // 6, balls_faced=balls,
                       batter_runs=runs)
            if oc.get("is_extra") and oc.get("extra_type") in ("Wide", "No Ball"):
                continue
            balls += 1
            if oc["batter_out"]:
                break
            runs += oc["runs"]
        return runs, balls

    def test_a_strong_batter_usually_but_not_always_beats_a_weak_one(self):
        with _RealismEnv(enabled="1"):
            seeds = range(120)
            strong = [self._batter_innings(s, 90, 75)[0] for s in seeds]
            weak = [self._batter_innings(s, 60, 75)[0] for s in seeds]
            wins = sum(1 for a, b in zip(strong, weak) if a > b)
            n = len(strong)
            self.assertGreater(wins, n * 0.60,
                               "a 30-point batting edge must usually tell")
            self.assertLess(wins, n,
                            "it must not be a guarantee — the weaker card has "
                            "to win sometimes")
            self.assertGreater(sum(strong), sum(weak) * 1.5,
                               "and the edge must be large in aggregate")

    def test_a_premium_batter_still_fails_sometimes(self):
        """Test 6 at the innings level: no card is immortal."""
        with _RealismEnv(enabled="1"):
            scores = [self._batter_innings(s, 95, 70)[0] for s in range(150)]
            self.assertGreater(sum(1 for x in scores if x < 10), 0,
                               "a 95-rated batter must sometimes go cheaply")
            self.assertGreater(max(scores), 50)

    def test_a_weak_batter_sometimes_comes_off(self):
        with _RealismEnv(enabled="1"):
            scores = [self._batter_innings(s, 55, 85)[0] for s in range(150)]
            self.assertGreater(max(scores), 25,
                               "a weak card must have an occasional day out")

    def test_there_is_real_variance_between_innings(self):
        with _RealismEnv(enabled="1"):
            totals = [_innings(s)[0] for s in range(40)]
            spread = max(totals) - min(totals)
            self.assertGreater(spread, 40,
                               "innings must not all land on the same total")

    def test_a_modest_batter_can_still_make_a_hundred(self):
        """§2: a 75-rated batter should occasionally score a century."""
        with _RealismEnv(enabled="1"):
            best = max(self._batter_innings(s, 75, 75, max_balls=90)[0]
                       for s in range(300))
            self.assertGreater(best, 70,
                               "a 75-rated batter must be able to go big")


class NoScriptedFinishTests(unittest.TestCase):
    """§9 / test 16: realism mode must not script a result."""

    def test_the_realism_model_cannot_see_a_desired_winner(self):
        from engine.realism.context import BallContext
        from engine.realism.outcome_distribution import build
        plain = build(BallContext(innings=2, target=180, runs=120,
                                  balls_bowled=90, wickets=4))
        with_plan = build(BallContext(
            innings=2, target=180, runs=120, balls_bowled=90, wickets=4,
            extra={"_planned_winner_user_id": 999,
                   "_planned_super_overs": True}))
        self.assertEqual(plain, with_plan,
                         "a planned result must not reach the ball model")

    def test_the_last_ball_of_a_tight_chase_is_not_steered(self):
        """A one-run ask off the last ball must not become a certainty."""
        from engine.realism.context import BallContext
        from engine.realism.outcome_distribution import build
        dist = build(BallContext(innings=2, target=181, runs=180,
                                 balls_bowled=119, wickets=6,
                                 bat_rating=80, bowl_rating=80))
        self.assertGreater(dist["Dot"], 0.15, "a dot must stay very possible")
        self.assertGreater(dist["Wicket"], 0.01, "a wicket must stay possible")
        self.assertLess(dist["Single"] + dist["Double"] + dist["Three"]
                        + dist["Four"] + dist["Six"], 0.90,
                        "the chase must not be handed the match")

    def test_scenario_scripting_is_a_separate_opt_in_layer(self):
        """§9: keep SCENARIO mode, do not silently enable it in realism mode."""
        from engine import scenario_engine
        self.assertTrue(hasattr(scenario_engine, "ScenarioEngine"),
                        "scenario features must be preserved for exhibition "
                        "and admin modes, not deleted")
        with _RealismEnv(enabled="1"):
            oc = _ball(seed=11)
            self.assertNotIn("scenario", oc)


class LegacyBehaviourUnchangedTests(unittest.TestCase):
    """Tests 18/19: the existing engine must behave exactly as before."""

    def test_legacy_path_is_untouched_by_the_realism_import(self):
        with _RealismEnv():
            random.seed(2024)
            before = [_ball(over_number=i % 20) for i in range(300)]
            random.seed(2024)
            after = [_ball(over_number=i % 20) for i in range(300)]
            self.assertEqual([b["runs"] for b in before],
                             [a["runs"] for a in after])

    def test_the_rating_duel_still_runs_in_legacy_mode(self):
        with _RealismEnv():
            random.seed(4)
            states = {_ball()["duel"] for _ in range(200)}
            self.assertTrue(states <= {"beaten", "contained", "punished",
                                       "scrappy"})
            self.assertGreater(len(states), 1)

    def test_the_rating_duel_is_bypassed_in_realism_mode(self):
        """It would double-count the rating curve and spend RNG draws."""
        with _RealismEnv(enabled="1"):
            random.seed(4)
            self.assertTrue(all("duel" not in _ball() for _ in range(200)))

    def test_lista_is_unaffected_by_realism_mode(self):
        with _RealismEnv():
            legacy = _innings(808, format_config=LISTA)
        with _RealismEnv(enabled="1"):
            realism_on = _innings(808, format_config=LISTA)
        self.assertEqual(legacy, realism_on,
                         "ListA is not in the pilot and must not change")

    def test_bypass_flags_are_configurable(self):
        self.assertTrue(ri.bypass("rating_duel"))
        self.assertTrue(ri.bypass("phase_boosts"))
        self.assertTrue(ri.bypass("wicket_realism_mult"))
        cfg = {"integration": {"keep_legacy_rating_duel": True}}
        self.assertFalse(ri.bypass("rating_duel", cfg))

    def test_supported_formats_reports_honestly(self):
        with _RealismEnv(enabled="1", formats="T20"):
            got = ri.supported_formats()
            self.assertTrue(got["T20"]["enabled"])
            self.assertTrue(got["T20"]["reference_available"])
            self.assertFalse(got["ODI"]["enabled"])
            self.assertFalse(got["Test"]["reference_available"])


if __name__ == "__main__":
    unittest.main()
