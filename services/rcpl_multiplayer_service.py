"""Durable group RCPL rooms. Every manager acts on the same versioned season."""
from dataclasses import dataclass, replace
import json
import time

from sqlalchemy.exc import IntegrityError

from services import auction_league_service as AL

LOBBY = "lobby"
_ACTIVE = (LOBBY, AL.PHASE_AUCTION, AL.PHASE_SEASON)


class RoomChanged(AL.AuctionLeagueError):
    """Another action committed while this one was being evaluated."""


@dataclass(frozen=True)
class Room:
    id: int
    chat_id: int
    host_id: int
    host_name: str
    version: int
    state: dict
    board_message_id: int | None


def _snapshot(row):
    return Room(row.id, row.chat_id, row.host_id, row.host_name, row.version,
                json.loads(row.state_json), row.board_message_id)


def _room_row(session, chat_id, room_id):
    from models import AuctionLeagueRoom
    rows = session.query(AuctionLeagueRoom).filter_by(chat_id=chat_id)
    if room_id is None:
        rows = rows.filter(AuctionLeagueRoom.status.in_(_ACTIVE))
    else:
        rows = rows.filter_by(id=room_id)
    return rows.one_or_none()


def get_room(chat_id, room_id=None):
    """Return the active room, or an exact room belonging to this chat (including finished rooms)."""
    from database import get_session
    with get_session() as session:
        row = _room_row(session, chat_id, room_id)
        return _snapshot(row) if row else None


def active_auctions(due_before=None):
    from database import get_session
    from models import AuctionLeagueRoom
    with get_session() as session:
        rows = session.query(AuctionLeagueRoom).filter_by(status=AL.PHASE_AUCTION)
        if due_before is not None:
            rows = rows.filter(AuctionLeagueRoom.next_event_at <= due_before)
        return [_snapshot(row) for row in rows.order_by(AuctionLeagueRoom.next_event_at)]


def _new_lobby(session, league_id):
    from handlers.challenge import normalize_challenge_league
    from models import ChallengeLeague
    league = session.get(ChallengeLeague, league_id)
    if not league or not league.is_active or league.auction_enabled is False:
        raise AL.AuctionLeagueError("That league is unavailable for RCPL.")
    teams = AL.league_teams(session, league)
    if len(teams) < 4 or len({team['name'] for team in teams}) != len(teams):
        raise AL.AuctionLeagueError("The league needs at least four active franchises, each with its own name.")
    key = normalize_challenge_league(league.short_code or league.name)
    state = AL.new_state(AL.league_dict(league, key), teams, teams[0]['name'], ai_retain=False)
    # Every side must be able to finish with a legal squad from this snapshot.
    pool = state['pool'].values()
    for role, minimum in AL.ROLE_MIN.items():
        need = minimum * len(teams)
        if sum(card['category'] == role for card in pool) < need:
            raise AL.AuctionLeagueError(f"This league needs at least {need} {AL.ROLE_PLURAL[role].lower()} for {len(teams)} franchises.")
    domestic = len(teams) * AL.required_domestic(state)
    if sum(not card['is_overseas'] for card in pool) < domestic:
        raise AL.AuctionLeagueError(f"This league needs at least {domestic} domestic players to field the XIs.")
    state.update(phase=LOBBY, managers={})
    return state


def create_room(chat_id, host_id, host_name, league_id):
    """First league selection opens the lobby. Racing selections reopen the winning room."""
    from database import get_session
    from models import AuctionLeagueRoom
    with get_session() as session:
        existing = _room_row(session, chat_id, None)
        if existing is not None:
            return _snapshot(existing)
        state = _new_lobby(session, league_id)
        row = AuctionLeagueRoom(chat_id=chat_id, host_id=host_id, host_name=str(host_name or 'Host')[:150],
                                status=LOBBY, version=1, state_json=json.dumps(state, separators=(',', ':')))
        session.add(row)
        try:
            session.flush()
            room = _snapshot(row)
            session.commit()
            return room
        except IntegrityError:
            session.rollback()
            existing = _room_row(session, chat_id, None)
            if existing is None:
                raise
            return _snapshot(existing)


