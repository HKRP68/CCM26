"""Batter/bowler encounters, captured per delivery and archived with a result.

Only main-innings deliveries count. Old scorecards cannot reconstruct who faced
whom, so they deliberately contribute no invented matchup statistics.
"""
import json

COUNTERS = ('runs', 'balls', 'legal_balls', 'dots', 'fours', 'sixes', 'dismissals')


def record_ball(state, batter, bowler, outcome):
    if not state.get('tournament_id'):
        return
    def player(p, side):
        return dict(name=p.get('name', 'Player'), roster_id=p.get('roster_id'),
                    team=state.get(f'{side}_team_name', 'Team'))
    bat, bowl = player(batter, 'bat'), player(bowler, 'bowl')
    rows = state.setdefault('tournament_duels', [])
    row = next((r for r in rows if r['batter'] == bat and r['bowler'] == bowl), None)
    if row is None:
        row = dict(batter=bat, bowler=bowl, **dict.fromkeys(COUNTERS, 0))
        rows.append(row)
    extra = outcome.get('extra_type') if outcome.get('is_extra') or outcome.get('type') == 'extra' else None
    wide, noball = extra == 'Wide', extra == 'No Ball'
    runs = int(outcome.get('runs') or 0)
    bat_runs = 0 if extra and not noball else runs
    row['runs'] += bat_runs
    row['balls'] += int(not wide)
    row['legal_balls'] += int(not (wide or noball))
    row['dots'] += int(not extra and runs == 0)
    row['fours'] += int(bat_runs == 4)
    row['sixes'] += int(bat_runs == 6)
    out = outcome.get('type') == 'wicket' or outcome.get('batter_out')
    how = str(outcome.get('wicket_type') or 'Caught').lower().replace(' ', '').replace('-', '')
    row['dismissals'] += int(bool(out) and how != 'runout')


def archived_duels(session, tournament_id):
    from models import TournamentMatch
    rows = []
    for scorecard, in (session.query(TournamentMatch.scorecard_json)
                       .filter_by(tournament_id=tournament_id, status='completed')):
        try:
            lines = json.loads(scorecard or '[]')
        except (ValueError, TypeError):
            continue
        if isinstance(lines, list) and lines and isinstance(lines[0], dict):
            rows.extend(lines[0].get('player_duels') or [])
    return rows


def season_duels(session, tour):
    """Current + linked seasons, using the same scope as Total Season Stats.

    Return encounters and coverage (seasons with data / existing seasons).
    Explicit links are not followed recursively and each source counts once.
    """
    from models import StatsSeason, Tournament
    from services import season_archive as SA
    rows, recorded, total = [], 0, 0
    refs = dict.fromkeys([SA.tour_ref(tour.id)] + SA.effective_refs(session, tour))
    for ref in refs:
        kind, number = SA._parse_ref(ref)
        source = session.get(Tournament if kind == 't' else StatsSeason, number)
        if source is None:
            continue
        total += 1
        if kind == 't':
            encounters = archived_duels(session, number)
        else:
            try:
                encounters = json.loads(source.player_duels_json or '[]')
            except (ValueError, TypeError):
                encounters = []
        if not isinstance(encounters, list):
            continue
        if encounters:
            recorded += 1
            rows.extend(encounters)
    return rows, recorded, total


def compare(rows, batter_query, bowler_query, *, across_seasons=False):
    """Season records follow a player across teams; ``Name @ Team`` filters.

    Within one tournament, roster identities remain distinct. Different full
    names must still be disambiguated even when a partial query matches both.
    """
    def norm(value):
        return ' '.join(str(value or '').split()).casefold()

    def identity(player):
        if across_seasons:
            return norm(player['name']), norm(player['team'])
        return player['team'], player['roster_id'], player['name']

    def select(role, query):
        name, _, team = query.partition('@')
        name, team = norm(name), norm(team)
        candidates = {}
        for row in rows:
            p = row[role]
            if name in norm(p['name']) and (not team or team == norm(p['team'])):
                # Across seasons the full name is the player identity, as in
                # Total Season Stats. Team/roster changes are not ambiguities.
                key = norm(p['name']) if across_seasons else identity(p)
                candidates.setdefault(key, {})[identity(p)] = p
        exact = {k: group for k, group in candidates.items()
                 if norm(next(iter(group.values()))['name']) == name}
        candidates = exact or candidates
        if len(candidates) > 1:
            options = ', '.join(f"{p['name']} @ {p['team']}"
                                for group in list(candidates.values())[:6]
                                for p in [next(iter(group.values()))])
            raise ValueError(f'More than one {role} matches. Use: {options}')
        return next(iter(candidates.values()), {})

    batters, bowlers = select('batter', batter_query), select('bowler', bowler_query)
    if not batters or not bowlers:
        return None
    matched = [r for r in rows if identity(r['batter']) in batters
               and identity(r['bowler']) in bowlers]
    if not matched:
        return None

    def display(players):
        first = next(iter(players.values()))
        if not across_seasons:
            return first
        teams = dict.fromkeys(p['team'].strip() for p in players.values())
        return dict(name=first['name'].strip(), team=', '.join(teams), roster_id=None)

    return dict(batter=display(batters), bowler=display(bowlers),
                **{k: sum(r.get(k, 0) for r in matched) for k in COUNTERS})
