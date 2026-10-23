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


def compare(rows, batter_query, bowler_query):
    """Resolve unique player identities. ``Name @ Team`` disambiguates names."""
    def select(role, query):
        name, _, team = query.partition(' @ ')
        name, team = name.strip().casefold(), team.strip().casefold()
        candidates = {}
        for row in rows:
            p = row[role]
            if name in p['name'].casefold() and (not team or team == p['team'].casefold()):
                key = (p['team'], p['roster_id'], p['name'])
                candidates[key] = p
        exact = {k: p for k, p in candidates.items() if p['name'].casefold() == name}
        candidates = exact or candidates
        if len(candidates) > 1:
            options = ', '.join(f"{p['name']} @ {p['team']}" for p in list(candidates.values())[:6])
            raise ValueError(f'More than one {role} matches. Use: {options}')
        return next(iter(candidates.values()), None)
    batter, bowler = select('batter', batter_query), select('bowler', bowler_query)
    if not batter or not bowler:
        return None
    matched = [r for r in rows if r['batter'] == batter and r['bowler'] == bowler]
    if not matched:
        return None
    return dict(batter=batter, bowler=bowler,
                **{k: sum(r.get(k, 0) for r in matched) for k in COUNTERS})
