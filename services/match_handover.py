"""Temporary tournament captain control; team/stat identities never change."""
import json
import secrets
import time

REQUEST_TTL = 60
LIVE_STATUSES = ('active', 'playing', 'toss', 'selecting', 'pending', 'accepted', 'in_progress')


def controller_uid(state, team_uid):
    return (state.get('captain_controllers') or {}).get(str(team_uid), team_uid)


def seat_for_user(state, user_id):
    """Map a current controller to the stable match participant identity."""
    for side in ('bat', 'bowl'):
        uid = state.get(f'{side}_team_id')
        if uid is not None and controller_uid(state, uid) == user_id:
            return uid
    return None


def controlled_match(session, user_id, *, exclude=None):
    """Include delegated captains in the one-active-match-per-player gate."""
    from models import Match, MatchState
    for match, raw in (session.query(Match, MatchState.state_json)
                       .join(MatchState, MatchState.match_id == Match.id)
                       .filter(Match.status.in_(LIVE_STATUSES),
                               MatchState.state_json.contains('"captain_controllers"'))):
        if match.id == exclude:
            continue
        try:
            controllers = (json.loads(raw) or {}).get('captain_controllers') or {}
        except (ValueError, TypeError, AttributeError):
            continue
        if user_id in controllers.values():
            return match
    return None


def _side_for_tg(state, tg_id):
    return next((side for side in ('bat', 'bowl')
                 if state.get(f'{side}_user_tg') == tg_id), None)


def _team(session, state, side):
    from models import TournamentTeam
    # The tournament's team name is carried with its squad through both innings.
    return (session.query(TournamentTeam)
            .filter_by(tournament_id=state['tournament_id'],
                       name=state.get(f'{side}_team_name')).one_or_none())


def _validate(session, state, sender_tg, target_tg):
    from models import Match, User
    from services.tournament_service import is_team_member
    if state.get('mode') != 'cipl_approach' or not state.get('tournament_id'):
        raise ValueError('Handover is available during an official tournament match.')
    match = session.get(Match, state.get('match_id'))
    if (not match or match.status not in LIVE_STATUSES
            or match.tournament_id != state['tournament_id']):
        raise ValueError('This tournament match is no longer live.')
    side = _side_for_tg(state, sender_tg)
    if side is None:
        raise ValueError('Only the currently playing captain can offer a handover.')
    team = _team(session, state, side)
    if not is_team_member(team, sender_tg):
        raise ValueError('Only the playing owner or co-owner can hand over this team.')
    if sender_tg == target_tg:
        raise ValueError('Choose another owner or co-owner of your team.')
    if _side_for_tg(state, target_tg):
        raise ValueError('The opposing captain cannot take control of your team.')
    if not is_team_member(team, target_tg):
        raise ValueError('The receiver must be a registered owner or co-owner of your team.')
    # The state mutation shares this transaction. Serialize simultaneous
    # acceptances for this manager across different matches (PostgreSQL).
    user = session.query(User).filter_by(telegram_id=target_tg).with_for_update().first()
    if not user or user.is_banned:
        raise ValueError('The receiver must have an active bot account (/debut).')
    busy = (session.query(Match)
            .filter(Match.id != match.id, Match.status.in_(LIVE_STATUSES),
                    (Match.user1_id == user.id) | (Match.user2_id == user.id)).first())
    if busy or controlled_match(session, user.id, exclude=match.id):
        raise ValueError('The receiver is already playing another match.')
    return side, team, user


def offer(session, state, sender_tg, target_tg, *, now=None):
    now = time.time() if now is None else now
    side, team, target = _validate(session, state, sender_tg, target_tg)
    seat = str(state[f'{side}_team_id'])
    pending = state.setdefault('captain_offers', {})
    existing = pending.get(seat)
    if existing and existing['expires_at'] > now:
        raise ValueError('Your team already has a pending handover. Cancel it or wait 60 seconds.')
    request = dict(token=secrets.token_hex(4), sender_tg=sender_tg,
                   target_tg=target_tg, expires_at=now + REQUEST_TTL,
                   team=team.name, seat=seat,
                   target_name=target.first_name or target.username or 'Co-owner')
    pending[seat] = request
    return dict(request)


def answer(session, state, actor_tg, token, action, *, now=None):
    now = time.time() if now is None else now
    pending = state.get('captain_offers') or {}
    req = next((r for r in pending.values() if r['token'] == token), None)
    if not req or req['expires_at'] <= now:
        raise ValueError('This handover expired or was already answered.')
    allowed = req['sender_tg'] if action == 'cancel' else req['target_tg']
    if actor_tg != allowed:
        raise ValueError('Only the receiving manager can answer; the sender can Cancel.')
    if action not in ('accept', 'decline', 'cancel'):
        raise ValueError('Unknown handover action.')
    if action == 'accept':
        side, _team_row, target = _validate(
            session, state, req['sender_tg'], req['target_tg'])
        if str(state[f'{side}_team_id']) != req['seat']:
            raise ValueError('The playing captain changed. Send a new handover.')
        state[f'{side}_user_tg'] = target.telegram_id
        state.setdefault('captain_controllers', {})[req['seat']] = target.id
        state.setdefault('user_names', {})[str(target.telegram_id)] = req['target_name']
        # Rules keyed by the previous captain's Telegram id follow the team.
        by_user = (state.get('xi_rules') or {}).get('by_user') or {}
        if str(req['sender_tg']) in by_user:
            by_user[str(target.telegram_id)] = by_user[str(req['sender_tg'])]
        state.setdefault('captain_handovers', []).append(dict(
            team=req['team'], from_tg=req['sender_tg'], to_tg=target.telegram_id,
            innings=state.get('innings'), over=state.get('current_over'), at=now))
    del pending[req['seat']]
    return dict(req, action=action)
