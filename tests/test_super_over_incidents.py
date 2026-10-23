"""Rare incidents must be generated and scored, not just narrated."""
import random
from types import SimpleNamespace

import pytest

from services import probability_engine as pe, super_over_engine as E
from services.sim_match import _normalize_outcome


def rng_for(play, chance=1):
    return SimpleNamespace(choices=lambda *a, **k: [play], choice=lambda values: values[0],
                           random=lambda: chance)


@pytest.mark.parametrize('play,runs', [('4', 4), ('6', 6), ('dot', 0), ('2', 2)])
def test_no_ball_runs_are_off_the_bat_without_the_penalty(play, runs):
    oc = E.no_ball_play(pe.BASE, rng=rng_for(play))
    assert oc['type'] == 'noball' and oc['runs'] == runs
    assert not oc.get('batter_out')


def test_no_ball_wicket_is_overturned_but_run_out_keeps_completed_runs():
    saved = E.no_ball_play(pe.BASE, rng=rng_for('W'))
    assert saved['disallowed_wicket'] == 'Bowled'
    assert not saved.get('batter_out')
    out = E.no_ball_play(pe.BASE, rng=rng_for('2', chance=0))
    assert out == dict(type='noball', runs=1, batter_out=True, how='Run Out')
    norm = _normalize_outcome(out)
    assert norm['type'] == 'extra' and norm['extra_type'] == 'No Ball'
    assert norm['batter_out'] and norm['wicket_type'] == 'Run Out'


def test_real_no_ball_sampling_keeps_run_outs_rare_and_boundaries_possible():
    rng = random.Random(64)
    outcomes = [E.no_ball_play(pe.BASE, rng=rng, pressure=0.4) for _ in range(15000)]
    outs = [o for o in outcomes if o.get('batter_out')]
    assert 0 < len(outs) / len(outcomes) < 0.03
    assert all(o['how'] == 'Run Out' and o['runs'] in (0, 1, 2) for o in outs)
    assert any(o.get('disallowed_wicket') for o in outcomes)
    assert any(o['runs'] == 6 for o in outcomes)


def test_probability_engine_emits_the_incident_not_only_a_test_script(monkeypatch):
    weights = {}
    real_normalize = pe._normalize
    def capture(probs):
        real_normalize(probs)
        weights.update(probs)
    monkeypatch.setattr(pe, '_normalize', capture)
    # Select the middle of the final no-ball interval without altering the
    # rating-shaped weights used to decide the play off that illegal delivery.
    monkeypatch.setattr(pe.random, 'random', lambda: (weights['wide'] + weights['noball'] / 2) / 100 if weights else 0.5)
    monkeypatch.setattr(E, 'no_ball_play', lambda probs, **kw: dict(type='noball', runs=1, batter_out=True, how='Run Out'))
    from handlers.super_over import _super_over_ball
    oc = _super_over_ball(dict(bat_rating=80), dict(bowl_rating=80), 'Hard', shot='Drive')
    assert oc['batter_out'] and oc['wicket_type'] == 'Run Out'
    assert oc['extra_type'] == 'No Ball' and oc['runs'] == 1


def test_legacy_wicket_normalization_keeps_the_run_out_label():
    oc = _normalize_outcome(dict(type='wicket', runs=1, how='Run Out'))
    assert oc['wicket_type'] == 'Run Out' and oc['batter_out']
    dropped = _normalize_outcome(dict(type='runs', runs=2, dropped_catch=True))
    assert dropped['dropped_catch']


@pytest.mark.parametrize('extra', [None, 'No Ball', 'Wide'])
def test_free_hit_protection_preserves_extra_penalties(extra):
    oc = dict(type='extra' if extra else 'wicket', extra_type=extra,
              runs=0, batter_out=True, wicket_type='Caught')
    protected = E.protect_dismissal(oc, free_hit=True)
    assert not protected['batter_out'] and protected['disallowed_wicket'] == 'Caught'
    assert protected['type'] == ('extra' if extra else 'run')
    assert protected['extra_type'] == extra


def test_chase_pressure_is_bounded_and_tracks_the_actual_equation():
    assert E.chase_pressure(20, 12, 4) > E.chase_pressure(20, 18, 4)
    assert E.chase_pressure(200, 0, 5) <= 0.55
