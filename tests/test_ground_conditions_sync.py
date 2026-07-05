"""Guard: config/ground_conditions.yaml (the loaded config) and
config/ground_conditions_defaults.yaml (the reference/fallback) must keep their
pitch_profiles and phase_boosts sections identical. They are hand-tuned together
on every calibration pass, so this test fails fast if a future edit touches only
one of them and lets them drift apart.
"""

import os

import yaml

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config")
_ACTIVE = os.path.join(_CONFIG_DIR, "ground_conditions.yaml")
_DEFAULTS = os.path.join(_CONFIG_DIR, "ground_conditions_defaults.yaml")


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def test_pitch_profiles_match():
    active = _load(_ACTIVE)
    defaults = _load(_DEFAULTS)
    assert active.get("pitch_profiles") == defaults.get("pitch_profiles"), (
        "pitch_profiles differ between ground_conditions.yaml and "
        "ground_conditions_defaults.yaml — keep them in sync.")


def test_phase_boosts_match():
    active = _load(_ACTIVE)
    defaults = _load(_DEFAULTS)
    assert active.get("phase_boosts") == defaults.get("phase_boosts"), (
        "phase_boosts differ between ground_conditions.yaml and "
        "ground_conditions_defaults.yaml — keep them in sync.")


def test_every_profile_scoring_matrix_sums_to_one():
    for path in (_ACTIVE, _DEFAULTS):
        cfg = _load(path)
        for pitch, profile in (cfg.get("pitch_profiles") or {}).items():
            total = sum((profile.get("scoring_matrix") or {}).values())
            assert abs(total - 1.0) <= 0.02, (
                f"{os.path.basename(path)}: pitch '{pitch}' scoring_matrix "
                f"sums to {total:.4f}, expected ~1.0")


if __name__ == "__main__":
    test_pitch_profiles_match()
    test_phase_boosts_match()
    test_every_profile_scoring_matrix_sums_to_one()
    print("ground-conditions sync: OK")
