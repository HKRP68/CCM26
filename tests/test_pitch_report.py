"""Tests for services.pitch_report — dynamic Challenge League conditions."""

import random

import pytest

from services.pitch_report import (
    PITCH_TYPES,
    best_toss_decision,
    build_pitch_report,
    format_pitch_report,
    generate_conditions,
    make_environment_hook,
    pitch_effectiveness,
)

_REQUIRED_FIELDS = {
    "pitch_type", "day_night", "match_time", "weather", "temperature",
    "humidity", "wind_direction", "wind_strength", "dew", "dew_text",
    "grass", "pitch_age", "outfield",
}


def test_generate_conditions_has_all_fields_for_every_pitch():
    rng = random.Random(7)
    for pitch in PITCH_TYPES:
        c = generate_conditions(pitch, rng=rng)
        assert _REQUIRED_FIELDS <= set(c)
        assert c["pitch_type"] == pitch
        assert 15 <= c["temperature"] <= 41
        assert c["humidity"] in ("Low", "Medium", "High")
        assert c["dew"] in (None, "Light", "Moderate", "Heavy")


def test_day_matches_have_no_meaningful_dew():
    rng = random.Random(0)
    seen_day = 0
    for _ in range(400):
        c = generate_conditions("Flat", rng=rng)
        if c["day_night"] == "Day":
            seen_day += 1
            assert c["dew"] is None
            assert "day match" in c["dew_text"]
    assert seen_day > 0  # sanity: we actually exercised the day branch


def test_dew_is_not_guaranteed_at_night():
    rng = random.Random(123)
    levels = set()
    for _ in range(600):
        c = generate_conditions("Green", rng=rng)
        if c["day_night"] == "Night":
            levels.add(c["dew"])
    # Across many night matches we must see both "no dew" and some dew.
    assert None in levels
    assert levels & {"Light", "Moderate", "Heavy"}


def test_report_contains_every_required_label_and_conclusion():
    text, conditions = build_pitch_report("Green", rng=random.Random(3))
    for label in ("PITCH REPORT", "Humidity", "Dew", "Grass", "Pitch:",
                  "Outfield", "Pacers", "Spinners", "Batters",
                  "Win the toss"):
        assert label in text
    assert conditions["best_toss"] in ("bat", "bowl")
    # Conclusion line reflects the computed decision.
    assert ("BOWL first" in text) or ("BAT first" in text)


def test_effectiveness_scores_in_range():
    for pitch in PITCH_TYPES:
        c = generate_conditions(pitch, rng=random.Random(11))
        eff = pitch_effectiveness(pitch, c)
        for key in ("pacers", "spinners", "batters"):
            score, label = eff[key]
            assert 0 <= score <= 5
            assert isinstance(label, str) and label


def test_best_toss_representative_cases():
    sunny_flat = {"weather": "Clear and Sunny", "dew": None, "day_night": "Day"}
    assert best_toss_decision("Flat", sunny_flat)[0] == "bat"

    overcast_green = {"weather": "Overcast", "dew": None, "day_night": "Day"}
    assert best_toss_decision("Green", overcast_green)[0] == "bowl"

    dewy_night = {"weather": "Humid", "dew": "Heavy", "day_night": "Night"}
    assert best_toss_decision("Dry", dewy_night)[0] == "bowl"


def test_environment_hook_none_without_conditions():
    assert make_environment_hook(None, {"over": 1, "total_overs": 20}) is None
    assert make_environment_hook({}, {"over": 1, "total_overs": 20}) is None


def test_environment_hook_keeps_weights_bounded():
    base = {"Dot": 0.30, "Single": 0.34, "Double": 0.09, "Three": 0.01,
            "Four": 0.10, "Six": 0.05, "Wicket": 0.06, "Extras": 0.05}
    rng = random.Random(99)
    for _ in range(60):
        pitch = rng.choice(PITCH_TYPES)
        c = generate_conditions(pitch, rng=rng)
        for innings in (1, 2):
            for over in (1, 8, 14, 19):
                hook = make_environment_hook(
                    c, {"over": over, "total_overs": 20, "innings": innings})
                if hook is None:
                    continue
                out = hook(dict(base))
                for k, v in out.items():
                    assert v >= 0.0
                    # ±25% per-bucket cap (spec balancing rule).
                    assert v <= base[k] * 1.25 + 1e-9
                    assert v >= base[k] * 0.75 - 1e-9


def test_format_is_stable_string():
    c = generate_conditions("Bouncy", rng=random.Random(5))
    eff = pitch_effectiveness("Bouncy", c)
    decision = best_toss_decision("Bouncy", c)
    text = format_pitch_report("Bouncy", c, eff, decision)
    assert text.startswith("🌱 <b>PITCH REPORT</b>")
    assert text.count("\n") >= 10
