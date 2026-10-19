"""Season record boards from existing scorecards, with no fabricated event stats.

Session-first; callers commit. Record boards are rebuilt on read, so scorecard
corrections immediately affect holders. Archived evidence is copied before delete.
"""
import asyncio
import html
import json
import logging
from fractions import Fraction

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# key, label, unit, direction. Fractions keep rate ties exact before formatting.
CATEGORIES = (
    ("runs", "Most runs", "runs", True),
    ("hs", "Highest individual score", "runs", True),
    ("sr", "Highest strike rate", "SR", True),
    ("avg", "Highest batting average", "average", True),
    ("fours", "Most fours", "fours", True),
    ("sixes", "Most sixes", "sixes", True),
    ("fifties", "Most fifties (50–99)", "fifties", True),
    ("hundreds", "Most hundreds", "hundreds", True),
    ("wkts", "Most wickets", "wickets", True),
    ("fig", "Best bowling figures", "figures", True),
    ("econ", "Best economy", "economy", False),
    ("bavg", "Best bowling average", "average", False),
    ("bsr", "Best bowling strike rate", "balls/wicket", False),
    ("three", "Most 3+ wicket hauls", "hauls", True),
    ("five", "Most 5+ wicket hauls", "hauls", True),
    ("total", "Highest team total", "runs", True),
    ("chase", "Highest successful chase", "runs", True),
)
LABELS = {k: label for k, label, _u, _d in CATEGORIES}
UNAVAILABLE = "Exact fastest milestones, partnerships, fielding and phase records need delivery/fielding evidence not present in these scorecards."


def config(session, season):
    from models import LeagueRecordConfig
    row = session.get(LeagueRecordConfig, season.id)
    if row is not None:
        return row
    from types import SimpleNamespace
    from services import league_season_service as LS
    tour = LS.season_tournament(session, season)
    return SimpleNamespace(
        min_bat_balls=(tour.min_balls_for_sr if tour and tour.min_balls_for_sr is not None else 100),
        min_bowl_balls=(tour.min_balls_for_econ if tour and tour.min_balls_for_econ is not None else 60),
        alerts_enabled=True, archive_json=None, standings_json=None)


def save_config(session, season, bat, bowl, alerts=True):
    from models import LeagueRecordConfig
    from services import league_season_service as LS
    values = []
    for raw in (bat, bowl):
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            raise ValueError("Minimum balls must be whole numbers between 0 and 100000.")
        if not 0 <= value <= 100000:
            raise ValueError("Minimum balls must be whole numbers between 0 and 100000.")
        values.append(value)
    row = session.get(LeagueRecordConfig, season.id)
    if row is None:
        row = LeagueRecordConfig(season_id=season.id)
        session.add(row)
    row.min_bat_balls, row.min_bowl_balls = values
    row.alerts_enabled = bool(alerts)
    tour = LS.season_tournament(session, season)
    if tour:
        tour.min_balls_for_sr, tour.min_balls_for_econ = values
    session.flush()
    return row


