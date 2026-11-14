"""Authenticated browser controls over the same draft engine and durable confirmations."""
import os
import re
import json
import secrets
from datetime import datetime,timedelta

from services import auction_service as A, franchise_draft as D, franchise_draft_rich as R,franchise_draft_admin as M

ADMIN_ACTIONS={'prices','custom','rounds','order','rules','team','removeteam','bind','clock','opening','purse','pooladd','poolremove','csv','importteams','retention','retain','unretain','start','pause','finish','undo','resolve','publish','continue','cancel','draftadmin_add','draftadmin_remove','adminpick','adminskip'}


def _admin(db,actor,trusted):
    from services.auction_webapp import AccessError
    if trusted:M.require_admin(db,actor,True)
    elif not D.is_draft_admin(db,actor):raise AccessError('Only Draft Admins can manage Franchise Draft settings.')


def _state(db,season,actor,manage):
    draft=D.get_draft(db,season)
    current=D.turn(db,season)
    result=dict(revision=draft.revision,rounds=draft.rounds,min_squad_size=season.min_squad_size,max_squad_size=draft.rounds,started=D.started(db,season),
                status=season.status,seconds=D.timer_seconds(draft),policy=D.timeout_policy_for(draft),
                deadline=season and draft.pick_deadline_at.isoformat()+'Z' if draft.pick_deadline_at else None,
                can_pick=bool(current and season.status==A.STATUS_LIVE and actor in D.owner_ids(current)),
                current_team=current.name if current else None,
                can_manage=manage,published=bool(season.published_at),fresh_squads=True)
    if manage:
        result.update(rating_prices=D._loads(draft.rating_prices_json,{}),custom_prices=D._loads(draft.custom_prices_json,{}),
            minimums=A.role_minimums(season),maximums=A.role_maximums(season),rating_rules=A.rating_rules(season),
            order=[t.id for t in D._ordered_teams(db,season)],retention_max=D._loads(draft.settings_json,{}).get('retention_max',0),
            opening_purse=season.opening_purse_lakh,chat_id=str(season.chat_id or ''))
    if season.published_at:
        from services import league_season_service as LS
        linked=LS.season_for_auction(db,season.id)
        tournament=LS.season_tournament(db,linked) if linked else None
        result['tournament_id']=tournament.id if tournament else None
    return result


