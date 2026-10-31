"""Record watch, match recaps, durable delivery and scorecard amendments.

No network I/O happens during scorecard transactions. A bot job sends/edits
pending rows after commit. Original message IDs survive subsequent corrections.
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def options(session, season):
    from models import LeagueRecordOptions
    row=session.get(LeagueRecordOptions,season.id)
    try:
        value=json.loads(row.options_json or '{}') if row else {}
    except (ValueError,TypeError):
        value={}
    if not isinstance(value,dict):value={}
    return dict(watch=bool(value.get('watch',True)),recap=bool(value.get('recap',True)))


def save_options(session,season,*,watch=True,recap=True):
    from models import LeagueRecordOptions
    row=session.get(LeagueRecordOptions,season.id)
    if row is None:
        row=LeagueRecordOptions(season_id=season.id);session.add(row)
    row.options_json=json.dumps(dict(watch=bool(watch),recap=bool(recap)))
    session.flush()


def scorecard_url(match_id):
    from services.match_broadcast import _webapp_host
    host=_webapp_host()
    return f'{host}/cricket?match_id={int(match_id)}' if host and match_id else None


def digest(game):
    content={k:game.get(k) for k in ('lines','runs1','runs2','result','winner','analytics','venue')}
    return hashlib.sha256(json.dumps(content,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()



def policy_digest(session,season,game):
    from services import league_records as R
    cfg=R.config(session,season)
    return hashlib.sha256(f"{digest(game)}:{cfg.min_bat_balls}:{cfg.min_bowl_balls}".encode()).hexdigest()

def record_changes(session,series,season,game):
    from services import league_records as R
    result=[]
    for scope,selected in [('season',season),('all-time',None)]:
        before=R.boards(session,series,selected,exclude_match=game['match_id'],through=game)
        after=R.boards(session,series,selected,through=game)
        result.extend(_board_changes(before, after, scope))
    return result


def _board_changes(before, after, scope):
    from services import league_records as R
    result = []
    for old,new in zip(before['boards'],after['boards']):
        if not new['holders']:
            continue
        prior=old['holders'][0]['value'] if old['holders'] else None
        value=new['holders'][0]['value']
        better=prior is None or (value>prior if new['descending'] else value<prior)
        old_names={e['name'] for e in old['holders']}
        if not better and value!=prior:
            continue
        holders=[h for h in new['holders'] if better or h['name'] not in old_names]
        if holders:
            result.append(dict(scope=scope,label=new['label'],value=new['holders'][0]['display'],unit=new['unit'],
                holders=list(dict.fromkeys(h['name'] for h in holders)),kind='broken' if better else 'equalled',
                first_record=prior is None,
                previous=R.format_value(prior) if prior is not None else 'first recorded mark',
                previous_holders=list(dict.fromkeys(e['name'] for e in old['holders']))))
    return result


def _match_summary_lines(series,season,game,*,corrected=False):
    from services import league_season_service as LS
    lines=[f"{'CORRECTED MATCH RECAP' if corrected else 'MATCH RECAP'} | {series.name} | {LS.season_title(season)}",
           f"{game.get('team1') or 'Team 1'} {game.get('runs1') if game.get('runs1') is not None else '—'} vs {game.get('team2') or 'Team 2'} {game.get('runs2') if game.get('runs2') is not None else '—'}",
           game.get('result') or 'Result recorded',f"Match #{game.get('match_id') or game['fixture_id']} · {game.get('venue') or 'Venue not supplied'}"]
    bat=sorted([l for l in game['lines'] if l.get('batted')],key=lambda l:-int(l.get('bat_runs') or 0))
    bowl=sorted([l for l in game['lines'] if l.get('bowled')],key=lambda l:(-int(l.get('bowl_wickets') or 0),int(l.get('bowl_runs') or 0)))
    if bat:
        p=bat[0];lines.append(f"Standout batting: {p.get('name')} {p.get('bat_runs',0)} from {p.get('bat_balls',0)}")
    if bowl:
        p=bowl[0];lines.append(f"Standout bowling: {p.get('name')} {p.get('bowl_wickets',0)}/{p.get('bowl_runs',0)}")
    for p in game['lines']:
        for n in (50,100,150):
            if p.get(f'balls_to_{n}'):
                lines.append(f"Milestone: {p.get('name')} reached {n} in {p[f'balls_to_{n}']} balls")
    return lines


def recap_text(session,series,season,game,*,corrected=False):
    from services import league_records as R
    lines = _match_summary_lines(series,season,game,corrected=corrected)
    changes=record_changes(session,series,season,game)
    for record in changes[:10]:
        lines.append(f"{record['scope']} {record['label']} {record['kind']}: {', '.join(record['holders'])} — {record['value']} {record['unit']}")
    if len(changes)>10:
        lines.append(f"Plus {len(changes)-10} more record achievements in the record book.")
    before=R.boards(session,series,season,exclude_match=game['match_id'],through=game)
    after=R.boards(session,series,season,through=game)
    for key,label in [('runs','Orange Cap'),('wkts','Purple Cap')]:
        old=next(b for b in before['boards'] if b['key']==key)['holders']
        new=next(b for b in after['boards'] if b['key']==key)['holders']
        if new and {h['name'] for h in old}!={h['name'] for h in new}:
            lines.append(f"{label} lead: {', '.join(dict.fromkeys(h['name'] for h in new))} ({new[0]['display']})")
    link=scorecard_url(game.get('match_id'))
    if link: lines.append(f'Scorecard: {link}')
    return '\n'.join(lines)[:4000]



def group_text(series,season,match_id,kind,player,items,*,final=False):
    from services import league_season_service as LS
    lines=[f"{'NEW RECORD' if kind=='new_record' else 'RECORD EQUALLED'} | {player}",
           f"{series.name} | {LS.season_title(season)} | Match #{match_id}",
           'Confirmed scorecard' if final else 'Live provisional']
    for item in items:
        text=f"{item['record']}: {item['value']} {item.get('unit','')} ({item['scope']})\nPrevious: {item['previous']} by {item.get('previous_holder','none')}"
        if len('\n'.join(lines))+len(text)>3650:
            lines.append('More achievements are retained in the season record book.');break
        lines.append(text)
    if items and items[0].get('opponent'):lines.append('Opponent: '+items[0]['opponent'])
    link=scorecard_url(match_id)
    if link:lines.append('Scorecard: '+link)
    return '\n'.join(lines)[:4000]

def queue(session,season,match_id,chat_id,kind,text,*,fixture_id=None,metadata=None,fingerprint=None):
    from models import LeagueRecordPublication
    from sqlalchemy.exc import IntegrityError
    ident=dict(season_id=season.id,match_id=int(match_id),chat_id=int(chat_id),kind=kind)
    row=session.query(LeagueRecordPublication).filter_by(**ident).first()
    if row is None:
        try:
            with session.begin_nested():
                row=LeagueRecordPublication(**ident,text=text[:4000],fixture_id=fixture_id,
                    metadata_json=json.dumps(metadata or {}),fingerprint=fingerprint,status='pending',revision=0,delivered_revision=-1)
                session.add(row);session.flush()
        except IntegrityError:
            row=session.query(LeagueRecordPublication).filter_by(**ident).one()
    elif row.text!=text[:4000] or (kind == 'match_records' and metadata is not None and
                                 json.loads(row.metadata_json or '{}') != metadata):
        row.text=text[:4000];row.revision+=1;row.status='pending'
        row.fingerprint=fingerprint or row.fingerprint
    if metadata is not None:row.metadata_json=json.dumps(metadata)
    return row


def queue_watch(session, state):
    # Record updates are deliberately silent until the result is committed.
    return []


def holders_markdown(session, series, season=None):
    from services import league_records as R, league_season_service as LS
    data = R.boards(session, series, season)
    lines = [f"# {series.name} — {LS.season_title(season) if season else 'All-time'} records", ""]
    for board in data['boards']:
        lines += [f"## {board['label']}", ""]
        for holder in board['holders']:
            lines.append(f"- {holder['name']} — {holder['display']} {board['unit']}")
        if not board['holders']:
            lines.append("No qualifying performance yet.")
        lines.append("")
    cfg = data['config']
    if cfg:
        lines.append(f"Rate qualification: {cfg.min_bat_balls} batting balls / {cfg.min_bowl_balls} bowling balls. Joint holders retained.")
    return '\n'.join(lines)


def match_markdown(session, series, season, game):
    """Competition-wide record book, frozen at the source match's completion."""
    from services import league_records as R, league_season_service as LS
    data = R.boards(session, series, through=game)
    included = [s for s in LS.seasons(session, series) if s.season_no <= season.season_no]
    lines = [f"# {series.name} — All-seasons records", "",
             f"Snapshot after Match #{game['match_id']} · {LS.season_title(season)}", "",
             "Includes this competition's linked current and archived seasons up to this match. "
             "Later matches and seasons are excluded.", "", "## Match summary", "",
             *_match_summary_lines(series, season, game), ""]
    link = scorecard_url(game.get('match_id'))
    if link:
        lines += [f"[Match scorecard]({link})", ""]
    lines += ["## All-time records established, broken or equalled this match", ""]
    before = R.boards(session, series, exclude_match=game['match_id'], through=game)
    changes = _board_changes(before, data, 'all-time')
    for record in changes:
        kind = 'established' if record['first_record'] else record['kind']
        lines += [f"### {record['label']} ({kind})",
                  f"{', '.join(record['holders'])}: {record['value']} {record['unit']}",
                  f"Previous: {record['previous']} — {', '.join(record['previous_holders']) or 'none'}", ""]
    if not changes:
        lines += ["No all-time records established, broken or equalled in this match.", ""]

    lines += ["## All-seasons record holders", "",
              "All joint holders are listed. Career totals combine every included season.", ""]
    match_records = {'hs', 'f50', 'f100', 'f150', 'fig', 'total', 'chase', 'partnership', 'p50', 'p100'}
    for board in data['boards']:
        lines += [f"### {board['label']}", ""]
        for holder in board['holders']:
            source = holder.get('source')
            context = []
            if holder.get('team'):
                context.append(f"{'Team' if source else 'Latest team'}: {holder['team']}")
            if source:
                context.append(f"{source['season']} (S{source['season_no']})")
                if source.get('match_id'):
                    context.append(f"Match #{source['match_id']}")
                else:
                    context.append(f"Fixture #{source['fixture_id']}")
                if source.get('team1') and source.get('team2'):
                    context.append(f"{source['team1']} vs {source['team2']}")
                if source.get('venue'):
                    context.append(source['venue'])
                if source.get('date'):
                    context.append(source['date'][:10])
                source_link = scorecard_url(source.get('match_id'))
                if source_link:
                    context.append(f"[Scorecard]({source_link})")
            elif board['key'] in match_records:
                context.append("Season aggregate; match details unavailable")
            else:
                context.append("Career total across included seasons")
            lines.append(f"- {holder['name']} — **{holder['display']} {board['unit']}** · {' · '.join(context)}")
        if not board['holders']:
            lines.append("No qualifying performance with saved evidence yet.")
        lines.append("")

    lines += ["## Career leaderboards — top 3 ranks", "",
              "Joint ranks are retained, including every player tied at the cutoff.", ""]
    for key in ('runs', 'wkts'):
        board = next(b for b in data['boards'] if b['key'] == key)
        lines += [f"### {board['label']}", "", "| Rank | Player | Total |", "| --- | --- | --- |"]
        entries = [e for e in board['entries'] if e['rank'] <= 3]
        for entry in entries:
            name = entry['name'].replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
            lines.append(f"| {entry['rank']} | {name} | {entry['display']} {board['unit']} |")
        if not entries:
            lines.append("| — | No qualifying performance yet | — |")
        lines.append("")

    lines += ["## Coverage and qualification", "",
              f"- Seasons included ({len(included)}): " + ', '.join(f"{LS.season_title(s)} (S{s.season_no})" for s in included),
              f"- Verified match scorecards: {len(data['matches'])}. Saved historical aggregates also contribute supported totals and best figures.",
              "- Simulated fixtures are excluded. Missing milestone, partnership, phase and fielding evidence is not reconstructed."]
    cfg = data['config']
    if cfg:
        lines.append(f"- Rate qualification: {cfg.min_bat_balls} batting balls / {cfg.min_bowl_balls} bowling balls, "
                     "applied to career totals using the latest included season's settings.")
    lines.append("- Joint records use exact values; rounding never breaks a tie.")
    return '\n'.join(lines)


