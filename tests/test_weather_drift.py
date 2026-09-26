"""In-match weather and dew (services/weather_drift.py).

Pure — no Telegram, no database.
"""

import random
import unittest

from services import weather_drift as wd
from services.pitch_report import WEATHERS, generate_conditions, make_environment_hook


def _conds(**kw):
    base = {"pitch_type": "Hard", "day_night": "Night", "weather": "Clear and Sunny",
            "temperature": 24, "humidity": "High", "wind_direction": "Headwind",
            "wind_strength": "Light", "dew": "Heavy", "grass": "Medium",
            "pitch_age": "Fresh", "outfield": "Normal"}
    base.update(kw)
    return base


def _play_through(conditions, overs=20):
    """Apply the whole forecast over by over; return (state, all texts)."""
    state = {"conditions": conditions, "innings": 1, "current_over": 1}
    texts = []
    for inn in (1, 2):
        for over in range(1, overs + 1):
            state["innings"], state["current_over"] = inn, over
            texts += wd.apply_due(state)
    return state, texts


class NoRainTests(unittest.TestCase):
    def test_no_forecast_ever_produces_rain(self):
        for seed in range(400):
            r = random.Random(seed)
            c = wd.plan(generate_conditions(r.choice(["Hard", "Green", "Dry"]), rng=r),
                        20, rng=r)
            for e in c["timeline"]:
                new = (e.get("set") or {}).get("weather")
                if new:
                    self.assertNotIn("rain", new.lower(), f"seed {seed}: {e}")
                    self.assertIn(new, WEATHERS)
                self.assertNotIn("rain", (e.get("text") or "").lower().replace(
                    "earlier showers", ""))

    def test_forecast_never_touches_overs_or_target(self):
        for seed in range(100):
            c = wd.plan(_conds(), 20, rng=random.Random(seed))
            for e in c["timeline"]:
                self.assertTrue(set(e["set"]) <= {"weather", "wind_strength",
                                                  "outfield", "temperature", "dew"})


class DewTests(unittest.TestCase):
    def test_night_dew_starts_dry_and_builds_to_the_forecast(self):
        for seed in range(50):
            c = wd.plan(_conds(dew="Heavy"), 20, rng=random.Random(seed))
            self.assertIsNone(c["dew"])
            self.assertEqual(c["dew_forecast"], "Heavy")
            state, _ = _play_through(c)
            self.assertEqual(state["conditions"]["dew"], "Heavy")

    def test_dew_levels_only_rise(self):
        order = [None, "Light", "Moderate", "Heavy"]
        c = wd.plan(_conds(dew="Moderate"), 20, rng=random.Random(3))
        state = {"conditions": c}
        seen = []
        for inn in (1, 2):
            for over in range(1, 21):
                state["innings"], state["current_over"] = inn, over
                wd.apply_due(state)
                seen.append(order.index(state["conditions"]["dew"]))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(order[seen[-1]], "Moderate")

    def test_day_match_never_gets_dew(self):
        for seed in range(50):
            c = wd.plan(_conds(day_night="Day", dew=None), 20, rng=random.Random(seed))
            state, _ = _play_through(c)
            self.assertIsNone(state["conditions"]["dew"])
            self.assertFalse(any(e["kind"] == "dew" for e in c["timeline"]))

    def test_dew_helps_the_chase_only_once_it_has_arrived(self):
        c = wd.plan(_conds(dew="Heavy", weather="Humid"), 20, rng=random.Random(1))
        ctx = {"over": 10, "total_overs": 20, "innings": 2}
        dry = make_environment_hook(dict(c, dew=None), ctx)
        wet = make_environment_hook(dict(c, dew="Heavy"), ctx)
        w = {"Dot": 1.0, "Single": 1.0, "Double": 1.0, "Three": 1.0, "Four": 1.0,
             "Six": 1.0, "Wicket": 1.0, "Extras": 1.0}
        dry_w = dry(dict(w)) if dry else w
        self.assertGreater(wet(dict(w))["Four"], dry_w["Four"])
        self.assertLess(wet(dict(w))["Wicket"], dry_w["Wicket"])


class TimelineTests(unittest.TestCase):
    def test_plan_is_idempotent(self):
        c = wd.plan(_conds(), 20, rng=random.Random(9))
        again = wd.plan(c, 20, rng=random.Random(10))
        self.assertIs(again, c)

    def test_each_change_is_applied_exactly_once(self):
        c = wd.plan(_conds(), 20, rng=random.Random(4))
        state = {"conditions": c, "innings": 2, "current_over": 20}
        first = wd.apply_due(state)
        self.assertEqual(wd.apply_due(state), [])
        self.assertEqual(len(first),
                         sum(1 for e in c["timeline"] if e.get("text")))

    def test_nothing_applies_before_its_over(self):
        c = wd.plan(_conds(), 20, rng=random.Random(4))
        state = {"conditions": c, "innings": 1, "current_over": 0}
        self.assertEqual(wd.apply_due(state), [])

    def test_states_without_conditions_are_untouched(self):
        self.assertEqual(wd.apply_due({"innings": 1, "current_over": 3}), [])
        self.assertIsNone(wd.plan(None))

    def test_weather_does_change_in_some_matches(self):
        changed = sum(1 for seed in range(100)
                      if any(e["kind"] == "weather"
                             for e in wd.plan(_conds(), 20,
                                              rng=random.Random(seed))["timeline"]))
        self.assertGreater(changed, 25)
        self.assertLess(changed, 90)

    def test_conditions_line(self):
        c = wd.plan(_conds(), 20, rng=random.Random(2))
        line = wd.conditions_line(c)
        self.assertIn("dew expected", line)
        self.assertEqual(wd.conditions_line(None), "")


if __name__ == "__main__":
    unittest.main()
