"""Import historical franchise identities into the existing draft's setup rows."""
import hashlib
import json

from models import AuctionSeason, ChallengeLeague, ChallengeTeam, Tournament, TournamentTeam, PlayerDraft, DraftTeam
from services import auction_service as A, franchise_draft as D


def sources(db, page=1, per_page=8):
    page=max(1,int(page))
    rows=[];more=False
    for kind,model in (('league',ChallengeLeague),('tournament',Tournament)):
        query=db.query(model).order_by(model.id.desc())
        batch=query.offset((page-1)*per_page).limit(per_page+1).all() if per_page else query.all()
        more=more or bool(per_page and len(batch)>per_page)
        rows.extend(dict(kind=kind,id=row.id,name=row.name) for row in (batch[:per_page] if per_page else batch))
    return rows,more


def selection(raw):
    parts=str(raw or '').split(':')
    if len(parts)!=2 or parts[0] not in ('league','tournament') or not parts[1].isdigit() or not 0<int(parts[1])<2**63:
        raise A.AuctionError('Choose a previous tournament or league from the source list.')
    return parts[0],int(parts[1])


def _id(value):
    if isinstance(value,bool) or not str(value).isdigit() or not 0<int(value)<10**16:
        raise A.AuctionError('Source Telegram IDs must be positive whole numbers. Correct the source ownership first.')
    return int(value)


def _ids(raw):
    if raw is None:return None
    try:values=json.loads(raw)
    except (ValueError,TypeError):raise A.AuctionError('The source has an invalid co-owner or manager list.') from None
    if not isinstance(values,list):raise A.AuctionError('The source has an invalid co-owner or manager list.')
    return list(dict.fromkeys(_id(value) for value in values))


def _source(db,kind,source_id):
    if kind not in ('league','tournament'):raise A.AuctionError('Choose a previous league or tournament.')
    if isinstance(source_id,bool) or not str(source_id).isdigit() or not 0<int(source_id)<2**63:
        raise A.AuctionError('Choose a valid source database ID.')
    source=db.get(ChallengeLeague if kind=='league' else Tournament,int(source_id))
    if source is None:raise A.AuctionError('That previous league or tournament no longer exists.')
    league_id=source.id if kind=='league' else source.league_id
    published=db.query(AuctionSeason).filter_by(league_id=league_id).order_by(AuctionSeason.id.desc()).first() if league_id else None
    old=A.franchises(db,published.id) if published else []
    if not old and league_id:
        draft=db.query(PlayerDraft).filter_by(league_id=league_id).order_by(PlayerDraft.id.desc()).first()
        old=db.query(DraftTeam).filter_by(draft_id=draft.id).all() if draft else []
    league_teams={t.id:t for t in db.query(ChallengeTeam).filter_by(league_id=league_id).all()} if league_id else {}
    played={}
    if kind=='league':
        entries=(db.query(TournamentTeam).join(Tournament,Tournament.id==TournamentTeam.tournament_id)
                 .filter(Tournament.league_id==league_id).order_by(Tournament.id.desc(),TournamentTeam.id.desc()).all())
        for entry in entries:played.setdefault(entry.challenge_team_id,entry)
        entries=sorted(league_teams.values(),key=lambda t:(t.sort_order,t.id))
    else:
        entries=db.query(TournamentTeam).filter_by(tournament_id=source.id).order_by(TournamentTeam.id).all()
    rows=[]
    for entry in entries:
        team=entry if kind=='league' else league_teams.get(entry.challenge_team_id)
        if kind=='tournament' and league_id and entry.challenge_team_id and team is None:
            raise A.AuctionError('A tournament team belongs to another league. Correct its source link before importing.')
        tournament_team=played.get(entry.id) if kind=='league' else entry
        name=(entry.name or '').strip()
        matches=[f for f in old if (f.name or '').strip().casefold() in {name.casefold(),(team.name or '').strip().casefold() if team else ''}]
        if len(matches)>1:raise A.AuctionError(f'Ambiguous historical franchise for {name}. Correct the source names first.')
        previous=next(iter(matches),None)
        explicit=tournament_team and (tournament_team.owner_tg_id is not None or tournament_team.co_owner_ids_json is not None or tournament_team.user_tg_id)
        owner_source=tournament_team if explicit else previous
        owner=(owner_source.owner_tg_id if owner_source.owner_tg_id is not None else getattr(owner_source,'user_tg_id',None)) if owner_source else None
        owner=_id(owner) if owner is not None else None
        co=_ids(owner_source.co_owner_ids_json) if owner_source else None
        if co is not None:co=[value for value in co if value!=owner]
        managers=_ids(getattr(previous,'team_manager_ids_json',None)) if previous else None
        if explicit and previous and owner!=previous.owner_tg_id:managers=[]
        rows.append(dict(source_team_id=entry.id,previous_team_id=team.id if team else None,
            carried_from_id=previous.id if published and previous else None,name=name,
            short_name=entry.short_name or (team.short_name if team else None) or getattr(previous,'short_name',None),
            logo_url=entry.logo_url or (team.logo_url if team else None) or getattr(previous,'logo_url',None),
            city=getattr(previous,'city',None),owner_tg_id=owner,
            owner_name=owner_source.owner_name if owner_source else None,
            co_owner_ids_json=json.dumps(co) if co is not None else None,
            team_manager_ids_json=json.dumps(managers) if managers is not None else None))
    if not rows:raise A.AuctionError('That source has no teams to import.')
    names=[r['name'].casefold() for r in rows]
    if any(not n for n in names) or len(names)!=len(set(names)):
        raise A.AuctionError('The source contains missing or duplicate team names. Correct it before importing.')
    return dict(kind=kind,id=source.id,name=source.name,league_id=league_id,
                previous_season_id=published.id if published else None,teams=rows)


