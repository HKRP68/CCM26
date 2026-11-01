"""Tournament medical pauses and one like-for-like substitute per team.

Detection and replacement are separate persisted steps. No delivery can be
played while an assessment is pending. The captain chooses from legal reserves;
the timeout/bot uses the closest match. Impact Player remains a separate budget.
"""
import random
from types import SimpleNamespace

from services import impact_player

# One roll per team per match, never one roll per over. Actual incidence is
# lower because a safe unused reserve and a player with workload are required.
MATCH_CHANCE = 0.05


def _role(player):
    return str(player.get('category') or '').casefold().replace(' ', '').replace('-', '')


def pending(state):
    value = state.get('concussion_pending')
    return value if isinstance(value, dict) else None


def _used(state, user_id):
    for rec in state.get('concussion_substitutions') or []:
        if str(rec.get('user_id')) == str(user_id):
            return True
        # Records saved by the old automatic system had only the team name.
        if rec.get('user_id') is None:
            side = impact_player.cipl_side(state, user_id)
            if side and rec.get('team') == state.get(f'{side}_team_name'):
                return True
    return False


def _identity(p):
    return p.get('parent_player_id') or p.get('player_id') or p.get('roster_id')


def _name_key(p):
    return ''.join(c for c in str(p.get('name') or '').casefold() if c.isalnum())


def swap_error(state, side, outgoing, incoming):
    """Validate the full post-substitution XI, without the tactical Impact cap."""
    if not impact_player.is_active(incoming) or incoming.get('injured'):
        return 'That reserve is unavailable.'
    if not _role(outgoing) or _role(outgoing) != _role(incoming):
        return 'A like-for-like substitute must have the same role.'
    if bool(outgoing.get('is_overseas')) != bool(incoming.get('is_overseas')):
        return 'The substitute must have the same overseas status.'
    for key in ('bat_rating', 'bowl_rating'):
        try:
            difference = abs(float(outgoing.get(key) or 0) - float(incoming.get(key) or 0))
        except (TypeError, ValueError):
            return 'The reserve has an invalid rating.'
        if not difference <= 10:
            return 'Both ratings must be within 10 points of the injured player.'
    if _role(outgoing) in ('bowler', 'allrounder'):
        if (outgoing.get('bowl_style') or '').casefold() != (incoming.get('bowl_style') or '').casefold():
            return 'The substitute must have the same bowling style.'
    before = impact_player.active_players(state.get(f'{side}_xi') or [])
    after = [p for p in before if p.get('roster_id') != outgoing.get('roster_id')] + [incoming]
    if len(before) != 11 or len(after) != 11:
        return 'The replacement must leave exactly 11 active players.'
    if len({p.get('roster_id') for p in after}) != 11:
        return 'A player cannot appear twice in the XI.'
    names = [_name_key(p) for p in after if _name_key(p)]
    if len({_identity(p) for p in after}) != 11 or len(set(names)) != len(names):
        return 'Two versions of the same player cannot be in the XI.'
    resolved = impact_player._rules_for(state, side)
    if resolved:
        from services.xi_rules import validate_challenge_xi_dicts
        rules, lo, hi = resolved
        ok, error = validate_challenge_xi_dicts(after, lo, hi, rules.get('rating_rules') or None)
        return '' if ok else error
    if state.get('is_letsplay') or state.get('lpmulti'):
        from services.xi_rules import validate_roster_xi
        pairs = [(None, SimpleNamespace(
            category=p.get('category') or 'Batsman',
            bowl_rating=p.get('bowl_rating') or 0,
            id=p.get('player_id') or p.get('roster_id'),
            parent_player_id=p.get('parent_player_id'),
            name=p.get('name') or 'Player', version=p.get('version') or 'Base'))
                 for p in after]
        ok, errors = validate_roster_xi(pairs)
        return '' if ok else '; '.join(errors)
    # Legacy Challenge League states may predate xi_rules; enforce its core
    # role rules even then, rather than silently accepting an illegal XI.
    from services.xi_rules import validate_challenge_xi_dicts
    ok, error = validate_challenge_xi_dicts(after)
    return '' if ok else error


