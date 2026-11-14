"""Draft 2.0 administration on the existing draft, pool, ledger and season rows.

Every preview runs the real operation in a rolled-back savepoint. Confirmation
rechecks current authority, the durable quote, revision and financial snapshot.
The caller owns the outer transaction, exactly as for owner pick confirmations.
"""
from datetime import datetime,timedelta
import hashlib
import json
import re
import secrets

from models import AuctionLot,AuctionFranchise,Player,FranchiseDraftAction
from services import auction_service as A,franchise_draft as D


def require_admin(db,actor,trusted_admin=False):
    if trusted_admin:
        if not isinstance(actor,int) or isinstance(actor,bool) or actor>=0:
            raise A.AuctionError('Invalid website administrator session.')
    else:D.require_draft_admin(db,actor)


def fingerprint(db,season):
    draft=D.get_draft(db,season)
    state=dict(season=[season.id,season.name,season.chat_id,season.currency_label,season.previous_league_id,season.league_id,season.published_at,season.status],
        config=[draft.rounds,draft.order_json,draft.rating_prices_json,draft.custom_prices_json,draft.settings_json],
        rules=[season.min_squad_size,season.max_squad_size,season.role_minimums_json,season.role_maximums_json,season.rating_rules_json],
        teams=[(t.id,t.name,t.short_name,t.city,t.logo_url,t.owner_tg_id,t.owner_name,t.co_owner_ids_json,t.team_manager_ids_json,t.purse_total_lakh,t.purse_remaining_lakh,t.squad_size) for t in A.franchises(db,season.id)],
        pool=[(p.id,p.player_id,p.name,p.version,p.country,p.rating,p.bat_rating,p.bowl_rating,p.category,p.status,p.sold_to_id,p.sold_price_lakh) for p in A.lots(db,season.id)])
    return hashlib.sha256(json.dumps(state,sort_keys=True,default=str).encode()).hexdigest()


def validate_final(db,season):
    draft=D.get_draft(db,season)
    for team in A.franchises(db,season.id):
        lots=A.squad(db,team.id)
        if not season.min_squad_size <= len(lots) <= draft.rounds or team.squad_size!=len(lots):
            raise A.AuctionError(f'{team.name} has an incomplete squad. Resolve remaining picks first.')
        short,over=A.role_shortfall(db,season,team)
        if short or over or team.purse_remaining_lakh<0:
            raise A.AuctionError(f'{team.name} does not meet the final purse/category rules.')
        for rule in A.rating_rules(season):
            if sum(p.rating<=rule['max_rating'] for p in lots)<rule['min_players']:
                raise A.AuctionError(f'{team.name} does not meet the final rating rules.')
        if len({p.player_id for p in lots})!=len(lots):
            raise A.AuctionError('Duplicate player assignments must be repaired before publication.')


def summary(db,season):
    draft=D.get_draft(db,season)
    teams=A.franchises(db,season.id)
    current=D.turn(db,season)
    return dict(rounds=draft.rounds,min_squad_size=season.min_squad_size,max_squad_size=draft.rounds,eligible=len(D._available(db,season)),status=season.status,
        name=season.name,chat_id=season.chat_id,published=bool(season.published_at),opening_purse=season.opening_purse_lakh,
        order=[t.name for t in D._ordered_teams(db,season)],
        minimums=A.role_minimums(season),maximums=A.role_maximums(season),rating_rules=A.rating_rules(season),
        timer_seconds=D.timer_seconds(draft),current=current.name if current is not None else None,
        settings=D._loads(draft.settings_json,{}),
        rating_prices=D._loads(draft.rating_prices_json,{}),
        custom_prices=D._loads(draft.custom_prices_json,{}),
        teams=[dict(id=t.id,name=t.name,owner=t.owner_tg_id,owner_name=t.owner_name,co_owners=A.co_owner_ids(t),managers=A.team_manager_ids(t),short_name=t.short_name,city=t.city,logo_url=t.logo_url,squad=t.squad_size,retained=t.retained_count or 0,
                    slots=max(0,draft.rounds-t.squad_size),purse=t.purse_remaining_lakh,
                    spent=t.purse_total_lakh-t.purse_remaining_lakh) for t in teams])


def readiness(db,season):
    transaction=db.begin_nested()
    try:
        result=_readiness(db,season)
    finally:
        transaction.rollback()
    db.expire_all()
    return result


