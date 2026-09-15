"""Guard: every layer that has an opinion about a pitch must cover the same
eight surfaces, and agree with them.

These are the checks that would have caught the drift the registry was written
to end — each one is a bug that was actually shipped:

  * ``services.probability_engine.PITCH_MODS`` had no ``Bouncy`` row, and its
    lookup fell back to ``"Flat"``, so the bounciest deck in the game was
    played as the flattest road in the table.
  * ``engine.format_config``'s T20 ``target_scores`` were still the pre-v3.0
    numbers (Green 123) long after the engine had been recalibrated to produce
    ~168 there, so every consumer of par read a Green innings as 45 runs ahead.
  * ``engine.game_state_engine``'s par table knew five surfaces, so Dusty,
    Bouncy and Even were all judged against Hard.
  * ``services.pitch_report`` told the toss winner to BAT on a road while
    ``engine.format_config`` told them to BOWL.
  * ``engine.chase_chance`` classed Bouncy with the low-scoring surfaces even
    though its par band is higher than Green's or Dry's.
"""

import pytest

from engine import (ball_outcome, chase_chance, format_config, ground_config,
                    pitch_registry, pitch_state)
from services import ground_conditions, match_constants, pitch_report
from services import probability_engine

PITCHES = set(pitch_registry.PITCHES)


# ── The registry itself ────────────────────────────────────────────────

def test_registry_is_internally_consistent():
    orders = [pitch_registry.order(p) for p in pitch_registry.PITCHES]
    assert orders == list(range(len(orders))), "order must be 0..n-1 in listing order"
    assert pitch_registry.DEFAULT in PITCHES
    assert set(pitch_registry.SELECTABLE) <= PITCHES
    for p in pitch_registry.PITCHES:
        prof = pitch_registry.profile(p)
        assert prof.toss in ("bat", "bowl")
        assert prof.toss_dn in ("bat", "bowl")
        assert len(prof.effectiveness) == 3
        assert all(0 <= v <= 5 for v in prof.effectiveness)
        assert prof.blurb and prof.character and prof.archetype


def test_unknown_pitch_resolves_to_the_neutral_surface():
    # Not to "Flat", which is what the manual engine used to do — that turns a
    # typo into the most batting-friendly surface in the game.
    assert pitch_registry.normalise("Nope") == "Even"
    assert pitch_registry.normalise(None) == "Even"
    assert pitch_registry.profile("Nope").favours == "Balanced"


def test_par_bands_rise_with_the_registry_order():
    pars = [pitch_registry.par(p) for p in pitch_registry.PITCHES]
    assert all(p is not None for p in pars), "every surface needs a par band"
    assert pars == sorted(pars), f"par must rise with order: {pars}"


# ── Coverage: no layer may know a different set of surfaces ────────────

@pytest.mark.parametrize("label,keys", [
    ("ground_config.pitch_profiles",
     set((ground_config.get_config() or {}).get("pitch_profiles", {}))),
    ("probability_engine.PITCH_MODS", set(probability_engine.PITCH_MODS)),
    ("ball_outcome.PITCH_RUN_FACTOR", set(ball_outcome.PITCH_RUN_FACTOR)),
    ("ball_outcome.PITCH_WICKET_FACTOR", set(ball_outcome.PITCH_WICKET_FACTOR)),
    ("ball_outcome.PITCH_SCORING_MATRIX", set(ball_outcome.PITCH_SCORING_MATRIX)),
    ("ball_outcome.LISTA_RUN_FACTORS", set(ball_outcome.LISTA_RUN_FACTORS)),
    ("ball_outcome.LISTA_WICKET_PITCH_MULT", set(ball_outcome.LISTA_WICKET_PITCH_MULT)),
    ("pitch_state.INNINGS2_EVOLUTION", set(pitch_state.INNINGS2_EVOLUTION)),
    ("pitch_state.EVOLUTION_NOTES", set(pitch_state.EVOLUTION_NOTES)),
    ("pitch_report._PITCH_BASE", set(pitch_report._PITCH_BASE)),
    ("pitch_report._PITCH_GRASS", set(pitch_report._PITCH_GRASS)),
])
def test_layer_covers_exactly_the_registry(label, keys):
    assert keys == PITCHES, (
        f"{label} covers {sorted(keys)}, registry has {sorted(PITCHES)}")


@pytest.mark.parametrize("fmt_name", ["T20", "ListA"])
@pytest.mark.parametrize("table", ["target_scores", "pitch_par_factors",
                                   "correct_toss_choice", "correct_toss_choice_dn",
                                   "rrr_baseline"])
