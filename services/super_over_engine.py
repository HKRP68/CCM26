"""Shared Super Over conditions and rare extra-ball incidents.

These rules apply to every Super Over, independently of tournament settings.
The normal ball engine still supplies rating, pitch, shot and delivery weights.
"""
import random

ENGINE_OVER = 19
TOTAL_OVERS = 20
EXTRAS_MULT = 2.0


def chase_pressure(target, runs, legal, wickets=0):
    """Bounded risk from the real equation; never steer toward a chosen score."""
    if not target:
        return 0.10
    left = max(1, 6 - legal)
    rate = max(0, target - runs) / left
    return min(0.55, 0.10 + max(0, rate - 1.5) * 0.12 + min(1, wickets) * 0.06)


def no_ball_play(probs, *, fielding_quality=None, pressure=0.0, rng=None):
    """Play off an illegal ball: boundaries, a reprieve, or a rare running error.

    Runs exclude the one-run no-ball penalty. A run out records completed runs
    only, and cannot coincide with a boundary. Other dismissals are overturned.
    """
    rng = rng or random
    shots = ('dot', '1', '2', '3', '4', '6', 'W')
    play = rng.choices(shots, weights=[max(0, probs.get(k, 0)) for k in shots], k=1)[0]
    result = dict(type='noball', runs=0)
    if play == 'W':
        result['disallowed_wicket'] = rng.choice(('Bowled', 'Caught'))
        return result
    runs = 0 if play == 'dot' else int(play)
    quality = 70 if fielding_quality is None else max(0, min(100, fielding_quality))
    risk = 0.015 + max(0, min(1, pressure)) * 0.02 + quality / 10000
    if runs in (1, 2, 3) and rng.random() < risk:
        return dict(type='noball', runs=runs - 1, batter_out=True, how='Run Out')
    return dict(result, runs=runs)


def protect_dismissal(outcome, *, free_hit=False):
    """A no-ball/free hit permits run outs, never bowled/caught/LBW wickets."""
    out = outcome.get('batter_out') or outcome.get('type') == 'wicket'
    no_ball = outcome.get('type') == 'extra' and outcome.get('extra_type') == 'No Ball'
    how = outcome.get('wicket_type') or outcome.get('how') or 'Caught'
    run_out = str(how).casefold().replace(' ', '').replace('-', '') == 'runout'
    if out and (free_hit or no_ball) and not run_out:
        extra = outcome.get('type') == 'extra'
        return dict(outcome, type='extra' if extra else 'run',
                    is_extra=extra, extra_type=outcome.get('extra_type') if extra else None,
                    batter_out=False, wicket_type=None, disallowed_wicket=how)
    return outcome