def _readiness(db,season):
    draft=D.get_draft(db,season)
    errors=[]
    teams=A.franchises(db,season.id)
    if not season.chat_id:errors.append('Bind a Telegram draft group.')
    if len(teams)<2:errors.append('Add at least two franchises.')
    for team in teams:
        if not team.owner_tg_id or not 0<int(team.owner_tg_id)<10**16:errors.append(f'{team.name}: assign a valid owner.')
        if team.purse_remaining_lakh<0:errors.append(f'{team.name}: negative purse.')
    available=D._available(db,season)
    if len({p.player_id for p in A.lots(db,season.id)})!=len(A.lots(db,season.id)):
        errors.append('Duplicate player database IDs in the season pool.')
    for lot in available:
        try:D.price_for(db,season,lot)
        except A.AuctionError as exc:errors.append(str(exc))
    if not errors:
        try:
            A.set_role_rules(db,season,A.role_minimums(season),A.role_maximums(season))
            A.set_rating_rules(db,season,A.rating_rules(season))
            D._validate_pool(db,season,draft,teams,available)
        except A.AuctionError as exc:errors.append(str(exc))
    return dict(ready=not errors,errors=errors,**summary(db,season))


def select_players(db,raw):
    pieces=[p.strip() for p in raw.split('|')]
    first=pieces[0]
    from services import player_query
    query=player_query.master_player_query(db)
    if first.casefold()=='all':
        players=query.order_by(Player.id).limit(20001).all()
        if not players or len(players)>20000:raise A.AuctionError('Import at most 20,000 active players at once; narrow the filters for a larger catalogue.')
        return players
    if first.casefold().startswith('ids '):
        try:ids=[int(p.lstrip('#')) for p in re.split(r'[,\s]+',first[4:].strip()) if p]
        except ValueError:raise A.AuctionError('Use ids 12,34,56 (catalogue database IDs).') from None
        players=query.filter(Player.id.in_(ids)).all()
        if set(ids)!={p.id for p in players}:raise A.AuctionError('Some catalogue IDs are unknown or inactive.')
    else:
        match=re.fullmatch(r'(\d{1,3})\s*-\s*(\d{1,3})',first)
        if not match or not 1<=int(match[1])<=int(match[2])<=999:
            raise A.AuctionError('Use ids 12,34 or 80-96 | BAT | Base. Category/version are optional.')
        query=query.filter(Player.rating.between(int(match[1]),int(match[2])))
        if len(pieces)>1 and pieces[1] and pieces[1]!='*':
            roles=A.normalize_draft_roles({pieces[1]:1})
            query=query.filter(Player.category.in_(list(roles)))
        if len(pieces)>2 and pieces[2] and pieces[2]!='*':
            from sqlalchemy import func,or_
            version=pieces[2].lower()
            query=query.filter(or_(func.lower(Player.version)==version,Player.version.is_(None)) if version=='base' else func.lower(Player.version)==version)
        players=query.order_by(Player.id).limit(2001).all()
    if not players or len(players)>2000:raise A.AuctionError('Select between 1 and 2,000 players. Narrow the filters.')
    return players


