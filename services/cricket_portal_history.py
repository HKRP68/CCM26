"""Season archives and comparisons, computed only for public website reads."""
import json

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


def _resolve_name(query, names, role):
    """Accept a unique short name, preferring an exact full-name match."""
    wanted = _norm(query)
    pool = {_norm(name): name for name in names if _norm(name)}
    if wanted in pool:
        return pool[wanted]
    matches = [name for key, name in pool.items() if wanted and wanted in key]
    if len(matches) > 1:
        raise ValueError(f"More than one {role} matches. Use: " + ', '.join(sorted(matches)[:10]))
    if not matches:
        raise ValueError(f"Choose a {role} from this competition.")
    return matches[0]


def _player_names(session, series, games):
    from models import ChallengePlayer, LeagueSeason, TournamentTeam
    from services.cricket_portal_statistics import _pairs, _legacy_rows
    names = [line['name'] for _, line in _pairs(games)]
    names.extend(row['name'] for row in _legacy_rows(session, series, None, games))
    registered = (session.query(ChallengePlayer.name)
        .join(TournamentTeam, TournamentTeam.challenge_team_id == ChallengePlayer.team_id)
        .join(LeagueSeason, LeagueSeason.tournament_id == TournamentTeam.tournament_id)
        .filter(LeagueSeason.series_id == series.id).distinct())
    names.extend(name for name, in registered)
    return names


def _encounters(session, series, season, games):
    """Use a saved season's full encounter archive once, or its scorecards.

    A StatsSeason can retain encounters without individual scorecards. Those
    totals are still evidence, but cannot establish a number of shared matches.
    """
    from services import league_season_service as LS
    selected = [season] if season is not None else LS.seasons(session, series)
    duels, covered_seasons, covered_matches = [], 0, 0
    for item in selected:
        encounters = []
        if LS.season_tournament(session, item) is None:
            saved = LS.season_saved(session, item)
            try:
                encounters = json.loads(getattr(saved, 'player_duels_json', None) or '[]')
            except (ValueError, TypeError):
                encounters = []
        evidence = []
        for game in games:
            if game.get('season_id') != item.id:
                continue
            rows = next((line['player_duels'] for line in game.get('lines') or []
                         if isinstance(line.get('player_duels'), list) and line['player_duels']), [])
            if rows:
                covered_matches += 1
                evidence.extend(rows)
        if not isinstance(encounters, list) or not encounters:
            encounters = evidence
        if encounters:
            covered_seasons += 1
            duels.extend(encounters)
    return duels, covered_matches, covered_seasons, len(selected)


def team_comparison(session, series, season, left, right):
    from services.cricket_portal_competitions import _fixture_context, _teams, _performance
    seasons, fixtures, rows = _fixture_context(session, series, season)
    teams = {_norm(t['name']): t for t in _teams(fixtures, rows)}
    left = _resolve_name(left, (t['name'] for t in teams.values()), 'team')
    right = _resolve_name(right, (t['name'] for t in teams.values()), 'team')
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
    all_games = _games(session, series)
    names = _player_names(session, series, all_games)
    left, right = _resolve_name(left, names, 'player'), _resolve_name(right, names, 'player')
    if _norm(left) == _norm(right):
        raise ValueError('Choose two different players.')
    a, b = player_profile(session, series, season, left), player_profile(session, series, season, right)
    if not a or not b:
        raise ValueError('Choose two recorded or registered players from this competition.')
    games = [g for g in all_games if season is None or g.get('season_id') == season.id]
    shared = []
    duels, coverage, recorded, total = _encounters(session, series, season, games)
    for game in games:
        names = {_norm(line.get('name')) for line in game.get('lines') or []}
        if {_norm(a['name']), _norm(b['name'])}.issubset(names):
            shared.append(game)
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
        duel_matches_covered=coverage, duel_seasons_covered=recorded, duel_seasons_selected=total,
        coverage='Player totals cover the selected seasons. Direct batter–bowler encounters use saved deliveries only; missing historical encounters are not inferred.')
