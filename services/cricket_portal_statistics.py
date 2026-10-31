"""Public Statsguru queries over verified scorecards, never on the scoring path.

Builders return only JSON-safe public cricket facts. The HTTP layer supplies its
bounded response cache; no score-delivery hook or background rebuild is needed.
Missing historical fields and ambiguous catalogue identities remain unknown.
"""
from collections import defaultdict


def _norm(value):
    return ' '.join(str(value or '').split()).casefold()


def _int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return None


def _rate(numerator, denominator, factor=1):
    if numerator is None or not denominator:
        return None
    return round(factor * numerator / denominator, 2)


def _match_key(game):
    return (game.get('season_id'), game.get('fixture_id') or game.get('match_id'))


def _games(session, series, season=None):
    """Read linked seasons, enriching pitch/format only from recorded sources."""
    from models import Match, PitchMatchStat, TournamentMatch
    from services import league_records as R, league_season_service as LS
    selected = [season] if season is not None else LS.seasons(session, series)
    tournaments = {item.id: item.tournament_id for item in selected}
    games = [dict(game) for item in selected for game in R.evidence(session, item)]
    fixture_ids = [g['fixture_id'] for g in games if g.get('fixture_id')]
    match_ids = [g['match_id'] for g in games if g.get('match_id')]
    fixtures = {m.id: m for m in session.query(TournamentMatch).filter(TournamentMatch.id.in_(fixture_ids))} if fixture_ids else {}
    matches = {m.id: m for m in session.query(Match).filter(Match.id.in_(match_ids))} if match_ids else {}
    pitches = {m.match_id: m for m in session.query(PitchMatchStat).filter(PitchMatchStat.match_id.in_(match_ids))} if match_ids else {}
    for game in games:
        fx = fixtures.get(game.get('fixture_id'))
        match = matches.get(game.get('match_id'))
        pitch = pitches.get(game.get('match_id'))
        tournament_id = tournaments.get(game.get('season_id'))
        if tournament_id is None:
            fx = match = pitch = None  # deleted fixture IDs can be reused by SQLite
        elif fx is not None and fx.tournament_id != tournament_id:
            fx = None
        if match is not None and match.tournament_id != tournament_id:
            match = pitch = None
        if match is None:
            pitch = None  # the pitch record belongs to its matched core Match
        detail = next((l.get('record_detail') for l in game.get('lines', []) if isinstance(l.get('record_detail'), dict)), {})
        # Stored archive keys are kept if available; a later venue default must
        # never be substituted for the actual surface of an old match.
        game['pitch'] = game.get('pitch') or game.get('pitch_type') or (getattr(pitch, 'pitch_type', None) if pitch else None) or (getattr(match, 'pitch_type', None) if match else None) or (getattr(fx, 'pitch_type', None) if fx else None)
        game['ball_format'] = game.get('ball_format') or detail.get('ball_format') or (getattr(pitch, 'ball_format', None) if pitch else None)
        game['venue'] = game.get('venue') or (getattr(match, 'stadium', None) if match else None)
        for key in ('inn1_wickets', 'inn2_wickets', 'inn1_balls', 'inn2_balls'):
            game[key] = game.get(key) if game.get(key) is not None else getattr(fx, key, None)
        game['url'] = f"/cricket?match_id={game['match_id']}" if game.get('match_id') else None
    games.sort(key=R.game_order)
    return games


def _opponent(game, line):
    team = _norm(line.get('team_name'))
    if team and team == _norm(game.get('team1')):
        return game.get('team2')
    if team and team == _norm(game.get('team2')):
        return game.get('team1')
    return None


def _played(line, kind):
    return bool(line.get('batted' if kind == 'bat' else 'bowled')) or bool(_int(line.get('bat_balls' if kind == 'bat' else 'bowl_balls')))


def _pairs(games):
    return [(g, line) for g in games for line in g.get('lines', []) if _norm(line.get('name'))]


