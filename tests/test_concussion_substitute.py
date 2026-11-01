"""Medical decisions preserve legal XIs, budgets, figures and resume pointers."""
import asyncio
import copy
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services import cipl_match, concussion_substitute as C, impact_player
from services.match_state_store import A_PICK_CONCUSSION, A_PICK_CIPL_BOWLER

ROLES = ['Batsman'] * 4 + ['Wicket Keeper'] + ['All-rounder'] * 2 + ['Bowler'] * 4


def player(rid, role='Batsman', **extra):
    return dict(roster_id=rid, name=f'Player {rid}', category=role,
                bat_rating=75, bowl_rating=60, card_rating=75, rating=75,
                bowl_style='Medium Pacer', is_overseas=False, **extra)


def state(side='bat'):
    s = cipl_match.build_cipl_state(
        match_id=1, overs=20, bat_user_id=1, bowl_user_id=2,
        bat_user_tg=11, bowl_user_tg=22,
        bat_xi=[player(i + 1, r) for i, r in enumerate(ROLES)],
        bowl_xi=[player(i + 101, r) for i, r in enumerate(ROLES)],
        bat_team_name='Alpha', bowl_team_name='Beta', chat_id=-100,
        bat_bench=[player(50), player(51)], bowl_bench=[player(150, 'Bowler')])
    s.update(tournament_id=7, current_over=4, xi_rules={'min_overseas': 0, 'max_overseas': 4},
             _medical_plans={str(1 if side == 'bat' else 2): {'innings': 1, 'over': 4}})
    s['bat_stats']['1'].update(runs=34, balls=18, fours=4, sixes=1)
    s['total_runs'] = 34
    s['partnership_runs'], s['partnership_balls'] = 34, 18
    s['current_bowler'] = s['bowl_xi'][7]
    s['bowl_stats']['108']['balls'] = 12
    return s


def reveal(s):
    rec = C.maybe_replace(s, rng=random.Random(1))
    assert rec is not None
    return rec


def replace(s, uid=1, rid=50):
    ok, message, rec = C.use(s, uid, rid, A_PICK_CONCUSSION)
    assert ok, message
    return rec


def test_detection_pauses_without_choosing_a_reserve_or_changing_figures():
    s = state()
    before = copy.deepcopy(s)
    rec = reveal(s)
    assert rec['out_roster_id'] == 1 and 'in_roster_id' not in rec
    assert len(C.options(s, 1)['incoming_options']) == 2
    for key in ('bat_xi', 'bat_stats', 'batting_order', 'bat_bench', 'total_runs', 'total_wickets'):
        assert s[key] == before[key]
    assert C.maybe_replace(s) is None
    with pytest.raises(ValueError, match='paused'):
        cipl_match.simulate_over(s)
    assert not impact_player.cipl_options(s, 1, A_PICK_CIPL_BOWLER)['can_use']


def test_retirement_keeps_figures_strike_positions_and_impact_budget():
    s = state()
    s['impact_players'] = {'usage': {'1': {'used': True}}}
    old_impact = copy.deepcopy(s['impact_players'])
    reveal(s)
    rec = replace(s, rid=51)  # the captain may choose either eligible reserve
    assert rec['out_roster_id'] == 1 and rec['in_roster_id'] == 51
    assert s['batting_order'][s['striker_idx']]['roster_id'] == 51
    assert (s['striker_idx'], s['non_striker_idx'], s['next_batsman_idx']) == (0, 1, 2)
    assert s['total_runs'] == 34 and s['total_wickets'] == 0
    assert s['bat_stats']['1']['runs'] == 34 and s['bat_stats']['1']['balls'] == 18
    assert s['bat_stats']['1']['retired_hurt'] and not s['bat_stats']['1']['out']
    old = next(p for p in s['bat_xi'] if p['roster_id'] == 1)
    assert not old['active'] and old['injured']
    assert len(impact_player.active_players(s['bat_xi'])) == 11
    assert s['impact_players'] == old_impact and not C.pending(s)
    assert impact_player.display_name(s['batting_order'][0]).endswith(' -CS')
    assert s['partnership_history'][-1]['runs'] == 34 and s['partnership_runs'] == 0
    assert len(s['bat_bench']) == 1
    from handlers.super_over import _xi_players
    assert 1 not in [p['roster_id'] for p in _xi_players(s['bat_xi'])]
    cipl_match.end_first_innings(s)
    assert 1 not in [p['roster_id'] for p in cipl_match.eligible_bowlers(s)]
    assert s['inn1_bat_stats']['1']['runs'] == 34


