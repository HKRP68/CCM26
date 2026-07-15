"""Fast regression guards for the Pitch Rule Engine encoding.

The full calibration is done with ``python -m tools.pitch_calibration`` (a slow
Monte-Carlo sweep). These tests assert only the *loose, reliable* invariants that
hold comfortably at a modest sample size, so CI stays fast and non-flaky:

  * ``get_chase_win_pct`` band selection is exact (deterministic).
  * First-innings par (median) lands in each pitch's spec band.
  * A batting pitch out-scores a bowling pitch.
  * The Sub-100 Rule holds (a side all-out under 100 is rare).
  * The Fighting Match Rule holds in aggregate (matches reach the closing overs).

Tighter, per-band chase-win% / capitulation calibration is intentionally NOT
asserted here — it is the harness's job and carries documented residuals.
"""

import random
import statistics

import pytest

from engine import ground_config
from tools import pitch_calibration as pc


# ── Deterministic getter unit tests ─────────────────────────────────────

def test_get_chase_win_pct_band_edges():
    # Flat: [<200:90][201-219:86][220-240:70][241-260:50][261+:30]
    assert ground_config.get_chase_win_pct("Flat", 180) == 90
    assert ground_config.get_chase_win_pct("Flat", 200) == 90
    assert ground_config.get_chase_win_pct("Flat", 201) == 86
    assert ground_config.get_chase_win_pct("Flat", 240) == 70
    assert ground_config.get_chase_win_pct("Flat", 261) == 30
    assert ground_config.get_chase_win_pct("Flat", 400) == 30
    # Dusty low-scoring bands.
    assert ground_config.get_chase_win_pct("Dusty", 130) == 75
    assert ground_config.get_chase_win_pct("Dusty", 131) == 50
    # Unknown pitch → safe default.
    assert ground_config.get_chase_win_pct("Nope", 180) == 50


def test_get_scoring_dynamics_shape():
    for pitch in ("Flat", "Dusty", "Green", "Even"):
        dyn = ground_config.get_scoring_dynamics(pitch)
        assert set(dyn) >= {"floor", "par_low", "par_high", "ceiling", "anomaly"}
        assert dyn["floor"] < dyn["par_low"] <= dyn["par_high"] < dyn["ceiling"] <= dyn["anomaly"]
    assert ground_config.get_scoring_dynamics("Nope") is None


# ── Simulation invariants (small-N Monte-Carlo) ─────────────────────────

_N = 70
_PITCHES = ("Flat", "Even", "Green", "Hard", "Dusty")


@pytest.fixture(scope="module")
def sims():
    random.seed(20260715)
    return {p: pc.collect(p, _N) for p in _PITCHES}


@pytest.mark.parametrize("pitch", _PITCHES)
def test_first_innings_par_in_band(sims, pitch):
    dyn = ground_config.get_scoring_dynamics(pitch)
    par = sims[pitch]["par"]
    # Generous pad — par is the primary target but N is small here.
    assert dyn["par_low"] - 16 <= par <= dyn["par_high"] + 16, (
        f"{pitch} median {par} outside par {dyn['par_low']}-{dyn['par_high']} (+/-16)")


@pytest.mark.parametrize("pitch", _PITCHES)
def test_ceiling_not_wildly_exceeded(sims, pitch):
    dyn = ground_config.get_scoring_dynamics(pitch)
    assert sims[pitch]["ceiling"] <= dyn["ceiling"] + 45


def test_batting_pitch_outscores_bowling_pitch(sims):
    assert sims["Flat"]["par"] > sims["Green"]["par"] + 20


@pytest.mark.parametrize("pitch", _PITCHES)
def test_sub_100_rule_per_pitch(sims, pitch):
    # A side all-out under 100 is rare. Turners (weak synthetic tail) get more
    # slack; batting/neutral pitches should almost never see it.
    cap = 16.0 if pitch == "Dusty" else 7.0
    assert sims[pitch]["sub100_rate"] <= cap, (
        f"{pitch} all-out<100 rate {sims[pitch]['sub100_rate']:.1f}% > {cap}%")


def test_sub_100_rule_aggregate(sims):
    non_turner = [m["sub100_rate"] for p, m in sims.items() if p != "Dusty"]
    assert statistics.mean(non_turner) < 4.0


def test_fighting_match_matches_run_deep(sims):
    # Most matches should reach the closing overs rather than ending early —
    # the Balanced Contest / Fighting Match rule.
    assert statistics.mean(m["deep_rate"] for m in sims.values()) > 55.0


def test_fighting_match_losses_are_not_routinely_blowouts(sims):
    # Median defended margin stays tight (close, fighting losses on average).
    assert statistics.mean(m["def_margin_median"] for m in sims.values()) < 55.0