def preview(db,season,kind,source_id):
    draft=D.get_draft(db,season)
    if draft is None:raise A.AuctionError('Select a Franchise Draft season.')
    if season.league_id or season.published_at or season.status!=A.STATUS_SETUP or D.started(db,season):
        raise A.AuctionError('Import franchises during draft setup, before the draft starts.')
    source=_source(db,kind,source_id)
    if season.previous_league_id and source['league_id'] and season.previous_league_id!=source['league_id']:
        raise A.AuctionError('This draft already follows a different league. Choose its previous league or a tournament from that league.')
    teams=A.franchises(db,season.id)
    for team in teams:
        try:
            if team.owner_tg_id is not None:_id(team.owner_tg_id)
            _ids(team.co_owner_ids_json);_ids(team.team_manager_ids_json)
        except A.AuctionError:
            raise A.AuctionError(f'{team.name} has invalid ownership details. Correct its owner/co-owner/manager IDs before importing.') from None
    origins=D._loads(draft.settings_json,{}).get('team_imports',{})
    rows=[];claimed=set()
    fields=('short_name','logo_url','city','owner_tg_id','owner_name','co_owner_ids_json','team_manager_ids_json')
    for incoming in source['teams']:
        identity=dict(kind=kind,source_id=source['id'],source_team_id=incoming['source_team_id'])
        found=[t for t in teams if origins.get(str(t.id))==identity or
               (isinstance(origins.get(str(t.id)),list) and identity in origins[str(t.id)]) or
               (source['league_id'] and season.previous_league_id==source['league_id'] and incoming['previous_team_id'] and t.previous_team_id==incoming['previous_team_id'])]
        if not found:found=[t for t in teams if t.name.strip().casefold()==incoming['name'].casefold()]
        if len(found)>1:raise A.AuctionError(f'Several target franchises match {incoming["name"]}. Resolve the duplicate links first.')
        target=next(iter(found),None)
        if target and target.id in claimed:raise A.AuctionError('Two source teams map to the same target franchise. Correct the source first.')
        if target:claimed.add(target.id)
        patch={};warnings=[]
        different_owner=target and target.owner_tg_id and target.owner_tg_id!=incoming['owner_tg_id']
        for field in fields:
            value=incoming[field]
            if value is None:continue
            if target is None or getattr(target,field) in (None,''):
                if different_owner and field in ('owner_name','co_owner_ids_json','team_manager_ids_json'):continue
                patch[field]=value
            elif getattr(target,field)!=value and field in ('owner_tg_id','co_owner_ids_json','team_manager_ids_json'):
                warnings.append(f'Existing {field.replace("_ids_json", "s").replace("_tg_id", "").replace("_", " ")} preserved')
        final={field:(patch[field] if field in patch else getattr(target,field) if target else None) for field in fields}
        if not final['owner_tg_id']:warnings.append('Owner missing: assign one before starting')
        rows.append(dict(name=target.name if target else incoming['name'],target_id=target.id if target else None,
                         incoming=incoming,identity=identity,patch=patch,final=final,warnings=warnings,
                         operation='Add' if target is None else 'Fill missing details' if patch else 'Keep existing'))
    result=dict(source=source,rows=rows,revision=draft.revision,added=sum(r['target_id'] is None for r in rows))
    state=dict(source=source,rows=rows,settings=draft.settings_json,order=draft.order_json,
               previous_league=season.previous_league_id,previous_season=season.previous_season_id,
               purse=season.opening_purse_lakh,
               targets=[(t.id,t.name,t.previous_team_id,t.carried_from_id,t.sort_order,t.purse_total_lakh,t.purse_remaining_lakh,t.squad_size,
                         *[getattr(t,f) for f in fields]) for t in teams])
    result['fingerprint']=hashlib.sha256(json.dumps(state,sort_keys=True,default=str).encode()).hexdigest()
    return result