@pytest.mark.parametrize('change', [
    {'category': 'Bowler'}, {'bat_rating': 95}, {'bowl_rating': 80},
    {'is_overseas': True}, {'active': False}, {'injured': True},
])
def test_illegal_reserves_never_generate_an_incident(change):
    s = state()
    for p in s['bat_bench']:
        p.update(change)
    assert C.maybe_replace(s) is None
    assert len(s['bat_xi']) == 11 and not C.pending(s)
    assert s['_medical_plans']['1'] is None


def test_revalidation_rejects_a_changed_reserve_without_any_partial_swap():
    s = state()
    reveal(s)
    s['bat_bench'][0]['bat_rating'] = 99
    before = copy.deepcopy(s)
    assert not C.use(s, 1, 50, A_PICK_CONCUSSION)[0]
    assert s == before
    assert [p['roster_id'] for p in C.options(s, 1)['incoming_options']] == [51]


def test_another_captain_or_wrong_action_cannot_substitute():
    s = state()
    reveal(s)
    before = copy.deepcopy(s)
    for uid, action in ((2, A_PICK_CONCUSSION), (99, A_PICK_CONCUSSION), (1, A_PICK_CIPL_BOWLER)):
        assert not C.use(s, uid, 50, action)[0]
        assert s == before


def test_one_substitute_per_team_survives_json_and_the_innings_break():
    s = state()
    reveal(s)
    replace(s)
    s = json.loads(json.dumps(s))
    assert not C.use(s, 1, 51, A_PICK_CONCUSSION)[0]
    # A second team still has its own medical allowance in the chase.
    cipl_match.end_first_innings(s)
    s.update(current_over=4, _medical_plans={'2': {'innings': 2, 'over': 4}})
    s['bat_stats']['101']['balls'] = 10
    s['bat_bench'] = [player(150)]
    reveal(s)
    replace(s, 2, 150)
    assert {r['user_id'] for r in s['concussion_substitutions']} == {1, 2}
    s['_medical_plans']['1'] = {'innings': 2, 'over': 4}
    assert C.maybe_replace(s) is None


def test_a_bowling_sub_inherits_quota_and_consecutive_spell_restrictions():
    s = state('bowl')
    s.update(prev_bowler_rid=108, spell_rid=108, spell_units=1)
    reveal(s)
    replace(s, 2, 150)
    incoming = next(p for p in s['bowl_xi'] if p['roster_id'] == 150)
    assert s['bowl_stats']['108']['balls'] == 12
    assert s['bowl_stats']['150']['balls'] == 0
    assert cipl_match.overs_left(s, incoming) == 2
    assert s['prev_bowler_rid'] == s['spell_rid'] == 150
    assert 150 not in [p['roster_id'] for p in cipl_match.eligible_bowlers(s)]
    s['bowl_stats']['150']['balls'] = 12
    assert cipl_match.overs_left(s, incoming) == 0
    assert 150 not in [p['roster_id'] for p in cipl_match.eligible_bowlers(s)]
    cipl_match.end_first_innings(s)
    assert s['batting_order'][7]['roster_id'] == 150
    assert 108 not in [p['roster_id'] for p in s['batting_order']]


def test_bowling_style_and_rating_rules_are_enforced():
    s = state('bowl')
    s['bowl_bench'][0]['bowl_style'] = 'Off Spinner'
    assert C.maybe_replace(s) is None
    s = state()
    # The injured player is the only <= 75 card; neither reserve qualifies.
    s['xi_rules']['rating_rules'] = [{'max_rating': 75, 'min_players': 1}]
    for p in s['bat_xi'][1:]:
        p['card_rating'] = 85
    for p in s['bat_bench']:
        p['card_rating'] = 85
    assert C.maybe_replace(s) is None


