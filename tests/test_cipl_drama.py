"""Match drama for /cipl and /letsplay: pitch character, the 270+ brake, the
contest layer, pressure mistakes, dropped catches, sledging and the
partnership line on the approach card."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ball_outcome import calculate_outcome  # noqa: E402
from services import cipl_match as cm  # noqa: E402
from services import match_drama as md  # noqa: E402
from services import sledging  # noqa: E402
from tests.test_cipl_approach import _make_state  # noqa: E402

try:
    import handlers.cipl_play as cp  # needs python-telegram-bot
    _HAVE_CP = True
except Exception:  # pragma: no cover - environment without Telegram deps
    _HAVE_CP = False

_W = {"Dot": 1.0, "Single": 1.0, "Double": 1.0, "Three": 1.0, "Four": 1.0,
      "Six": 1.0, "Wicket": 1.0, "Extras": 1.0}


def _apply(hook):
    return hook(dict(_W)) if hook else dict(_W)


def _play_over(s):
    s["current_bowler"] = cm.eligible_bowlers(s)[0]
    s["bowling_approach"] = "balanced"
    s["batting_approach"] = "balanced"
    return cm.simulate_over(s)


class PitchCharacterTests(unittest.TestCase):

    def test_green_seams_early_and_burns_off(self):
        early, s_early = md.pitch_character("Green", "powerplay", 1, "Fast", 0.05)
        late, s_late = md.pitch_character("Green", "death", 1, "Fast", 0.9)
        self.assertGreater(early["Wicket"], 1.15)
        self.assertGreater(s_early, s_late)
        self.assertAlmostEqual(late.get("Wicket", 1.0), 1.0)

    def test_dusty_rewards_spin_not_pace(self):
        spin, _ = md.pitch_character("Dusty", "middle", 1, "Leg spin", 0.5)
        pace, _ = md.pitch_character("Dusty", "middle", 1, "Fast", 0.5)
        self.assertGreater(spin["Wicket"], 1.2)
        self.assertLess(spin["Six"], 1.0)
        self.assertLess(pace["Wicket"], 1.0)

    def test_dry_cracks_at_the_death(self):
        m, _ = md.pitch_character("Dry", "death", 1, "Fast", 0.9)
        self.assertGreater(m["Wicket"], 1.1)

    def test_bouncy_pays_express_pace(self):
        quick, _ = md.pitch_character("Bouncy", "middle", 1, "Fast", 0.5)
        medium, _ = md.pitch_character("Bouncy", "middle", 1, "Medium-fast", 0.5)
        self.assertGreater(quick["Wicket"], 1.15)
        self.assertNotIn("Wicket", medium)

    def test_even_is_neutral(self):
        hook, strength = md.make_pitch_character_hook("Even", "middle", 1, "Fast", 0.5)
        self.assertIsNone(hook)
        self.assertEqual(strength, 0.0)

    def test_every_multiplier_is_bounded(self):
        for pitch in ("Dusty", "Green", "Dry", "Bouncy", "Even", "Hard", "Flat", "Dead"):
            for phase in ("powerplay", "middle", "death"):
                for btype in ("Fast", "Medium-fast", "Off spin", "Leg spin"):
                    for inn in (1, 2):
                        m, _ = md.pitch_character(pitch, phase, inn, btype, 0.4)
                        for v in m.values():
                            self.assertTrue(0.75 <= v <= 1.3, (pitch, phase, btype, v))


class ScoreCeilingTests(unittest.TestCase):

    def test_quiet_until_the_innings_runs_away(self):
        self.assertIsNone(md.make_score_ceiling_hook("Flat", 30, 30, 120))
        self.assertIsNone(md.make_score_ceiling_hook("Flat", 300, 10, 120))  # too early

    def test_brake_grows_with_the_projection(self):
        # A death-overs ball on a road: almost all boundaries, no wicket.
        hot = {"Dot": 0.01, "Single": 0.06, "Double": 0.04, "Three": 0.003,
               "Four": 0.50, "Six": 0.39, "Wicket": 0.0, "Extras": 0.005}

        def share(hook, key):
            w = hook(dict(hot))
            return w[key] / sum(w.values())

        mild = md.make_score_ceiling_hook("Flat", 150, 72, 120)
        steep = md.make_score_ceiling_hook("Flat", 240, 108, 120)
        self.assertLess(share(steep, "Six"), share(mild, "Six"))
        self.assertLess(share(steep, "Six"), 0.39)
        # Even a zero wicket weight gets a real chance back.
        self.assertGreater(share(steep, "Wicket"), 0.02)
        # Bounded: the batting side keeps some of its momentum.
        self.assertGreater(share(steep, "Four") + share(steep, "Six"), 0.25)

    def test_projection_prices_in_the_death_surge(self):
        # 150 off 14 overs on a 228-par road is on par, not heading for 214.
        self.assertGreater(md.projected_total(150, 84, 120, par=228), 220)
        self.assertLess(md.projected_total(150, 84, 120, par=228), 240)

    def test_chase_of_a_normal_target_is_left_alone(self):
        self.assertIsNone(md.make_score_ceiling_hook(
            "Flat", 150, 72, 120, innings=2, target=200))


class ContestTests(unittest.TestCase):

    def test_runaway_first_innings_is_tightened(self):
        w = _apply(md.make_contest_hook("Even", 1, 140, 1, 60, 120))
        self.assertGreater(w["Wicket"], 1.0)
        self.assertLessEqual(w["Wicket"], 1.10)

    def test_normal_innings_untouched(self):
        self.assertIsNone(md.make_contest_hook("Even", 1, 95, 2, 60, 120))
        self.assertIsNone(md.make_contest_hook("Even", 1, 140, 6, 60, 120))

    def test_never_inside_the_chase_model_window(self):
        self.assertIsNone(md.make_contest_hook(
            "Even", 2, 150, 1, 70, 120, target=160, chase_active=True))


class PressureTests(unittest.TestCase):

    def test_middle_overs_carry_no_pressure(self):
        self.assertLess(md.bowl_pressure("middle", 1, None, 80, 2, 70, 4),
                        md.PRESSURE_FLOOR)
        self.assertLess(md.bat_pressure("middle", 1, None, 80, 2, 70, 0),
                        md.PRESSURE_FLOOR)

    def test_close_chase_at_the_death_squeezes_both_sides(self):
        bowl = md.bowl_pressure("death", 2, 180, 160, 5, 12, 6)
        bat = md.bat_pressure("death", 2, 180, 150, 6, 12, 2)
        self.assertGreaterEqual(bowl, 0.6)
        self.assertGreaterEqual(bat, 0.6)
        self.assertLessEqual(max(bowl, bat), 1.0)

    def test_pressure_effects_are_bounded(self):
        hook, kw = md.bowl_pressure_effects(1.0)
        self.assertLessEqual(kw["drop_mult"], 2.2)
        self.assertAlmostEqual(sum(kw["extras_split"]), 1.0, places=6)
        w = _apply(hook)
        self.assertLessEqual(w["Extras"], 2.3)
        self.assertEqual(md.bowl_pressure_effects(0.1), (None, {}))

    def test_mixup_only_converts_catches_and_bowleds(self):
        random.seed(1)
        oc = {"type": "wicket", "batter_out": True, "wicket_type": "LBW", "runs": 0}
        for _ in range(50):
            md.maybe_mixup(oc, 1.0)
        self.assertEqual(oc["wicket_type"], "LBW")
        hits = 0
        for _ in range(400):
            oc = {"type": "wicket", "batter_out": True, "wicket_type": "Caught", "runs": 0}
            md.maybe_mixup(oc, 1.0)
            if oc.get("mixup"):
                hits += 1
                self.assertEqual(oc["wicket_type"], "Run Out")
                self.assertEqual(oc["runs"], 1)
        self.assertTrue(40 < hits < 130, hits)

    def test_no_mixup_on_a_free_hit_or_without_pressure(self):
        oc = {"type": "wicket", "batter_out": True, "wicket_type": "Caught", "runs": 0}
        random.seed(2)
        for _ in range(100):
            md.maybe_mixup(oc, 1.0, free_hit=True)
            md.maybe_mixup(oc, 0.1)
        self.assertNotIn("mixup", oc)


def _players():
    bat = {"name": "Bat", "batting_rating": 70, "bowling_rating": 20,
           "fielding_rating": 70, "batting_hand": "Right", "bowling_hand": "Right",
           "bowling_type": "Medium-fast"}
    bowl = {"name": "Bowl", "batting_rating": 30, "bowling_rating": 75,
            "fielding_rating": 70, "batting_hand": "Right", "bowling_hand": "Right",
            "bowling_type": "Fast"}
    return bat, bowl


class EngineDefaultsTests(unittest.TestCase):
    """The new engine knobs must leave /sim's deliveries exactly as they were."""

    def _roll(self, n=400, **extra):
        bat, bowl = _players()
        random.seed(99)
        return [calculate_outcome(batter=bat, bowler=bowl, pitch="Even",
                                  streak={"boundaries": 0}, over_number=k % 20,
                                  batter_runs=0, fielding_quality=60, **extra)
                for k in range(n)]

    def test_defaults_are_identical(self):
        self.assertEqual(self._roll(),
                         self._roll(drop_mult=1.0, misfield_mult=1.0,
                                    extras_split=None))

    def test_drop_mult_drops_more(self):
        base = sum(1 for o in self._roll(3000) if o.get("dropped_catch"))
        more = sum(1 for o in self._roll(3000, drop_mult=2.5) if o.get("dropped_catch"))
        self.assertGreater(more, base)

    def test_extras_split_steers_the_extra_type(self):
        outs = self._roll(4000, extras_split=[1.0, 0.0, 0.0, 0.0])
        kinds = {o.get("extra_type") for o in outs if o.get("is_extra")}
        self.assertEqual(kinds, {"Wide"})