def evidence(session, season):
    from models import TournamentMatch, TournamentTeam
    from services import league_season_service as LS
    tour = LS.season_tournament(session, season)
    if tour is None:
        try:
            data = json.loads(config(session, season).archive_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []
    teams = {t.id: t.name for t in session.query(TournamentTeam).filter_by(tournament_id=tour.id)}
    result = []
    for m in session.query(TournamentMatch).filter_by(tournament_id=tour.id, status="completed").order_by(TournamentMatch.completed_at, TournamentMatch.id):
        if m.is_simulated:
            continue
        try:
            lines = json.loads(m.scorecard_json or "[]")
        except (ValueError, TypeError):
            lines = []
        lines = [x for x in lines if isinstance(x, dict)] if isinstance(lines, list) else []
        result.append(dict(fixture_id=m.id, match_id=m.match_id, stage=m.stage,
                           season=LS.season_title(season), season_no=season.season_no, season_id=season.id,
                           venue=m.venue, date=m.completed_at.isoformat() if m.completed_at else None,
                           team1=teams.get(m.team1_id), team2=teams.get(m.team2_id),
                           winner=teams.get(m.winner_team_id), result=m.result_text,
                           runs1=m.inn1_runs, runs2=m.inn2_runs, lines=lines))
    return result


def standings_view(session, season):
    from services import league_season_service as LS, tournament_service as TS
    tour = LS.season_tournament(session, season)
    if tour:
        return [dict(name=t.name, played=t.played, won=t.won, lost=t.lost,
                     tied=t.tied, no_result=t.no_result, points=t.points, nrr=t._nrr)
                for t in TS.points_table(session, tour.id)]
    try:
        value = json.loads(config(session, season).standings_json or '[]')
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def preserve(session, season):
    from models import LeagueRecordConfig
    data = evidence(session, season)
    row = session.get(LeagueRecordConfig, season.id)
    if row is None:
        cfg = config(session, season)
        row = LeagueRecordConfig(season_id=season.id, min_bat_balls=cfg.min_bat_balls,
                                 min_bowl_balls=cfg.min_bowl_balls, alerts_enabled=cfg.alerts_enabled)
        session.add(row)
    row.standings_json = json.dumps(standings_view(session, season), separators=(",", ":"))
    row.archive_json = json.dumps(data, separators=(",", ":"))
    session.flush()


def _n(line, key):
    return int(line.get(key) or 0)


def _name(name):
    return " ".join((name or "").lower().split())


def format_value(value):
    if isinstance(value, tuple):
        return f"{value[0]}/{-value[1]}"
    if isinstance(value, Fraction):
        return f"{float(value):.2f}"
    return str(value)


def boards(session, series, season=None, *, stage=None, team=None, exclude_match=None, live=None):
    """All holders are ranked by exact metric; display order never breaks a tie.

    Event-only boards stay empty for imported aggregate-only archives. Live is
    one provisional match evidence dict, replacing its completed copy if any.
    """
    from services import league_season_service as LS
    selected = [season] if season is not None else LS.seasons(session, series)
    cfg = config(session, season or selected[-1]) if selected else None
    games, players = [], {}
    for s in selected:
        games.extend(evidence(session, s))
    games = [g for g in games if exclude_match is None or g['match_id'] != exclude_match]
    if live:
        games = [g for g in games if g['match_id'] != live['match_id']]
        games.append(live)
    games = [g for g in games if (not stage or g['stage'] == stage) and
             (not team or team in (g['team1'], g['team2']) or any(l.get('team_name') == team for l in g['lines']))]
    raw = {k: [] for k, *_ in CATEGORIES}
    def add(k, who, value, g=None, side=None):
        if value is None:
            return
        raw[k].append(dict(name=who or "Unknown", team=side, value=value, display=format_value(value), source=g))
    for g in games:
        for l in g['lines']:
            if team and l.get('team_name') != team:
                continue
            key = _name(l.get('name'))
            if not key:
                continue
            p = players.setdefault(key, dict(name=l['name'], team=l.get('team_name'), matches=0,
                runs=0, balls=0, outs=0, fours=0, sixes=0, wkts=0, conceded=0, bowled=0,
                fifties=0, hundreds=0, three=0, five=0, last=None))
            p['last'] = g
            p['team'] = l.get('team_name') or p['team']
            p['matches'] += 1
            if l.get('batted'):
                r = _n(l,'bat_runs')
                for field, key2 in [('runs','bat_runs'),('balls','bat_balls'),('fours','bat_fours'),('sixes','bat_sixes')]:
                    p[field] += _n(l,key2)
                p['outs'] += bool(l.get('bat_out'))
                if p['fifties'] is not None: p['fifties'] += 50 <= r < 100
                if p['hundreds'] is not None: p['hundreds'] += r >= 100
                add('hs', p['name'], r, g, p['team'])
            if l.get('bowled'):
                w, rc = _n(l,'bowl_wickets'), _n(l,'bowl_runs')
                p['wkts'] += w; p['conceded'] += rc; p['bowled'] += _n(l,'bowl_balls')
                if p['three'] is not None: p['three'] += w >= 3
                if p['five'] is not None: p['five'] += w >= 5
                if w:
                    add('fig',p['name'],(w,-rc),g,p['team'])
        for who, score in ((g['team1'],g['runs1']), (g['team2'],g['runs2'])):
            if who and score is not None and (not team or who == team):
                add('total',who,score,g)
        if g['team2'] and g['winner'] == g['team2'] and g['runs1'] is not None and g['runs2'] is not None and g['runs2'] > g['runs1'] and (not team or team == g['team2']):
            add('chase',g['team2'],g['runs2'],g)
    # Legacy archives may contain aggregates but no match evidence. Use their
    # supported totals only; do not invent milestones or match provenance.
    for s in selected:
        if evidence(session, s) or stage:
            continue
        for r in LS._grouped(LS.season_rows(session, s)).values():
            if team and team != r.team_name:
                continue
            key = _name(r.name)
            p = players.setdefault(key, dict(name=r.name,team=r.team_name,matches=0,runs=0,balls=0,outs=0,fours=0,sixes=0,wkts=0,conceded=0,bowled=0,fifties=None,hundreds=None,three=None,five=None,last=None))
            for a,b in [('matches','matches'),('runs','bat_runs'),('balls','bat_balls'),('outs','bat_outs'),('fours','bat_fours'),('sixes','bat_sixes'),('wkts','bowl_wickets'),('conceded','bowl_runs'),('bowled','bowl_balls')]:
                p[a] += getattr(r,b)
            # Missing innings evidence means these counts are unknown, even
            # when other seasons supplied a partial count.
            for metric in ('fifties', 'hundreds', 'three', 'five'):
                p[metric] = None
            if r.highest_score:
                add('hs',r.name,r.highest_score,side=r.team_name)
            if r.best_bowl_wickets:
                add('fig',r.name,(r.best_bowl_wickets,-r.best_bowl_runs),side=r.team_name)
    for p in players.values():
        for k, f in [('runs','runs'),('fours','fours'),('sixes','sixes'),('wkts','wkts'),('fifties','fifties'),('hundreds','hundreds'),('three','three'),('five','five')]:
            if p[f]:
                add(k,p['name'],p[f],side=p['team'])
        if p['balls'] > 0 and p['balls'] >= cfg.min_bat_balls:
            add('sr',p['name'],Fraction(100*p['runs'],p['balls']),side=p['team'])
            if p['outs']:
                add('avg',p['name'],Fraction(p['runs'],p['outs']),side=p['team'])
        if p['bowled'] > 0 and p['bowled'] >= cfg.min_bowl_balls:
            add('econ',p['name'],Fraction(6*p['conceded'],p['bowled']),side=p['team'])
            if p['wkts']:
                add('bavg',p['name'],Fraction(p['conceded'],p['wkts']),side=p['team'])
                add('bsr',p['name'],Fraction(p['bowled'],p['wkts']),side=p['team'])
    result = []
    for k, label, unit, descending in CATEGORIES:
        entries = sorted(raw[k],key=lambda x: x['name'].lower())
        entries.sort(key=lambda x:x['value'],reverse=descending)
        previous, rank = None, 0
        for index, entry in enumerate(entries,1):
            if entry['value'] != previous:
                rank = index
            entry['rank'] = rank
            previous = entry['value']
        result.append(dict(key=k,label=label,unit=unit,descending=descending,entries=entries,
                           holders=[e for e in entries if e['rank']==1]))
    return dict(boards=result,players=sorted(players.values(),key=lambda p:-p['runs']),
                matches=games,config=cfg,
                teams=sorted({x for g in games for x in (g['team1'],g['team2']) if x}),
                summary=dict(matches=len(games),runs=sum(
                    (g['runs1'] or 0) * (not team or team == g['team1']) +
                    (g['runs2'] or 0) * (not team or team == g['team2']) for g in games),
                             fours=sum(p['fours'] for p in players.values()),sixes=sum(p['sixes'] for p in players.values()),wickets=sum(p['wkts'] for p in players.values())))


def _live_game(state, season):
    from services.tournament_service import _collect_player_lines
    from services import league_season_service as LS
    return dict(fixture_id=state.get('reserved_fixture_id'),match_id=state['match_id'],
                stage=state.get('tournament_stage') or 'league',season=LS.season_title(season),season_no=season.season_no,season_id=season.id,
                venue=state.get('venue'),date=None,team1=state.get('inn1_bat_team') or state.get('bat_team_name'),
                team2=state.get('bat_team_name') if state.get('inn1_bat_team_id') is not None else state.get('bowl_team_name'),
                winner=None,result=None,runs1=state.get('inn1_runs') if state.get('inn1_bat_team_id') is not None else state.get('total_runs'),
                runs2=state.get('total_runs') if state.get('inn1_bat_team_id') is not None else None,
                lines=_collect_player_lines(state))


def collect_alerts(session,state,*,final=False):
    from models import LeagueRecordAlert, TournamentMatch
    from services import league_season_service as LS
    season = LS.season_for_tournament(session,state.get('tournament_id')) if state.get('tournament_id') else None
    if season is None or not state.get('match_id') or not config(session,season).alerts_enabled:
        return []
    series = LS.series_of(session,season)
    mid = int(state['match_id'])
    live = None if final else _live_game(state,season)
    # Once a completed result exists the live copy would double-count its totals.
    completed = session.query(TournamentMatch).filter_by(match_id=mid,status='completed').first()
    if final and completed is None:
        return []
    if completed is not None and not final:
        return []
    alerts=[]
    for scope, selected in [('season',season),('all-time',None)]:
        old=boards(session,series,selected,exclude_match=mid)
        new=boards(session,series,selected,live=live)
        for before,after in zip(old['boards'],new['boards']):
            if not after['holders'] or (not final and after['key'] in ('sr','avg','econ','bavg','bsr','chase')):
                continue
            old_value=before['holders'][0]['value'] if before['holders'] else None
            new_value=after['holders'][0]['value']
            better=old_value is None or (new_value>old_value if after['descending'] else new_value<old_value)
            if old_value is not None and new_value != old_value and not better:
                continue
            old_names={_name(e['name']) for e in before['holders']}
            for holder in after['holders']:
                # Only a changed/new holder can generate an equal-record alert.
                if not better and _name(holder['name']) in old_names:
                    continue
                kind='new_record' if better else 'record_equalled'
                ident=dict(season_id=season.id,match_id=mid,scope=scope,category=after['key'],holder=_name(holder['name']),kind=kind)
                if session.query(LeagueRecordAlert).filter_by(**ident).first():
                    continue
                try:
                    with session.begin_nested():
                        session.add(LeagueRecordAlert(**ident));session.flush()
                except IntegrityError:
                    continue
                previous=format_value(old_value) if old_value is not None else 'first recorded mark'
                text=f"{'NEW RECORD' if better else 'RECORD EQUALLED'} | {series.name} | {scope}\n{after['label']}: {holder['name']} — {holder['display']} {after['unit']}\nPrevious: {previous} | {LS.season_title(season)} | Match #{mid}{' | Confirmed scorecard' if final else ' | Live provisional'}"
                alerts.append((kind,text,dict(player=holder['name'],record=after['label'],value=holder['display'],previous=previous,season=LS.season_title(season),scope=scope,match=mid)))
    # Combine season and all-time achievements for the same holder and record.
    merged={}
    for kind,text,fields in alerts:
        key=(kind,fields['record'],fields['player'])
        if key in merged:
            merged[key][1] += '\nAlso achieved in the all-time record book.'
        else:
            merged[key]=[kind,text,fields]
    return list(merged.values())


async def announce(context,state,*,final=False):
    """Called after a persisted over, or the committed final scorecard.

    Claim before sending: replay/restart safe, at-most-once delivery. A Telegram
    failure is logged; it must never stop the match or replay a celebration.
    """
    if not state.get('tournament_id') or not state.get('chat_id'):
        return
    def work():
        from database import get_session
        s=get_session()
        try:
            result=collect_alerts(s,state,final=final);s.commit();return result
        except Exception:
            s.rollback();raise
        finally:
            s.close()
    try:
        alerts=await asyncio.to_thread(work)
        from services.event_media_service import fire_event_media
        grouped = {}
        for kind, text, fields in alerts:
            grouped.setdefault((kind, fields['player']), []).append((text, fields))
        for (kind, player), achievements in grouped.items():
            text = "\n\n".join(t for t, _f in achievements)
            # Telegram limits message text to 4096 characters.
            for start in range(0, len(text), 4000):
                await context.bot.send_message(chat_id=state['chat_id'], text=text[start:start+4000])
            fields = dict(achievements[0][1])
            fields['record'] = ", ".join(f['record'] for _t, f in achievements)
            fields['value'] = ", ".join(f['value'] for _t, f in achievements)
            await fire_event_media(context, state['chat_id'], kind,
                fields={k:html.escape(str(v)) for k,v in fields.items()}, cooldown=False)
    except Exception:
        logger.exception('League record announcement failed for match %s',state.get('match_id'))