def test_medical_overseas_for_overseas_is_legal_at_the_cap():
    s = state()
    for p in s['bat_xi'][:4] + s['bat_bench']:
        p['is_overseas'] = True
    reveal(s)
    replace(s)
    assert sum(bool(p.get('is_overseas')) for p in impact_player.active_players(s['bat_xi'])) == 4


def test_duplicate_variants_spent_impact_players_and_malformed_xis_are_rejected():
    for reason in ('variant', 'spent', 'short', 'no_keeper', 'no_bowling'):
        s = state()
        if reason == 'variant':
            s['bat_xi'][1]['parent_player_id'] = 999
            for p in s['bat_bench']:
                p['parent_player_id'] = 999
        elif reason == 'spent':
            s['impact_players'] = {'usage': {'1': {'used': True, 'in_roster_id': 50, 'out_roster_id': 51}}}
        elif reason == 'short':
            s['bat_xi'].pop()
        elif reason == 'no_keeper':
            s['bat_xi'][4]['category'] = 'Batsman'
        else:
            for p in s['bat_xi'][5:]:
                p['category'] = 'Batsman'
        assert C.maybe_replace(s) is None, reason


def test_personal_roster_role_and_duplicate_rules_are_enforced_too():
    s = state()
    s.pop('xi_rules')
    s['is_letsplay'] = True
    assert reveal(s)
    replace(s)
    s = state()
    s.pop('xi_rules')
    s['is_letsplay'] = True
    s['bat_xi'][4]['category'] = 'Batsman'
    assert C.maybe_replace(s) is None


def test_casual_matches_disabled_events_and_short_games_never_roll():
    for changes in ({'tournament_id': None}, {'concussion_enabled': False}, {'overs': 2}):
        s = state()
        s.pop('_medical_plans')
        s.update(changes)
        with patch.object(C.random, 'random') as roll:
            assert C.maybe_replace(s) is None
            roll.assert_not_called()


def test_incidence_is_two_rolls_per_match_not_per_over():
    s = state()
    s.pop('_medical_plans')
    with patch.object(C.random, 'random', return_value=0.9) as roll:
        for innings in (1, 2):
            for over in range(1, 20):
                s.update(innings=innings, current_over=over)
                assert C.maybe_replace(s) is None
        assert roll.call_count == 2


def test_no_incident_is_generated_after_an_innings_finishes():
    s = state()
    s.update(current_over=20, current_bowler=s['bowl_xi'][7],
             batting_approach='balanced', bowling_approach='balanced',
             _medical_plans={'1': {'innings': 1, 'over': 20}})
    with patch('services.cipl_match.calculate_outcome', return_value=dict(type='run', runs=1)):
        summary = cipl_match.simulate_over(s)
    assert not C.pending(s) and not summary.get('concussion_substitution')


def test_real_over_announces_the_pause_and_stats_survive_the_captains_choice():
    s = state()
    s.update(batting_approach='balanced', bowling_approach='balanced')
    with patch('services.cipl_match.calculate_outcome', return_value=dict(type='run', runs=1)):
        summary = cipl_match.simulate_over(s)
    assert summary['concussion_substitution']['out_roster_id'] in (1, 2)
    assert C.pending(s) and s['current_over'] == 5 and s['total_wickets'] == 0
    assert not s.get('concussion_substitutions')
    replace(s)
    from services.tournament_service import _collect_player_lines
    lines = _collect_player_lines(s)
    assert lines[0]['concussion_substitutions'][0]['in_roster_id'] == 50
    assert sum(r['balls'] for r in lines[0]['player_duels']) == 6
    assert next(r for r in lines if r['roster_id'] == 1)['bat_runs'] >= 34


def test_legacy_completed_substitution_consumes_only_its_own_teams_budget():
    s = state()
    s.pop('_medical_plans')
    s['_concussion_plan'] = {'innings': 1, 'over': 4}
    s['concussion_substitutions'] = [{'team': 'Alpha', 'in_roster_id': 51}]
    assert C.maybe_replace(s) is None
    assert C._used(s, 1) and not C._used(s, 2)