def test_format_config_tables_cover_the_registry(fmt_name, table):
    fmt = format_config.get_format(fmt_name)
    assert set(getattr(fmt, table)) == PITCHES, (
        f"{fmt_name}.{table} is missing {sorted(PITCHES - set(getattr(fmt, table)))}")


def test_selectable_pitches_are_what_the_pickers_offer():
    assert list(match_constants.PITCH_TYPES) == list(pitch_registry.SELECTABLE)
    assert list(pitch_report.PITCH_TYPES) == list(pitch_registry.SELECTABLE)
    assert ground_conditions.list_pitches() == list(pitch_registry.SELECTABLE)


# ── Agreement: the layers must tell the same story ─────────────────────

def test_toss_advice_is_the_same_everywhere():
    t20 = format_config.get_format("T20")
    for p in pitch_registry.PITCHES:
        assert t20.correct_toss_choice[p] == pitch_registry.toss_call(p)
        assert pitch_report._PITCH_TOSS[p] == pitch_registry.toss_call(p)
        assert ground_conditions.get_pitch_meta(p)["ideal_toss"] == \
            pitch_registry.toss_call(p)


def test_day_night_always_favours_bowling_first():
    # Dew in the second innings: there is no surface where batting first under
    # lights is the call, which is why the D/N column is not a copy of the day.
    for p in pitch_registry.PITCHES:
        assert pitch_registry.toss_call(p, "Night") == "bowl"


def test_format_config_par_matches_the_configured_par_bands():
    t20 = format_config.get_format("T20")
    neutral = t20.par_scores[t20.overs]
    for p in pitch_registry.PITCHES:
        band = pitch_registry.par_band(p)
        projected = neutral * t20.pitch_par_factors[p]
        assert band[0] - 8 <= projected <= band[1] + 8, (
            f"{p}: par curve projects {projected:.0f}, band is {band}")
        assert abs(t20.target_scores[p] - pitch_registry.par(p)) <= 2


def test_game_state_engine_reads_the_format_config_par_tables():
    from engine import game_state_engine as gsme
    t20 = format_config.get_format("T20")
    assert gsme._PITCH_PAR_FACTOR == t20.pitch_par_factors
    # And the T20 branch must actually use the FormatConfig it is handed: this
    # used to be skipped by name, so a T20 match never saw its own config.
    assert gsme.get_par_score(20, t20, "Dusty") < gsme.get_par_score(20, t20, "Flat")
    assert gsme.get_par_score(20, t20, "Bouncy") != gsme.get_par_score(20, t20, "Hard")


def test_rrr_baseline_rises_with_par():
    for fmt_name in ("T20", "ListA"):
        fmt = format_config.get_format(fmt_name)
        rates = [fmt.rrr_baseline[p] for p in pitch_registry.PITCHES]
        assert rates == sorted(rates), f"{fmt_name}: {rates}"


def test_batting_effectiveness_tracks_the_par_order():
    bat = [pitch_registry.effectiveness(p)[2] for p in pitch_registry.PITCHES]
    assert bat == sorted(bat), f"batter ratings must not fall as par rises: {bat}"


