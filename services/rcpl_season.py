"""Shared RCPL fixture readiness, live match links, and season results."""

from services import auction_league_service as AL


def next_fixture(state, user_id=None, opponent_id=None):
    for fixture in AL.pending_fixtures(state):
        managers = managers_for(state, fixture).values()
        if (user_id is None or user_id in managers) and (opponent_id is None or opponent_id in managers):
            return fixture
    return None


def managers_for(state, fixture):
    return {team: state["managers"][team]["id"] for team in (fixture["home"], fixture["away"])
            if team in state["managers"]}


def ready_to_start(state, fixture):
    required = set(managers_for(state, fixture).values())
    return bool(required) and required.issubset(fixture.get("ready", ()))


def prepare(state):
    while state["phase"] == AL.PHASE_SEASON:
        fixture = next_fixture(state)
        if fixture is None or managers_for(state, fixture):
            break
        AL.simulate_fixture(state, fixture)


def play(state, user_id, fixture_no=None):
    if state["phase"] != AL.PHASE_SEASON:
        raise AL.AuctionLeagueError("This season has finished.")
    fixture = AL.fixture_by_no(state, fixture_no) if fixture_no is not None else next_fixture(state, user_id)
    if not fixture or fixture["status"] != "pending":
        raise AL.AuctionLeagueError("This fixture is no longer open. Check the RCPL schedule.")
    if user_id not in managers_for(state, fixture).values():
        raise AL.AuctionLeagueError("Only a manager in this fixture can mark ready.")
    if fixture.get("match_id"):
        raise AL.AuctionLeagueError(f"Match #{fixture['match_id']} has started. Use /continue if it was saved.")
    ready = fixture.setdefault("ready", [])
    if user_id in ready:
        return False
    ready.append(user_id)
    return True


def _winner(fixture, user_id):
    players = fixture.get("match_teams") or {}
    return players.get(str(user_id)) if user_id is not None else None


def bind_match(session, chat_id, tag, match_id):
    """Link the fixture and the Match in the launcher's transaction."""
    from services import rcpl_multiplayer_service as MP

    def bind(room):
        state = room.state
        fixture = AL.fixture_by_no(state, tag["fixture_no"])
        if (state["phase"] != AL.PHASE_SEASON or not fixture or fixture["status"] != "pending"
                or not ready_to_start(state, fixture) or fixture.get("match_id")):
            raise AL.AuctionLeagueError("This RCPL fixture is no longer available to start.")
        fixture["match_id"] = match_id
        fixture["match_teams"] = {
            str(tag["user_id"]): tag["user_team"],
            str(tag["opp_user_id"]): tag["opp_team"],
        }

    MP.change_room(chat_id, tag["room_id"], bind, session=session)


def record_match(session, match_state, tag, winner_user_id):
    from models import AuctionLeagueRoom
    from services import rcpl_multiplayer_service as MP

    row = session.get(AuctionLeagueRoom, tag["room_id"])
    if not row or row.status != AL.PHASE_SEASON:
        return None

    def record(room):
        state = room.state
        fixture = AL.fixture_by_no(state, tag["fixture_no"])
        match_id = match_state.get("match_id")
        if not match_id or not fixture or fixture.get("match_id") != match_id:
            return False
        return AL.record_user_match(state, fixture["no"], match_state,
                                    _winner(fixture, winner_user_id), match_id=match_id)

    return MP.change_room(tag["chat_id"], tag["room_id"], record, session=session).state


def recover_match(session, state):
    """Resolve a forfeit, or make an interrupted fixture available again.

    Scored finishes normally write the result alongside the Match. A saved
    match keeps its link so the captains can continue the same innings.
    """
    changed = False
    for fixture in AL.pending_fixtures(state):
        if fixture.get("match_id"):
            changed |= _recover_fixture(session, state, fixture)
    return changed


def _recover_fixture(session, state, fixture):
    import json
    from models import Match, MatchState, SavedMatch

    match_id = fixture["match_id"]
    match = session.get(Match, match_id)
    if match and match.status in ("active", "playing", "in_progress", "pending"):
        return False
    if session.query(SavedMatch.id).filter_by(match_id=match_id, status="saved").first():
        return False
    if match and match.status == "completed":
        winner = _winner(fixture, match.winner_id)
        if winner and match.margin_type == "forfeit":
            return AL.record_result(
                state, fixture, inn1_team=fixture["home"], inn1_runs=0, inn1_wkts=0, inn1_balls=0,
                inn2_team=fixture["away"], inn2_runs=0, inn2_wkts=0, inn2_balls=0,
                winner=winner, text=f"{winner} won by forfeit", conceded=True)
        saved = session.query(MatchState).filter_by(match_id=match_id).one_or_none()
        if saved and match.end_reason == "completed":
            match_state = json.loads(saved.state_json)
            if match_state.get("inn1_bat_team") and match_state.get("innings") == 2:
                return AL.record_user_match(state, fixture["no"], match_state, winner, match_id=match_id)
    fixture.pop("match_id", None)
    fixture.pop("match_teams", None)
    return True