def payload(db, season, result, request,*,actor=None,trusted_admin=False):
    from services.auction_webapp import integer, telegram_link
    manage=trusted_admin or D.is_draft_admin(db,actor)
    result['can_manage_draft']=manage
    from services.admin_ids import is_bot_admin
    result['can_manage_admins']=trusted_admin or is_bot_admin(actor)
    if result['can_manage_admins']:
        result['draft_admins']=[dict(id=str(row.tg_id),name=row.name or '') for row in D.draft_admins(db)]
    if manage:
        from services.franchise_team_import import sources
        result['sources']=sources(db,per_page=None)[0]
    username = (os.getenv('FRANCHISE_AUCTION_BOT_USERNAME') or os.getenv('FRANCHISE_BOT_USERNAME') or '').strip().lstrip('@')
    bot_url = f'https://t.me/{username}?start=draft_{season.id if season else 0}' if re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) else None
    result.update(draft_bot_url=bot_url, draft_group_url=telegram_link(season)
                  if season and str(season.chat_id or '').startswith('-100') else None)
    if season is None:
        return result
    if D.get_draft(db, season) is None:
        raise A.AuctionError('Choose a Franchise Draft season.')
    part = request.get('part', 'board')
    page = integer(request.get('page', 1), 'Page', 1, 100_000)
    config = D.get_draft(db, season)
    teams = A.franchises(db, season.id)
    result['draft']=_state(db,season,actor,manage)
    pages = 1
    if part == 'board':
        _, text = R.board(db, season)
    elif part == 'pool':
        pages = max(1, (len(D._available(db, season)) + 11) // 12)
        page = min(page, pages)
        _, text = R.pool(db, season, page)
    elif part == 'order':
        pages = (config.rounds + 2) // 3
        page = min(page, pages)
        _, text = R.order(db, season, page=1 + (page - 1) * 3)
    elif part == 'history':
        from models import AuctionEvent
        count = db.query(AuctionEvent).filter_by(season_id=season.id).count()
        pages = max(1, (count + 11) // 12)
        page = min(page, pages)
        _, text = R.history(db, season, page)
    elif part == 'squads':
        selected = request.get('team_id')
        team = next((t for t in teams if str(t.id) == str(selected)), None) if selected else next(iter(teams), None)
        if selected and team is None:
            raise A.AuctionError('Choose a franchise from this draft season.')
        _, text = R.squad(db, season, team) if team else R.Card('👥 Franchise squads').text('No franchises added yet.').pair()
        result['team_id'] = team.id if team else None
    elif part in ('purses', 'rules'):
        _, text = (R.purses if part == 'purses' else R.rules)(db, season)
    elif part=='me':
        own=next((t for t in teams if actor in D.owner_ids(t)),None)
        _,text=R.squad(db,season,own) if own else R.Card('💼 My Franchise').text('No owner or co-owner assignment for this season.').pair()
    elif part in ('settings','teams','readiness','sources','catalogue'):
        if not manage:
            from services.auction_webapp import AccessError
            raise AccessError('Only Draft Admins can open these management views.')
        if part=='readiness':
            result['readiness']=M.readiness(db,season)
            _,text=R.readiness(db,season,check=result['readiness'])
        else:_,text=R.Card('⚙️ Draft management',season.name).text('Configure and preview changes below. All signings and budgets use the existing CMU draft database.').pair()
        if part=='catalogue':
            from services import player_query
            from models import Player,AuctionLot
            filters=player_query.empty_filters()
            filters.update(rating_min=str(request.get('rating_min','')),rating_max=str(request.get('rating_max','')),
                           role=str(request.get('role','')),versions=[request['version']] if request.get('version') else [])
            query=player_query.master_player_query(db,filters)
            needle=str(request.get('query',''))[:100].strip()
            if needle:
                from sqlalchemy import or_
                clauses=[column.ilike('%'+needle+'%') for column in (Player.name,Player.country,Player.version)]
                if needle.isdigit() and int(needle)<2**63:clauses.append(Player.id==int(needle))
                query=query.filter(or_(*clauses))
            count=query.count();pages=max(1,(count+23)//24);page=min(page,pages)
            players=player_query.ordered(query).offset((page-1)*24).limit(24).all()
            lots={p.player_id:p for p in A.lots(db,season.id)}
            # Previous holders are information, never an eligibility filter.
            result['catalogue']=[dict(id=p.id,name=p.name,rating=p.rating,bat_rating=p.bat_rating,bowl_rating=p.bowl_rating,
                category=p.category,country=p.country,version=p.version or 'Base',pool_id=lots[p.id].id if p.id in lots else None,
                status=lots[p.id].status if p.id in lots else 'not_in_pool') for p in players]
            result['catalogue_count']=count
    elif part=='export':
        import csv,io
        from models import FranchiseDraftPick,AuctionLot
        output=io.StringIO();writer=csv.writer(output)
        writer.writerow(['Pick','Franchise','Player ID','Player','Version','Overall','Category','Country','Price (Cr)'])
        names={t.id:t.name for t in teams}
        for pick in db.query(FranchiseDraftPick).filter_by(season_id=season.id).order_by(FranchiseDraftPick.overall_no):
            lot=db.get(AuctionLot,pick.lot_id)
            if lot:writer.writerow([pick.overall_no,names.get(pick.franchise_id,''),lot.player_id,lot.name,lot.version,lot.rating,lot.category,lot.country,
                                    f'{pick.price_lakh//100}.{pick.price_lakh%100:02d}'])
        result.update(download=output.getvalue(),filename=f'cmu-draft-{season.id}.csv')
        _,text=R.Card('📥 Draft results').text('The results file is ready.').pair()
    else:
        raise A.AuctionError('Choose a draft board, pool, order, history, squads, purses or rules.')
    if part=='pool':
        available=D._available(db,season)
        needle=str(request.get('query','')).strip().casefold()
        if needle:available=[p for p in available if needle in f'{p.name} {p.version} {p.country} {p.player_id}'.casefold()]
        pages=max(1,(len(available)+11)//12);page=min(page,pages)
        result['pool_players']=[dict(id=p.id,name=p.name,version=p.version or 'Base',rating=p.rating,category=p.category,country=p.country,
                                    price_lakh=_price(db,season,p)) for p in available[(page-1)*12:page*12]]
    result.update(draft_html=text, draft_part=part, page=page, pages=pages,
                  teams=[dict(id=t.id, name=t.name,**(dict(owner_id=str(t.owner_tg_id or ''),owner_name=t.owner_name,co_owners=[str(v) for v in A.co_owner_ids(t)],
                       managers=[str(v) for v in A.team_manager_ids(t)],short_name=t.short_name,city=t.city,logo_url=t.logo_url,purse_lakh=t.purse_remaining_lakh) if manage else {})) for t in teams])
    return result


def _price(db,season,lot):
    try:return D.price_for(db,season,lot)
    except A.AuctionError:return None


def perform(db,season,actor,data,*,trusted_admin=False):
    """The authenticated HTTP boundary owns the transaction and admin-session identity."""
    from services.auction_webapp import integer,boolean,AccessError
    action=data.get('action')
    if action=='draft_resume' or (action=='draft_preview' and data.get('operation')=='start'
                                and season is not None and season.status==A.STATUS_PAUSED):
        _admin(db,actor,trusted_admin)
        if season is None or D.get_draft(db,season) is None:raise A.AuctionError('Select a Franchise Draft season.')
        M.resume(db,season,actor,trusted_admin=trusted_admin)
        return dict(message='Draft resumed. The current franchise is on the clock.')
    if action=='draft_create':
        _admin(db,actor,trusted_admin)
        from models import LeagueSeason,ChallengeLeague
        from services import league_season_service as LS,franchise_team_import as I
        name=str(data.get('name','')).strip()
        if not name:raise A.AuctionError('Enter a name for the draft season.')
        rounds=integer(data.get('rounds'),'Rounds',1,D.MAX_ROUNDS)
        minimum=integer(data['min_squad_size'],'Minimum squad size',1,rounds) if str(data.get('min_squad_size','')).strip() else None
        purse=D.parse_money(str(data.get('purse',''))+'cr')
        kind,source_id=I.selection(data['source']) if data.get('source') else (None,None)
        previous=db.query(LeagueSeason).filter_by(league_id=source_id).order_by(LeagueSeason.season_no.desc()).first() if kind=='league' else None
        if kind=='tournament':
            previous=db.query(LeagueSeason).filter_by(tournament_id=source_id).first()
            if previous and LS.latest_season(db,previous.series).id!=previous.id:previous=None
        by=None if trusted_admin else actor
        if previous:
            try:_,season=LS.start_franchise_draft(db,previous.series,name=name,purse_lakh=purse,rounds=rounds,by_tg_id=by)
            except LS.LeagueSeasonError as exc:raise A.AuctionError(str(exc)) from None
        else:
            season=A.create_season(db,name,opening_purse_lakh=purse)
            D.enable(db,season,rounds=rounds,min_squad_size=minimum,by_tg_id=by)
        if previous and minimum is not None:D.configure_rounds(db,season,rounds,min_squad_size=minimum,by_tg_id=by)
        if kind:I.apply(db,season,kind,source_id,actor=by)
        return dict(season_id=season.id,message='Draft created with empty squads. Previous-team players remain available in the catalogue.')
    if season is None or D.get_draft(db,season) is None:raise A.AuctionError('Select a Franchise Draft season.')
    if action=='draft_preview':
        _admin(db,actor,trusted_admin)
        kind=data.get('operation')
        if kind not in ADMIN_ACTIONS:raise A.AuctionError('Choose a draft management action.')
        values=data.get('values')
        if not isinstance(values,dict):raise A.AuctionError('Enter the action settings.')
        row=M.prepare(db,season,actor,kind,values,str(data.get('reason','')),trusted_admin=trusted_admin)
        _,text=R.preview_action(db,season,row)
        return dict(confirmation=dict(type='admin',token=row.token,html=text))
    if action=='draft_answer':
        _admin(db,actor,trusted_admin)
        row=M.answer(db,season,actor,str(data.get('token','')),accept=boolean(data.get('accept'),'Confirmation'),trusted_admin=trusted_admin)
        return dict(message='Configuration applied and audited.' if row.status=='applied' else 'Configuration cancelled.')
    if trusted_admin:raise AccessError('Owner picks require the owner’s or co-owner’s verified Telegram sign-in.')
    if action=='draft_pick':
        query=str(data.get('player','')).strip()
        try:row=D.preview_pick(db,season,actor,query)
        except D.AmbiguousDraftPlayer as exc:
            _,text=R.player_choices(db,season,exc.players)
            return dict(selection=dict(html=text,players=[dict(id=p.id,name=p.name,version=p.version or 'Base') for p in exc.players[:12]]))
        from models import AuctionLot,AuctionFranchise
        _,text=R.confirmation(db,season,row,db.get(AuctionLot,row.lot_id),db.get(AuctionFranchise,row.franchise_id))
        return dict(confirmation=dict(type='pick',token=row.token,html=text))
    if action=='draft_pick_answer':
        accept=boolean(data.get('accept'),'Confirmation');token=str(data.get('token',''))
        D.confirm_pick(db,season,token,actor) if accept else D.cancel_pick(db,season,token,actor)
        return dict(message='Signing confirmed. The next franchise will be announced in Telegram.' if accept else 'Selection cancelled; purse unchanged.')
    if action in ('draft_skip','draft_skip_answer'):
        from models import FranchiseDraftAction
        draft=D.get_draft(db,season)
        D._require_owner(D.turn(db,season),actor)
        if action=='draft_skip':
            if season.status!=A.STATUS_LIVE:raise A.AuctionError('The draft is not live.')
            row=FranchiseDraftAction(token=secrets.token_hex(12),season_id=season.id,actor_tg_id=actor,revision=draft.revision,
                kind='owner_skip',payload_json='{}',preview_json='{}',status='pending')
            db.add(row);db.flush()
            return dict(confirmation=dict(type='skip',token=row.token,html='<b>Skip this turn?</b><br>Your place enters the catch-up queue. No purse is deducted.'))
        row=db.get(FranchiseDraftAction,str(data.get('token','')))
        if row is None or row.kind!='owner_skip' or row.season_id!=season.id or row.actor_tg_id!=actor or row.status!='pending' or datetime.utcnow()-row.created_at>timedelta(minutes=10):
            raise A.AuctionError('This skip confirmation is expired or belongs to another owner.')
        accept=boolean(data.get('accept'),'Confirmation')
        if accept and row.revision!=draft.revision:raise A.AuctionError('The turn changed; refresh the draft board.')
        changed=db.query(FranchiseDraftAction).filter_by(token=row.token,status='pending').update({'status':'applied' if accept else 'cancelled'},synchronize_session=False)
        if changed!=1:raise A.AuctionError('This skip was already answered.')
        if accept:D.skip_turn(db,season,actor)
        return dict(message='Turn deferred to the catch-up queue.' if accept else 'Skip cancelled.')
    raise A.AuctionError('Choose a valid draft action.')