def test_pending_and_future_plans_are_durable_but_future_plans_are_hidden():
    from services.match_webapp_service import strip_hidden_plans
    s = state()
    reveal(s)
    restored = json.loads(json.dumps(s))
    assert len(C.options(restored, 1)['incoming_options']) == 2
    assert '_medical_plans' not in strip_hidden_plans(restored)
    assert C.pending(strip_hidden_plans(restored)) is not None


def test_app_knows_whose_medical_turn_it_is_and_lists_legal_reserves():
    from services.match_webapp_service import whose_turn, turn_state_name, approach_pick_value
    from services.crickidex_arena import _approach_payload
    for side, uid, role in (('bat', 1, 'batting'), ('bowl', 2, 'bowling')):
        s = state(side)
        reveal(s)
        assert whose_turn(s, A_PICK_CONCUSSION, uid)[1]
        assert not whose_turn(s, A_PICK_CONCUSSION, 99)[1]
        assert turn_state_name(A_PICK_CONCUSSION) == 'selecting_concussion_substitute'
        assert _approach_payload(s, A_PICK_CONCUSSION, role)['medical']['incomingOptions']
        assert not _approach_payload(s, A_PICK_CONCUSSION, 'spectator')['medical']['incomingOptions']
    assert approach_pick_value('concussion', {'rosterId': 50}) == 50


def test_chat_and_app_submissions_reject_the_wrong_captain_and_duplicate_taps():
    import handlers.cipl_play as cp
    s = state()
    reveal(s)
    ctx = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    action = {'value': A_PICK_CONCUSSION}

    async def save(_ctx, _mid, _state, next_action=None):
        if next_action:
            action['value'] = next_action

    with patch.object(cp, '_gs', new=AsyncMock(side_effect=lambda *a: s)), \
         patch.object(cp, '_get_next_action', new=AsyncMock(side_effect=lambda *a: action['value'])), \
         patch.object(cp, '_ss', new=AsyncMock(side_effect=save)), \
         patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=True)), \
         patch.object(cp, '_prompt_bowler', new=AsyncMock()) as prompt:
        assert not asyncio.run(cp.submit_concussion(ctx, 1, 22, 50))[0]
        assert not asyncio.run(cp.submit_pick(ctx, 1, 22, cp.PICK_BOWLER, 108))[0]
        assert asyncio.run(cp.submit_pick(ctx, 1, 11, 'concussion', 51, source='app'))[0]
        assert not asyncio.run(cp.submit_concussion(ctx, 1, 11, 50))[0]
    prompt.assert_awaited_once()
    assert len(s['concussion_substitutions']) == 1
    assert s['concussion_substitutions'][0]['in_roster_id'] == 51


def test_timeout_selects_the_closest_legal_reserve_without_forfeit():
    import handlers.cipl_play as cp
    s = state()
    reveal(s)
    s['bat_bench'][1]['bat_rating'] = 80
    ctx = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()),
                          job=SimpleNamespace(data={'mid': 1, 'expected': A_PICK_CONCUSSION}))
    with patch.object(cp, '_gs', new=AsyncMock(return_value=s)), \
         patch.object(cp, '_get_next_action', new=AsyncMock(return_value=A_PICK_CONCUSSION)), \
         patch.object(cp, '_ss', new=AsyncMock()), \
         patch.object(cp, '_clear_action_reminder', new=AsyncMock()), \
         patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=True)), \
         patch.object(cp, '_prompt_bowler', new=AsyncMock()), \
         patch.object(cp, '_forfeit_live_match', new=AsyncMock()) as forfeit:
        asyncio.run(cp._on_timeout(ctx))
    forfeit.assert_not_awaited()
    assert s['concussion_substitutions'][0]['in_roster_id'] == 50


