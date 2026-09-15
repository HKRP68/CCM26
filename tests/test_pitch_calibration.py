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
    """Band selection is exact, and the bands step down at the documented edges.

    Bands are <=150, <=170, <=190, <=210, <=230, <=250, <=270, 271+ on every
    pitch, so the *edges* are exact even though the values in them are now
    derived rather than transcribed: each grid is a logistic in runs relative to
    that pitch's own par (see docs/pitch-types.md), because the doc's original
    grid was keyed on absolute totals from before section 5A moved par and had
    drifted up to fifty runs off on the bowling surfaces.
    """
    flat = [ground_config.get_chase_win_pct("Flat", t)
            for t in (150, 170, 190, 210, 230, 250, 270, 400)]
    # Selection lands on the band whose max the total is <=, not the next one.
    assert ground_config.get_chase_win_pct("Flat", 150) == flat[0]
    assert ground_config.get_chase_win_pct("Flat", 151) == flat[1]
    assert ground_config.get_chase_win_pct("Flat", 231) == flat[5]
    assert ground_config.get_chase_win_pct("Flat", 271) == flat[7]
    assert ground_config.get_chase_win_pct("Flat", 400) == flat[7]
    # Dusty is the hardest chase in the game, Dead the easiest — at every total.
    for total in (150, 190, 230, 300):
        pcts = {p: ground_config.get_chase_win_pct(p, total)
                for p in ("Dusty", "Green", "Dry", "Bouncy", "Even", "Hard",
                          "Flat", "Dead")}
        assert pcts["Dusty"] == min(pcts.values()), pcts
        assert pcts["Dead"] == max(pcts.values()), pcts
    # Unknown pitch → safe default.
    assert ground_config.get_chase_win_pct("Nope", 180) == 50


def test_chase_win_pct_falls_monotonically_with_the_target():
    """No pitch may make a bigger target easier to chase than a smaller one."""
    for pitch in ("Flat", "Dead", "Hard", "Even", "Bouncy", "Dry", "Green", "Dusty"):
        pcts = [ground_config.get_chase_win_pct(pitch, t)
                for t in (140, 160, 180, 200, 220, 240, 260, 300)]
        assert pcts == sorted(pcts, reverse=True), f"{pitch}: {pcts}"


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
    # Restore the process-global RNG afterwards so seeding here can't make other
    # tests order-dependent.
    prev = random.getstate()
    try:
        random.seed(20260715)
        return {p: pc.collect(p, _N) for p in _PITCHES}
    finally:
        random.setstate(prev)


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
