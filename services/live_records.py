"""Read-only cricket statistics. Live polling never rebuilds season history.

Only linked, active competitions are published; captain choices, user/chat IDs,
private games and raw state never leave this module.
"""
import json
import threading
import time

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_BUILD_LOCKS = {}
RECORDS_TTL = 20


def public_series(session, series_id):
    from models import LeagueSeries
    return session.query(LeagueSeries).filter_by(id=series_id, is_active=True).first()


def series_list(session):
    from models import LeagueSeries, LeagueSeason
    return session.query(LeagueSeries).filter(LeagueSeries.is_active.is_(True),
        LeagueSeries.id.in_(session.query(LeagueSeason.series_id))).order_by(
        LeagueSeries.sort_order, LeagueSeries.name).all()


def selected_season(session, series, raw):
    from services import league_season_service as LS
    seasons = LS.seasons(session, series)
    if raw == 'all':
        return seasons, None
    selected = LS.get_season(session, series, raw) if raw else (seasons[-1] if seasons else None)
    if raw and selected is None:
        raise ValueError('Unknown season')
    return seasons, selected


def _number(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


def _figures(state, side):
    stats = state.get('bat_stats' if side == 'bat' else 'bowl_stats') or {}
    players = state.get('bat_xi' if side == 'bat' else 'bowl_xi') or []
    result = []
    for player in players:
        if not isinstance(player, dict):
            continue
        line = stats.get(str(player.get('roster_id'))) or {}
        balls = _number(line.get('balls'))
        if side == 'bat':
            if not balls and not line.get('out'):
                continue
            result.append(dict(name=str(player.get('name') or 'Unknown'), runs=_number(line.get('runs')),
                               balls=balls, fours=_number(line.get('fours')), sixes=_number(line.get('sixes')),
                               out=bool(line.get('out'))))
        elif balls:
            result.append(dict(name=str(player.get('name') or 'Unknown'), runs=_number(line.get('runs')),
                               balls=balls, wickets=_number(line.get('wickets'))))
    return result


def live_snapshot(session, series, season=None):
    from models import LeagueSeason, Match, MatchState
    from services.cipl_match import balls_bowled, balls_per_unit
    selected = session.query(LeagueSeason).filter_by(series_id=series.id)
    if season is not None:
        selected = selected.filter(LeagueSeason.id == season.id)
    tournament_ids = [r.tournament_id for r in selected if r.tournament_id is not None]
    if not tournament_ids:
        return dict(matches=[], updated_at=int(time.time()))
    rows = (session.query(Match, MatchState).join(MatchState, MatchState.match_id == Match.id)
            .filter(Match.tournament_id.in_(tournament_ids), Match.status.in_(('active', 'playing')))
            .order_by(Match.id.desc()).limit(50).all())
    matches = []
    for match, stored in rows:
        try:
            state = json.loads(stored.state_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict) or state.get('is_private') or state.get('mode') != 'cipl_approach':
            continue
        bpu = balls_per_unit(state)
        balls = max(0, balls_bowled(state))
        first = None
        if _number(state.get('innings')) == 2:
            first = dict(team=str(state.get('inn1_bat_team') or 'First innings'),
                         runs=_number(state.get('inn1_runs')), wickets=_number(state.get('inn1_wickets')))
        matches.append(dict(id=match.id, innings=_number(state.get('innings')) or 1,
            batting_team=str(state.get('bat_team_name') or 'Batting side'),
            bowling_team=str(state.get('bowl_team_name') or 'Bowling side'),
            runs=_number(state.get('total_runs')), wickets=_number(state.get('total_wickets')),
            balls=balls, balls_per_over=bpu, overs=_number(state.get('overs')),
            target=_number(state.get('target')) or None, first_innings=first,
            batting=_figures(state, 'bat'), bowling=_figures(state, 'bowl'),
            version=_number(stored.version), updated_at=stored.last_modified.isoformat() if stored.last_modified else None,
            url=f'/cricket?match_id={match.id}'))
    return dict(matches=matches, updated_at=int(time.time()))


def _source_signature(session, series, season):
    """Small aggregates invalidate a cached record book when a result arrives."""
    from sqlalchemy import func
    from models import LeagueSeason, TournamentMatch
    tournaments = session.query(LeagueSeason.tournament_id).filter_by(series_id=series.id)
    if season:
        tournaments = tournaments.filter(LeagueSeason.id == season.id)
    return tuple(session.query(func.count(TournamentMatch.id), func.max(TournamentMatch.id),
        func.max(TournamentMatch.completed_at), func.sum(TournamentMatch.inn1_runs),
        func.sum(TournamentMatch.inn2_runs), func.sum(TournamentMatch.inn1_wickets),
        func.sum(TournamentMatch.inn2_wickets)).filter(
            TournamentMatch.tournament_id.in_(tournaments), TournamentMatch.status == 'completed').one())


def invalidate_records_cache():
    """Used after scorecard corrections; in-flight live polls are unaffected."""
    with _CACHE_LOCK:
        _CACHE.clear()


def invalidate_after_commit(session):
    """Defer correction invalidation until the outer transaction is durable."""
    from sqlalchemy import event
    session.info['live_records_dirty'] = True
    if session.info.get('live_records_hooks'):
        return
    session.info['live_records_hooks'] = True

    def committed(db):
        if db.in_nested_transaction():
            return
        if db.info.pop('live_records_dirty', False):
            invalidate_records_cache()

    def rolled_back(db):
        if not db.in_nested_transaction():
            db.info.pop('live_records_dirty', None)

    event.listen(session, 'after_commit', committed)
    event.listen(session, 'after_rollback', rolled_back)


def confirmed_snapshot(session, series, season=None):
    """Cache JSON-safe confirmed boards separately from the fast live feed."""
    from services import league_records as R
    key = (str(session.get_bind().url), series.id, season.id if season else None)
    signature = _source_signature(session, series, season)
    # A slow historical build only serializes requests for this competition;
    # other competitions and the live feed never acquire this build lock.
    with _CACHE_LOCK:
        build_lock = _BUILD_LOCKS.setdefault(key, threading.Lock())
    with build_lock:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached and now - cached[0] < RECORDS_TTL and cached[2] == signature:
            return cached[1]
        data = R.boards(session, series, season)
        records = [dict(key=b['key'], label=b['label'], unit=b['unit'],
                        holders=[dict(name=h['name'], team=h.get('team'), display=h['display'])
                                 for h in b['holders'][:5]]) for b in data['boards']]
        players = [dict(name=p['name'], team=p.get('team'), matches=p['matches'], runs=p['runs'],
                        balls=p['balls'], wickets=p['wkts'], conceded=p['conceded'], bowled=p['bowled'],
                        fours=p['fours'], sixes=p['sixes']) for p in data['players']]
        matches = [dict(id=g['match_id'], team1=g.get('team1'), team2=g.get('team2'),
                        runs1=g.get('runs1'), runs2=g.get('runs2'), result=g.get('result'), stage=g.get('stage'))
                   for g in data['matches'][-20:]][::-1]
        result = dict(summary=data['summary'], records=records, players=players, matches=matches,
                      standings=R.standings_view(session, season) if season else [],
                      updated_at=int(time.time()))
        with _CACHE_LOCK:
            if len(_CACHE) >= 100:
                evicted = next(iter(_CACHE))
                _CACHE.pop(evicted)
                _BUILD_LOCKS.pop(evicted, None)
            _CACHE[key] = (now, result, signature)
        return result
