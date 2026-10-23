"""Read-only Challenge League squads, including tournament ownership lookup."""
import json
from html import escape
from types import SimpleNamespace

from services import squad_ratings as ratings
from services.auction_squad_report import grouped


def league_rows(session):
    from models import ChallengeLeague, ChallengeMode
    return (session.query(ChallengeLeague).join(ChallengeMode)
            .filter(ChallengeLeague.is_active.is_(True), ChallengeMode.is_active.is_(True))
            .order_by(ChallengeLeague.sort_order, ChallengeLeague.name, ChallengeLeague.id).all())


def team_rows(session, league_id=None):
    from models import ChallengeTeam, ChallengeLeague, ChallengeMode
    from sqlalchemy.orm import joinedload
    query = (session.query(ChallengeTeam).join(ChallengeLeague).join(ChallengeMode)
             .options(joinedload(ChallengeTeam.league))
             .filter(ChallengeTeam.is_active.is_(True), ChallengeLeague.is_active.is_(True),
                     ChallengeMode.is_active.is_(True)))
    if league_id is not None:
        query = query.filter(ChallengeTeam.league_id == int(league_id))
    return query.order_by(ChallengeTeam.sort_order, ChallengeTeam.name, ChallengeTeam.id).all()


def matches(rows, query, fields, *, partial=True):
    def normal(value):
        return ' '.join(str(value or '').strip().lstrip('/').casefold().split())
    needle = normal(query)
    if not needle:
        return []
    if needle.startswith('#') and needle[1:].isdigit():
        return [row for row in rows if row.id == int(needle[1:])]
    def labels(row):
        return [normal(getattr(row, field, '')) for field in fields]
    exact = [row for row in rows if needle in labels(row)]
    if exact or not partial:
        return exact
    return [row for row in rows if any(needle in value for value in labels(row))]


def owned_teams(session, tg_id):
    from models import TournamentTeam
    from services import tournament_service as TS
    if tg_id is None:
        return []
    tours = tournament_rows(session)
    by_id = {tour.id: tour for tour in tours}
    if not by_id:
        return []
    rows = (session.query(TournamentTeam).filter(TournamentTeam.tournament_id.in_(by_id))
            .order_by(TournamentTeam.tournament_id, TournamentTeam.sort_order, TournamentTeam.name).all())
    owned = [(by_id[team.tournament_id], team) for team in rows if TS.is_team_member(team, tg_id)]
    current = [(tour, team) for tour, team in owned if tour.is_active]
    return current or owned


def tournament_rows(session):
    from models import Tournament
    from services import tournament_service as TS
    return (session.query(Tournament).filter(TS.kind_filter(TS.KIND_CHALLENGE),
            Tournament.status != 'cancelled').order_by(Tournament.id.desc()).all())


def tournament_team(session, tour, entry):
    """Use the roster FK, with a league-scoped name fallback for legacy teams."""
    candidates = team_rows(session, tour.league_id) if tour.league_id else team_rows(session)
    if entry.challenge_team_id:
        return next((team for team in candidates if team.id == entry.challenge_team_id), None)
    if not tour.league_id:
        return None
    found = matches(candidates, entry.name, ('name', 'short_name'), partial=False)
    return found[0] if len(found) == 1 else None


def player_row(player):
    """League snapshots win; missing fields fall back to the linked printed card."""
    try:
        details = json.loads(player.details_json or '{}')
    except (TypeError, ValueError):
        details = {}
    details = details if isinstance(details, dict) else {}
    details = {str(key).casefold(): value for key, value in details.items()}
    source = player.source_player
    def value(keys, attribute):
        for key in keys:
            if details.get(key) not in (None, ''):
                return details[key]
        return getattr(source, attribute, None) if source is not None else None
    return SimpleNamespace(
        id=player.id, name=player.name or 'Player',
        category=str(value(('category', 'role'), 'category') or 'Unknown'),
        rating=ratings.number(value(('rating', 'overall', 'ovr'), 'rating')),
        bat_rating=ratings.number(value(('bat_rating', 'batting_rating', 'bat'), 'bat_rating')),
        bowl_rating=ratings.number(value(('bowl_rating', 'bowling_rating', 'bowl'), 'bowl_rating')))


def squad_view(session, team, *, tour=None, entry=None):
    from models import ChallengePlayer
    from sqlalchemy.orm import joinedload
    from services import rich_message as R
    rows = (session.query(ChallengePlayer).filter_by(team_id=team.id)
            .options(joinedload(ChallengePlayer.source_player))
            .order_by(ChallengePlayer.sort_order, ChallengePlayer.name, ChallengePlayer.id).all())
    players = [player_row(player) for player in rows]
    title = f"👥 {entry.name if entry is not None else team.name} — Squad"
    league = team.league
    scope = f"{league.name}" + (f" · {tour.name}" if tour is not None else '')
    summary = ratings.summary(ratings.averages(players))
    lines = [f'<b>{escape(title)}</b>', escape(scope), f'{len(players)} players', escape(summary)]
    blocks = [R.heading(title, size=2), R.paragraph(scope), R.paragraph(f'{len(players)} players'), R.paragraph(summary)]
    for category, members in grouped(players):
        lines.append(f'\n<b>{escape(category)} ({len(members)})</b>')
        blocks.append(R.heading(f'{category} ({len(members)})', size=3))
        table = [[R.cell(R.bold(label), header=True) for label in ('Player', 'Category', 'Overall', 'Bat', 'Bowl')]]
        for player in members:
            values = [ratings.display(getattr(player, field)) for field in ratings.RATING_FIELDS]
            lines.append(f'{escape(player.name)} · {escape(player.category)} · OVR {values[0]} · BAT {values[1]} · BOWL {values[2]}')
            table.append([R.cell(player.name), R.cell(player.category),
                          *[R.cell(value, align='right') for value in values]])
        blocks.append(R.table(table, bordered=True, striped=True, compact=True))
    if not players:
        lines.append('<i>No players are configured for this team yet.</i>')
        blocks.append(R.paragraph(R.italic('No players are configured for this team yet.')))
    note = 'Squad ratings average the recorded values across the full squad. — means not recorded.'
    lines.append(f'\n<i>{note}</i>')
    blocks.append(R.footer(note))
    return blocks, '\n'.join(lines)