def test_a_surface_that_favours_pace_actually_pays_pacers():
    """The Pitch Report's 0-5 ratings and the engine's wicket factors have to
    agree about who the surface helps, or the card lies about the match."""
    for p in pitch_registry.PITCHES:
        pace_eff, spin_eff, _ = pitch_registry.effectiveness(p)
        pace = max(ball_outcome.get_pitch_wicket_multiplier(p, s)
                   for s in ("Fast", "Fast-medium", "Medium-fast"))
        spin = max(ball_outcome.get_pitch_wicket_multiplier(p, s)
                   for s in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"))
        if pace_eff > spin_eff:
            assert pace > spin, f"{p} rates pacers above spinners but pays spin more"
        elif spin_eff > pace_eff:
            assert spin > pace, f"{p} rates spinners above pacers but pays pace more"


def test_every_bowling_type_is_priced_on_every_surface():
    """No bowler may land on ``default`` — that is how a left-arm orthodox came
    to be worse off on a neutral track than an off spinner."""
    types = ("Fast", "Fast-medium", "Medium-fast",
             "Off spin", "Finger spin", "Leg spin", "Wrist spin")
    profiles = (ground_config.get_config() or {}).get("pitch_profiles", {})
    for p in pitch_registry.PITCHES:
        configured = set((profiles.get(p) or {}).get("wicket_factors", {}))
        assert set(types) <= configured, (
            f"{p} does not price {sorted(set(types) - configured)}")
        assert set(types) <= set(ball_outcome.PITCH_WICKET_FACTOR[p])


def test_scoring_matrices_say_the_same_thing_as_the_config():
    """The hardcoded fallbacks are only reached when the YAML is missing, so
    they have to be the same cricket, not an older draft of it."""
    for p in pitch_registry.PITCHES:
        cfg = ground_config.get_scoring_matrix(p)
        fallback = ball_outcome.PITCH_SCORING_MATRIX[p]
        assert abs(sum(fallback.values()) - 1.0) < 0.02
        for bucket, value in fallback.items():
            assert abs(cfg[bucket] - value) < 0.02, f"{p}/{bucket}"


def test_run_factors_rise_with_par_in_both_engines():
    cfg = [ground_config.get_run_factor(p) for p in pitch_registry.PITCHES]
    fallback = [ball_outcome.PITCH_RUN_FACTOR[p] for p in pitch_registry.PITCHES]
    lista = [ball_outcome.LISTA_RUN_FACTORS[p] for p in pitch_registry.PITCHES]
    assert cfg == sorted(cfg), f"config run factors out of order: {cfg}"
    assert fallback == sorted(fallback), f"fallback out of order: {fallback}"
    assert lista == sorted(lista), f"ListA out of order: {lista}"


def test_chase_grids_are_centred_on_each_pitch_s_own_par():
    """Every grid used to be keyed on absolute totals from before par moved, so
    a 190 chase on a turner — thirty runs above par — read as a 58% proposition
    while the same total on a road read 78%.

    The bands are 20 runs wide, so "the 50% crossing" is located to within a
    band rather than to the run.
    """
    for p in pitch_registry.PITCHES:
        par = pitch_registry.par(p)
        crossing = next((t for t in range(80, 401, 5)
                         if ground_config.get_chase_win_pct(p, t) <= 50), None)
        assert crossing is not None, f"{p}: chase grid never drops below 50%"
        assert abs(crossing - par) <= 25, (
            f"{p}: chase grid crosses 50% at {crossing}, par is {par:.0f}")
        assert (ground_config.get_chase_win_pct(p, int(par - 40))
                > ground_config.get_chase_win_pct(p, int(par + 40)))


def test_the_turner_is_the_hardest_chase_and_the_road_the_easiest():
    for total in (150, 190, 230, 300):
        pcts = {p: ground_config.get_chase_win_pct(p, total)
                for p in pitch_registry.PITCHES}
        assert pcts["Dusty"] == min(pcts.values()), pcts
        assert pcts["Dead"] == max(pcts.values()), pcts


def test_chase_pitch_modifier_agrees_with_the_par_table():
    for p in pitch_registry.PITCHES:
        mod = chase_chance.pitch_modifier(p)
        par, neutral = pitch_registry.par(p), pitch_registry.par("Even")
        assert mod == (3 if par > neutral + 5 else -3 if par < neutral - 5 else 0), p


def test_manual_engine_differentiates_every_surface():
    """PITCH_MODS must not contain two surfaces that play identically — Hard and
    Flat used to differ by a single +0.2 on singles."""
    mods = probability_engine.PITCH_MODS
    seen = {}
    for p, m in mods.items():
        key = tuple(sorted(m.items()))
        assert key not in seen, f"{p} and {seen[key]} have identical pitch mods"
        seen[key] = p


def test_manual_engine_pitch_mods_track_the_par_order():
    """More batting-friendly surfaces must give the batter more and the bowler
    less, in the same order as the par bands."""
    def score(p):
        m = probability_engine.PITCH_MODS[p]
        return m.get("4", 0) + m.get("6", 0) - m.get("dot", 0) - 2 * m.get("W", 0)

    scores = [score(p) for p in pitch_registry.PITCHES]
    assert scores == sorted(scores), dict(zip(pitch_registry.PITCHES, scores))


def test_manual_engine_synergy_signs_match_the_config():
    """Who the surface pays has to be the same story in both engines."""
    pace_keys = ("Fast Pacer", "Medium Pacer")
    spin_keys = ("Off Spinner", "Leg Spinner")
    syn = probability_engine.PITCH_BOWLER_SYNERGY
    for p in pitch_registry.PITCHES:
        pace = sum(syn.get((p, k), {}).get("W", 0) for k in pace_keys)
        spin = sum(syn.get((p, k), {}).get("W", 0) for k in spin_keys)
        favours = pitch_registry.favours(p)
        if favours == "Pace":
            assert pace > spin, f"{p} favours pace but its synergy pays spin"
        elif favours == "Spin":
            assert spin > pace, f"{p} favours spin but its synergy pays pace"