def _match_file_metadata(session, series, season, game):
    return {'filename': f"match-{game['match_id']}-all-seasons-records.md",
            'markdown': match_markdown(session, series, season, game)}


def _match_file_caption(series, match_id, *, corrected=False):
    return f"{series.name} | Match #{match_id} — all-seasons records{' (corrected)' if corrected else ''}"


def record_update_text(changes, match_id):
    """One small message; combine identical season and all-time achievements."""
    merged = {}
    for change in changes:
        action = 'set' if change['first_record'] else change['kind']
        key = (change['label'], tuple(change['holders']), change['value'], change['unit'], action)
        merged.setdefault(key, []).append(change['scope'])
    if not merged:
        return ''
    lines = [f"🏅 Records · Match #{match_id}"]
    for (label, holders, value, unit, action), scopes in list(merged.items())[:4]:
        names = ', '.join(holders)
        if len(names) > 55:
            names = names[:52] + '…'
        line = f"• {label} {action}: {names} — {value} {unit} ({' / '.join(scopes)})"
        lines.append(line[:130])
    if len(merged) > 4:
        lines.append(f"+{len(merged) - 4} more records")
    return '\n'.join(lines)


def queue_record_update(session, state):
    from services import league_records as R, league_season_service as LS
    season = LS.season_for_tournament(session, state['tournament_id'])
    if season is None or not R.config(session, season).alerts_enabled:
        return None
    game = next((g for g in R.evidence(session, season) if g['match_id'] == state['match_id']), None)
    if game is None:
        return None
    text = record_update_text(record_changes(session, LS.series_of(session, season), season, game), state['match_id'])
    if not text:
        return None
    return queue(session, season, state['match_id'], state['chat_id'], 'record_summary',
                 text, fixture_id=game['fixture_id'], fingerprint=hashlib.sha256(text.encode('utf-8')).hexdigest())


