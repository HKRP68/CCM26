"""Button menus and guided, role-checked conversations for Franchise Auction HQ."""
from __future__ import annotations

import html
import io
import json
from pathlib import Path
import secrets
import time
from datetime import datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from database import get_session
from services import auction_service as A, auction_webapp as W
from services.auction_async import run_session, AuctionWorkerBusy, BUSY_MESSAGE
from services.franchise_bot_settings import SETTINGS, GROUP_LABELS, parse_value, whole, ids
from handlers.franchise_commands import draft_command_menu

BOT_COMMANDS = [BotCommand(*row) for row in [
    ('auction','Open Franchise Auction & Draft HQ'),('start','Open Franchise Auction & Draft HQ'),
    ('newauction','Create a new auction'),('bind','Bind an auction to this group'),
    ('settings','Manage auction rules'),('importplayers','Import player CSV into this group'),
    ('sampleplayers','Download the player CSV example'),
    ('roles','Show your current roles'),('id','Show Telegram user and group IDs'),
    ('bid','Bid in the auction group'),('help','How auctions and setup work'),('cancel','Cancel a guided input'),
    ('commands','Search every auction command and alias'),
    ('draft','Open the franchise season draft'),('dpick','Choose a player for your draft turn'),
    ('dhelp','Franchise draft setup and commands'),
    ('dskip','Defer your current draft pick'),('dskips','View the skipped-pick queue'),
    ('dadmin','Open the Draft Admin panel'),('draftcommands','Every draft command and example'),
    ('dadminadd','Appoint a Draft Admin (bot admins only)'),('dadmins','List Draft Admins'),
]]
_menu_names={command.command for command in BOT_COMMANDS}
BOT_COMMANDS.extend(BotCommand(name,description) for name,description in draft_command_menu() if name not in _menu_names)
PROMPT_KEY = 'franchise_prompt'

# form key: shared service action, fields (key, label, parser).
FORMS = {
    'new':('admin_create',[('name','Name of the new auction','text')]),
    'newdraft':('native_newdraft',[('name','Name of the new draft season','text'),('rounds','Number of rounds and players per squad','id'),('purse_lakh','Opening purse per team in whole lakh: 10000 = 100 Cr','id')]),
    'fromleague':('native_from_league',[('league_id','Previous league ID from Existing leagues','id'),('name','Name of the next auction season','text')]),
    'fromleaguedraft':('native_draft_from_league',[('league_id','Previous league ID from Existing leagues','id'),('name','Name of the next draft season','text'),('rounds','Number of rounds and players per squad','id'),('purse_lakh','Opening purse per team in whole lakh: 10000 = 100 Cr','id')]),
    'draftprices':('native_fd_prices',[('text','Rating prices, one per line, for example 96:23cr and 95:21cr','text')]),
    'draftcustom':('native_fd_custom',[('text','Custom prices, one per line, for example Joe Root:25.5cr','text')]),
    'draftrounds':('native_fd_rounds',[('rounds','Total rounds and maximum squad size; add rounds by increasing this number','id')]),
    'draftorderfull':('native_fd_order',[('text','Team IDs or exact names separated by |, in round 1 order; or random','text')]),
    'drafttimer':('native_fd_timer',[('minutes','Pick time in whole minutes (5, 8, 10, 15 etc.); 0 disables the timer','nonnegative')]),
    'draftclockpolicy':('native_fd_clock',[('text','Pick duration and timeout policy: 30s | pause, 120s | skip, 5m | auto, or 0 | admin','text')]),
    'draftretention':('native_fd_retention',[('maximum','Maximum retained contracts per team (0 disables retention)','nonnegative')]),
    'draftretain':('native_fd_retain',[('team','Exact franchise name or ID','text'),('player','Exact pool #ID or catalogue pID to retain','text'),('price','Contract price, for example 10cr','text')]),
    'draftunretain':('native_fd_unretain',[('team','Exact franchise name or ID','text'),('player','Retained pool #ID to refund','text')]),
    'draftpool':('native_fd_pooladd',[('text','Catalogue players: ids 12,34 or 80-96 | BAT | Base; * includes every category/version','text')]),
    'draftwithdraw':('native_fd_poolremove',[('text','Eligible pool #IDs to withdraw, separated by spaces','text')]),
    'draftcorrection':('native_fd_correction',[('kind','Correction: prices, custom, rounds, undo, purse, pooladd, poolremove, clock or resolve','text'),('reason','Reason recorded in the audit log (up to 300 characters)','text'),('value','Correction value; skip for undo','optional')]),
    'draftundo':('native_fd_undo',[('reason','Reason to reverse the latest turn (up to 300 characters)','text')]),
    'bind':('admin_bind',[('chat_id','Telegram group ID (negative; use /id in the group)','int')]),
    'teamadd':('admin_franchise',[('name','Franchise name','text'),('owner_tg_id','Owner Telegram user ID','id'),('co_owners','Co-owner IDs, comma separated; skip for none','ids'),('team_managers','Team Manager IDs, comma separated; skip for none','ids')]),
    'pool':('admin_pool_range',[('low','Minimum player rating','int'),('high','Maximum player rating','int'),('set_name','Set name; skip uses the default','optional')]),
    'purse':('admin_team_purse',[('mode','Choose add, deduct or set','purse_mode'),('amount_lakh','Amount in whole lakh: 100 = 1 Cr','nonnegative'),('note','Reason for this purse change','text')]),
    'retain':('admin_retention_force',[('player_id','Player catalogue ID','id'),('amount_lakh','Retention price in whole lakh; skip uses the rules','optional_int')]),
    'unretain':('admin_unretain',[('lot_id','Retained player lot ID','id')]),
    'picks':('admin_draft_picks',[('picks','Number of expansion picks','int')]),
    'rtm':('admin_rtm_cards',[('cards','Number of RTM cards','int')]),
    'draftorder':('admin_draft_order',[('team_ids','Franchise IDs in draft order, comma separated','ids')]),
    'tradedeadline':('admin_trade_deadline',[('deadline_at','Trade deadline as ISO date with timezone; skip clears it','optional'),('deadline_matches','Played-match deadline; skip clears it','optional_int')]),
    'trade':('admin_trade_force',[('team_a_id','First franchise ID','id'),('team_b_id','Second franchise ID','id'),('lot_ids_a','Lot IDs sent by the first team; skip for none','ids'),('lot_ids_b','Lot IDs sent by the second team; skip for none','ids'),('cash_lakh','Cash in whole lakh from first team to second; negative reverses it','int'),('note','Reason for this manager trade','text')]),
    'clone':('admin_clone',[('name','Name of the next season','text')]),
    'publish':('admin_publish',[('confirm_name','Type the exact auction name to publish squads','text')]),
    'restart':('admin_restart',[('confirm_name','Type the exact auction name to restart this auction','text')]),
    'cancelauction':('admin_cancel',[('confirm_name','Type the exact auction name to cancel this auction','text')]),
    'simulate':('admin_simulate',[('confirm_name','Type the exact auction name to simulate the remaining lots','text')]),
    'addmanager':('admin_add_admin',[('telegram_id','Telegram user ID to appoint as Auction Manager','id')]),
    'delmanager':('admin_remove_admin',[('telegram_id','Telegram user ID to remove as Auction Manager','id')]),
    'adddraftmanager':('native_draft_admin_add',[('telegram_id','Telegram user ID to appoint as Draft Admin','id')]),
    'deldraftmanager':('native_draft_admin_remove',[('telegram_id','Telegram user ID to remove as Draft Admin','id')]),
    'previous':('admin_previous_league',[('league_id','Previous published league ID','id')]),
    'previous_team':('admin_previous_team',[('team_name','Exact previous team name','text')]),
    'teamplayer':('admin_team_add_player',[('player_id','Player catalogue ID','id'),('price_lakh','Signing price in whole lakh; 0 is free','int'),('note','Reason for this signing','text')]),
    'removeplayer':('admin_team_remove_player',[('lot_id','Player lot ID to remove','id'),('note','Reason for removal and refund','text')]),
    'clear':('admin_pool_clear',[('confirm_name','Type the exact auction name to clear eligible queued players','text')]),
}
TEAM_FORMS = {'purse','retain','unretain','picks','rtm','previous_team','teamplayer','removeplayer'}
MANAGER_ROLES = {'addmanager','delmanager','adddraftmanager','deldraftmanager'}