def _legacy_rows(session, series, season, games):
    """Keep aggregate-only seasons without inventing scorecard provenance."""
    from services import league_season_service as LS
    selected = [season] if season is not None else LS.seasons(session, series)
    evidence_seasons = {game.get('season_id') for game in games}
    result = []
    for item in selected:
        if item.id in evidence_seasons:
            continue
        for row in LS._grouped(LS.season_rows(session, item)).values():
            result.append(dict(season_id=item.id, season=LS.season_title(item), season_no=item.season_no,
                name=row.name, team_name=row.team_name,
                **{key: getattr(row, key, None) for key in ('matches', 'bat_innings', 'bat_runs', 'bat_balls',
                    'bat_outs', 'bat_fours', 'bat_sixes', 'bowl_innings', 'bowl_wickets', 'bowl_runs',
                    'bowl_balls', 'highest_score', 'best_bowl_wickets', 'best_bowl_runs')}))
    return result


def _aggregate(pairs, legacy=()):
    stats = dict(matches=len({_match_key(g) for g, _ in pairs}), innings=0, bowling_innings=0,
                 runs=0, balls=0, outs=0, fours=0, sixes=0, wickets=0, bowled=0, conceded=0)
    unknown = set()
    high, best = None, None
    for game, line in pairs:
        if _played(line, 'bat'):
            stats['innings'] += 1
            for key, raw in [('runs', 'bat_runs'), ('balls', 'bat_balls'), ('fours', 'bat_fours'), ('sixes', 'bat_sixes')]:
                value = _int(line.get(raw))
                if value is None:
                    unknown.add(key)
                else:
                    stats[key] += value
            if line.get('bat_out') is None:
                unknown.add('outs')
            else:
                stats['outs'] += bool(line['bat_out'])
            value = _int(line.get('bat_runs'))
            if value is not None:
                high = max(high if high is not None else value, value)
        if _played(line, 'bowl'):
            stats['bowling_innings'] += 1
            for key, raw in [('wickets', 'bowl_wickets'), ('bowled', 'bowl_balls'), ('conceded', 'bowl_runs')]:
                value = _int(line.get(raw))
                if value is None:
                    unknown.add(key)
                else:
                    stats[key] += value
            wkts, runs = _int(line.get('bowl_wickets')), _int(line.get('bowl_runs'))
            if wkts is not None and runs is not None:
                candidate = (wkts, -runs)
                best = max(best if best is not None else candidate, candidate)
    for row in legacy:
        for key, raw in [('matches', 'matches'), ('runs', 'bat_runs'), ('balls', 'bat_balls'),
                         ('outs', 'bat_outs'), ('fours', 'bat_fours'), ('sixes', 'bat_sixes'),
                         ('wickets', 'bowl_wickets'), ('bowled', 'bowl_balls'), ('conceded', 'bowl_runs')]:
            value = _int(row.get(raw))
            if value is None:
                unknown.add(key)
            else:
                stats[key] += value
        for key, raw, activity in [('innings', 'bat_innings', ('bat_runs', 'bat_balls', 'bat_outs')),
                                   ('bowling_innings', 'bowl_innings', ('bowl_wickets', 'bowl_runs', 'bowl_balls'))]:
            value = _int(row.get(raw))
            # Old imported aggregate rows may retain zero schema defaults for
            # innings despite positive totals. Do not invent an innings count.
            if value is None or (value == 0 and any((_int(row.get(a)) or 0) > 0 for a in activity)):
                unknown.add(key)
            else:
                stats[key] += value
        score = _int(row.get('highest_score'))
        if score:
            high = max(high if high is not None else score, score)
        wkts = _int(row.get('best_bowl_wickets'))
        raw_conceded = row.get('best_bowl_runs')
        conceded = _int(raw_conceded) if raw_conceded is not None and raw_conceded >= 0 else None
        if wkts and conceded is not None:
            candidate = (wkts, -conceded)
            best = max(best if best is not None else candidate, candidate)
    for key in unknown:
        stats[key] = None
    stats.update(average=_rate(stats['runs'], stats['outs']), sr=_rate(stats['runs'], stats['balls'], 100),
                 economy=_rate(stats['conceded'], stats['bowled'], 6), bowling_average=_rate(stats['conceded'], stats['wickets']),
                 bowling_strike_rate=_rate(stats['bowled'], stats['wickets']), highest_score=None if 'runs' in unknown else high,
                 best_bowling=f'{best[0]}/{-best[1]}' if best is not None and not ({'wickets', 'conceded'} & unknown) else None,
                 unknown_fields=sorted(unknown))
    return stats