def apply(db,season,kind,source_id,*,fingerprint=None,revision=None,actor=None):
    """Apply under the existing draft lock; the caller commits or rolls back."""
    if actor is not None:D.require_draft_admin(db,actor)
    draft=D.get_draft(db,season)
    if draft is None:raise A.AuctionError('Select a Franchise Draft season.')
    if revision is not None and int(draft.revision)!=int(revision):raise A.AuctionError('The draft changed. Preview the import again.')
    D._claim(db,season,draft)
    if actor is not None:D.require_draft_admin(db,actor)
    db.expire_all()
    plan=preview(db,season,kind,source_id)
    if fingerprint and plan['fingerprint']!=fingerprint:raise A.AuctionError('The source or draft changed. Preview the import again.')
    order=[t.id for t in D._ordered_teams(db,season)]
    source=plan['source']
    existing={t.id:t for t in A.franchises(db,season.id)}
    maximum=max((t.sort_order for t in existing.values()),default=0)
    payload=[]
    for i,row in enumerate(plan['rows'],1):
        target=existing.get(row['target_id'])
        item=dict(name=row['name'],purse_total_lakh=target.purse_total_lakh if target else int(season.opening_purse_lakh or 0),
                  sort_order=target.sort_order if target else maximum+i)
        item.update({k:v for k,v in row['patch'].items() if k not in ('co_owner_ids_json','team_manager_ids_json')})
        for field,key in (('co_owner_ids_json','co_owner_tg_ids'),('team_manager_ids_json','team_manager_tg_ids')):
            if field in row['patch']:item[key]=_ids(row['patch'][field])
        payload.append(item)
    A.import_franchises(db,season,{'franchises':payload},by_tg_id=actor)
    options=D._loads(draft.settings_json,{})
    origins=options.setdefault('team_imports',{})
    by_name={t.name.strip().casefold():t for t in A.franchises(db,season.id)}
    if not season.previous_league_id:season.previous_league_id=source['league_id']
    if not season.previous_season_id:season.previous_season_id=source['previous_season_id']
    for row in plan['rows']:
        team=by_name[row['name'].casefold()]
        if row['incoming']['previous_team_id'] and not team.previous_team_id:team.previous_team_id=row['incoming']['previous_team_id']
        if row['incoming']['carried_from_id'] and not team.carried_from_id:team.carried_from_id=row['incoming']['carried_from_id']
        team.draft_picks_total=draft.rounds-int(team.retained_count or 0)
        previous=origins.get(str(team.id),[])
        previous=previous if isinstance(previous,list) else [previous]
        if row['identity'] not in previous:previous.append(row['identity'])
        origins[str(team.id)]=previous
        if team.id not in order:order.append(team.id)
    draft.settings_json=D._dump(options);draft.order_json=D._dump(order)
    if source['league_id']:A.link_previous_season(db,season,source['league_id'])
    D._event(db,season,'fd_teams_imported',f'🏟 <b>Franchises imported</b>\n'
             f'{D._e(source["name"])} · {plan["added"]} added · {len(plan["rows"])-plan["added"]} existing preserved.\n'
             'Owners, co-owners and available team details copied. Draft purses and player-retention rules apply.',
             by_tg_id=actor,metadata={'source':source,'import':plan})
    db.flush()
    return plan