def queue_match_file(session, state):
    from services import league_records as R, league_season_service as LS
    season = LS.season_for_tournament(session, state['tournament_id'])
    if season is None or not R.config(session, season).alerts_enabled:
        return None
    game = next((g for g in R.evidence(session, season) if g['match_id'] == state['match_id']), None)
    if game is None:
        return None
    series = LS.series_of(session, season)
    metadata = _match_file_metadata(session, series, season, game)
    return queue(session, season, state['match_id'], state['chat_id'], 'match_records',
                 _match_file_caption(series, state['match_id']), fixture_id=game['fixture_id'],
                 metadata=metadata,
                 fingerprint=hashlib.sha256(metadata['markdown'].encode('utf-8')).hexdigest())


def queue_recap(session,state):
    from services import league_records as R, league_season_service as LS
    season=LS.season_for_tournament(session,state.get('tournament_id')) if state.get('tournament_id') else None
    if not season or not state.get('chat_id') or not options(session,season)['recap']:
        return None
    game=next((g for g in R.evidence(session,season) if g['match_id']==state.get('match_id')),None)
    if game is None: return None
    return queue(session,season,state['match_id'],state['chat_id'],'recap',
                 recap_text(session,LS.series_of(session,season),season,game),fixture_id=game['fixture_id'],fingerprint=policy_digest(session,season,game))


