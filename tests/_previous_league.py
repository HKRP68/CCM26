"""Build last season's league for a test, and point a season at it.

Retention is only ever from a franchise's previous-season squad, so any test
that retains through a command needs one. ``squads`` maps a last-season team
name to the catalogue cards it held.
"""

import itertools

_LEAGUE_NO = itertools.count(1)


def link_squads(session, A, season, squads):
    from models import ChallengeLeague, ChallengePlayer, ChallengeTeam
    mode = A._ensure_default_mode(session)
    league = ChallengeLeague(mode_id=mode.id,
                             name=f"Last season #{next(_LEAGUE_NO)}-{season.id}")
    session.add(league)
    session.flush()
    for team_name, players in squads.items():
        team = ChallengeTeam(league_id=league.id, name=team_name)
        session.add(team)
        session.flush()
        for player in players:
            session.add(ChallengePlayer(team_id=team.id, name=player.name,
                                        source_player_id=player.id))
    session.flush()
    season.previous_league_id = league.id
    A.pin_previous_teams(session, season)
    session.commit()
    return league