def test_no_reserve_or_failed_injury_check_keeps_play_paused():
    import handlers.cipl_play as cp
    s = state()
    reveal(s)
    s['bat_bench'] = []  # an admin ruled every reserve out since the injury
    with patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=True)), \
         patch.object(cp, '_prompt_concussion', new=AsyncMock()) as prompt:
        assert not asyncio.run(cp._resolve_concussion(SimpleNamespace(), 1, s))[0]
        prompt.assert_awaited_once()
    assert C.pending(s) and len(impact_player.active_players(s['bat_xi'])) == 11
    with patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=False)):
        assert not asyncio.run(cp._resolve_concussion(SimpleNamespace(), 1, s, 50))[0]
    assert C.pending(s)


def test_medical_choice_follows_a_handover_without_giving_a_second_allowance():
    from services.match_webapp_service import whose_turn
    s = state()
    reveal(s)
    s['captain_controllers'] = {'1': 99}
    s['bat_user_tg'] = 999
    assert whose_turn(s, A_PICK_CONCUSSION, 99)[1]
    assert not whose_turn(s, A_PICK_CONCUSSION, 1)[1]
    replace(s)
    assert C._used(s, 1)


def test_the_hundred_medical_reserve_shares_the_twenty_ball_cap():
    s = state('bowl')
    s['ball_format'] = 'The100'
    s.update(spell_rid=108, spell_units=2)
    s['bowl_stats']['108']['balls'] = 10
    reveal(s)
    replace(s, 2, 150)
    p = next(p for p in s['bowl_xi'] if p['roster_id'] == 150)
    assert cipl_match.overs_left(s, p) == 2
    assert 150 not in [p['roster_id'] for p in cipl_match.eligible_bowlers(s)]
    s.update(spell_rid=107, spell_units=1)
    s['bowl_stats']['150']['balls'] = 10
    assert cipl_match.overs_left(s, p) == 0
    assert 150 not in [p['roster_id'] for p in cipl_match.eligible_bowlers(s)]


def test_resume_routes_a_pending_injury_back_to_the_medical_prompt():
    import handlers.cipl_play as cp
    s = state()
    reveal(s)
    with patch.object(cp, '_gs', new=AsyncMock(return_value=s)), \
         patch.object(cp, '_flush_dirty_locked', new=AsyncMock()), \
         patch.object(cp, '_get_next_action', new=AsyncMock(return_value=A_PICK_CIPL_BOWLER)), \
         patch.object(cp, '_prompt_concussion', new=AsyncMock()) as medical_prompt, \
         patch.object(cp, '_prompt_bowler', new=AsyncMock()) as bowler_prompt:
        assert asyncio.run(cp._resume_locked(SimpleNamespace(bot_data={}), 1))
    medical_prompt.assert_awaited_once()
    bowler_prompt.assert_not_awaited()


def test_failed_medical_prompt_is_marked_for_retry_and_contains_owner_tags():
    import handlers.cipl_play as cp
    s = state()
    reveal(s)
    with patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=True)), \
         patch.object(cp, '_new_action_message', new=AsyncMock(side_effect=RuntimeError('send failed'))) as send, \
         patch.object(cp, '_mark_prompt_delivered', new=AsyncMock()) as delivered:
        asyncio.run(cp._prompt_concussion(SimpleNamespace(bot_data={}), 1, s))
    assert delivered.call_args.args[-2:] == (False, A_PICK_CONCUSSION)
    buttons = send.call_args.args[3]
    assert buttons[0][0].callback_data == cp._imp_cb('cipl_cs_', 11, 1, 50)
    from services.button_access import check_callback_owner
    click = SimpleNamespace(callback_query=SimpleNamespace(
        data=buttons[0][0].callback_data, from_user=SimpleNamespace(id=22),
        message=SimpleNamespace(chat_id=-100, message_id=123)))
    assert not check_callback_owner(click)


def test_the_bot_uses_the_same_validated_medical_choice():
    import handlers.cipl_play as cp
    s = state()
    s.update(is_bot_match=True, bot_user_id=1)
    reveal(s)
    ctx = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    with patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=True)), \
         patch.object(cp, '_ss', new=AsyncMock()), \
         patch.object(cp, '_prompt_bowler', new=AsyncMock()):
        asyncio.run(cp._prompt_concussion(ctx, 1, s))
    assert not C.pending(s) and C._used(s, 1)
    assert len(impact_player.active_players(s['bat_xi'])) == 11