def _identities(session, pairs):
    """Resolve a version only when IDs/name evidence select a unique card."""
    from models import Player
    from services.league_record_insights import safe_image_url
    by_name = defaultdict(list)
    ids = defaultdict(set)
    for _, line in pairs:
        pid = _int(line.get('player_id'))
        if pid:
            ids[_norm(line['name'])].add(pid)
    names = {_norm(line['name']) for _, line in pairs}
    # One catalogue read, rather than a query for every player in a leaderboard.
    if not names:
        return {}
    from sqlalchemy import func
    for player in session.query(Player).filter(func.lower(func.trim(Player.name)).in_(names)):
        by_name[_norm(player.name)].append(player)
    result = {}
    for name in names:
        candidates = by_name.get(name, [])
        referenced = ids.get(name, set())
        if referenced:
            matching = [p for p in candidates if p.id in referenced]
            # An ID absent from the current catalogue must not be replaced by a
            # same-name card that happens to remain in the database.
            candidates = matching if len(matching) == len(referenced) else []
        unique = candidates[0] if len(candidates) == 1 else None
        result[name] = dict(ovr=unique.rating if unique else None,
            bat=unique.bat_rating if unique else None, bowl=unique.bowl_rating if unique else None,
            role=unique.category if unique else None, country=unique.country if unique else None,
            version=unique.version if unique else None, image=safe_image_url(unique.image_url) if unique else None,
            ambiguous=len(candidates) > 1 or len(referenced) > 1,
            note='Same-name player versions are combined; ratings are not inferred.' if len(candidates) > 1 or len(referenced) > 1 else None)
    return result


def _rows(session, pairs, discipline='batting', legacy=()):
    grouped, grouped_legacy = defaultdict(list), defaultdict(list)
    for game, line in pairs:
        grouped[_norm(line['name'])].append((game, line))
    for row in legacy:
        grouped_legacy[_norm(row['name'])].append(row)
    identities = _identities(session, pairs + [(None, row) for row in legacy])
    rows = []
    for name in set(grouped) | set(grouped_legacy):
        entries, aggregates = grouped[name], grouped_legacy[name]
        identity = identities[name]
        dated_rows = [(g.get('season_no') or 0, g.get('date') or '', line) for g, line in entries]
        dated_rows.extend((row.get('season_no') or 0, '', row) for row in aggregates)
        named_rows = [row for _, _, row in sorted(dated_rows, key=lambda item: (item[0], item[1]))]
        teams = list(dict.fromkeys(line.get('team_name') for line in named_rows if line.get('team_name')))
        stats = _aggregate(entries, aggregates)
        rows.append(dict(stats, name=named_rows[-1]['name'], team=teams[-1] if teams else None,
                         teams=teams, ovr=identity['ovr'], bat=identity['bat'], bowl=identity['bowl'],
                         ambiguous_identity=identity['ambiguous'], aggregate_only_seasons=len(aggregates)))
    metric = 'wickets' if discipline == 'bowling' else 'runs'
    rows.sort(key=lambda p: (p[metric] is None, -(p[metric] or 0), _norm(p['name'])))
    return rows


def _coverage(games, legacy=()):
    return dict(matches=len(games), phase_matches=sum(bool((g.get('analytics') or {}).get('phases')) for g in games),
                unknown_pitch_matches=sum(not g.get('pitch') for g in games),
                aggregate_only_seasons=len({row['season_id'] for row in legacy}),
                aggregate_only_player_rows=len(legacy),
                note=('Historical season aggregates are included in totals; match-level, opponent, pitch, recent and phase evidence is unavailable for those seasons. '
                      if legacy else '') + 'Phase figures use recorded deliveries only; missing historical values are shown as unavailable.')


