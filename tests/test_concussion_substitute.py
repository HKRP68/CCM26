"""A concussion swap cannot add a wicket, lose statistics or consume Impact."""
import copy
import json
import random
from unittest.mock import patch

from services import cipl_match, concussion_substitute as C, impact_player


def state():
    def p(rid, role='Batsman'):
        return dict(roster_id=rid, name=f'Player {rid}', category=role,
                    bat_rating=75, bowl_rating=40, bowl_style='Medium Pacer', is_overseas=False)
    s = cipl_match.build_cipl_state(
        match_id=1, overs=20, bat_user_id=1, bowl_user_id=2,
        bat_user_tg=11, bowl_user_tg=22, bat_xi=[p(i) for i in range(1, 12)],
        bowl_xi=[p(i, 'Bowler') for i in range(101, 112)],
        bat_team_name='Alpha', bowl_team_name='Beta', chat_id=-100,
        bat_bench=[p(50)], bowl_bench=[])
    s.update(tournament_id=7, current_over=4,
             _concussion_plan={'innings': 1, 'over': 4})
    s['bat_stats']['1'].update(runs=34, balls=18, fours=4, sixes=1)
    s['total_runs'] = 34
    s['partnership_runs'], s['partnership_balls'] = 34, 18
    return s


def test_retirement_keeps_figures_and_strike_positions_without_costing_a_wicket():
    s = state()
    s['impact_players'] = {'usage': {'1': {'used': True}}}
    old_impact = copy.deepcopy(s['impact_players'])
    rec = C.maybe_replace(s, rng=random.Random(1))
    assert rec['out_roster_id'] == 1 and rec['in_roster_id'] == 50
    assert s['batting_order'][s['striker_idx']]['roster_id'] == 50
    assert (s['striker_idx'], s['non_striker_idx'], s['next_batsman_idx']) == (0, 1, 2)
    assert s['total_runs'] == 34 and s['total_wickets'] == 0
    assert s['bat_stats']['1']['runs'] == 34 and s['bat_stats']['1']['balls'] == 18
    assert s['bat_stats']['1']['retired_hurt'] and not s['bat_stats']['1']['out']
    assert not next(p for p in s['bat_xi'] if p['roster_id'] == 1)['active']
    assert len(impact_player.active_players(s['bat_xi'])) == 11
    assert not s['bat_bench'] and s['bat_stats']['50']['balls'] == 0
    assert s['impact_players'] == old_impact
    assert impact_player.display_name(s['batting_order'][0]).endswith(' -CS')
    assert s['partnership_history'][-1]['runs'] == 34
    assert s['partnership_runs'] == 0
    # JSON save/reload preserves the one-incident limit.
    s = json.loads(json.dumps(s))
    assert C.maybe_replace(s) is None
    from handlers.super_over import _xi_players
    assert 1 not in [p['roster_id'] for p in _xi_players(s['bat_xi'])]
    cipl_match.end_first_innings(s)
    assert 1 not in [p['roster_id'] for p in cipl_match.eligible_bowlers(s)]
    assert s['inn1_bat_stats']['1']['runs'] == 34


def test_ineligible_reserves_do_not_cause_a_substitution():
    for change in ({'category': 'Bowler'}, {'bat_rating': 95}, {'is_overseas': True}, {'active': False}):
        s = state()
        s['bat_bench'][0].update(change)
        assert C.maybe_replace(s) is None
        assert len(s['bat_xi']) == 11 and s['batting_order'][0]['roster_id'] == 1
        assert s['_concussion_plan'] is None


def test_casual_matches_and_disabled_events_never_roll():
    for changes in ({'tournament_id': None}, {'concussion_enabled': False}):
        s = state()
        s.update(changes)
        with patch.object(C.random, 'random') as roll:
            assert C.maybe_replace(s) is None
            roll.assert_not_called()


def test_incidence_is_one_roll_per_match_not_per_over():
    s = state()
    s.pop('_concussion_plan')
    with patch.object(C.random, 'random', return_value=0.9) as roll:
        for over in range(1, 20):
            s['current_over'] = over
            assert C.maybe_replace(s) is None
        assert roll.call_count == 1


def test_real_over_announces_the_swap_and_preserves_archived_stats():
    s = state()
    s.update(current_bowler=s['bowl_xi'][0], batting_approach='balanced', bowling_approach='balanced')
    with patch('services.cipl_match.calculate_outcome', return_value=dict(type='run', runs=1)):
        summary = cipl_match.simulate_over(s)
    assert summary['concussion_substitution']['in_roster_id'] == 50
    assert s['total_wickets'] == 0
    assert s['current_over'] == 5
    assert 'concussion' in s['commentary_log'][-1]['text'].lower()
    from services.tournament_service import _collect_player_lines
    lines = _collect_player_lines(s)
    assert lines[0]['concussion_substitutions'][0]['in_roster_id'] == 50
    assert sum(r['balls'] for r in lines[0]['player_duels']) == 6
    assert next(r for r in lines if r['roster_id'] == 1)['bat_runs'] >= 34