def _e(value, limit=150):
    return html.escape(str(value or '')[:limit])


def _money(value, currency='₹'):
    amount=int(value or 0)
    whole,fraction=divmod(abs(amount),100)
    return _e(currency,10) + ('-' if amount<0 else '') + f'{whole:,}.{fraction:02d} Cr'


def _resolve(db, actor, sid, chat_id):
    if chat_id < 0:
        season=A.season_for_chat(db,chat_id)
        if sid and (not season or season.id!=sid):
            raise W.AccessError('This button belongs to a different auction group. Open /auction here.')
        rows=[season] if season else []
        return season,rows
    return W.resolve(db,actor,sid)


def read_workspace(db, actor, sid, chat_id, view='home', argument=''):
    if chat_id>0 and view in ('seasons','auctionseasons','draftseasons'):
        sid=None
    season,rows=_resolve(db,actor,sid,chat_id)
    if chat_id < 0 and season is None and view == 'home':
        from services import franchise_draft as D
        result = W.base_payload(db, None, actor, [])
        if D.is_draft_admin(db, actor):
            result['unbound_drafts'] = [dict(id=s.id, name=s.name)
                for s in W.available_seasons(db, actor)
                if D.get_draft(db, s) is not None and s.status in (A.STATUS_SETUP, A.STATUS_PAUSED)]
        return result
    if view=='draftsources':
        from services import franchise_draft as D,franchise_team_import as I
        D.require_draft_admin(db,actor)
        if season is None or D.get_draft(db,season) is None:raise A.AuctionError('Select a Franchise Draft season first.')
        if D.started(db,season) or season.status!=A.STATUS_SETUP:raise A.AuctionError('Import teams before the draft starts.')
        result=W.base_payload(db,season,actor,rows)
        result['import_sources'],result['has_more_sources']=I.sources(db,max(1,int(argument or 1)))
        result['franchise_draft']={'source_picker':True}
        return result
    if view in ('auctionseasons','draftseasons'):
        result=W.base_payload(db,None,actor,rows)
        mode='draft' if view=='draftseasons' else 'auction'
        result['auctions']=[row for row in result['auctions'] if row['mode']==mode]
        return result
    if view=='leagues':
        if not W.any_admin_for(db,actor):raise W.AccessError('Only Auction Managers and Draft Admins can browse previous leagues.')
        from models import ChallengeLeague
        page=max(1,int(argument or 1))
        leagues=db.query(ChallengeLeague).order_by(ChallengeLeague.id.desc()).offset((page-1)*8).limit(9).all()
        result=W.base_payload(db,season,actor,rows)
        result.update(leagues=[dict(id=row.id,name=row.name) for row in leagues[:8]],has_more_leagues=len(leagues)>8)
        return result
    if view in ('seasons','help','home','roles') and season is None:
        return W.base_payload(db,None,actor,rows)
    if season is None:raise A.AuctionError('Create or select an auction first. Use /auction.')
    if view=='seasons':return W.base_payload(db,season,actor,rows)
    api_view={'home':'arena','live':'arena','franchisedraft':'arena','team':'teams','teams':'teams','myteam':'teams','pool':'pool','rules':'rules',
              'control':'admin','settings':'admin','fields':'admin','form':'admin','teamroles':'admin','finish':'admin',
              'preseason':'preseason','draft':'preseason','trades':'trades','roles':'arena','help':'arena'}.get(view)
    if api_view is None:raise A.AuctionError('Open /auction for a fresh menu.')
    request={'view':api_view}
    if api_view=='pool':request['page']=(max(1,int(argument or 1))-1)//4+1
    if api_view=='preseason' and argument:request['team_id']=int(argument)
    result=W.payload(db,season,actor,rows,request)
    from services import franchise_draft as D
    draft=D.get_draft(db,season)
    if draft:
        from services import franchise_draft_rich as DR
        board_blocks,board_text=DR.board(db,season)
        result['franchise_draft']={'rounds':draft.rounds,'cursor':draft.cursor,
            'signed':sum(int(t.squad_size or 0) for t in A.franchises(db,season.id)),
            'skipped':len(D.skipped_turns(db,season)),
            'board':board_text,'board_blocks':board_blocks,'order':D.render_order(db,season)}
        if view=='pool':
            blocks,text=DR.pool(db,season,page=max(1,int(argument or 1)))
            result['franchise_draft'].update(pool=text,pool_blocks=blocks)
            from handlers.franchise_draft_v2 import pick_buttons
            available=D._available(db,season);page=max(1,min(max(1,(len(available)+11)//12),int(argument or 1)))
            result['franchise_draft']['picks']=[dict(text=b.text,data=b.callback_data) for row in (pick_buttons(db,season,actor,available[(page-1)*12:page*12]) if chat_id<0 else []) for b in row]
        elif view=='rules':
            blocks,text=DR.rules(db,season)
            result['franchise_draft'].update(rulebook=text,rulebook_blocks=blocks)
    if view in ('preseason','draft'):
        result['previous_league_id']=season.previous_league_id
    return result


def _native_perform(db, season, actor, action, values):
    from models import AuctionEvent
    before=db.query(AuctionEvent.id).filter_by(season_id=season.id).order_by(AuctionEvent.id.desc()).first()
    result=W.perform(db,season,actor,dict(action=action,**values))
    # These shared, UI-independent services predate the dedicated bot. Keep
    # native control changes labelled correctly in the shared auction DB.
    events=db.query(AuctionEvent).filter(AuctionEvent.season_id==season.id,
        AuctionEvent.kind=='miniapp_admin',AuctionEvent.by_tg_id==actor,
        AuctionEvent.id>(before[0] if before else 0)).all()
    for event in events:
        event.kind='bot_admin'
        event.headline=(event.headline or '').replace('Miniapp:', 'Bot:', 1)
    return result



def execute_action(db, actor, sid, chat_id, action, values):
    if action in ('native_draft_admin_add','native_draft_admin_remove'):
        from services import franchise_draft as D
        from services.admin_ids import is_bot_admin
        if not is_bot_admin(actor):raise W.AccessError('Only Bot Owners and Bot Admins can appoint or remove Draft Admins.')
        fn=D.add_draft_admin if action.endswith('_add') else D.remove_draft_admin
        fn(db,whole(values.get('telegram_id')),by_tg_id=actor)
        return dict(message='Draft Admin assignment updated.')
    if action in ('native_newdraft','native_draft_from_league'):
        from services import franchise_draft as D, league_season_service as LS
        if not D.is_draft_admin(db,actor):raise W.AccessError('Only Draft Admins can create a draft season.')
        from models import ChallengeLeague, LeagueSeason
        name=str(values.get('name') or '').strip()
        rounds=whole(values.get('rounds'))
        purse=whole(values.get('purse_lakh'))
        if not 1 <= purse <= 1_000_000:raise A.AuctionError('Opening purse must be between 1 and 1,000,000 whole lakh.')
        if action=='native_draft_from_league':
            league=db.get(ChallengeLeague,whole(values.get('league_id')))
            if league is None:raise A.AuctionError('That previous league does not exist.')
            previous=db.query(LeagueSeason).filter_by(league_id=league.id).order_by(LeagueSeason.season_no.desc()).first()
            if previous:
                try:_,season=LS.start_franchise_draft(db,previous.series,name=name,purse_lakh=purse,rounds=rounds,by_tg_id=actor)
                except LS.LeagueSeasonError as exc:raise A.AuctionError(str(exc)) from None
            else:
                season=A.season_from_league(db,league,name,by_tg_id=actor)
                D.enable(db,season,rounds=rounds,by_tg_id=actor)
                A.set_opening_purse(db,season,purse,by_tg_id=actor)
            from services import franchise_team_import as I
            I.apply(db,season,'league',league.id,actor=actor)
        else:
            season=A.create_season(db,name,opening_purse_lakh=purse)
            D.enable(db,season,rounds=rounds,by_tg_id=actor)
        return dict(message='Draft season created. Add teams and player pool, set draft prices, then start from /draft.',season_id=season.id)
    if action=='native_from_league':
        if not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can create auctions from previous leagues.')
        from models import ChallengeLeague
        league=db.get(ChallengeLeague,whole(values.get('league_id')))
        if league is None:raise A.AuctionError('That previous league does not exist. Open Existing leagues to choose one.')
        season=A.season_from_league(db,league,str(values.get('name') or '').strip(),by_tg_id=actor)
        A.apply_staged_defaults(season)
        return dict(message='Auction created from the previous league. Assign franchise roles, then begin retention.',season_id=season.id)
    if action=='admin_create':
        if not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can create auctions.')
        result=W.perform(db,None,actor,dict(action=action,**values))
        season=db.get(__import__('models').AuctionSeason,result['season_id'])
        A.apply_staged_defaults(season)
        return result
    season,_=_resolve(db,actor,sid,chat_id)
    if season is None:raise A.AuctionError('Select an auction first.')
    if action.startswith('native_fd_'):
        from services import franchise_draft as D
        if not D.is_draft_admin(db,actor):raise W.AccessError('Only Draft Admins can manage a draft.')
        operation=action.removeprefix('native_fd_')
        if operation in ('clock','retention','retain','unretain','pooladd','poolremove','correction'):
            from handlers.franchise_draft_v2 import route
            command={'clock':'dclock','retention':'dretention','retain':'dretain','unretain':'dunretain','pooladd':'dpooladd','poolremove':'dpoolremove','correction':'dcorrect'}[operation]
            raw=(str(values.get('maximum','')) if operation=='retention' else
                 f'{values.get("team", "")} | {values.get("player", "")} | {values.get("price", "")}' if operation=='retain' else
                 f'{values.get("team", "")} | {values.get("player", "")}' if operation=='unretain' else
                 f'{values.get("kind", "")} | {values.get("reason", "")} | {values.get("value") or ""}' if operation=='correction' else values.get('text',''))
            return dict(message='Review the draft preview.',draft_packet=route(db,season,actor,command,raw))
        if operation in ('prices','custom','rounds','start','undo'):
            from handlers.franchise_draft_v2 import action_packet
            payload={'text':values.get('text','')} if operation in ('prices','custom') else {'rounds':whole(values.get('rounds'))} if operation=='rounds' else {}
            return dict(message='Review and confirm the draft configuration.',draft_packet=action_packet(db,season,actor,operation,payload,values.get('reason','')))
        if operation=='timer':D.set_timer(db,season,whole(values.get('minutes')),by_tg_id=actor)
        elif operation=='order':
            from handlers.franchise_draft import parse_order
            raw=values.get('text','')
            D.set_order(db,season,parse_order(db,season,raw),randomize=raw.strip().casefold()=='random',by_tg_id=actor)
        elif operation=='pause':D.pause(db,season,by_tg_id=actor)
        else:raise A.AuctionError('Choose a draft control.')
        return dict(message='Draft updated. Open /draft for the current turn, squads and purses.')
    if action=='admin_previous_league':
        if not W.admin_for(db,actor,season):raise W.AccessError('Only this season’s admins can link previous leagues.')
        from models import ChallengeLeague
        if db.get(ChallengeLeague,whole(values.get('league_id'))) is None:
            raise A.AuctionError('That previous league does not exist. Open Existing leagues to choose one.')
    return _native_perform(db,season,actor,action,values)


def _callback(actor,sid,route):
    value=f'fa:{actor}:{sid or 0}:{route}'
    if len(value.encode())>64:raise ValueError('Auction button is too long.')
    return value


def _button(actor,sid,label,route):
    return InlineKeyboardButton(label,callback_data=_callback(actor,sid,route))


def _private_url(context,sid):
    username=getattr(context.bot,'username',None)
    return f'https://t.me/{username}?start=auction_{sid or 0}' if username else None


def _reset_prompt(context):
    context.user_data.pop(PROMPT_KEY,None)
    context.user_data['franchise_prompt_generation']=context.user_data.get('franchise_prompt_generation',0)+1


def _prompt(context, update, record):
    _reset_prompt(context)
    record.update(chat_id=update.effective_chat.id,created=time.monotonic(),generation=context.user_data['franchise_prompt_generation'])
    context.user_data[PROMPT_KEY]=record


def _current_sid(context):
    return context.user_data.get('franchise_season_id')


def _actor(update):
    user=getattr(getattr(update,'callback_query',None),'from_user',None) or update.effective_user
    if not user:raise W.AccessError('Open this menu with your Telegram account.')
    return int(user.id)


def _rows(actor,sid,items):
    return [[_button(actor,sid,label,route) for label,route in row] for row in items]


def _lines(lines):
    result=[]
    for line in lines:
        if sum(len(v)+1 for v in result)+len(line)>3650:
            result.append('… Open the next page or the matching group command for more.')
            break
        result.append(line)
    return '\n'.join(result)


def _store_action(context,chat_id,sid,action,values):
    records=context.user_data.setdefault('franchise_menu_actions',{})
    now=time.monotonic()
    for key in list(records):
        if now-records[key]['created']>600:records.pop(key)
    while len(records)>=20:records.pop(next(iter(records)))
    key=secrets.token_hex(4)
    records[key]=dict(chat_id=chat_id,sid=sid,action=action,values=values,created=now)
    return key


def render_workspace(data, actor, sid, view, argument, context, chat_id):
    season=data.get('season') or {};sid=season.get('id') or sid
    manager=bool(data.get('admin'));team_ids=data.get('my_team_ids') or []
    currency=season.get('currency') or '₹'
    draft=data.get('franchise_draft')
    workspace='Draft' if draft or view=='draftseasons' else 'Auction' if season or view=='auctionseasons' else 'Auction &amp; Draft'
    header=[f'<b>🏏 Franchise {workspace} HQ</b>',_e(season.get('name') or 'Your franchise workspace'),
            '<b>Roles:</b> '+_e(' · '.join(data.get('role_labels') or ['Spectator']),400)]
    buttons=[]
    if view in ('home','roles'):
        buttons += _rows(actor,sid,[[('🔨 Franchise Auction','v:auctionseasons'),('🏏 Franchise Draft','v:draftseasons')]])
        if season:
            counts=data.get('counts') or {}
            header += [f"Status: <b>{_e(season['status'])}</b>",
                f"{counts.get('drafted',0)} drafted · {counts.get('queued',0)} available" if draft else f"{counts.get('sold',0)} sold · {counts.get('queued',0)} waiting"]
            if draft:
                header += [f"📋 Draft · {draft['rounds']} rounds · {draft['signed']} signed · {draft['skipped']} deferred"]
                buttons += _rows(actor,sid,[[('📋 Draft room','v:franchisedraft'),('👥 Squads','v:teams')]])
            else:buttons += _rows(actor,sid,[[('🔨 Live room','v:live'),('👥 Squads','v:teams')]])
            if team_ids:buttons += _rows(actor,sid,[[('💼 My franchises','v:myteam'),('📚 Player pool','v:pool:1')],[('📖 Rulebook','v:rules')]])
            if manager:
                field=data.get('teams') or []
                checks=[('Group bound',bool(season.get('chat_id'))),('At least two franchises',len(field)>=2),('Player pool ready',bool(counts.get('total')))]
                header += ['','<b>Setup checklist</b>']+[('✅ ' if ready else '○ ')+label for label,ready in checks]
                if draft:
                    buttons += _rows(actor,sid,[[('🎛 Draft controls','v:franchisedraft'),('⚙️ Squad rules','v:settings')],[('🏷 Teams & roles','v:teamroles'),('🏁 Publish & next season','v:finish')]])
                else:buttons += _rows(actor,sid,[[('🎛 Control room','v:control'),('⚙️ All settings','v:settings')],[('🏷 Teams & roles','v:teamroles'),('🛡 Retention','v:preseason')],[('🆕 Expansion draft','v:draft'),('🤝 Trades','v:trades')]])
        else:
            header+=['','Use existing competitions or create the next season. No debut, coins or collecting-game account is required.']
            if chat_id < 0:
                header += ['','This group has no bound season. A Draft Admin can bring an existing draft here with <code>/dbind draftID</code>, then use /start and /dstart.']
                for row in data.get('unbound_drafts', []):
                    header += [f'{_e(row["name"])} · <code>/dbind {row["id"]}</code>']
            if manager:header+=['Choose an existing auction, or start from an existing league for retention.']
            else:header+=['Ask your organiser to assign your Telegram ID to a franchise. Open /auction in its group to watch.']
        create=[]
        if data.get('auction_admin'):create.append(('➕ New auction','f:new'))
        if data.get('draft_admin'):create.append(('📋 New draft','f:newdraft'))
        if create:buttons+=_rows(actor,sid,[create,[('📚 Existing leagues','v:leagues:1')]])
        if data.get('bot_admin'):
            buttons+=_rows(actor,sid,[[('Appoint Draft Admin','f:adddraftmanager'),('Remove Draft Admin','f:deldraftmanager')]])
        buttons+=_rows(actor,sid,[[('🔀 Choose season','v:seasons'),('❓ Help','v:help')]])
        from handlers.franchise_commands import command_button
        buttons.append([command_button(actor)])
    elif view in ('seasons','auctionseasons','draftseasons'):
        mode='draft' if view=='draftseasons' else 'auction' if view=='auctionseasons' else None
        header+=['',f'<b>Choose a franchise {mode or "season"}</b>']
        for row in (data.get('auctions') or [])[:30]:
            label='🏏 Draft' if row.get('mode')=='draft' else '🔨 Auction'
            buttons+=_rows(actor,row['id'],[[(f"{label} · {row['name'][:28]} · {row['status']}",'v:home')]])
        if not data.get('auctions'):header+=[f'Your assigned franchise {mode+"s" if mode else "seasons"} will appear here.']
        if mode=='draft' and data.get('draft_admin'):buttons+=_rows(actor,sid,[[('➕ Create Franchise Draft','f:newdraft'),('📚 Continue existing season','v:leagues:1')]])
        elif mode=='auction' and data.get('auction_admin'):buttons+=_rows(actor,sid,[[('➕ Create Franchise Auction','f:new'),('📚 Continue existing season','v:leagues:1')]])
    elif view=='leagues':
        page=max(1,int(argument or 1))
        header+=['',f'<b>Existing leagues · page {page}</b>',
                 'Choose an auction or a fixed-price draft for the next season. Check franchise owners before starting.']
        for league in data.get('leagues',[]):
            header+=[f"#{league['id']} · {_e(league['name'],70)}"]
            if data.get('auction_admin'):buttons+=_rows(actor,sid,[[(f"Next season: {league['name'][:25]}",f"f:fromleague:{league['id']}")]])
            if data.get('draft_admin'):buttons+=_rows(actor,sid,[[(f"Draft: {league['name'][:25]}",f"f:fromleaguedraft:{league['id']}")]])
        if not data.get('leagues'):header+=['No leagues found on this page.']
        if page>1:buttons+=_rows(actor,sid,[[('← Previous leagues',f'v:leagues:{page-1}')]])
        if data.get('has_more_leagues'):buttons+=_rows(actor,sid,[[('More leagues →',f'v:leagues:{page+1}')]])
        if season:buttons+=_rows(actor,sid,[[('Link league to this auction','f:previous')]])
    elif view=='draftsources':
        header+=['','<b>🏟 Import franchises, owners &amp; team details</b>',
                 'Choose a previous tournament or league. Review its teams and ownership before confirming. Existing roles and budgets stay intact.']
        for source in data.get('import_sources',[]):
            label=('🏆 Tournament' if source['kind']=='tournament' else '🏟 League')+f' #{source["id"]} · '+source['name'][:35]
            buttons+=_rows(actor,sid,[[(label,f'timp:{source["kind"]}:{source["id"]}')]])
        if not data.get('import_sources'):header+=['No previous tournaments or leagues found.']
        page=max(1,int(argument or 1))
        if page>1:buttons+=_rows(actor,sid,[[('← Earlier sources',f'v:draftsources:{page-1}')]])
        if data.get('has_more_sources'):buttons+=_rows(actor,sid,[[('More sources →',f'v:draftsources:{page+1}')]])
        buttons+=_rows(actor,sid,[[('Back to Draft','v:franchisedraft')]])
    elif view=='franchisedraft' or (draft and view in ('live','control')):
        if not draft:raise A.AuctionError('Select a draft season, or use /dsetup during setup.')
        header += ['',draft['board'],'','Pick: <code>/dpick Joe Root</code> · /dpool · /dsquad · /drules']
        if manager:
            header+=['','Manage draft prices and rounds here. Configure role minimums, maximums and rating rules under Squad rules.']
            buttons+=_rows(actor,sid,[[('Rating prices','f:draftprices'),('Custom player prices','f:draftcustom')],
                [('Draft order','f:draftorderfull'),('Total rounds','f:draftrounds')],
                [('⏱ Pick time (minutes)','f:drafttimer'),('➕ Add franchise','f:teamadd')],
                [('⏱ Seconds & timeout policy','f:draftclockpolicy'),('🛡 Audited correction','f:draftcorrection')],
                [('📥 Add pool players','f:draftpool'),('📤 Withdraw pool players','f:draftwithdraw')],
                [('🔒 Retention rules','f:draftretention'),('Keep contract','f:draftretain')],
                [('Refund retained contract','f:draftunretain')],
                [('Undo last turn','dgo:undo'),('Squad rules','v:settings')],
                [('Teams & roles','v:teamroles'),('Publish squads','f:publish')]])
            if season.get('status')=='setup':buttons+=_rows(actor,sid,[[('🏟 Import previous teams & owners','v:draftsources:1')]])
            if season.get('status') in ('setup','paused'):buttons+=_rows(actor,sid,[[('Start / resume','dgo:start')]])
            elif season.get('status')=='live':buttons+=_rows(actor,sid,[[('Pause','dgo:pause')]])
    elif view=='live':
        lot=data.get('lot')
        if lot:
            header += ['',f"<b>{_e(lot['name'],70)}</b> · {_e(lot['role'])} · {lot['rating']} OVR",
                       'Current bid: <b>'+_money(lot.get('bid_lakh'),currency)+'</b>',
                       'Next bid: <b>'+_money(lot['next_lakh'],currency)+'</b>']
            leading=next((t for t in data.get('teams',[]) if t['id']==lot.get('bidder_id')),None)
            header+=['Leading: '+_e(leading['name'] if leading else 'No bids')]
            if lot.get('deadline'):header+=['Server deadline: '+_e(lot['deadline'])]
            if chat_id<0 and season.get('status')=='live':
                from services.auction_rich import BID_CB,RTM_CB
                if lot.get('status')=='on_block':
                    buttons.append([InlineKeyboardButton('Bid '+_money(lot['next_lakh'],currency).replace('&amp;','&'),callback_data=f"{BID_CB}{lot['id']}_{lot['next_lakh']}")])
                elif lot.get('status')=='rtm_offered':
                    header+=['Use the shared RTM buttons on the pinned auction board, or /artm.']
        else:header+=['','No player is on the block. The manager opens the next player when the auction is ready.']
        if chat_id>0 and season.get('telegram_url'):buttons.append([InlineKeyboardButton('Open group live board',url=season['telegram_url'])])
        header+=['','Group bidding is shared. A custom /bid number is crore unless you add L; guided menus always use whole lakh.']
    elif view in ('teams','myteam','teamroles'):
        field=data.get('teams') or []
        if view=='myteam':field=[t for t in field if t.get('mine')]
        header+=['','<b>'+('Franchises & team roles' if view=='teamroles' else 'Franchise squads & purses')+'</b>']
        for team in field[:20]:
            header += [f"{_e(team['name'],45)} · {_money(team['purse_lakh'],currency)} · {team['size']}/{season['max_squad']} players"]
            buttons+=_rows(actor,sid,[[(_e(team['name'],30).replace('&amp;','&'),f"v:team:{team['id']}")]])
        if manager:buttons+=_rows(actor,sid,[[('➕ Add franchise','f:teamadd')]])
        if manager and draft and season.get('status')=='setup':buttons+=_rows(actor,sid,[[('🏟 Import previous teams & owners','v:draftsources:1')]])
    elif view=='team':
        pieces=(argument or '').split('-');tid=int(pieces[0]);page=max(1,int(pieces[1] if len(pieces)>1 else 1))
        team=next((t for t in data.get('teams',[]) if t['id']==tid),None)
        if not team:raise A.AuctionError('That franchise is no longer in this auction.')
        header+=['',f"<b>{_e(team['name'])}</b>",'Your team roles: '+_e(' · '.join(team.get('my_role_labels') or []) or 'Viewer'),
                 f"Purse: <b>{_money(team['purse_lakh'],currency)}</b>"+('' if draft else f" · RTM {team.get('rtm_left',0)}")]
        squad=team.get('squad') or []
        for p in squad[(page-1)*8:page*8]:header+=[f"• {_e(p['name'],55)} · {p['rating']} · {_money(p.get('price_lakh'),currency)}"]
        if not squad:header+=['No players signed yet.']
        if page>1:buttons+=_rows(actor,sid,[[('← Players',f'v:team:{tid}-{page-1}')]])
        if page*8<len(squad):buttons+=_rows(actor,sid,[[('More players →',f'v:team:{tid}-{page+1}')]])
        if manager:
            if draft:
                buttons+=_rows(actor,sid,[[('💰 Adjust purse',f'f:purse:{tid}'),('Owner ID',f'role:owner_tg_id:{tid}')],
                    [('Co-owner IDs',f'role:co_owners:{tid}'),('Draft room','v:franchisedraft')]])
            else:buttons+=_rows(actor,sid,[[('💰 Adjust purse',f'f:purse:{tid}'),('🆕 Add player',f'f:teamplayer:{tid}')],
                [('Remove player',f'f:removeplayer:{tid}'),('Team Manager IDs',f'role:team_managers:{tid}')],
                [('Owner ID',f'role:owner_tg_id:{tid}'),('Co-owner IDs',f'role:co_owners:{tid}')],
                [('Retention',f'v:preseason:{tid}'),('Expansion picks',f'f:picks:{tid}')],[('RTM cards',f'f:rtm:{tid}'),('Previous team',f'f:previous_team:{tid}')]])
    elif view=='pool':
        page=max(1,int(argument or 1));start=((page-1)%4)*10
        if draft:header+=['',draft['pool']]
        else:
            header+=['',f'<b>Player pool · page {page}</b>']
            for p in data.get('players',[])[start:start+10]:header+=[f"#{p['player_id']} · {_e(p['name'],38)} · {p['rating']} · {_e(p['status'])} · {_money(p['base_lakh'],currency)}"]
        nav=[]
        if page>1:nav.append(('← Previous',f'v:pool:{page-1}'))
        if page*(12 if draft else 10)<data.get('total',0):nav.append(('Next →',f'v:pool:{page+1}'))
        if nav:buttons+=_rows(actor,sid,[nav])
        if draft:
            buttons=[[InlineKeyboardButton(pick['text'],callback_data=pick['data'])] for pick in draft.get('picks',[])]+buttons
        if manager:header+=['','Import fresh players with /importplayers in the bound group.'];buttons+=_rows(actor,sid,[[('Build rating set','f:pool')]])
    elif view=='rules':
        rules=data.get('rules') or {}
        if draft:
            header+=['',draft['rulebook']]
        else:header+=['','<b>Auction rulebook</b>',f"Purse {_money(rules.get('opening_lakh'),currency)} · squad {rules.get('min_squad')}–{rules.get('max_squad')}",
            f"Overseas {rules.get('min_overseas')}–{rules.get('max_overseas')} · opening clock {rules.get('bid_seconds')}s",
            f"Retention {rules.get('min_retentions')}–{rules.get('max_retentions')} · RTM {'on' if rules.get('rtm_enabled') else 'off'}",
            'The server enforces purse reserves, role/rating limits, turn order, stale bids and deadlines.','Use /arules in the auction group for the complete rule tables.']
    elif view in ('settings','fields'):
        header+=['','<b>Draft settings</b>' if draft else '<b>All auction settings</b>','Common values use guided replies. Money is whole lakh: 100 = 1 Cr.']
        draft_keys={'name','currency_label','opening_purse_lakh','role_minimums','role_maximums','rating_rules'}
        if draft:header+=['Draft rounds, order and fixed prices are managed in Draft room. Overseas players have no limit.']
        if view=='settings':
            for group,label in GROUP_LABELS.items():
                if draft and group not in ('general','squad'):continue
                buttons+=_rows(actor,sid,[[(label,'v:fields:'+group+'-1')]])
        else:
            parts=(argument or 'general-1').rsplit('-',1);group=parts[0];page=int(parts[1])
            keys=[key for key,field in SETTINGS.items() if field.group==group and (not draft or key in draft_keys)]
            if not keys:raise A.AuctionError('Choose a settings category.')
            for key in keys[(page-1)*6:page*6]:buttons+=_rows(actor,sid,[[(SETTINGS[key].label,'s:'+key)]])
            nav=[]
            if page>1:nav.append(('← Previous',f'v:fields:{group}-{page-1}'))
            if page*6<len(keys):nav.append(('Next →',f'v:fields:{group}-{page+1}'))
            if nav:buttons+=_rows(actor,sid,[nav])
            buttons+=_rows(actor,sid,[[('All categories','v:settings')]])
    elif view in ('preseason','draft'):
        header+=['','<b>Retention & expansion draft</b>']
        previous=data.get('previous_league_id')
        header+=[f'Previous league: #{previous}' if previous else 'Link a previous league to load your existing squads and retention rights.']
        buttons+=_rows(actor,sid,[[('Existing leagues','v:leagues:1'),('Link previous league','f:previous')]])
        if not data.get('team_id'):
            header+=['Add franchises or create the season from an existing league first.']
        else:
            tid=data['team_id'];review=data.get('retention') or {};ready=data.get('readiness') or {}
            header += [f"Selected: <b>{_e(review.get('name') or tid)}</b>",f"{len(review.get('players') or [])} retained · {_money(review.get('spent_lakh'),currency)} spent",'Next draft team: '+_e(ready.get('next_team') or 'No active turn')]
            for t in data.get('teams',[])[:16]:buttons+=_rows(actor,sid,[[(t['name'][:28],f"v:{view}:{t['id']}")]])
            buttons+=_rows(actor,sid,[[('Retain player',f'f:retain:{tid}'),('Set picks',f'f:picks:{tid}')]])
            if review.get('token'):
                op=_store_action(context,chat_id,sid,'admin_retention_confirm',dict(team_id=tid,token=review['token']))
                buttons+=_rows(actor,sid,[[('Finalize reviewed retentions','op:'+op)]])
            eligible=data.get('eligible') or {}
            for player in eligible.get('players',[])[:6]:
                op=_store_action(context,chat_id,sid,'admin_draft_pick',dict(team_id=tid,player_id=player['player_id']))
                buttons+=_rows(actor,sid,[[(f"Pick {player['name'][:28]}",'op:'+op)]])
            skip=_store_action(context,chat_id,sid,'admin_draft_skip',dict(team_id=tid))
            buttons+=_rows(actor,sid,[[('Skip selected team pick','op:'+skip),('Draft order','f:draftorder')]])
        header+=['','Use /retain for player negotiations and /retainlock to finalize. After picks and bidding, publish squads from Finish & next season.']
    elif view=='trades':
        header+=['','<b>Trade centre</b>',_e(data.get('window'),250)]
        for team in data.get('teams',[]):header += [f"Team #{team['id']}: {_e(team['name'],45)}"]
        for trade in data.get('trades',[])[:8]:
            header += [f"Trade #{trade['id']} · {_e(trade['status'])}"]
            if trade['status']=='pending_admin':
                for action,label in [('admin_trade_approve','Approve'),('admin_trade_veto','Veto')]:
                    key=_store_action(context,chat_id,sid,action,dict(trade_id=trade['id']))
                    buttons+=_rows(actor,sid,[[(f"{label} #{trade['id']}",'op:'+key)]])
        buttons+=_rows(actor,sid,[[('Complete manager trade','f:trade'),('Trade deadlines','f:tradedeadline')]])
    elif view=='control':
        header+=['','<b>Auction Manager control room</b>']
        buttons+=_rows(actor,sid,[[('Start / resume','go:admin_start'),('Pause','go:admin_pause')],[('Open next player','go:admin_next'),('Live desk','v:live')],
            [('Bind group','f:bind'),('Teams & roles','v:teamroles')],[('Import players help','v:help'),('All settings','v:settings')],
            [('Retention','v:preseason'),('Draft','v:draft')],[('Trades','v:trades'),('Next season & finish','v:finish')]])
        if data.get('bot_admin'):buttons+=_rows(actor,sid,[[('Appoint manager','f:addmanager'),('Remove manager','f:delmanager')]])
        # Current lot context is fetched fresh when opening a live operation.
        buttons+=_rows(actor,sid,[[('Sell current player','go:admin_sold'),('Mark unsold','go:admin_unsold')],[('Undo latest bid','go:admin_undo_bid')]])
    elif view=='finish':
        header+=['','<b>Finish & next season</b>',
                 'Publish completed squads or continue with another season. Actions ask for the exact draft name.' if draft
                 else 'Actions ask for the exact auction name. Existing clock, squad and trade safeguards remain active.']
        if draft:buttons+=_rows(actor,sid,[[('Publish squads','f:publish'),('Existing leagues → next season','v:leagues:1')],[('Add rounds','f:draftrounds'),('Draft room','v:franchisedraft')],[('Cancel draft','f:cancelauction')]])
        else:buttons+=_rows(actor,sid,[[('Publish squads','f:publish'),('Clone next season','f:clone')],[('Simulate remainder','f:simulate'),('Restart auction','f:restart')],[('Cancel auction','f:cancelauction')]])
    elif view=='help':
        header+=['','<b>From retention to published squads</b>',
            '1. Choose an existing auction, or Existing leagues → Next season to carry previous franchises and squad rights.',
            '2. Add this bot to the group; grant pin-message permission for its live board. Bind the group using its negative /id.',
            '3. Add franchises and their owners / co-owners / Team Managers.',
            '4. Link the previous league if needed, negotiate retentions, finalize them, and complete expansion picks.',
            '5. Build rating sets from the shared catalogue in Player pool. /importplayers adds players using a CSV.',
            '6. Review settings and start bidding. Finish the auction, publish squads, then clone the next season.','',
            '<b>In the group</b>','/bid 1.5 = 1.5 Cr; /bid 75L = 75 lakh. Shared bid buttons always carry their displayed price.',
            '/aboard · /asquad · /apurse · /arules · /adminhelp',
            '/commands searches every command and alias, including retention, picks, settings, trades and publishing.',
            'Private guided controls use whole lakh: 100 = 1 Cr. /cancel stops an unfinished input.',
            'For a full season draft: New draft or Existing leagues → Draft. /dhelp explains prices, rotating rounds and /dpick confirmations.',
            'Auction Managers manage auctions; Draft Admins manage drafts. Owners and co-owners alone make draft picks and skips.']
    else:raise A.AuctionError('Open /auction for a fresh menu.')
    if view not in ('home','roles'):buttons+=_rows(actor,sid,[[('🏠 Draft home' if draft else '🏠 Auction home','v:home')]])
    buttons+=_rows(actor,sid,[[('↻ Refresh',f'v:{view}'+(':'+str(argument) if argument else ''))]])
    return ('\n'.join(header) if draft else _lines(header)),InlineKeyboardMarkup(buttons)


async def _send(update,text,markup=None,*,blocks=None):
    from services import rich_message as R
    from services.franchise_draft_delivery import blocks_for_html
    blocks=blocks or blocks_for_html(text)
    query=getattr(update,'callback_query',None)
    if query is not None and getattr(query,'message',None) is not None:
        if len(R.html_parts(text))>1:
            return await R.reply_rich(query.message,blocks,text,reply_markup=markup)
        if await R.edit_rich(query,blocks,text,reply_markup=markup):return
    await R.reply_rich(update.effective_message,blocks,text,reply_markup=markup)


async def _show(update,context,view='home',sid=None,argument=''):
    actor=_actor(update);chat_id=update.effective_chat.id
    sid=sid if sid is not None else (_current_sid(context) if chat_id>0 else None)
    private_views={'control','settings','fields','form','teamroles','preseason','draft','trades','finish','leagues','draftsources'}
    if chat_id<0 and view in private_views:
        # Never show owner IDs, ledgers, private shortlists or forms in a group.
        data=await run_session(get_session,lambda db:read_workspace(db,actor,sid,chat_id,'roles'))
        if not data.get('admin'):raise W.AccessError('Only this season’s admins can use these controls.')
        selected=(data.get('season') or {}).get('id') or sid
        url=_private_url(context,selected)
        rows=[[InlineKeyboardButton('Open private auction controls',url=url)]] if url else []
        await _send(update,'🎛 Open a private chat with this bot and use /auction to manage the competition.',InlineKeyboardMarkup(rows))
        return
    data=await run_session(get_session,lambda db:read_workspace(db,actor,sid,chat_id,view,argument),commit=True)
    actual=(data.get('season') or {}).get('id')
    if actual:context.user_data['franchise_season_id']=actual
    draft=data.get('franchise_draft') or {}
    if chat_id < 0 and view == 'home' and draft:
        view='franchisedraft'
    text,markup=render_workspace(data,actor,actual or sid,view,argument,context,chat_id)
    blocks=None
    # Preserve the same surrounding role and setup text in both renderings.
    # The main draft body uses native tables instead of reparsing them as lines.
    for name in ('board','pool','rulebook'):
        body=draft.get(name)
        if body and body in text and draft.get(name+'_blocks'):
            from services.franchise_draft_rich import blocks_for_html
            before,after=text.split(body,1)
            prefix=blocks_for_html(before) if before.strip() else []
            suffix=blocks_for_html(after) if after.strip() else []
            if prefix is not None and suffix is not None:
                blocks=prefix+draft[name+'_blocks']+suffix
            break
    await _send(update,text,markup,blocks=blocks)


async def menu_handler(update,context):
    _reset_prompt(context)
    args=list(context.args or [])
    sid=None
    view='home'
    if args:
        draft_link=args[0].startswith('draft_')
        value=args[0].removeprefix('draft_' if draft_link else 'auction_')
        if value.isdigit() and int(value)>0:sid=int(value)
        if draft_link:view='franchisedraft' if sid else 'draftseasons'
    try:await _show(update,context,view,sid)
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def help_handler(update,context):
    try:await _show(update,context,'help')
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def roles_handler(update,context):
    try:await _show(update,context,'roles')
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def id_handler(update,context):
    user=update.effective_user;chat=update.effective_chat
    text=f'Your Telegram ID: <code>{int(user.id)}</code>\nChat ID: <code>{int(chat.id)}</code>'
    other=getattr(getattr(update.effective_message,'reply_to_message',None),'from_user',None)
    if other:text+=f'\nReplied user ID: <code>{int(other.id)}</code>'
    await _send(update,text)


async def cancel_handler(update,context):
    _reset_prompt(context)
    await _send(update,'Unfinished input cancelled. Open /auction to continue.')


async def seasons_handler(update,context):
    _reset_prompt(context)
    try:await _show(update,context,'seasons')
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def sample_handler(update,context):
    path=Path(__file__).resolve().parents[1]/'docs'/'franchise-players-example.csv'
    await update.effective_message.reply_document(document=io.BytesIO(path.read_bytes()),
        filename='franchise-players-example.csv',
        caption='<b>📄 Player CSV template</b>\nExample rows only: replace them with your competition’s player list. Then use <code>/importplayers</code> in the bound auction or draft group and upload the edited CSV.',
        parse_mode='HTML')


async def _start_form(update,context,key,sid,team_id=None,role_key=None):
    if update.effective_chat.id<0:
        await _show(update,context,'control',sid);return
    actor=_actor(update)
    if key in ('new','fromleague','newdraft','fromleaguedraft'):
        from services import franchise_draft as D
        allowed=await run_session(get_session,lambda db:D.is_draft_admin(db,actor) if key in ('newdraft','fromleaguedraft') else W.admin_for(db,actor))
        if not allowed:raise W.AccessError('Only the corresponding Auction Managers or Draft Admins can create a season.')
        data={}
    elif key in MANAGER_ROLES:
        from services.admin_ids import is_bot_admin
        data={'bot_admin':is_bot_admin(actor)}
    else:data=await run_session(get_session,lambda db:read_workspace(db,actor,sid,update.effective_chat.id,'form'))
    if key in MANAGER_ROLES and not data.get('bot_admin'):raise W.AccessError('Only Bot Owners and Bot Admins appoint Auction Managers and Draft Admins.')
    if role_key:
        if role_key not in ('owner_tg_id','co_owners','team_managers'):raise A.AuctionError('Choose a team role.')
        record=dict(kind='role',sid=sid,key=role_key,team_id=int(team_id),payload={},step=0)
        _prompt(context,update,record)
        await _send(update,'Send '+_e(role_key.replace('_',' '))+' Telegram IDs. Use clear to remove this assignment. /cancel keeps the current roles.')
        return
    if key not in FORMS:raise A.AuctionError('Choose a form from /auction.')
    payload={}
    step=0
    corrections={'draftprices':'prices','draftcustom':'custom','draftrounds':'rounds','draftpool':'pooladd','draftwithdraw':'poolremove','draftclockpolicy':'clock'}
    if key in corrections and data.get('franchise_draft') and (data.get('season') or {}).get('status')!='setup':
        payload['kind']=corrections[key];key='draftcorrection';step=1
    if key in ('fromleague','fromleaguedraft') and team_id:
        payload['league_id']=int(team_id)
        step=1
    if key in TEAM_FORMS:
        team=next((t for t in data.get('teams',[]) if t['id']==int(team_id or 0)),None)
        if not team:raise A.AuctionError('Choose a franchise first.')
        payload['team_id']=team['id']
        if key=='purse':payload['expected_purse_lakh']=team['purse_lakh']
    record=dict(kind='form',sid=sid,key=key,payload=payload,step=step)
    _prompt(context,update,record)
    hint='Prices accept cr/crore or L/lakh.' if key in ('draftprices','draftcustom') else 'Guided money values use whole lakh.'
    await _send(update,'✏️ '+_e(FORMS[key][1][step][1])+'\n\nSend /cancel to stop. '+hint)


async def new_handler(update,context):
    try:await _start_form(update,context,'new',None)
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def settings_handler(update,context):
    try:await _show(update,context,'settings')
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def bind_handler(update,context):
    actor=_actor(update);chat_id=update.effective_chat.id
    try:
        sid=whole(context.args[0]) if context.args else _current_sid(context)
        if chat_id>0:
            await _start_form(update,context,'bind',sid);return
        if not sid:raise A.AuctionError('Use /bind auction_id in the target group, or create one here with /anew Name.')
        def work(db):
            from models import AuctionSeason
            season=db.get(AuctionSeason,sid)
            if season is None:raise A.AuctionError('No such season.')
            if not W.admin_for(db,actor,season):raise W.AccessError('Only this season’s admins can bind its group.')
            result=_native_perform(db,season,actor,'admin_bind',{'chat_id':chat_id})
            return result
        await run_session(get_session,work,commit=True)
        context.user_data['franchise_season_id']=sid
        await _show(update,context,'home',sid)
    except (A.AuctionError,ValueError,AuctionWorkerBusy) as exc:await _send(update,_e(str(exc),1000))


async def callback_handler(update,context):
    query=update.callback_query
    try:
        _,owner,sid,route=query.data.split(':',3)
        if int(owner)!=_actor(update):
            await query.answer('Open /auction for your own menu.',show_alert=True);return
        sid=int(sid) or None
        await query.answer()
        _reset_prompt(context)
        parts=route.split(':');kind=parts[0]
        if kind=='timp':
            from handlers.franchise_draft_v2 import action_packet
            def preview_import(db):
                season,_=_resolve(db,_actor(update),sid,update.effective_chat.id)
                if season is None:raise A.AuctionError('Select a draft first.')
                if update.effective_chat.id<0:raise A.AuctionError('Open the import controls privately with this bot.')
                return action_packet(db,season,_actor(update),'importteams',{'source_kind':parts[1],'source_id':whole(parts[2])})
            packet=await run_session(get_session,preview_import,commit=True)
            await _send(update,packet['text'],packet.get('markup'),blocks=packet.get('blocks'));return
        if kind=='v':await _show(update,context,parts[1],sid,':'.join(parts[2:]));return
        if kind=='f':await _start_form(update,context,parts[1],sid,int(parts[2]) if len(parts)>2 else None);return
        if kind=='role':await _start_form(update,context,'role',sid,int(parts[2]),parts[1]);return
        if kind=='s':
            key=parts[1]
            if key not in SETTINGS:raise A.AuctionError('Choose a supported setting.')
            if update.effective_chat.id<0:await _show(update,context,'settings',sid);return
            data=await run_session(get_session,lambda db:read_workspace(db,_actor(update),sid,update.effective_chat.id,'settings'))
            current=(data.get('settings') or {}).get(key,(data.get('rules') or {}).get(key,'—'))
            _prompt(context,update,dict(kind='setting',sid=sid,key=key,payload={},step=0))
            field=SETTINGS[key]
            await _send(update,f'<b>{_e(field.label)}</b>\nCurrent: <code>{_e(json.dumps(current,ensure_ascii=False),500)}</code>\n\nSend {_e(field.example,650)}.\n\nMoney uses whole lakh. /cancel keeps the current value.')
            return
        if kind=='dgo':
            operation=parts[1]
            if operation not in ('start','pause','undo'):raise A.AuctionError('Choose a draft control.')
            data=await run_session(get_session,lambda db:read_workspace(db,_actor(update),sid,update.effective_chat.id,'control'))
            if not data.get('admin'):raise W.AccessError('Only Draft Admins can manage drafts.')
            if operation=='undo':
                await _start_form(update,context,'draftundo',sid);return
            if operation=='start':
                result=await run_session(get_session,lambda db:execute_action(db,_actor(update),sid,update.effective_chat.id,'native_fd_start',{}),commit=True)
                packet=result['draft_packet']
                await _send(update,packet['text'],packet.get('markup'),blocks=packet.get('blocks'))
                return
            key=_store_action(context,update.effective_chat.id,sid,'native_fd_'+operation,{})
            await _send(update,'Confirm '+_e(operation)+' for this draft?',InlineKeyboardMarkup(_rows(_actor(update),sid,[[('Confirm','op:'+key),('Back','v:franchisedraft')]])))
            return
        if kind=='go':
            action=parts[1]
            if action not in ('admin_start','admin_pause','admin_next','admin_sold','admin_unsold','admin_undo_bid'):raise A.AuctionError('Choose a live control.')
            data=await run_session(get_session,lambda db:read_workspace(db,_actor(update),sid,update.effective_chat.id,'control'))
            values={}
            if action in ('admin_sold','admin_unsold','admin_undo_bid'):
                live=await run_session(get_session,lambda db:read_workspace(db,_actor(update),sid,update.effective_chat.id,'live'))
                lot=live.get('lot')
                if not lot:raise A.AuctionError('There is no current player.')
                values=dict(lot_id=lot['id'],stage=lot.get('rtm_stage'),expected_bids=lot['bids'])
            key=_store_action(context,update.effective_chat.id,sid,action,values)
            await _send(update,'Confirm '+_e(action[6:].replace('_',' '))+' for this auction? The current lot and price will be checked again.',InlineKeyboardMarkup(_rows(_actor(update),sid,[[('Confirm','op:'+key),('Back','v:control')]])))
            return
        if kind=='op':
            record=context.user_data.get('franchise_menu_actions',{}).pop(parts[1],None)
            if not record or record['sid']!=sid or record['chat_id']!=update.effective_chat.id or time.monotonic()-record['created']>600:
                raise A.AuctionError('This action expired. Open a fresh menu.')
            result=await run_session(get_session,lambda db:execute_action(db,_actor(update),sid,update.effective_chat.id,record['action'],record['values']),commit=True)
            packet=result.get('draft_packet')
            if packet:await _send(update,packet['text'],packet.get('markup'),blocks=packet.get('blocks'))
            else:await _send(update,_e(result.get('message','Auction updated.'))+'\nOpen /auction to see the latest state.')
            return
        raise A.AuctionError('Open /auction for a fresh menu.')
    except (A.AuctionError,ValueError,TypeError,IndexError,KeyError,AuctionWorkerBusy) as exc:
        await _send(update,_e(str(exc) if isinstance(exc,(A.AuctionError,AuctionWorkerBusy)) else 'That input is invalid. Open /auction to try again.',1000))


def _parse_form(kind,value):
    text=str(value).strip()
    skip=text.lower() in ('skip','clear','none','-')
    if kind=='ids':return ids(text)
    if kind=='purse_mode':
        if text.lower() not in ('add','deduct','set'):raise ValueError('Choose add, deduct or set.')
        return text.lower()
    if kind in ('int','optional_int','id','nonnegative'):
        if kind=='optional_int' and skip:return None
        number=whole(text)
        if kind=='id' and not 0<number<10**16:raise ValueError('Send a positive Telegram or player ID.')
        if kind=='nonnegative' and number<0:raise ValueError('Send a non-negative amount. Use deduct mode to remove funds.')
        return number
    if kind=='optional':return None if skip else text
    if not text:raise ValueError('Send a value or /cancel.')
    return text


def _setting_action(db,actor,sid,chat_id,key,value):
    season,_=_resolve(db,actor,sid,chat_id)
    if not season or not W.admin_for(db,actor,season):raise W.AccessError('Only this season’s admins can change settings.')
    action='admin_settings';values={'settings':{key:value}}
    # Safe live controls use their existing service actions; squad/retention
    # rulebook edits retain the shared setup-only guard.
    if key in ('bid_seconds','staged_clock'):action='admin_timer';values={'seconds':' '.join(map(str,value)) if isinstance(value,list) else str(value)}
    elif key in ('direct_bids','focus_mode'):action='admin_direct' if key=='direct_bids' else 'admin_focus';values={'enabled':value}
    elif key=='countdown_seconds':action='admin_countdown';values={'seconds':value}
    elif key=='bid_gap_seconds':action='admin_bid_gap';values={'seconds':value}
    elif key in ('snipe_window_seconds','snipe_extend_seconds','max_extensions'):
        values=dict(window=season.snipe_window_seconds,extend=season.snipe_extend_seconds,max_extensions=season.max_extensions)
        values[{'snipe_window_seconds':'window','snipe_extend_seconds':'extend','max_extensions':'max_extensions'}[key]]=value
        action='admin_snipe'
    elif key=='auto_accelerated':action='admin_accelerated';values={'enabled':value}
    elif key=='increments':
        A.set_increment_rules(db,season,value,by_tg_id=actor)
        A.log_event(db,season,'bot_admin','⚙️ Bot: bid increments updated.',by_tg_id=actor,by_admin=True)
        return dict(message='Bid increment ladder updated.')
    return _native_perform(db,season,actor,action,values)


async def prompt_handler(update,context):
    record=context.user_data.get(PROMPT_KEY)
    if not record or record['chat_id']!=update.effective_chat.id:return
    if time.monotonic()-record['created']>900:
        _reset_prompt(context);await _send(update,'That input expired. Open /auction to start again.');raise ApplicationHandlerStop
    # Consume before awaiting a database worker: two simultaneous replies cannot
    # apply the same step twice or submit a duplicate purse adjustment.
    context.user_data.pop(PROMPT_KEY,None)
    generation=record['generation'];actor=_actor(update);chat_id=update.effective_chat.id
    starting_step=record.get('step',0)
    try:
        raw=update.effective_message.text or ''
        if record['kind']=='setting':
            value=parse_value(record['key'],raw)
            result=await run_session(get_session,lambda db:_setting_action(db,actor,record['sid'],chat_id,record['key'],value),commit=True)
        elif record['kind']=='role':
            key=record['key'];value=ids(raw)
            if key=='owner_tg_id':
                if len(value)>1:raise ValueError('A franchise has one owner. Use the co-owner or manager list for more people.')
                value=value[0] if value else None
            def work(db):
                season,_=_resolve(db,actor,record['sid'],chat_id)
                if not season or not W.admin_for(db,actor,season):raise W.AccessError('Only this season’s admins can change team roles.')
                team=W.team(db,season,record['team_id'])
                payload=dict(team_id=team.id,name=team.name,owner_tg_id=team.owner_tg_id,owner_name=team.owner_name,
                             co_owners=A.co_owner_ids(team),team_managers=A.team_manager_ids(team))
                payload[key]=value
                return _native_perform(db,season,actor,'admin_franchise',payload)
            result=await run_session(get_session,work,commit=True)
        else:
            action,fields=FORMS[record['key']]
            key,label,parser=fields[record['step']]
            record['payload'][key]=_parse_form(parser,raw)
            record['step']+=1
            if record['step']<len(fields):
                if context.user_data.get('franchise_prompt_generation')==generation:context.user_data[PROMPT_KEY]=record
                await _send(update,'✏️ '+_e(fields[record['step']][1])+'\n\n/cancel stops this form.')
                raise ApplicationHandlerStop
            if action=='admin_bind':
                target=record['payload']['chat_id']
                if target>=0:raise ValueError('Use a negative group ID from /id in the group.')
                from telegram.error import TelegramError
                try:await context.bot.get_chat(target)
                except TelegramError:raise ValueError('This bot cannot access that group. Add it to the group and check /id, then try again.') from None
            result=await run_session(get_session,lambda db:execute_action(db,actor,record['sid'],chat_id,action,record['payload']),commit=True)
        if result.get('season_id'):context.user_data['franchise_season_id']=result['season_id']
        packet=result.get('draft_packet')
        if packet:await _send(update,packet['text'],packet.get('markup'),blocks=packet.get('blocks'))
        else:await _send(update,'✅ '+_e(result.get('message','Auction updated.'))+'\nOpen /auction to continue.')
    except ApplicationHandlerStop:raise
    except (A.AuctionError,ValueError,TypeError,IndexError,KeyError,AuctionWorkerBusy) as exc:
        # An action denied after revocation is not kept alive as a pending form.
        if not isinstance(exc,W.AccessError) and context.user_data.get('franchise_prompt_generation')==generation:
            if record['kind']=='form':record['step']=starting_step
            context.user_data[PROMPT_KEY]=record
        await _send(update,_e(str(exc) if isinstance(exc,(A.AuctionError,ValueError,AuctionWorkerBusy)) else 'Check the supplied value and try again.',1000)+'\nSend /cancel to stop.')
    raise ApplicationHandlerStop


def register(app):
    app.add_handler(CommandHandler(['start','auction','menu'],menu_handler))
    app.add_handler(CommandHandler('auctions',seasons_handler))
    app.add_handler(CommandHandler('newauction',new_handler))
    app.add_handler(CommandHandler('bind',bind_handler))
    app.add_handler(CommandHandler('settings',settings_handler))
    app.add_handler(CommandHandler('roles',roles_handler))
    app.add_handler(CommandHandler('id',id_handler))
    app.add_handler(CommandHandler('help',help_handler))
    app.add_handler(CommandHandler('sampleplayers',sample_handler))
    app.add_handler(CommandHandler('cancel',cancel_handler))
    app.add_handler(CallbackQueryHandler(callback_handler,pattern=r'^fa:'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,prompt_handler),group=-1)