def perform(db,season,actor,kind,payload,reason,*,trusted_admin=False):
    draft=D.get_draft(db,season)
    require_admin(db,actor,trusted_admin)
    from services.admin_ids import is_bot_admin
    global_admin=trusted_admin or is_bot_admin(actor)
    audit_actor=actor
    actor=None if trusted_admin else actor
    if kind in ('adminpick','adminskip'):
        from services.auction_webapp import integer
        target=integer(payload.get('team_id'),'Franchise ID',1)
        if not reason.strip():raise A.AuctionError('Record a reason for acting on behalf of a franchise.')
        if season.published_at or season.league_id:raise A.AuctionError('Published squads are locked.')
        if season.status not in (A.STATUS_LIVE,A.STATUS_PAUSED):raise A.AuctionError('Start the draft before acting for a team.')
        D._claim(db,season)
        if season.published_at or season.league_id or season.status not in (A.STATUS_LIVE,A.STATUS_PAUSED):
            raise A.AuctionError('The draft changed. Review its current status before acting.')
        current=D.turn(db,season)
        if current is None or current.id!=target:
            raise A.AuctionError('That franchise is not on the clock. Review /draft for the current team.')
        if kind=='adminpick':
            # Quotes are created and consumed inside this transaction. Only the
            # outer, actor-bound administrator action is ever exposed to clients.
            quote=D.preview_pick(db,season,audit_actor,str(payload.get('player','')).strip(),_admin_reason=reason,_trusted_admin=trusted_admin)
            D.confirm_pick(db,season,quote.token,audit_actor,_admin_reason=reason,_trusted_admin=trusted_admin)
        else:
            D._skip_locked(db,season,actor=audit_actor,admin_reason=reason)
            if season.status==A.STATUS_PAUSED:draft.paused_seconds_left=D.timer_seconds(draft) or None
    elif kind in ('draftadmin_add','draftadmin_remove'):
        if not global_admin:raise A.AuctionError('Only Bot Owners, Bot Admins or website administrators can appoint Draft Admins.')
        from services.auction_webapp import integer
        target=integer(payload.get('telegram_id'),'Draft Admin Telegram ID',1,10**16)
        if kind=='draftadmin_add':D.add_draft_admin(db,target,name=payload.get('name'),by_tg_id=actor)
        else:D.remove_draft_admin(db,target,by_tg_id=actor)
    elif kind in ('prices','custom'):
        text=payload['text']
        if kind=='custom' and text.casefold().startswith('remove '):
            D._price_editable(db,season,reason)
            D._claim(db,season)
            prices=D._loads(draft.custom_prices_json,{})
            for name in text[7:].splitlines():
                lot=D.find_player(db,season,name.strip())
                prices.pop(str(lot.id),None)
            draft.custom_prices_json=D._dump(prices)
        else:
            fn=D.set_rating_prices if kind=='prices' else D.set_custom_prices
            fn(db,season,text,by_tg_id=actor,correction_reason=reason)
        # Preview reports every unresolved eligible rating before acceptance.
        for lot in D._available(db,season):D.price_for(db,season,lot)
    elif kind=='rounds':D.configure_rounds(db,season,payload.get('max_squad_size',payload.get('rounds')),
        min_squad_size=payload.get('min_squad_size') if str(payload.get('min_squad_size','')).strip() else None,by_tg_id=actor)
    elif kind=='finish':D.finish(db,season,by_tg_id=actor)
    elif kind=='undo':D.undo_last(db,season,by_tg_id=actor)
    elif kind=='pause':D.pause(db,season,by_tg_id=actor)
    elif kind=='rules':
        from services.franchise_bot_settings import parse_value
        values={key:parse_value(field,payload[key]) if str(payload.get(key,'')).strip().casefold()!='clear' else ([] if key=='ratings' else {})
                for key,field in (('minimums','role_minimums'),('maximums','role_maximums'),('ratings','rating_rules')) if key in payload}
        D.update_squad_rules(db,season,by_tg_id=actor,**values)
    elif kind=='order':
        from handlers.franchise_draft import parse_order
        text=payload['text'].strip()
        D.set_order(db,season,parse_order(db,season,text),randomize=text.casefold()=='random',by_tg_id=actor)
    elif kind in ('publish','continue'):
        from services import league_season_service as LS
        validate_final(db,season)
        if season.status!=A.STATUS_COMPLETED:raise A.AuctionError('Complete every draft turn before finalizing squads.')
        linked=LS.season_for_auction(db,season.id)
        try:
            if kind=='publish':
                if season.published_at:raise A.AuctionError('These squads are already finalized.')
                LS.publish_season_squads(db,linked) if linked else A.publish_to_league(db,season)
            else:
                if not season.published_at:raise A.AuctionError('Finalize and publish squads before continuing the season.')
                if linked is None:
                    from models import LeagueSeason
                    series=LS.create_series(db,payload.get('competition_name') or f'{season.name} · Competition {season.id}')
                    linked=LeagueSeason(series_id=series.id,season_no=1,squad_mode=LS.MODE_DRAFT,league_id=season.league_id,auction_season_id=season.id)
                    db.add(linked);db.flush()
                LS.create_season_tournament(db,linked,name=payload.get('name'),activate=False)
        except LS.LeagueSeasonError as exc:raise A.AuctionError(str(exc)) from None
    elif kind=='team':
        D._editable(db,season);D._claim(db,season);D._editable(db,season)
        from services.auction_webapp import integer
        def ids(text):return [] if not str(text or '').strip() else [integer(v,'Telegram ID',1,10**16) for v in re.split(r'[,\s]+',str(text).strip())]
        team=db.get(AuctionFranchise,integer(payload['team_id'],'Franchise ID',1)) if payload.get('team_id') else None
        if team is not None and team.season_id!=season.id:raise A.AuctionError('Choose a franchise from this season.')
        if payload.get('team_id') and team is None:raise A.AuctionError('That franchise no longer exists.')
        owner=integer(payload.get('owner_id'),'Owner ID',1,10**16)
        if team is None:
            if D.started(db,season):raise A.AuctionError('Add franchises before draft launch.')
            team=D.add_team(db,season,payload['name'],owner,co_owners=ids(payload.get('co_owners')),by_tg_id=actor)
        else:
            D.update_team_roles(db,season,team,owner_tg_id=owner,co_owners=ids(payload.get('co_owners')),by_tg_id=actor)
        for key,limit in (('name',120),('short_name',30),('city',120),('owner_name',120),('logo_url',500)):
            if key in payload:
                value=str(payload[key] or '').strip()
                if key=='name' and not value:raise A.AuctionError('A franchise needs a name.')
                if key=='name' and any(t.id!=team.id and t.name.casefold()==value.casefold() for t in A.franchises(db,season.id)):
                    raise A.AuctionError('That franchise name is already in this draft.')
                if key=='logo_url' and value and not re.match(r'^https?://',value):raise A.AuctionError('Use an HTTPS or HTTP logo URL.')
                setattr(team,key,value[:limit] or None)
        A.set_team_managers(db,team,ids(payload.get('managers')))
    elif kind=='removeteam':
        if D.started(db,season):raise A.AuctionError('Remove franchises before draft launch.')
        D._editable(db,season);D._claim(db,season)
        team=db.get(AuctionFranchise,int(payload['team_id']))
        if team is None or team.season_id!=season.id:raise A.AuctionError('Choose a franchise from this draft.')
        if payload.get('name')!=team.name:raise A.AuctionError('Type the franchise name to confirm removal.')
        order=[t.id for t in D._ordered_teams(db,season) if t.id!=team.id]
        A.remove_franchise(db,season,team,by_tg_id=actor)
        draft.order_json=D._dump(order)
    elif kind=='importteams':
        from services import franchise_team_import as I
        I.apply(db,season,payload['source_kind'],payload['source_id'],fingerprint=payload['fingerprint'],actor=actor)
    elif kind=='start':
        check=readiness(db,season)
        if not check['ready']:raise A.AuctionError('\n'.join(check['errors'][:20]))
        D.start(db,season,by_tg_id=actor)
    else:
        D._editable(db,season)
        D._claim(db,season)
        D._editable(db,season)
        if kind=='resolve':
            if season.status!=A.STATUS_PAUSED or payload.get('operation')!='skip':
                raise A.AuctionError('Pause first. Administrator resolution supports an explicitly approved skip.')
            D._skip_locked(db,season,actor=actor)
            draft.paused_seconds_left=D.timer_seconds(draft) or None
        elif kind=='clock':
            seconds=int(payload['seconds']);policy=payload['policy']
            if not 0<=seconds<=86400 or policy not in ('pause','skip','auto','admin'):
                raise A.AuctionError('Use 0–86400 seconds and pause, skip, auto or admin.')
            options=D._loads(draft.settings_json,{})
            options.update(pick_seconds=seconds,timeout_policy=policy)
            draft.settings_json=D._dump(options)
            draft.pick_minutes=(seconds+59)//60
            draft.pick_deadline_at=None
            draft.paused_seconds_left=seconds if season.status==A.STATUS_PAUSED and seconds else None
        elif kind=='bind':
            from services.auction_webapp import integer
            A.bind_chat(db,season,integer(payload['chat_id'],'Telegram group ID',-(2**63),-1))
        elif kind=='opening':
            A.set_opening_purse(db,season,D.parse_money(payload['amount']),by_tg_id=actor)
        elif kind=='csv':
            from services import franchise_player_import as P
            rows=P.parse_csv(str(payload['text']).encode('utf-8'))
            P.import_players(db,season,rows,actor_tg_id=actor,trusted_admin=trusted_admin)
        elif kind=='cancel':
            if payload.get('name')!=season.name:raise A.AuctionError('Type the draft name to cancel it.')
            A.cancel(db,season,by_tg_id=actor)
        elif kind=='retention':
            if D.started(db,season):raise A.AuctionError('Configure retention before the draft starts.')
            cap=int(payload['maximum'])
            if not 0<=cap<=draft.rounds:raise A.AuctionError('Retention maximum must fit the squad limit.')
            if any(t.retained_count>cap for t in A.franchises(db,season.id)):
                raise A.AuctionError('Undo excess retentions before reducing their maximum.')
            options=D._loads(draft.settings_json,{})
            options['retention_max']=cap
            draft.settings_json=D._dump(options)
        elif kind=='retain':
            if D.started(db,season):raise A.AuctionError('Retain contracts before starting the draft.')
            team=db.get(AuctionFranchise,int(payload['team_id']))
            if team is None or team.season_id!=season.id:raise A.AuctionError('That team belongs to another season.')
            maximum=D._loads(draft.settings_json,{}).get('retention_max',0)
            if team.retained_count>=maximum:raise A.AuctionError('Configure /dretention before keeping this contract.')
            lot=D.find_player(db,season,payload['player'])
            holder=A.previous_squad_map(db,season).get(lot.player_id)
            if holder is None or holder.id!=team.id:raise A.AuctionError('This player was not held by this franchise in the previous season.')
            price=D.parse_money(payload['price'])
            minimum_price=D._validate_pick(db,season,team,lot,price)
            D._invariants(season,draft)
            season.min_base_price_lakh=minimum_price
            A._sign_before_auction(db,season,team,db.get(Player,lot.player_id),price,
                kind=A.ACQ_RETAINED,counter='retained_count',ledger_kind=A.LEDGER_RETENTION,
                verb='retain',note='Draft retained contract: '+reason,event_kind='fd_retained',
                mark='🔒',word='retained',by_tg_id=actor,quiet=True)
            team.draft_picks_total=draft.rounds-team.retained_count
            D._event(db,season,'fd_retained',f'🔒 <b>{D._e(team.name)} retained {D._e(lot.name)}</b>\n'
                f'{D._money(season,price)} · existing contract occupies one squad slot.',by_tg_id=actor,lot=lot,franchise=team)
        elif kind=='unretain':
            if D.started(db,season):raise A.AuctionError('Correct retained contracts before starting the draft.')
            team=db.get(AuctionFranchise,int(payload['team_id']))
            lot=D.find_player(db,season,payload['player'],available_only=False)
            if team is None or team.season_id!=season.id or lot.sold_to_id!=team.id or lot.acquisition!=A.ACQ_RETAINED:
                raise A.AuctionError('Choose a contract retained by this franchise in this season.')
            refund=int(lot.sold_price_lakh)
            team.purse_remaining_lakh+=refund
            team.squad_size-=1;team.retained_count-=1
            team.draft_picks_total=draft.rounds-team.retained_count
            A._ledger(db,team,A.LEDGER_REFUND,refund,lot=lot,note=reason or 'Reversed retained draft contract',by_tg_id=actor)
            lot.status=A.LOT_QUEUED;lot.acquisition=A.ACQ_AUCTION
            lot.sold_to_id=lot.sold_price_lakh=lot.sold_at=None
            D._event(db,season,'fd_unretained',f'↩️ <b>{D._e(lot.name)} retained contract reversed</b>\n'
                f'{D._e(team.name)} refunded {D._money(season,refund)}.',by_tg_id=actor,lot=lot,franchise=team,
                metadata={'refund_lakh':refund})
        elif kind=='pooladd':
            if D.started(db,season) and not reason:raise A.AuctionError('Future availability changes require a reason while paused.')
            players=select_players(db,payload['text'])
            existing={lot.player_id:lot for lot in A.lots(db,season.id)}
            for player in players:
                lot=existing.get(player.id)
                if lot is not None and lot.status==A.LOT_UNSOLD:lot.status=A.LOT_QUEUED
            A.add_players_to_pool(db,season,players)
        elif kind=='poolremove':
            if D.started(db,season) and not reason:raise A.AuctionError('Future availability changes require a reason while paused.')
            for identifier in re.split(r'[,\s]+',payload['text'].strip()):
                lot=D.find_player(db,season,identifier)
                lot.status=A.LOT_UNSOLD
        elif kind=='purse':
            from services import auction_management as M
            teams=A.franchises(db,season.id)
            if payload['team']!='all':teams=[t for t in teams if str(t.id)==payload['team'] or t.name.casefold()==payload['team'].casefold()]
            if not teams:raise A.AuctionError('Choose a team name/ID or all.')
            for team in teams:
                M.adjust_purse(db,season,team,mode='set',amount_lakh=D.parse_money(payload['amount']),
                    expected_purse_lakh=team.purse_remaining_lakh,note=reason or 'Draft setup budget',by_tg_id=actor)
        else:raise A.AuctionError('Unknown draft configuration action.')
    db.flush()


