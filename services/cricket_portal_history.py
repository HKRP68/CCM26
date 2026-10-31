"""Season archives and comparisons, computed only for public website reads."""
from collections import defaultdict

from services.cricket_portal_statistics import _norm


def seasons_snapshot(session, series):
    from services import league_season_service as LS, league_records as R
    from services.cricket_portal_statistics import _games, _pairs, _legacy_rows, _rows
    from services.cricket_portal_competitions import _fixture_context, _caps
    seasons, fixtures, _ = _fixture_context(session, series, None)
    games = _games(session, series)
    pairs = _pairs(games)
    legacy = _legacy_rows(session, series, None, games)
    result = []
    for item in seasons:
        selected_pairs = [(g, l) for g, l in pairs if g.get('season_id') == item.id]
        players = _rows(session, selected_pairs, legacy=[r for r in legacy if r['season_id'] == item.id])
        rows = [f for f in fixtures if f['season_id'] == item.id]
        completed = [f for f in rows if f['status'] == 'completed']
        stage = LS.season_stage(session, item)
        honours = LS.honours(item)
        finals = [f for f in completed if f.get('stage') == 'final' and f.get('winner')]
        champion = (honours.get('champion') or {}).get('team') or (finals[-1]['winner'] if finals else None)
        tables = R.standings_view(session, item)
        result.append(dict(id=item.id, number=item.season_no, name=LS.season_title(item),
            status='Archived records' if not item.tournament_id and (completed or any(r['season_id'] == item.id for r in legacy)) else stage['label'], archived=bool(item.stats_season_id or not item.tournament_id),
            champion=champion, honours=honours, standings=tables,
            matches=None if any(r['season_id'] == item.id for r in legacy) and not completed else len(completed), fixtures=len(rows), players=len(players),
            orange_cap=_caps(players, 'runs'), purple_cap=_caps(players, 'wickets'),
            aggregate_only=any(r['season_id'] == item.id for r in legacy),
            url=f'/stats/{series.id}?season={item.id}', records_url=f'/stats/{series.id}?season={item.id}#records'))
    return dict(seasons=list(reversed(result)), total=len(result),
                all_time_url=f'/stats/{series.id}?season=all')


def team_comparison(session, series, season, left, right):
    from services.cricket_portal_competitions import _fixture_context, _teams, _performance
    seasons, fixtures, rows = _fixture_context(session, series, season)
    teams = {_norm(t['name']): t for t in _teams(fixtures, rows)}
    a, b = teams.get(_norm(left)), teams.get(_norm(right))
    if not a or not b:
        raise ValueError('Choose two teams from this competition.')
    if _norm(a['name']) == _norm(b['name']):
        raise ValueError('Choose two different teams.')
    shared = [f for f in fixtures if f['status'] == 'completed' and
              {_norm(f['team1']), _norm(f['team2'])} == {_norm(a['name']), _norm(b['name'])}]
    season_order = {item.id: item.season_no for item in seasons}
    shared.sort(key=lambda f: (season_order.get(f.get('season_id'), 0), f.get('date') or '', f.get('id') or 0), reverse=True)
    a, b = dict(a), dict(b)
    a['stats'] = _performance(shared, a['name'])
    b['stats'] = _performance(shared, b['name'])
    breakdown = []
    for item in reversed(seasons):
        games = [f for f in shared if f['season_id'] == item.id]
        if games:
            breakdown.append(dict(id=item.id, name=next(f['season'] for f in games), matches=len(games),
                left=_performance(games, a['name']), right=_performance(games, b['name'])))
    return dict(kind='team', left=a, right=b, matches=len(shared),
        tied=a['stats']['tied'], no_result=a['stats']['no_result'],
        unknown_result=a['stats'].get('unknown_result', 0),
        seasons=breakdown, recent=shared[:20], simulated=sum(bool(f.get('simulated')) for f in shared),
        coverage='Only recorded head-to-head fixtures count. Older aggregate-only seasons cannot establish a meeting.')


def player_comparison(session, series, season, left, right):
    from services.cricket_portal_statistics import player_profile, _games, _pairs, _aggregate
    if _norm(left) == _norm(right):
        raise ValueError('Choose two different players.')
    a, b = player_profile(session, series, season, left), player_profile(session, series, season, right)
    if not a or not b:
        raise ValueError('Choose two recorded or registered players from this competition.')
    games = _games(session, series, season)
    shared = []
    duels = []
    coverage = 0
    for game in games:
        names = {_norm(line.get('name')) for line in game.get('lines') or []}
        if {_norm(a['name']), _norm(b['name'])}.issubset(names):
            shared.append(game)
        encounters = next((line.get('player_duels') for line in game.get('lines') or []
                           if isinstance(line.get('player_duels'), list)), [])
        if encounters:
            coverage += 1
            duels.extend(encounters)
    def encounter(batter, bowler):
        counters = ('runs', 'balls', 'legal_balls', 'dots', 'fours', 'sixes', 'dismissals')
        matched = [row for row in duels if isinstance(row, dict) and
                   _norm((row.get('batter') or {}).get('name')) == _norm(batter) and
                   _norm((row.get('bowler') or {}).get('name')) == _norm(bowler)]
        if not matched:
            return None
        total = {key: sum(max(0, int(row.get(key) or 0)) for row in matched) for key in counters}
        total.update(batter=batter, bowler=bowler,
                     sr=round(100*total['runs']/total['balls'], 2) if total['balls'] else None)
        return total
    return dict(kind='player', left=a, right=b, shared_matches=len(shared),
        shared_left=_aggregate([(g, l) for g, l in _pairs(shared) if _norm(l['name']) == _norm(a['name'])]),
        shared_right=_aggregate([(g, l) for g, l in _pairs(shared) if _norm(l['name']) == _norm(b['name'])]),
        duels=[row for row in (encounter(a['name'], b['name']), encounter(b['name'], a['name'])) if row],
        duel_matches_covered=coverage,
        coverage='Player totals cover the selected seasons. Direct batter–bowler encounters use saved deliveries only; missing historical encounters are not inferred.')