class SledgingTests(unittest.TestCase):

    class _Always:
        @staticmethod
        def random():
            return 0.0

    def test_capped_per_innings_with_a_cooldown(self):
        state = {"innings": 1}
        events = []
        for ball in range(0, 120):
            ev = sledging.maybe_sledge(state, "dots", batter="A", bowler="B",
                                       batter_rid=1, ball_no=ball, rng=self._Always)
            if ev:
                events.append(ball)
        self.assertEqual(len(events), sledging.MAX_PER_INNINGS)
        gaps = [b - a for a, b in zip(events, events[1:])]
        self.assertTrue(all(g >= sledging.COOLDOWN_BALLS for g in gaps), gaps)

    def test_new_innings_resets_the_count(self):
        state = {"innings": 1}
        for ball in range(0, 120, 12):
            sledging.maybe_sledge(state, "dots", batter="A", bowler="B",
                                  ball_no=ball, rng=self._Always)
        state["innings"] = 2
        self.assertIsNotNone(sledging.maybe_sledge(
            state, "dots", batter="A", bowler="B", ball_no=0, rng=self._Always))

    def test_effect_hook_lasts_six_balls_then_clears(self):
        state = {"innings": 1}
        ev = sledging.maybe_sledge(state, "dots", batter="A", bowler="B",
                                   batter_rid=7, ball_no=0, rng=self._Always)
        self.assertIn(ev["mode"], ("fired", "rattled"))
        self.assertIsNotNone(sledging.make_hook(state, 7))
        self.assertIsNone(sledging.make_hook(state, 8))
        for _ in range(sledging.EFFECT_BALLS):
            sledging.tick(state, 7)
        self.assertIsNone(sledging.make_hook(state, 7))

    def test_sendoff_carries_no_effect(self):
        state = {"innings": 1}
        ev = sledging.maybe_sledge(state, "sendoff", batter="A", bowler="B",
                                   batter_rid=None, ball_no=0, rng=self._Always)
        self.assertEqual(ev["mode"], "sendoff")
        self.assertNotIn("sledge", state)

    def test_trigger_priority(self):
        kw = dict(runs=0, is_extra=False, prev_was_dot=False, trailing_dots=0,
                  prev_was_boundary=False, chase_tight=False)
        self.assertEqual(sledging.pick_trigger(wicket=True, dropped=False, **kw), "sendoff")
        self.assertEqual(sledging.pick_trigger(wicket=False, dropped=True, **kw), "drop")
        kw["trailing_dots"] = 3
        self.assertEqual(sledging.pick_trigger(wicket=False, dropped=False, **kw), "dots")
        self.assertIsNone(sledging.pick_trigger(
            wicket=False, dropped=False, runs=1, is_extra=True, prev_was_dot=True,
            trailing_dots=0, prev_was_boundary=False, chase_tight=False))


