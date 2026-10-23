"""Rare tournament concussion events, separate from tactical Impact Players.

The incident is revealed between overs after a batter has faced deliveries.
A like-for-like reserve replaces them permanently; no wicket is charged. The
one roll per match caps incidence at 0.5%, independent of match length.
"""
import random

from services import impact_player

MATCH_CHANCE = 0.005


def _role(player):
    return str(player.get('category') or '').casefold().replace(' ', '').replace('-', '')


def _suitable(state, outgoing, incoming):
    if not impact_player.is_active(incoming) or not _role(outgoing):
        return False
    if _role(outgoing) != _role(incoming):
        return False
    if bool(outgoing.get('is_overseas')) != bool(incoming.get('is_overseas')):
        return False
    for key in ('bat_rating', 'bowl_rating'):
        if abs(float(outgoing.get(key) or 0) - float(incoming.get(key) or 0)) > 10:
            return False
    if _role(outgoing) in ('bowler', 'allrounder'):
        if outgoing.get('bowl_style') != incoming.get('bowl_style'):
            return False
    # Retain XI rating rules too. Concussion replacements are not subject to
    # the Impact rule forbidding overseas-for-overseas when already at the cap.
    resolved = impact_player._rules_for(state, 'bat')
    if resolved:
        from services.xi_rules import validate_challenge_xi_dicts
        rules, lo, hi = resolved
        after = [p for p in impact_player.active_players(state.get('bat_xi'))
                 if p.get('roster_id') != outgoing['roster_id']] + [incoming]
        ok, _ = validate_challenge_xi_dicts(after, lo, hi, rules.get('rating_rules') or None)
        if not ok:
            return False
    return True


def maybe_replace(state, *, rng=None):
    """Called once a completed over, while another over remains to be played."""
    if not state.get('tournament_id') or state.get('concussion_enabled', True) is False:
        return None
    if state.get('concussion_substitutions'):
        return None
    rng = rng or random
    if '_concussion_plan' not in state:
        plan = None
        if rng.random() < MATCH_CHANCE and int(state.get('overs') or 0) >= 3:
            plan = {'innings': rng.choice((1, 2)),
                    'over': rng.randint(2, int(state['overs']) - 1)}
        state['_concussion_plan'] = plan
    plan = state['_concussion_plan']
    if not plan or (state.get('innings'), state.get('current_over')) != (plan['innings'], plan['over']):
        return None
    # Consume even if there is no safe like-for-like replacement. Never reroll
    # until a convenient player is injured or a stronger substitute qualifies.
    state['_concussion_plan'] = None
    order = state.get('batting_order') or []
    bench = state.get('bat_bench') or []
    used_ids = {p.get('roster_id') for p in state.get('bat_xi') or []}
    choices = []
    for idx in (state.get('striker_idx'), state.get('non_striker_idx')):
        if not isinstance(idx, int) or not 0 <= idx < len(order):
            continue
        outgoing = order[idx]
        stats = (state.get('bat_stats') or {}).get(str(outgoing['roster_id']), {})
        if not stats.get('balls') or stats.get('out') or not impact_player.is_active(outgoing):
            continue
        replacements = [p for p in bench if p.get('roster_id') not in used_ids
                        and _suitable(state, outgoing, p)]
        if replacements:
            choices.append((idx, outgoing, replacements))
    if not choices:
        return None
    idx, outgoing, replacements = rng.choice(choices)
    incoming = dict(rng.choice(replacements), active=True, concussion_replacement=True,
                    replaced_roster_id=outgoing['roster_id'])
    # The old identity stays in the XI for scorecards and tournament statistics.
    for i, p in enumerate(state['bat_xi']):
        if p['roster_id'] == outgoing['roster_id']:
            state['bat_xi'][i] = dict(p, active=False, concussion_replaced=True,
                                     replaced_by_roster_id=incoming['roster_id'])
            break
    state['bat_xi'].append(incoming)
    if state.get('partnership_balls') or state.get('partnership_runs'):
        state.setdefault('partnership_history', []).append(dict(
            wicket=state.get('total_wickets', 0) + 1,
            batsman1=order[state['striker_idx']]['name'],
            batsman2=order[state['non_striker_idx']]['name'],
            runs=state.get('partnership_runs', 0), balls=state.get('partnership_balls', 0),
            notout=True, retired_hurt=True))
        state['partnership_runs'] = state['partnership_balls'] = 0
    order[idx] = incoming
    state['bat_bench'] = [p for p in bench if p['roster_id'] != incoming['roster_id']]
    old_stats = state['bat_stats'][str(outgoing['roster_id'])]
    old_stats.update(retired_hurt=True, how_out='Retired hurt (concussion)',
                     dismissal='Retired hurt (concussion)')
    from services.cipl_match import _new_bat_stat
    state['bat_stats'][str(incoming['roster_id'])] = _new_bat_stat()
    rec = dict(team=state.get('bat_team_name'), innings=state['innings'],
               over=state['current_over'], out_player=outgoing['name'],
               in_player=incoming['name'], out_roster_id=outgoing['roster_id'],
               in_roster_id=incoming['roster_id'])
    text = (f"🩺 Concussion assessment: {outgoing['name']} retires hurt. "
            f"{incoming['name']} comes in as the like-for-like concussion substitute "
            f"for {rec['team']}. The injured player cannot return. "
            'Impact Player allowance is unchanged.')
    rec['text'] = text
    state['concussion_substitutions'] = [rec]
    state.setdefault('commentary_log', []).append(dict(
        type='new_batsman', name=incoming['name'], text=text,
        over=str(state['current_over']), concussion=True))
    return rec