def _phases(games, name=None, team=None):
    totals = {}
    for game in games:
        analytics = game.get('analytics') or {}
        if analytics.get('invalid_reason'):
            continue
        for row in analytics.get('phases') or []:
            if name and _norm(row.get('name')) != _norm(name):
                continue
            if team and _norm(row.get('team')) != _norm(team):
                continue
            key = (row.get('side'), row.get('phase'))
            total = totals.setdefault(key, dict(side=key[0], phase=key[1], runs=0, balls=0, wickets=0))
            for metric in ('runs', 'balls', 'wickets'):
                total[metric] += _int(row.get(metric)) or 0
    rows = list(totals.values())
    for row in rows:
        row['sr'] = _rate(row['runs'], row['balls'], 100) if row['side'] == 'bat' else None
        row['economy'] = _rate(row['runs'], row['balls'], 6) if row['side'] == 'bowl' else None
    return sorted(rows, key=lambda r: (r['side'] or '', ('powerplay', 'middle', 'death').index(r['phase']) if r['phase'] in ('powerplay', 'middle', 'death') else 4))


def _integer_filter(value, default, maximum):
    try:
        return min(maximum, max(1, int(value)))
    except (ValueError, TypeError):
        return default


def explorer(session, series, season=None, filters=None):
    """Filtered, bounded player aggregates. All filters combine with AND."""
    filters = filters or {}
    games = _games(session, series, season)
    pairs = _pairs(games)
    legacy = _legacy_rows(session, series, season, games)
    options = dict(pitches=sorted({g['pitch'] for g in games if g.get('pitch')}),
        venues=sorted({g['venue'] for g in games if g.get('venue')}),
        teams=sorted({l['team_name'] for l in [line for _, line in pairs] + legacy if l.get('team_name')}),
        opponents=sorted({_opponent(g, l) for g, l in pairs if _opponent(g, l)}),
        stages=sorted({g['stage'] for g in games if g.get('stage')}))
    selected = []
    for game, line in pairs:
        if filters.get('player') and _norm(filters['player']) not in _norm(line['name']):
            continue
        if filters.get('team') and _norm(filters['team']) != _norm(line.get('team_name')):
            continue
        if filters.get('opponent') and _norm(filters['opponent']) != _norm(_opponent(game, line)):
            continue
        if any(filters.get(key) and _norm(filters[key]) != _norm(game.get(key)) for key in ('pitch', 'venue', 'stage')):
            continue
        selected.append((game, line))
    selected_legacy = []
    if not any(filters.get(key) for key in ('opponent', 'pitch', 'venue', 'stage')):
        selected_legacy = [row for row in legacy
            if (not filters.get('player') or _norm(filters['player']) in _norm(row['name']))
            and (not filters.get('team') or _norm(filters['team']) == _norm(row.get('team_name')))]
    rows = _rows(session, selected, filters.get('discipline'), selected_legacy)
    page = _integer_filter(filters.get('page'), 1, 100000)
    size = _integer_filter(filters.get('limit'), 25, 100)
    selected_games = list({_match_key(g): g for g, _ in selected}.values())
    totals = _aggregate(selected, selected_legacy)
    return dict(rows=rows[(page-1)*size:page*size], filters=options, total=len(rows), page=page, page_size=size,
                summary=dict(matches=None if selected_legacy else len(selected_games),
                    scorecard_matches=len(selected_games), players=len(rows), runs=totals['runs'], wickets=totals['wickets']),
                coverage=_coverage(selected_games, selected_legacy))


def _splits(pairs, key):
    buckets = defaultdict(list)
    for game, line in pairs:
        value = _opponent(game, line) if key == 'opponent' else game.get(key)
        if value:
            buckets[value].append((game, line))
    return [dict(name=name, stats=_aggregate(entries)) for name, entries in sorted(buckets.items())]