def prepare(db,season,actor,kind,payload,reason='',*,trusted_admin=False):
    require_admin(db,actor,trusted_admin)
    reason=str(reason).strip()
    if len(reason)>300:raise A.AuctionError('Use a correction reason of at most 300 characters.')
    if D.started(db,season) and kind not in ('start','pause','finish','publish','continue','draftadmin_add','draftadmin_remove') and not reason:
        raise A.AuctionError('A live-season correction needs a reason. Pause first, then use /dcorrect kind | reason | value.')
    team_import=None
    if kind=='order' and str(payload.get('text','')).strip().casefold()=='random':
        order=[t.id for t in A.franchises(db,season.id)]
        secrets.SystemRandom().shuffle(order)
        payload=dict(payload,text='|'.join(map(str,order)),randomized=True)
    if kind=='importteams':
        from services import franchise_team_import as I
        team_import=I.preview(db,season,payload['source_kind'],payload['source_id'])
        payload=dict(payload,fingerprint=team_import['fingerprint'])
    before=summary(db,season)
    old_prices={}
    for lot in D._available(db,season):
        try:old_prices[lot.id]=D.price_for(db,season,lot)
        except A.AuctionError:old_prices[lot.id]=None
    stamp=fingerprint(db,season)
    transaction=db.begin_nested()
    try:
        perform(db,season,actor,kind,payload,reason,trusted_admin=trusted_admin)
        after=summary(db,season)
        prices=[]
        requested_ids=[]
        if kind in ('prices','custom'):
            draft=D.get_draft(db,season)
            ratings=D._loads(draft.rating_prices_json,{})
            for lot in D._available(db,season):
                prices.append(dict(id=lot.id,player_id=lot.player_id,name=lot.name,version=lot.version,
                    rating=lot.rating,rating_price=ratings.get(str(lot.rating)),
                    previous_price=old_prices.get(lot.id),price=D.price_for(db,season,lot)))
            if kind=='custom':
                text=payload['text']
                names=text[7:].splitlines() if text.casefold().startswith('remove ') else [name for name,_ in D.custom_price_rows(db,season,text)]
                pool=A.lots(db,season.id)
                requested_ids=[D.find_player(db,season,name.strip(),_lots=pool).id for name in names]
    finally:
        transaction.rollback()
    db.expire_all()
    preview=dict(before=before,after=after,prices=prices,requested_ids=requested_ids,fingerprint=stamp,team_import=team_import)
    row=FranchiseDraftAction(token=secrets.token_hex(12),season_id=season.id,actor_tg_id=actor,
        revision=D.get_draft(db,season).revision,kind=kind,payload_json=D._dump(payload),
        preview_json=D._dump(preview),reason=reason,status='pending')
    db.add(row);db.flush()
    return row