def _reserves(state, side, outgoing):
    # Never re-use anybody who has already taken part, including an Impact
    # Player taken off. Inactive historical identities still count as spent.
    played = state.get(f'{side}_xi') or []
    spent = {p.get('roster_id') for p in played}
    spent_identities = {_identity(p) for p in played}
    spent_names = {_name_key(p) for p in played if _name_key(p)}
    usage = (state.get('impact_players') or {}).get('usage') or {}
    spent.update(impact_player._spent_roster_ids(usage))
    return [p for p in state.get(f'{side}_bench') or []
            if p.get('roster_id') not in spent
            and _identity(p) not in spent_identities
            and (not _name_key(p) or _name_key(p) not in spent_names)
            and not swap_error(state, side, outgoing, p)]


def options(state, user_id):
    rec = pending(state)
    side = impact_player.cipl_side(state, user_id)
    if not rec or side is None or str(rec.get('user_id')) != str(user_id):
        return {'can_use': False, 'message': 'No medical substitution is awaiting your team.'}
    if _used(state, user_id):
        return {'can_use': False, 'message': 'Your team has already used its one concussion substitute.'}
    outgoing = next((p for p in impact_player.active_players(state.get(f'{side}_xi') or [])
                     if p.get('roster_id') == rec.get('out_roster_id')), None)
    reserves = _reserves(state, side, outgoing) if outgoing else []
    return {'can_use': bool(reserves), 'side': side, 'incident': rec,
            'outgoing': outgoing, 'incoming_options': reserves,
            'message': ('Choose a like-for-like concussion substitute. Impact Player is unchanged.'
                        if reserves else 'No fit reserve keeps the XI legal. Play stays paused; an admin must restore an eligible reserve.')}


def best_reserve(opts):
    outgoing = opts.get('outgoing') or {}
    return min(opts.get('incoming_options') or [], key=lambda p: (
        sum(abs(float(p.get(k) or 0) - float(outgoing.get(k) or 0))
            for k in ('bat_rating', 'bowl_rating')), int(p['roster_id'])), default=None)


def maybe_replace(state, *, rng=None):
    """Reveal an injury after an over; leave the replacement to the captain.

    Kept under its old name for engine compatibility. No stats or XI identities
    are changed until ``use`` accepts the captain's replacement.
    """
    if (not state.get('tournament_id') or state.get('concussion_enabled', True) is False
            or state.get('standalone_super_over') or pending(state)):
        return None
    rng = rng or random
    if '_medical_plans' not in state:
        plans = {}
        legacy = state.get('_concussion_plan', 'new')
        for side in ('bat', 'bowl'):
            uid = state.get(f'{side}_team_id')
            plan = None
            if legacy != 'new':
                plan = legacy if side == 'bat' else None
            elif not _used(state, uid) and int(state.get('overs') or 0) >= 3:
                if rng.random() < MATCH_CHANCE:
                    plan = {'innings': rng.choice((1, 2)),
                            'over': rng.randint(2, int(state['overs']) - 1)}
            plans[str(uid)] = plan
        state['_medical_plans'] = plans
    for side in ('bat', 'bowl'):
        uid = state.get(f'{side}_team_id')
        plan = state['_medical_plans'].get(str(uid))
        if (not plan or _used(state, uid) or
                (state.get('innings'), state.get('current_over')) != (plan['innings'], plan['over'])):
            continue
        # Consume even when there is no suitable reserve; never fish for an
        # easier victim or re-roll after reloading the state.
        state['_medical_plans'][str(uid)] = None
        if '_concussion_plan' in state:
            state['_concussion_plan'] = None
        choices = []
        if side == 'bat':
            order = state.get('batting_order') or []
            players = [order[i] for i in (state.get('striker_idx'), state.get('non_striker_idx'))
                       if isinstance(i, int) and 0 <= i < len(order)]
        else:
            # Bowling workload: the player who just completed this over.
            players = [state.get('current_bowler') or {}]
        for outgoing in players:
            stats = (state.get(f'{side}_stats') or {}).get(str(outgoing.get('roster_id')), {})
            if (stats.get('balls', 0) > 0 and not stats.get('out')
                    and impact_player.is_active(outgoing) and _reserves(state, side, outgoing)):
                choices.append(outgoing)
        if not choices:
            continue
        outgoing = rng.choice(choices)
        injury = 'Concussion assessment' if side == 'bat' else 'Bowling shoulder injury'
        rec = dict(user_id=uid, team=state.get(f'{side}_team_name'),
                   innings=state['innings'], over=state['current_over'],
                   out_player=outgoing['name'], out_roster_id=outgoing['roster_id'],
                   injury=injury, side=side)
        rec['text'] = (f"🩺 {outgoing['name']} — {injury.lower()}. Play is paused. "
                       f"{rec['team']} may choose one like-for-like concussion substitute. "
                       'The injured player cannot return; Impact Player is unchanged.')
        state['concussion_pending'] = rec
        # Preserve the original reserve pool through temporary injury filtering.
        # An admin declaring a reserve fit must be able to unblock this pause.
        state['concussion_reserve_pool'] = [dict(p) for p in state.get(f'{side}_bench') or []]
        state.setdefault('commentary_log', []).append(dict(
            type='injury', text=rec['text'], over=str(state['current_over']), concussion=True))
        return rec
    return None


