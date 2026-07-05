"""Handedness matchup layer in services.probability_engine (Layer 7b).

Verifies that spin turning away from the bat and the left-arm pace angle
across a right-hander lift the wicket rate — and that the layer is a strict
no-op when the batting/bowling hand is unknown (legacy callers unaffected).
"""

import random

from services.probability_engine import (
    calculate_outcome,
    _handedness_boost,
    _HAND_SPIN_BOOST,
    _HAND_LEFTPACE_BOOST,
)


# ── Pure-function checks on the boost itself ────────────────────────────────

def test_boost_offspin_turns_away_from_lhb():
    # Right-arm off spin (finger) turns away from a left-hander.
    assert _handedness_boost("Off Spinner", "Right", "Left", "Flat") == _HAND_SPIN_BOOST
    # ...but turns into a right-hander → neutral.
    assert _handedness_boost("Off Spinner", "Right", "Right", "Flat") == 1.0


def test_boost_legspin_turns_away_from_rhb():
    # Right-arm leg spin (wrist) turns away from a right-hander.
    assert _handedness_boost("Leg Spinner", "Right", "Right", "Flat") == _HAND_SPIN_BOOST
    assert _handedness_boost("Leg Spinner", "Right", "Left", "Flat") == 1.0


def test_boost_left_arm_spin_is_mirrored():
    # Left-arm orthodox (finger) turns away from a right-hander.
    assert _handedness_boost("Off Spinner", "Left", "Right", "Flat") == _HAND_SPIN_BOOST
    # Left-arm wrist (chinaman) turns away from a left-hander.
    assert _handedness_boost("Leg Spinner", "Left", "Left", "Flat") == _HAND_SPIN_BOOST


def test_boost_left_arm_pace_across_rhb():
    b = _handedness_boost("Fast Pacer", "Left", "Right", "Flat")
    assert abs(b - _HAND_LEFTPACE_BOOST) < 1e-9
    # Same angle on Green seams more.
    assert _handedness_boost("Fast Pacer", "Left", "Right", "Green") > b
    # Right-arm pace vs RHB → neutral.
    assert _handedness_boost("Fast Pacer", "Right", "Right", "Flat") == 1.0


def test_boost_noop_without_hands():
    assert _handedness_boost("Off Spinner", None, "Left", "Flat") == 1.0
    assert _handedness_boost("Leg Spinner", "Right", None, "Flat") == 1.0


# ── End-to-end: the layer actually shifts outcomes, and no-op is exact ──────

def _rate(n, **kw):
    random.seed(20260705)
    w = 0
    for _ in range(n):
        oc = calculate_outcome("Off Spinner", "Right", "Off Break", None,
                               "Flat", 10, 20, "Drive", 75, 75, **kw)
        if oc["type"] == "wicket":
            w += 1
    return w / n


def test_offspin_takes_more_wickets_vs_lhb_than_rhb():
    n = 60000
    vs_lhb = _rate(n, bat_hand="Left")
    vs_rhb = _rate(n, bat_hand="Right")
    # Turning away from the LHB must produce a clearly higher wicket rate.
    assert vs_lhb > vs_rhb * 1.05, (vs_lhb, vs_rhb)


def test_no_hand_matches_neutral_hand_exactly():
    # With no bat_hand, an off-spinner vs an (implicit) opposite matchup must
    # equal the neutral RHB case bit-for-bit — proving the layer is skipped.
    random.seed(99)
    a = [calculate_outcome("Off Spinner", "Right", "Off Break", None, "Flat",
                           10, 20, "Drive", 75, 75) for _ in range(300)]
    random.seed(99)
    b = [calculate_outcome("Off Spinner", "Right", "Off Break", None, "Flat",
                           10, 20, "Drive", 75, 75, bat_hand="Right") for _ in range(300)]
    assert [x["type"] for x in a] == [x["type"] for x in b]