def change_room(chat_id, room_id, operation, *, session=None):
    """Persist one synchronous state transition. A caller supplying a session owns its commit.

    An operation can return False to skip a write (for example a duplicate match result).
    The conditional UPDATE protects bids, seat claims and worker ticks across bot processes.
    """
    if room_id is None:
        raise AL.AuctionLeagueError('Open the current RCPL board in this group.')
    if session is None:
        from database import get_session
        with get_session() as own_session:
            room = change_room(chat_id, room_id, operation, session=own_session)
            own_session.commit()
            return room
    from models import AuctionLeagueRoom
    row = _room_row(session, chat_id, room_id)
    if row is None:
        raise AL.AuctionLeagueError('This RCPL room does not belong to this group.')
    room = _snapshot(row)
    if row.status not in _ACTIVE:
        raise AL.AuctionLeagueError('This RCPL room has finished.')
    if operation(room) is False:
        return _snapshot(row)
    encoded = json.dumps(room.state, separators=(',', ':'))
    if encoded == row.state_json:
        return room
    status = room.state['phase']
    next_event = None
    if status == AL.PHASE_AUCTION:
        from services.rcpl_multiplayer_auction import next_event_at
        next_event = next_event_at(room.state)
    saved = (session.query(AuctionLeagueRoom)
             .filter_by(id=room.id, chat_id=chat_id, version=room.version)
             .update({'state_json': encoded, 'version': room.version + 1, 'status': status,
                      'next_event_at': next_event}, synchronize_session=False))
    if saved != 1:
        raise RoomChanged('The RCPL board changed. Reopen /rcpl and try again.')
    session.expire(row)
    return replace(room, version=room.version + 1)


def set_board_message(chat_id, room_id, message_id, *, expected_old=None):
    """Record the public board without advancing the gameplay version or overwriting a newer board."""
    from database import get_session
    from models import AuctionLeagueRoom
    with get_session() as session:
        updated = (session.query(AuctionLeagueRoom)
                   .filter_by(chat_id=chat_id, id=room_id, board_message_id=expected_old)
                   .update({'board_message_id': message_id}, synchronize_session=False))
        session.commit()
        return updated == 1


def managed_team(state, user_id):
    return next((team for team, manager in state['managers'].items() if manager['id'] == user_id), None)


def _require_lobby(room):
    if room.state['phase'] != LOBBY:
        raise AL.AuctionLeagueError('Franchise selection closed when the auction started.')


def _require_host(room, user_id):
    if room.host_id != user_id:
        raise AL.AuctionLeagueError('Only the host can do that.')


def claim_team(chat_id, room_id, user_id, display_name, team_index):
    def claim(room):
        _require_lobby(room)
        teams = room.state['team_order']
        if not isinstance(team_index, int) or not 0 <= team_index < len(teams):
            raise AL.AuctionLeagueError('Choose a franchise from the current board.')
        team = teams[team_index]
        current = managed_team(room.state, user_id)
        if current == team:
            return False
        if current:
            raise AL.AuctionLeagueError('Leave your current franchise before choosing another.')
        if team in room.state['managers']:
            raise AL.AuctionLeagueError('Another manager already claimed that franchise.')
        room.state['managers'][team] = {'id': user_id, 'name': str(display_name or 'Manager')[:150]}
    return change_room(chat_id, room_id, claim)


def leave_team(chat_id, room_id, user_id):
    def leave(room):
        _require_lobby(room)
        team = managed_team(room.state, user_id)
        if team is None:
            raise AL.AuctionLeagueError('You have not claimed a franchise here.')
        del room.state['managers'][team]
    return change_room(chat_id, room_id, leave)


def start_room(chat_id, room_id, user_id):
    from services import rcpl_multiplayer_auction as auction
    def start(room):
        _require_host(room, user_id)
        _require_lobby(room)
        if not room.state['managers']:
            raise AL.AuctionLeagueError('At least one manager must choose a franchise first.')
        auction.start(room.state, now=time.time())
    return change_room(chat_id, room_id, start)


def bid(chat_id, room_id, user_id, seq=None, expected_price=None):
    from services import rcpl_multiplayer_auction as auction
    return change_room(chat_id, room_id, lambda room: auction.bid(room.state, user_id, seq, expected_price))


def control_auction(chat_id, room_id, user_id, action):
    from services import rcpl_multiplayer_auction as auction
    control = {"pause": auction.pause, "resume": auction.resume}[action]

    def change(room):
        _require_host(room, user_id)
        control(room.state, now=time.time())

    return change_room(chat_id, room_id, change)


def finish_auction(chat_id, room_id, user_id, seq=None, expected_price=None):
    from services import rcpl_multiplayer_auction as auction
    def finish(room):
        _require_host(room, user_id)
        auction.finish_early(room.state, seq, expected_price)
    return change_room(chat_id, room_id, finish)


def cancel_room(chat_id, room_id, user_id):
    def cancel(room):
        _require_host(room, user_id)
        room.state['phase'] = AL.PHASE_ABANDONED
    return change_room(chat_id, room_id, cancel)
