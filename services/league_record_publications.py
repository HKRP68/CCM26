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
                    previous=R.format_value(prior) if prior is not None else 'first recorded mark',
                    previous_holders=list(dict.fromkeys(e['name'] for e in old['holders']))))
    return result


def recap_text(session,series,season,game,*,corrected=False):
    from services import league_records as R, league_season_service as LS
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
    elif row.text!=text[:4000]:
        row.text=text[:4000];row.revision+=1;row.status='pending'
        row.fingerprint=fingerprint or row.fingerprint
    if metadata is not None:row.metadata_json=json.dumps(metadata)
    return row


def queue_watch(session,state):
    from services import league_records as R, league_record_insights as I, league_season_service as LS
    season=LS.season_for_tournament(session,state.get('tournament_id')) if state.get('tournament_id') else None
    if not season or not state.get('match_id') or not state.get('chat_id') or not R.config(session,season).alerts_enabled or not options(session,season)['watch']:
        return []
    series=LS.series_of(session,season)
    from models import LeagueRecordPublication
    prefix=f"watch:{int(state.get('innings') or 1)}:"
    prior=session.query(LeagueRecordPublication).filter_by(season_id=season.id,match_id=int(state['match_id']),chat_id=int(state['chat_id'])).filter(LeagueRecordPublication.kind.like(prefix+'%')).all()
    seen={key for row in prior for key in json.loads(row.metadata_json or '{}').get('watch_keys',[])}
    items=[item for item in I.watch_list(session,series,season,state=state) if item['key'] not in seen]
    if not items:return []
    keys=[item['key'] for item in items]
    token=hashlib.sha256('|'.join(sorted(keys)).encode()).hexdigest()[:24]
    text=f"RECORD WATCH | {series.name} | Match #{state['match_id']}\n"+'\n'.join(item['text'] for item in items)
    return [queue(session,season,state['match_id'],state['chat_id'],prefix+token,text,metadata=dict(watch_keys=keys))]



def queue_recap(session,state):
    from services import league_records as R, league_season_service as LS
    season=LS.season_for_tournament(session,state.get('tournament_id')) if state.get('tournament_id') else None
    if not season or not state.get('chat_id') or not options(session,season)['recap']:
        return None
    game=next((g for g in R.evidence(session,season) if g['match_id']==state.get('match_id')),None)
    if game is None: return None
    return queue(session,season,state['match_id'],state['chat_id'],'recap',
                 recap_text(session,LS.series_of(session,season),season,game),fixture_id=game['fixture_id'],fingerprint=policy_digest(session,season,game))


def reconcile(session,tournament_id):
    """Called inside a scorecard transaction. Only queue edits; never send."""
    from models import LeagueRecordPublication
    from services import league_records as R, league_season_service as LS
    season=LS.season_for_tournament(session,tournament_id)
    if season is None: return 0
    series=LS.series_of(session,season)
    games={g['match_id']:g for g in R.evidence(session,season)}
    count=0
    for row in session.query(LeagueRecordPublication).filter_by(season_id=season.id):
        if row.kind.startswith('watch:'): continue
        game=games.get(row.match_id)
        fingerprint=policy_digest(session,season,game) if game else 'removed'
        if row.fingerprint is None:
            # First official completion anchors provisional announcements.
            if game is not None:
                row.fingerprint=fingerprint;row.fixture_id=game['fixture_id']
            continue
        if fingerprint==row.fingerprint: continue
        row.fingerprint=fingerprint;row.revision+=1;row.status='pending'
        if game:
            row.text=recap_text(session,series,season,game,corrected=True)
            if row.kind!='recap':
                row.text=('SCORECARD CORRECTION — earlier provisional record claims are superseded by this verified summary.\n'+row.text)[:4000]
        else:
            row.text=f'RECORD UPDATE RETRACTED | {series.name}\nMatch #{row.match_id} was removed or is no longer eligible. Its earlier records and milestones no longer count.'
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
            q=s.query(LeagueRecordPublication).filter_by(status='pending').order_by(LeagueRecordPublication.id)
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