def test_injured_player_and_their_other_card_cannot_be_reused_as_impact_or_medical():
    s = state()
    s['bat_xi'][0]['player_id'] = 1000
    s['bat_bench'][0]['parent_player_id'] = 1000
    s['bat_bench'][0]['player_id'] = 1001
    reveal(s)
    assert [p['roster_id'] for p in C.options(s, 1)['incoming_options']] == [51]
    replace(s, rid=51)
    retired = next(p for p in s['bat_xi'] if p['roster_id'] == 1)
    s['bat_bench'].append(retired)
    remaining = [p['roster_id'] for p in impact_player.cipl_bench_for(s, 'bat')]
    assert 1 not in remaining and 50 not in remaining


def test_real_over_saves_medical_pointer_before_showing_the_captains_prompt():
    import handlers.cipl_play as cp
    s = state()
    s.update(batting_approach='balanced', bowling_approach='balanced')
    pointers = []

    async def save(_context, _mid, saved, next_action=None):
        pointers.append((next_action, bool(C.pending(saved))))

    ctx = SimpleNamespace(bot_data={}, job_queue=None)
    with patch('services.cipl_match.calculate_outcome', return_value=dict(type='run', runs=1)), \
         patch.object(cp, '_ss', new=AsyncMock(side_effect=save)), \
         patch.object(cp, '_edit_action_message', new=AsyncMock()), \
         patch.object(cp, '_post_tracked', new=AsyncMock()), \
         patch.object(cp, '_prompt_concussion', new=AsyncMock()) as prompt, \
         patch('services.league_records.announce', new=AsyncMock()), \
         patch.object(cp, '_fire_milestones_async'), \
         patch.object(cp, '_announce_drama_async'):
        asyncio.run(cp._run_over(ctx, 1, s))
    assert pointers[0] == (A_PICK_CONCUSSION, True)
    prompt.assert_awaited_once()
    assert s['current_over'] == 5


def test_medical_successor_of_an_impact_player_keeps_the_chosen_batting_slot():
    s = state('bowl')
    original = s['bowl_xi'][7]
    outgoing = dict(original, roster_id=151, name='Earlier Impact', impact_replacement=True)
    s['bowl_xi'][7] = dict(original, active=False, impact_replaced=True)
    s['bowl_xi'].append(outgoing)
    s['current_bowler'] = outgoing
    s['bowl_stats']['151'] = dict(s['bowl_stats']['108'])
    s['impact_players'] = {'usage': {'2': dict(used=True, out_roster_id=108,
                                              in_roster_id=151, bat_position=2)}}
    reveal(s)
    replace(s, 2, 150)
    cipl_match.end_first_innings(s)
    assert s['batting_order'][2]['roster_id'] == 150
    assert len(s['batting_order']) == 11
    assert not any(p['roster_id'] in (108, 151) for p in s['batting_order'])


def test_a_concurrent_state_save_never_acknowledges_an_uncommitted_medical_swap():
    import handlers.cipl_play as cp
    s = state()
    reveal(s)
    accept = AsyncMock()
    with patch.object(cp, '_gs', new=AsyncMock(return_value=s)), \
         patch.object(cp, '_get_next_action', new=AsyncMock(return_value=A_PICK_CONCUSSION)), \
         patch.object(cp, '_refresh_tournament_impact_benches', new=AsyncMock(return_value=True)), \
         patch.object(cp, '_ss', new=AsyncMock(side_effect=cp.StaleMatchState('newer snapshot'))), \
         patch.object(cp, '_prompt_bowler', new=AsyncMock()) as prompt:
        ok, message = asyncio.run(cp.submit_concussion(
            SimpleNamespace(bot_data={}), 1, 11, 50, on_accept=accept))
    assert not ok and 'moved on' in message
    accept.assert_not_awaited()
    prompt.assert_not_awaited()