def _roster_profile(session, series, season, name):
    """A squad member can have a public profile before a first appearance."""
    import json
    from models import ChallengePlayer, LeagueSeason, TournamentTeam
    from sqlalchemy.orm import joinedload
    query = (session.query(ChallengePlayer, TournamentTeam.name)
             .join(TournamentTeam, TournamentTeam.challenge_team_id == ChallengePlayer.team_id)
             .join(LeagueSeason, LeagueSeason.tournament_id == TournamentTeam.tournament_id)
             .options(joinedload(ChallengePlayer.source_player))
             .filter(LeagueSeason.series_id == series.id))
    if season is not None:
        query = query.filter(LeagueSeason.id == season.id)
    matches = [(player, team) for player, team in query if _norm(player.name) == _norm(name)]
    if not matches:
        return None
    identities = {}
    for player, team in matches:
        try:
            details = json.loads(player.details_json or '{}')
            details = {_norm(k): v for k, v in details.items()} if isinstance(details, dict) else {}
        except (ValueError, TypeError):
            details = {}
        master = player.source_player
        def value(attr, *aliases):
            for key in aliases:
                if details.get(key) is not None:
                    return details[key]
            return getattr(master, attr, None) if master is not None else None
        from services.league_record_insights import safe_image_url
        identity = dict(ovr=_int(value('rating', 'rating', 'overall', 'ovr')),
            bat=_int(value('bat_rating', 'bat_rating', 'batting_rating', 'bat', 'batting')),
            bowl=_int(value('bowl_rating', 'bowl_rating', 'bowling_rating', 'bowl', 'bowling')),
            role=value('category', 'category', 'role'), country=value('country', 'country'),
            version=getattr(master, 'version', None), image=safe_image_url(getattr(master, 'image_url', None)))
        signature = (player.source_player_id, tuple(str(identity[k]) for k in sorted(identity)))
        identities[signature] = identity
    if len(identities) == 1:
        identity = next(iter(identities.values()))
        identity.update(ambiguous=False, note='Current competition roster; no recorded appearances yet.')
    else:
        identity = dict(ovr=None, bat=None, bowl=None, role=None, country=None, version=None, image=None,
                        ambiguous=True, note='Multiple same-name roster versions; ratings are not inferred.')
    stats = _aggregate([])
    return dict(name=matches[0][0].name, identity=identity, selected=dict(stats), career=dict(stats),
        roster_only=True, teams=list(dict.fromkeys(team for _, team in matches)),
        seasons=[], recent=[], form=dict(matches=0, recent_runs=0, recent_wickets=0), records=[],
        splits=dict(opponent=[], pitch=[], venue=[]), phases=[], coverage=_coverage([]))