class OverDramaTests(unittest.TestCase):

    def test_summary_carries_drama_fields(self):
        random.seed(5)
        s = _make_state()
        summary = _play_over(s)
        for key in ("drops", "sledges", "pressure_moments"):
            self.assertIn(key, summary)
        self.assertNotIn("over_drama", s)

    def test_a_drop_is_recorded_with_a_fielder(self):
        random.seed(8)
        s = _make_state()
        orig = cm.calculate_outcome

        def _dropping(*a, **kw):
            return {"type": "run", "runs": 1, "is_extra": False,
                    "batter_out": False, "dropped_catch": True,
                    "description": "DROPPED!"}

        cm.calculate_outcome = _dropping
        try:
            summary = _play_over(s)
        finally:
            cm.calculate_outcome = orig
        self.assertTrue(summary["drops"])
        drop = summary["drops"][0]
        self.assertTrue(drop["fielder"])
        self.assertEqual(drop["batter_runs"], 0)
        self.assertEqual(len(s["drops"]), len(summary["drops"]))
        bs = s["bat_stats"][drop["batter_rid"]]
        self.assertGreaterEqual(bs["lives"], 1)
        self.assertEqual(bs["dropped_on"], 0)
        best = cm.costliest_drop(s)
        self.assertIsNotNone(best)


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class ChatTests(unittest.TestCase):

    def _state(self):
        random.seed(11)
        s = _make_state()
        _play_over(s)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        return s

    def test_approach_card_shows_the_partnership(self):
        s = self._state()
        card = cp._approach_card(s)
        self.assertIn("🤝 Partnership:", card)
        self.assertIn(f"<b>{s['partnership_runs']}</b> ({s['partnership_balls']})", card)

    def test_block_card_shows_the_partnership(self):
        from tests.test_rich_text_surfaces import flatten
        s = self._state()
        text = flatten(cp._approach_card_blocks(s))
        self.assertIn("Partnership", text)
        self.assertIn(f"{s['partnership_runs']} ({s['partnership_balls']})", text)

    def test_drop_card_names_everyone(self):
        s = self._state()
        card = cp._drop_card(s, {"batter": "Kohli", "fielder": "Smith",
                                 "bowler": "Starc", "batter_runs": 23,
                                 "team": "IND", "pressure": True})
        self.assertIn("PRESSURE DROP", card)
        self.assertIn("Smith puts down <b>Kohli</b> on <b>23</b>", card)

    _DRAMA = {
        "pressure": {"wides": 2, "noballs": 0, "drops": 1, "mixups": 1,
                     "misfields": 0},
        "drops": [{"batter": "Kohli", "batter_runs": 12, "fielder": "Smith"}],
        "sledges": [{"lines": ['Starc: "Hello"', "Kohli <smiles>"],
                     "escalated": True}],
    }

    def test_over_summary_has_no_drama(self):
        s = self._state()
        summary = _play_over(s)
        summary.update(pressure_moments=self._DRAMA["pressure"],
                       drops=self._DRAMA["drops"], sledges=self._DRAMA["sledges"])
        text = cp._render_over_summary(s, summary)
        for word in ("Pressure:", "Dropped:", "Verbal fight", "smiles"):
            self.assertNotIn(word, text)

    def test_commentary_block_shows_drama(self):
        from tests.test_rich_text_surfaces import flatten
        s = self._state()
        s["last_over_drama"] = self._DRAMA
        # The sledge is also in the over's feed; it must be listed only once.
        s["last_over_commentary"] = list(s.get("last_over_commentary") or []) + [
            {"type": "sledge", "text": "🗣️ Kohli <smiles>"}]
        block = cp._commentary_block(s)
        self.assertIn("😰 Pressure: 2 wides · 1 drop · 1 mix-up", block)
        self.assertIn("🫳 Dropped: Kohli on 12 (by Smith)", block)
        self.assertIn("⚡ Verbal fight!", block)
        self.assertIn("&lt;smiles&gt;", block)
        self.assertEqual(block.count("smiles"), 1)
        rich = flatten(cp._approach_card_blocks(s))
        for word in ("Pressure: 2 wides", "Dropped: Kohli on 12", "Verbal fight",
                     "Kohli <smiles>"):
            self.assertIn(word, rich)

    def test_commentary_block_without_drama_is_unchanged(self):
        s = self._state()
        s["last_over_drama"] = {}
        self.assertNotIn("Pressure", cp._commentary_block(s))

    def test_made_them_pay_line(self):
        out = cp._paid_for_drop_lines({}, [
            ("fifty", {"player": "A", "dropped_on": 12}),
            ("fifty", {"player": "B"}),
        ])
        self.assertEqual(len(out), 1)
        self.assertIn("dropped on 12", out[0])


if __name__ == "__main__":
    unittest.main()
