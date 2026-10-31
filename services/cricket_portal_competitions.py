"""Read-only competition, fixture and team views for the cricket portal.

Builds are called by the website's separate response cache. Nothing is invoked
by a ball simulation, result delivery, or transaction completion. Only explicit
public cricket fields are serialized; match setup/admin settings are excluded.
"""
import json
from collections import defaultdict


def _loads(raw, fallback):
    try:
        value = json.loads(raw) if raw else fallback
        return value if isinstance(value, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _key(name):
    return ' '.join(str(name or '').lower().split())


def _int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else str(value) if value else None


def _selected(session, series, season):
    from services import league_season_service as LS
    return [season] if season is not None else LS.seasons(session, series)


def _fixture_context(session, series, season):
    """Current schedule plus preserved archive evidence, without private state."""
    from models import Match, TournamentMatch, TournamentTeam
    from services import league_records as R, league_season_service as LS
    fixtures, team_rows = [], []
    seasons = _selected(session, series, season)
    for selected in seasons:
        title = LS.season_title(selected)
        tour = LS.season_tournament(session, selected)
        if tour is None:
            # Archive evidence already contains the public team snapshots.
            games = _loads(R.config(session, selected).archive_json, [])
            saved = LS.season_saved(session, selected)
            manifest = _loads(getattr(saved, 'franchise_results_json', None), {})
            # Saved fixture history also retains no-results and explicitly
            # simulated games; player scorecard archives intentionally don't.
            if isinstance(manifest.get('matches'), list):
                recorded = {g.get('fixture_id'): g for g in games if isinstance(g, dict)}
                complete = []
                for row in manifest['matches']:
                    if not isinstance(row, dict) or not row.get('team1') or not row.get('team2'):
                        continue
                    original = recorded.get(row.get('fixture_id'), {})
                    first = row.get('score1') if isinstance(row.get('score1'), dict) else {}
                    second = row.get('score2') if isinstance(row.get('score2'), dict) else {}
                    kind = row.get('result')
                    complete.append(dict(original, fixture_id=row.get('fixture_id'),
                        team1=row['team1'], team2=row['team2'], winner=row.get('winner'),
                        runs1=first.get('runs'), runs2=second.get('runs'),
                        wickets1=first.get('wickets'), wickets2=second.get('wickets'),
                        stage=row.get('stage'), date=row.get('when'), result_type=kind,
                        simulated=bool(row.get('simulated')),
                        result=original.get('result') or ('No result' if kind == 'no_result' else
                            'Match tied' if kind == 'tie' else f"{row['winner']} won" if row.get('winner') else None)))
                games = complete

            for game in games:
                if not isinstance(game, dict):
                    continue
                fixtures.append(dict(id=game.get('fixture_id'), match_id=game.get('match_id'),
                    stage=game.get('stage') or 'league', status='completed', date=game.get('date'),
                    team1=game.get('team1'), team2=game.get('team2'), runs1=_int(game.get('runs1')),
                    runs2=_int(game.get('runs2')), wickets1=game.get('wickets1'), wickets2=game.get('wickets2'),
                    winner=game.get('winner'), result=game.get('result'), venue=game.get('venue'),
                    pitch=game.get('pitch'), url=_match_url(game.get('match_id')), season=title,
                    season_id=selected.id, round_no=None, match_no=game.get('match_no'), simulated=bool(game.get('simulated')),
                    result_type=game.get('result_type') or ('win' if game.get('winner') else 'tie' if game.get('runs1') is not None and game.get('runs1') == game.get('runs2') else None)))
            continue
        rows = session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()
        teams = {row.id: row for row in rows}
        actual_pitches = dict(session.query(Match.id, Match.pitch_type).filter(
            Match.tournament_id == tour.id))
        team_rows.extend((selected, tour, row) for row in rows)
        matches = session.query(TournamentMatch).filter_by(tournament_id=tour.id).order_by(
            TournamentMatch.round_no, TournamentMatch.match_no, TournamentMatch.id)
        for match in matches:
            first, second, winner = teams.get(match.team1_id), teams.get(match.team2_id), teams.get(match.winner_team_id)
            fixtures.append(dict(id=match.id, match_id=match.match_id, stage=match.stage,
                status=match.status, date=_iso(match.completed_at),
                team1=first.name if first else match.slot1_label or 'To be decided',
                team2=second.name if second else match.slot2_label or 'To be decided',
                team1_known=first is not None, team2_known=second is not None,
                runs1=match.inn1_runs, runs2=match.inn2_runs, wickets1=match.inn1_wickets,
                wickets2=match.inn2_wickets, winner=winner.name if winner else None,
                result=match.result_text, venue=match.venue,
                pitch=(actual_pitches.get(match.match_id) or match.pitch_type) if match.status == 'completed' else match.pitch_type,
                url=_match_url(match.match_id), season=title, season_id=selected.id,
                round_no=match.round_no, match_no=match.match_no, simulated=bool(match.is_simulated),
                result_type=match.result_type or ('win' if winner else 'tie' if match.inn1_runs is not None and match.inn1_runs == match.inn2_runs else None)))
    return seasons, fixtures, team_rows


def _match_url(match_id):
    value = _int(match_id)
    return f'/cricket?match_id={value}' if value and value > 0 else None


def _performance(fixtures, name):
    played = won = lost = tied = no_result = unknown_result = 0
    totals, wickets, form = [], [], []
    completed = sorted((f for f in fixtures if f['status'] == 'completed' and
        _key(name) in (_key(f['team1']), _key(f['team2']))), key=lambda f: (f.get('date') or '', f.get('id') or 0))
    for fixture in completed:
        played += 1
        side = '1' if _key(fixture['team1']) == _key(name) else '2'
        runs = fixture.get('runs' + side)
        if runs is not None:
            totals.append(runs)
        taken = fixture.get('wickets2' if side == '1' else 'wickets1')
        if taken is not None:
            wickets.append(taken)
        if fixture.get('winner'):
            if _key(fixture['winner']) == _key(name):
                won += 1
                form.append('W')
            else:
                lost += 1
                form.append('L')
        elif fixture.get('result_type') == 'tie':
            tied += 1
            form.append('T')
        elif fixture.get('result_type') == 'no_result':
            no_result += 1
            form.append('NR')
        else:
            unknown_result += 1
            form.append('?')
    return dict(played=played, won=won, lost=lost, tied=tied, no_result=no_result, unknown_result=unknown_result,
        # Missing historical scores must not turn a partial sum into a career
        # total. Preserve the known contribution and samples separately.
        runs=sum(totals) if played and len(totals) == played else None,
        wickets=sum(wickets) if played and len(wickets) == played else None,
        known_runs=sum(totals), known_wickets=sum(wickets),
        highest_score=max(totals) if totals else None,
        highest_recorded_score=max(totals) if totals else None,
        average_score=round(sum(totals)/len(totals), 2) if totals else None,
        win_percentage=round(100*won/played, 2) if played else None,
        form=form[-5:], innings_with_scores=len(totals), innings_with_wickets=len(wickets))


def _teams(fixtures, team_rows):
    from services.league_records import safe_image_url
    names = {}
    for fixture in fixtures:
        for side in ('team1', 'team2'):
            name = fixture.get(side)
            if name and name != 'To be decided' and fixture.get(side + '_known', True):
                names[_key(name)] = name
    latest = {}
    for selected, tour, row in team_rows:
        names[_key(row.name)] = row.name
        latest[_key(row.name)] = row
    result = []
    for key, name in sorted(names.items()):
        row = latest.get(key)
        result.append(dict(name=name, captain=getattr(row, 'owner_name', None) or None, captain_role='Team manager' if getattr(row, 'owner_name', None) else None,
            logo_url=(safe_image_url(row.logo_url) if row and str(row.logo_url or '').startswith(('https://', 'http://')) else f'/stats/media/teams/{row.id}.png' if row else None), **_performance(fixtures, name)))
    return result


def _caps(players, field):
    maximum = max((p.get(field) or 0 for p in players), default=0)
    return [dict(name=p['name'], team=p.get('team'), value=maximum)
            for p in players if maximum > 0 and p.get(field) == maximum]


def _injuries(session, team_rows):
    from models import TournamentInjury
    by_id = {row.id: row for _, _, row in team_rows}
    if not by_id:
        return []
    result = []
    rows = session.query(TournamentInjury).filter(TournamentInjury.tournament_team_id.in_(by_id),
        TournamentInjury.matches_remaining > 0).order_by(TournamentInjury.created_at.desc())
    for row in rows:
        result.append(dict(player=row.player_name or 'Player', team=by_id[row.tournament_team_id].name,
            injury=row.injury_type, matches_remaining=row.matches_remaining,
            date=_iso(row.created_at), match_id=row.match_id, url=_match_url(row.match_id)))
    return result


def portal_snapshot(session, series, season=None):
    """Factual schedule/news and tournament centre for a selected competition."""
    from services import live_records, league_records as R, league_season_service as LS
    seasons, fixtures, rows = _fixture_context(session, series, season)
    confirmed = live_records.confirmed_snapshot(session, series, season)
    teams = _teams(fixtures, rows)
    news = []
    for fixture in sorted(fixtures, key=lambda f: (f.get('date') or '', f.get('id') or 0), reverse=True):
        if fixture['status'] != 'completed':
            continue
        winner = fixture.get('winner')
        title = (f"{winner} win against {fixture['team2'] if _key(winner) == _key(fixture['team1']) else fixture['team1']}"
                 if winner else f"{fixture['team1']} and {fixture['team2']}: {fixture.get('result') or 'result recorded'}")
        news.append(dict(kind='match_victory' if winner else 'match_result', title=title,
            body=fixture.get('result') or f"{fixture['team1']} {fixture.get('runs1') if fixture.get('runs1') is not None else '—'} · {fixture['team2']} {fixture.get('runs2') if fixture.get('runs2') is not None else '—'}",
            date=fixture.get('date'), match_id=fixture.get('match_id'), url=fixture.get('url'),
            season=fixture['season'], simulated=fixture.get('simulated', False)))
    # Editorial stories use the existing moderation boundary; this read never
    # generates stories, changes read counts, or calls a publisher.
    from models import NewsArticle
    articles = session.query(NewsArticle).filter_by(status='published').order_by(
        NewsArticle.published_at.desc(), NewsArticle.id.desc()).limit(10)
    for article in articles:
        news.append(dict(kind='article', article_id=article.id, title=article.headline,
            body=(article.body or '')[:280], date=_iso(article.published_at or article.created_at),
            match_id=None, url=None))
    injuries = _injuries(session, rows)
    for injury in injuries:
        count = injury['matches_remaining']
        news.append(dict(kind='injury', title=f"{injury['player']}: {injury['injury']}",
            body=f"{injury['team']} · unavailable for {count} more match{'es' if count != 1 else ''}.",
            date=injury['date'], match_id=injury['match_id'], url=injury['url']))
    from services.cricket_portal_news import transfer_stories
    news.extend(transfer_stories(session, seasons))
    news.sort(key=lambda n: n.get('date') or '', reverse=True)
    venues = defaultdict(list)
    for fixture in fixtures:
        if fixture.get('venue') and fixture['status'] == 'completed':
            venues[fixture['venue']].append(fixture)
    venue_stats = []
    for name, games in sorted(venues.items()):
        first = [f['runs1'] for f in games if f.get('runs1') is not None]
        all_scores = [f[k] for f in games for k in ('runs1', 'runs2') if f.get(k) is not None]
        venue_stats.append(dict(name=name, matches=len(games),
            average_first_innings=round(sum(first)/len(first), 2) if first else None,
            highest_total=max(all_scores) if all_scores else None))
    selected_rows = [dict(id=s.id, name=LS.season_title(s),
        status=LS.season_stage(session, s)['key']) for s in seasons]
    return dict(fixtures=fixtures, teams=teams, news=news[:30], venues=venue_stats,
        tournament=dict(standings=confirmed.get('standings', []),
            orange_cap=_caps(confirmed.get('players', []), 'runs'),
            purple_cap=_caps(confirmed.get('players', []), 'wickets'),
            playoffs=[f for f in fixtures if f.get('stage') not in ('league', 'group')],
            seasons=selected_rows, honours=LS.honours(season) if season else {},
            tables=[dict(season=LS.season_title(s), standings=R.standings_view(session, s)) for s in seasons]),
        news_note='Published CMU newsroom stories, recorded match results and active injury reports. Articles and interviews use the existing editorial approval process.')


def _squad(session, row):
    """Roster fields only: never serialize source model objects or user IDs."""
    from models import ChallengePlayer
    from services.cricket_portal_media import player_image_url
    from sqlalchemy.orm import joinedload
    if not row or not row.challenge_team_id:
        return []
    result = []
    players = session.query(ChallengePlayer).options(joinedload(ChallengePlayer.source_player)).filter_by(team_id=row.challenge_team_id).order_by(
        ChallengePlayer.sort_order, ChallengePlayer.name)
    for player in players:
        details = {str(k).casefold(): v for k, v in _loads(player.details_json, {}).items()}
        master = player.source_player
        def value(attr, aliases):
            # Stored Challenge Data ratings can exist without a master card.
            for key in aliases:
                if details.get(key) is not None:
                    return details[key]
            return getattr(master, attr, None) if master else None
        result.append(dict(name=player.name,
            ovr=_int(value('rating', ('rating', 'overall', 'ovr'))),
            bat=_int(value('bat_rating', ('bat_rating', 'batting_rating', 'bat', 'batting'))),
            bowl=_int(value('bowl_rating', ('bowl_rating', 'bowling_rating', 'bowl', 'bowling'))),
            role=value('category', ('category', 'role')), country=value('country', ('country',)),
            image=player_image_url(master), form=None))
    return result


def team_profile(session, series, season, name):
    """Squad and performance scoped to this competition; None for unknown teams."""
    from services import league_records as R
    _, fixtures, rows = _fixture_context(session, series, season)
    names = _teams(fixtures, rows)
    team = next((t for t in names if _key(t['name']) == _key(name)), None)
    if team is None:
        return None
    latest = next((row for _, _, row in reversed(rows) if _key(row.name) == _key(team['name'])), None)
    squad = _squad(session, latest)
    source = 'current_roster' if squad else 'recorded_appearances'
    if not squad:
        # An archived season may retain appearances but no recoverable roster.
        from services import league_season_service as LS
        seen = {}
        for selected in _selected(session, series, season):
            for player in LS.season_rows(session, selected):
                if _key(player.team_name) == _key(team['name']):
                    seen[_key(player.name)] = dict(name=player.name, ovr=None, bat=None, bowl=None,
                        role=None, country=None, form=None)
            for game in _loads(R.config(session, selected).archive_json, []):
                if not isinstance(game, dict):
                    continue
                for player in game.get('lines') or []:
                    if isinstance(player, dict) and _key(player.get('team_name')) == _key(team['name']) and player.get('name'):
                        seen.setdefault(_key(player['name']), dict(name=player['name'], ovr=None, bat=None,
                            bowl=None, role=None, country=None, form=None))
        squad = list(seen.values())
    relevant = [f for f in fixtures if _key(team['name']) in (_key(f['team1']), _key(f['team2']))]
    recent = sorted((f for f in relevant if f['status'] == 'completed'),
        key=lambda f: (f.get('date') or '', f.get('id') or 0), reverse=True)[:10]
    upcoming = [f for f in relevant if f['status'] in ('scheduled', 'live')]
    performance = _performance(fixtures, team['name'])
    records = [dict(label='Highest recorded total', value=performance['highest_score']),
        dict(label='Matches won', value=performance['won']),
        dict(label='Average recorded innings score', value=performance['average_score'])]
    return dict(name=team['name'], captain=team.get('captain'), captain_role=team.get('captain_role'), logo_url=team.get('logo_url'),
        squad=squad, squad_source=source, performance=performance, records=records,
        recent_results=recent, upcoming_fixtures=upcoming,
        injuries=[i for i in _injuries(session, rows) if _key(i['team']) == _key(team['name'])])