def reconcile(session,tournament_id, *, appended_match=None):
    """Called inside a scorecard transaction. Only queue edits; never send."""
    from models import LeagueRecordPublication, TournamentMatch
    from sqlalchemy import and_, or_, select
    from services import league_records as R, league_season_service as LS
    season=LS.season_for_tournament(session,tournament_id)
    if season is None: return 0
    series=LS.series_of(session,season)
    seasons = {s.id: s for s in LS.seasons(session, series)}
    games_by_season = {}
    count=0
    affected = [s.id for s in seasons.values() if s.season_no >= season.season_no]
    rows = session.query(LeagueRecordPublication).filter(or_(
        LeagueRecordPublication.season_id == season.id,
        and_(LeagueRecordPublication.season_id.in_(affected), LeagueRecordPublication.kind.in_(('match_records', 'record_summary')))))
    # Old report files are no longer delivered; don't regenerate their Markdown
    # on every result after dispatch has cancelled them.
    rows = rows.filter(or_(LeagueRecordPublication.kind != 'match_records',
                          LeagueRecordPublication.status != 'cancelled'))
    if appended_match is not None and appended_match.completed_at is not None:
        # Record snapshots stop at their source match, ordered by season/date/id.
        # A later completion cannot affect earlier snapshots. NULL dates sort
        # last in game_order, so they must remain eligible, as must publications
        # from later seasons when an old season receives a new/backdated match.
        earlier = select(TournamentMatch.id).where(
            TournamentMatch.tournament_id == int(tournament_id),
            TournamentMatch.status == 'completed',
            TournamentMatch.completed_at.isnot(None),
            or_(TournamentMatch.completed_at < appended_match.completed_at,
                and_(TournamentMatch.completed_at == appended_match.completed_at,
                     TournamentMatch.id < appended_match.id)))
        rows = rows.filter(or_(LeagueRecordPublication.season_id != season.id,
                              LeagueRecordPublication.fixture_id.is_(None),
                              ~LeagueRecordPublication.fixture_id.in_(earlier)))
    for row in rows:
        if row.kind.startswith(('watch:', 'record:')): continue
        source_season = seasons[row.season_id]
        if source_season.id not in games_by_season:
            games_by_season[source_season.id] = {g['match_id']: g for g in R.evidence(session, source_season)}
        game = games_by_season[source_season.id].get(row.match_id)
        metadata = _match_file_metadata(session, series, source_season, game) if game and row.kind == 'match_records' else None
        fingerprint = (hashlib.sha256(metadata['markdown'].encode('utf-8')).hexdigest() if metadata else
                       policy_digest(session,source_season,game) if game else 'removed')
        summary_text = None
        if game and row.kind == 'record_summary':
            summary_text = record_update_text(record_changes(session, series, source_season, game), row.match_id) or f'🏅 Records · Match #{row.match_id}: earlier record claims withdrawn.'
            fingerprint = hashlib.sha256(summary_text.encode('utf-8')).hexdigest()
        if row.fingerprint is None:
            # First official completion anchors provisional announcements.
            if game is not None:
                row.fingerprint=fingerprint;row.fixture_id=game['fixture_id']
            continue
        if fingerprint==row.fingerprint: continue
        row.fingerprint=fingerprint;row.revision+=1;row.status='pending'
        if game and row.kind == 'match_records':
            row.text = _match_file_caption(series, row.match_id, corrected=True)
            row.metadata_json = json.dumps(metadata)
        elif game and row.kind == 'record_summary':
            row.text = summary_text
        elif game:
            row.text=recap_text(session,series,source_season,game,corrected=True)
            if row.kind!='recap':
                row.text=('SCORECARD CORRECTION — earlier provisional record claims are superseded by this verified summary.\n'+row.text)[:4000]
        else:
            row.text=f'RECORD UPDATE RETRACTED | {series.name}\nMatch #{row.match_id} was removed or is no longer eligible. Its earlier records and milestones no longer count.'
            if row.kind == 'match_records':
                metadata = json.loads(row.metadata_json or '{}')
                metadata['markdown'] = row.text
                row.metadata_json = json.dumps(metadata)
        count+=1
    session.flush()
    return count