def use(state, user_id, in_roster_id, next_action):
    """Apply one medical swap atomically. Returns ``(ok, message, record)``."""
    from services.match_state_store import A_PICK_CONCUSSION
    if next_action != A_PICK_CONCUSSION or state.get('over_in_progress'):
        return False, 'A concussion substitute can only be chosen during the medical pause.', None
    opts = options(state, user_id)
    incoming = next((p for p in opts.get('incoming_options', [])
                     if p.get('roster_id') == in_roster_id), None)
    if not opts.get('can_use') or not incoming:
        return False, opts.get('message') if not opts.get('can_use') else 'That reserve is not a legal like-for-like replacement.', None
    side, outgoing = opts['side'], opts['outgoing']
    out_rid = outgoing['roster_id']
    # All validation above is read-only. Only now mutate XI, stats and budget.
    incoming = dict(incoming, active=True, concussion_replacement=True,
                    replaced_roster_id=out_rid)
    xi = state[f'{side}_xi']
    out_idx = next(i for i, p in enumerate(xi) if p.get('roster_id') == out_rid)
    xi[out_idx] = dict(outgoing, active=False, injured=True, concussion_replaced=True,
                       replaced_by_roster_id=in_roster_id)
    xi.append(incoming)
    state[f'{side}_bench'] = [p for p in state.get(f'{side}_bench') or []
                             if p.get('roster_id') != in_roster_id]
    if side == 'bat':
        order = state['batting_order']
        idx = next(i for i, p in enumerate(order) if p.get('roster_id') == out_rid)
        if state.get('partnership_balls') or state.get('partnership_runs'):
            state.setdefault('partnership_history', []).append(dict(
                wicket=state.get('total_wickets', 0) + 1,
                batsman1=order[state['striker_idx']]['name'],
                batsman2=order[state['non_striker_idx']]['name'],
                runs=state.get('partnership_runs', 0), balls=state.get('partnership_balls', 0),
                notout=True, retired_hurt=True))
        state['partnership_runs'] = state['partnership_balls'] = 0
        order[idx] = incoming
        state['bat_stats'][str(out_rid)].update(
            retired_hurt=True, how_out='Retired hurt (concussion)', dismissal='Retired hurt (concussion)')
        from services.cipl_match import _new_bat_stat
        state['bat_stats'][str(in_roster_id)] = _new_bat_stat()
    else:
        from services.cipl_match import _new_bowl_stat
        # Share the outgoing bowler's remaining quota; a medical replacement
        # must never manufacture a fresh four overs or bypass a consecutive cap.
        incoming['concussion_bowling_offset'] = int(
            (state.get('bowl_stats') or {}).get(str(out_rid), {}).get('balls') or 0)
        incoming['concussion_quota_innings'] = state.get('innings')
        state['bowl_stats'][str(in_roster_id)] = _new_bowl_stat()
        for key in ('prev_bowler_rid', 'spell_rid'):
            if state.get(key) == out_rid:
                state[key] = in_roster_id
    rec = dict(opts['incident'], in_player=incoming['name'], in_roster_id=in_roster_id)
    rec['text'] = (f"🩺 Concussion substitute: {incoming['name']} replaces {outgoing['name']} "
                   f"for {rec['team']}. The injured player cannot return. "
                   'One medical replacement used; Impact Player allowance is unchanged.')
    state.setdefault('concussion_substitutions', []).append(rec)
    state.pop('concussion_pending', None)
    state.pop('concussion_reserve_pool', None)
    state.setdefault('commentary_log', []).append(dict(
        type='concussion_substitute', name=incoming['name'], text=rec['text'],
        over=str(rec['over']), concussion=True))
    return True, rec['text'], rec