def player_profile(session, series, season, name):
    wanted = _norm(name)
    all_games = _games(session, series)
    career_pairs = [(g, l) for g, l in _pairs(all_games) if _norm(l['name']) == wanted]
    legacy = [row for row in _legacy_rows(session, series, None, all_games) if _norm(row['name']) == wanted]
    if not career_pairs and not legacy:
        return _roster_profile(session, series, season, name)
    selected_pairs = [(g, l) for g, l in career_pairs if season is None or g.get('season_id') == season.id]
    selected_legacy = [row for row in legacy if season is None or row['season_id'] == season.id]
    identity = _identities(session, career_pairs + [(None, row) for row in legacy])[wanted]
    breakdown = {}
    for game, line in career_pairs:
        bucket = breakdown.setdefault(game.get('season_id'), dict(id=game.get('season_id'), name=game.get('season'), pairs=[], legacy=[]))
        bucket['pairs'].append((game, line))
    for row in legacy:
        bucket = breakdown.setdefault(row['season_id'], dict(id=row['season_id'], name=row['season'], pairs=[], legacy=[]))
        bucket['legacy'].append(row)
    recent = []
    for game, line in sorted(career_pairs, key=lambda p: (p[0].get('season_no') or 0, p[0].get('date') or '', p[0].get('fixture_id') or 0), reverse=True)[:10]:
        recent.append(dict(date=game.get('date'), opponent=_opponent(game, line), team=line.get('team_name'),
            venue=game.get('venue'), pitch=game.get('pitch'), batted=_played(line, 'bat'), bowled=_played(line, 'bowl'), runs=_int(line.get('bat_runs')) if _played(line, 'bat') else None,
            balls=_int(line.get('bat_balls')) if _played(line, 'bat') else None, wickets=_int(line.get('bowl_wickets')) if _played(line, 'bowl') else None,
            conceded=_int(line.get('bowl_runs')) if _played(line, 'bowl') else None, match_id=game.get('match_id'), url=game.get('url')))
    career = _aggregate(career_pairs, legacy)
    records = [dict(label='Highest score', value=career['highest_score'], unit='runs'),
               dict(label='Best bowling', value=career['best_bowling'], unit='figures')]
    matched_games = list({_match_key(g): g for g, _ in selected_pairs}.values())
    form = recent[:5]
    display = career_pairs[-1][1]['name'] if career_pairs else legacy[-1]['name']
    coverage = _coverage(matched_games, selected_legacy)
    coverage['career_aggregate_only_seasons'] = len({row['season_id'] for row in legacy})
    return dict(name=display, identity=identity, selected=_aggregate(selected_pairs, selected_legacy), career=career,
        seasons=[dict(id=b['id'], name=b['name'], stats=_aggregate(b['pairs'], b['legacy']), aggregate_only=bool(b['legacy'])) for b in breakdown.values()],
        recent=recent, form=dict(matches=len(form) if form or not legacy else None,
            recent_runs=None if (not form and legacy) or any(r['batted'] and r['runs'] is None for r in form) else sum(r['runs'] or 0 for r in form),
            recent_wickets=None if (not form and legacy) or any(r['bowled'] and r['wickets'] is None for r in form) else sum(r['wickets'] or 0 for r in form)),
        records=records, splits={key: _splits(selected_pairs, key) for key in ('opponent', 'pitch', 'venue')},
        phases=_phases(matched_games, name=name), coverage=coverage)


def venue_profile(session, series, season, name):
    games = [g for g in _games(session, series, season) if _norm(g.get('venue')) == _norm(name)]
    if not games:
        return None
    pairs = _pairs(games)
    teams, pitches = {}, {}
    innings = []
    for game in games:
        for team, runs in ((game.get('team1'), game.get('runs1')), (game.get('team2'), game.get('runs2'))):
            if not team:
                continue
            row = teams.setdefault(team, dict(name=team, matches=0, runs=0, wins=0))
            row['matches'] += 1
            if runs is not None:
                row['runs'] += _int(runs) or 0
                innings.append(_int(runs) or 0)
            else:
                row['unknown_scores'] = row.get('unknown_scores', 0) + 1
            row['wins'] += bool(game.get('winner') and _norm(game['winner']) == _norm(team))
        if game.get('pitch'):
            row = pitches.setdefault(game['pitch'], dict(name=game['pitch'], matches=0, runs=0))
            row['matches'] += 1
            if game.get('runs1') is not None and game.get('runs2') is not None:
                row['runs'] += (_int(game['runs1']) or 0) + (_int(game['runs2']) or 0)
            else:
                row['unknown_scores'] = row.get('unknown_scores', 0) + 1
    for row in list(teams.values()) + list(pitches.values()):
        if row.get('unknown_scores'):
            row['known_runs'] = row['runs']
            row['runs'] = None
    firsts = [_int(g.get('runs1')) for g in games if _int(g.get('runs1')) is not None]
    return dict(name=games[-1]['venue'], summary=dict(matches=len(games), innings=len(innings), runs=sum(innings) if len(innings) == len(games)*2 else None,
        known_runs=sum(innings), unknown_score_innings=len(games)*2-len(innings), first_innings_sample=len(firsts),
        average_first_innings=round(sum(firsts)/len(firsts), 2) if firsts else None,
        highest_total=max(innings) if innings else None,
        successful_chases=sum(bool(g.get('winner') and _norm(g['winner']) == _norm(g.get('team2')) and g.get('runs1') is not None and g.get('runs2') is not None and g['runs2'] > g['runs1']) for g in games)),
        teams=list(teams.values()), players=_rows(session, pairs)[:25], pitches=list(pitches.values()),
        phases=_phases(games), coverage=_coverage(games))