async def dispatch(context,*,ids=None):
    """Deliver/revise committed publications. Telegram failures stay pending."""
    from database import get_session
    from models import LeagueRecordPublication
    def claim():
        s=get_session()
        try:
            stale=datetime.utcnow()-timedelta(minutes=5)
            s.query(LeagueRecordPublication).filter(LeagueRecordPublication.status=='sending',LeagueRecordPublication.updated_at<stale).update({'status':'pending'},synchronize_session=False)
            # Never send queued legacy season-report documents after this change.
            s.query(LeagueRecordPublication).filter_by(kind='match_records', status='pending').update({'status': 'cancelled'}, synchronize_session=False)
            q=s.query(LeagueRecordPublication).filter_by(status='pending').filter(~LeagueRecordPublication.kind.like('watch:%'), ~LeagueRecordPublication.kind.like('record:%')).order_by(LeagueRecordPublication.id)
            if ids is not None: q=q.filter(LeagueRecordPublication.id.in_(ids))
            result=[]
            for row in q.limit(50):
                n=s.query(LeagueRecordPublication).filter_by(id=row.id,status='pending',revision=row.revision).update({'status':'sending','updated_at':datetime.utcnow()},synchronize_session=False)
                if n:
                    result.append(dict(id=row.id,chat_id=row.chat_id,message_id=row.message_id,text=row.text,revision=row.revision,kind=row.kind,metadata=json.loads(row.metadata_json or '{}')))
            s.commit();return result
        except Exception:
            s.rollback();raise
        finally:s.close()
    def finish(item,message_id=None,failed=False):
        s=get_session()
        try:
            row=s.get(LeagueRecordPublication,item['id'])
            if row:
                if message_id is not None: row.message_id=message_id
                if not failed: row.delivered_revision=item['revision']
                row.status='pending' if failed or row.revision!=item['revision'] else 'sent'
            s.commit()
        finally:s.close()
    for item in await asyncio.to_thread(claim):
        try:
            if item['message_id']:
                try:
                    await context.bot.edit_message_text(chat_id=item['chat_id'],message_id=item['message_id'],text=item['text'])
                except Exception as exc:
                    error=str(exc).lower()
                    if any(reason in error for reason in ('message to edit not found',"message can't be edited",'message can not be edited')):
                        replacement=await context.bot.send_message(chat_id=item['chat_id'],text=item['text'])
                        item['message_id']=getattr(replacement,'message_id',None)
                    elif 'message is not modified' not in error:
                        raise
                message_id=item['message_id']
            else:
                message=await context.bot.send_message(chat_id=item['chat_id'],text=item['text'])
                message_id=getattr(message,'message_id',None)
            await asyncio.to_thread(finish,item,message_id)
            if not item['message_id'] and item['kind'].startswith('record:'):
                from services.event_media_service import fire_record_media
                await fire_record_media(context,item['chat_id'],item['metadata'].get('event','new_record'),item['metadata'].get('fields') or {})
        except Exception:
            await asyncio.to_thread(finish,item,failed=True)
            logger.exception('Record publication delivery failed for %s',item['id'])


async def watch(context,state):
    if not state.get('tournament_id') or not state.get('chat_id'):return
    def work():
        from database import get_session
        s=get_session()
        try:
            rows=queue_watch(s,state);ids=[r.id for r in rows];s.commit();return ids
        except Exception:s.rollback();raise
        finally:s.close()
    try:
        ids=await asyncio.to_thread(work)
        if ids:await dispatch(context,ids=ids)
    except Exception:logger.exception('Record watch failed')


async def publication_job(context):
    try:await dispatch(context)
    except Exception:logger.exception('Record publication job failed')
