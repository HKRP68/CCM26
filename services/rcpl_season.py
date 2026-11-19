"""Group season progression using the auction-built squads and RCPL simulator."""

from services import auction_league_service as AL


def _simulate_ai_fixtures(state):
    while state["phase"] == AL.PHASE_SEASON:
        pending = AL.pending_fixtures(state)
        if not pending:
            break
        fixture = pending[0]
        if any(team in state["managers"] for team in (fixture["home"], fixture["away"])):
            break
        AL.simulate_fixture(state, fixture)


def prepare(state):
    if state["phase"] == AL.PHASE_SEASON:
        _simulate_ai_fixtures(state)


def play(state, user_id, fixture_no):
    if state["phase"] != AL.PHASE_SEASON:
        raise AL.AuctionLeagueError("This season has finished.")
    pending = AL.pending_fixtures(state)
    if not pending or pending[0]["no"] != fixture_no:
        raise AL.AuctionLeagueError("This fixture is no longer next. Open the current season board.")
    fixture = pending[0]
    managers = state["managers"]
    eligible = {managers[team]["id"] for team in (fixture["home"], fixture["away"]) if team in managers}
    if user_id not in eligible:
        raise AL.AuctionLeagueError("Only a manager in this fixture can mark ready.")
    ready = fixture.setdefault("ready", [])
    if user_id in ready:
        return False
    ready.append(user_id)
    if eligible.issubset(ready):
        AL.simulate_fixture(state, fixture)
        _simulate_ai_fixtures(state)
    return True