def answer(db,season,actor,token,*,accept,trusted_admin=False):
    row=db.get(FranchiseDraftAction,token)
    require_admin(db,actor,trusted_admin)
    if row is None or row.season_id!=season.id or row.actor_tg_id!=actor:
        raise A.AuctionError('This configuration preview belongs to another administrator or season.')
    if row.status!='pending' or datetime.utcnow()-row.created_at>timedelta(minutes=10):
        raise A.AuctionError('This configuration preview expired or was already answered.')
    if accept:
        draft=D.get_draft(db,season)
        if draft.revision!=row.revision:raise A.AuctionError('The draft changed. Generate a new preview.')
        D._claim(db,season)
        db.expire_all()
        if fingerprint(db,season)!=D._loads(row.preview_json,{})['fingerprint']:
            raise A.AuctionError('The pool, membership, rules or budget changed. Generate a new preview.')
    changed=db.query(FranchiseDraftAction).filter_by(token=token,status='pending').update(
        {'status':'applied' if accept else 'cancelled'},synchronize_session=False)
    if changed!=1:raise A.AuctionError('This configuration preview was already answered.')
    if accept:
        perform(db,season,actor,row.kind,D._loads(row.payload_json,{}),row.reason or '',trusted_admin=trusted_admin)
        label='Website administrator' if trusted_admin else str(actor)
        D._event(db,season,'fd_admin_action',f'🛡 <b>Draft {D._e(row.kind)} confirmed</b>\n'
            f'Reason: {D._e(row.reason or "Setup configuration")}\nAdministrator: <code>{label}</code>',
            by_tg_id=actor,metadata={'action_token':token,'payload':D._loads(row.payload_json,{}),'preview':D._loads(row.preview_json,{})})
    db.flush()
    db.expire(row)
    return row
