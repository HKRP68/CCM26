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

BOT_COMMANDS = [BotCommand(*row) for row in [
    ('auction','Open your auction workspace'),('start','Open Franchise Auction HQ'),
    ('newauction','Create a new auction'),('bind','Bind an auction to this group'),
    ('settings','Manage auction rules'),('importplayers','Import player CSV into this group'),
    ('sampleplayers','Download the player CSV example'),
    ('roles','Show your current roles'),('id','Show Telegram user and group IDs'),
    ('bid','Bid in the auction group'),('help','How auctions and setup work'),('cancel','Cancel a guided input'),
]]
PROMPT_KEY = 'franchise_prompt'

# form key: shared service action, fields (key, label, parser).
FORMS = {
    'new':('admin_create',[('name','Name of the new auction','text')]),
    'fromleague':('native_from_league',[('league_id','Previous league ID from Existing leagues','id'),('name','Name of the next auction season','text')]),
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
    'previous':('admin_previous_league',[('league_id','Previous published league ID','id')]),
    'previous_team':('admin_previous_team',[('team_name','Exact previous team name','text')]),
    'teamplayer':('admin_team_add_player',[('player_id','Player catalogue ID','id'),('price_lakh','Signing price in whole lakh; 0 is free','int'),('note','Reason for this signing','text')]),
    'removeplayer':('admin_team_remove_player',[('lot_id','Player lot ID to remove','id'),('note','Reason for removal and refund','text')]),
    'clear':('admin_pool_clear',[('confirm_name','Type the exact auction name to clear eligible queued players','text')]),
}
TEAM_FORMS = {'purse','retain','unretain','picks','rtm','previous_team','teamplayer','removeplayer'}
MANAGER_ROLES = {'addmanager','delmanager'}


def _e(value, limit=150):
    return html.escape(str(value or '')[:limit])


def _money(value, currency='₹'):
    return _e(currency,10) + f'{int(value or 0)/100:,.2f} Cr'


def _resolve(db, actor, sid, chat_id):
    if chat_id < 0:
        season=A.season_for_chat(db,chat_id)
        if sid and (not season or season.id!=sid):
            raise W.AccessError('This button belongs to a different auction group. Open /auction here.')
        rows=[season] if season else []
        return season,rows
    return W.resolve(db,actor,sid)


def read_workspace(db, actor, sid, chat_id, view='home', argument=''):
    season,rows=_resolve(db,actor,sid,chat_id)
    if view=='leagues':
        if not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can browse previous leagues.')
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
    api_view={'home':'arena','live':'arena','team':'teams','teams':'teams','myteam':'teams','pool':'pool','rules':'rules',
              'control':'admin','settings':'admin','fields':'admin','form':'admin','teamroles':'admin','finish':'admin',
              'preseason':'preseason','draft':'preseason','trades':'trades','roles':'arena','help':'arena'}.get(view)
    if api_view is None:raise A.AuctionError('Open /auction for a fresh menu.')
    request={'view':api_view}
    if api_view=='pool':request['page']=(max(1,int(argument or 1))-1)//4+1
    if api_view=='preseason' and argument:request['team_id']=int(argument)
    result=W.payload(db,season,actor,rows,request)
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
    if action=='admin_previous_league':
        if not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can link previous leagues.')
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
    header=[f'<b>🏏 Franchise Auction HQ</b>',_e(season.get('name') or 'Your auction workspace'),
            '<b>Roles:</b> '+_e(' · '.join(data.get('role_labels') or ['Spectator']),400)]
    buttons=[]
    if view in ('home','roles'):
        if season:
            counts=data.get('counts') or {}
            header += [f"Status: <b>{_e(season['status'])}</b>",f"{counts.get('sold',0)} sold · {counts.get('queued',0)} waiting"]
            buttons += _rows(actor,sid,[[('🔨 Live room','v:live'),('👥 Squads','v:teams')]])
            if team_ids:buttons += _rows(actor,sid,[[('💼 My franchises','v:myteam'),('📚 Player pool','v:pool:1')],[('📖 Rulebook','v:rules')]])
            if manager:
                field=data.get('teams') or []
                checks=[('Group bound',bool(season.get('chat_id'))),('At least two franchises',len(field)>=2),('Player pool ready',bool(counts.get('total')))]
                header += ['','<b>Setup checklist</b>']+[('✅ ' if ready else '○ ')+label for label,ready in checks]
                buttons += _rows(actor,sid,[[('🎛 Control room','v:control'),('⚙️ All settings','v:settings')],[('🏷 Teams & roles','v:teamroles'),('🛡 Retention','v:preseason')],[('🆕 Expansion draft','v:draft'),('🤝 Trades','v:trades')]])
        else:
            header+=['','Use existing competitions or create the next season. No debut, coins or collecting-game account is required.']
            if manager:header+=['Choose an existing auction, or start from an existing league for retention.']
            else:header+=['Ask your organiser to assign your Telegram ID to a franchise. Open /auction in its group to watch.']
        if manager:buttons+=_rows(actor,sid,[[('➕ New auction','f:new'),('📚 Existing leagues','v:leagues:1')]])
        buttons+=_rows(actor,sid,[[('🔀 Choose auction','v:seasons'),('❓ Help','v:help')]])
    elif view=='seasons':
        header+=['','<b>Choose an auction</b>']
        for row in (data.get('auctions') or [])[:30]:buttons+=_rows(actor,row['id'],[[(f"{row['name'][:30]} · {row['status']}",'v:home')]])
        if not data.get('auctions'):header+=['Your assigned auctions will appear here.']
    elif view=='leagues':
        page=max(1,int(argument or 1))
        header+=['',f'<b>Existing leagues · page {page}</b>',
                 'Create the next auction from a league to carry its franchises and previous-squad rights. Assign owners before starting.']
        for league in data.get('leagues',[]):
            header+=[f"#{league['id']} · {_e(league['name'],70)}"]
            buttons+=_rows(actor,sid,[[(f"Next season: {league['name'][:25]}",f"f:fromleague:{league['id']}")]])
        if not data.get('leagues'):header+=['No leagues found on this page.']
        if page>1:buttons+=_rows(actor,sid,[[('← Previous leagues',f'v:leagues:{page-1}')]])
        if data.get('has_more_leagues'):buttons+=_rows(actor,sid,[[('More leagues →',f'v:leagues:{page+1}')]])
        if season:buttons+=_rows(actor,sid,[[('Link league to this auction','f:previous')]])
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
    elif view=='team':
        pieces=(argument or '').split('-');tid=int(pieces[0]);page=max(1,int(pieces[1] if len(pieces)>1 else 1))
        team=next((t for t in data.get('teams',[]) if t['id']==tid),None)
        if not team:raise A.AuctionError('That franchise is no longer in this auction.')
        header+=['',f"<b>{_e(team['name'])}</b>",'Your team roles: '+_e(' · '.join(team.get('my_role_labels') or []) or 'Viewer'),
                 f"Purse: <b>{_money(team['purse_lakh'],currency)}</b> · RTM {team.get('rtm_left',0)}"]
        squad=team.get('squad') or []
        for p in squad[(page-1)*8:page*8]:header+=[f"• {_e(p['name'],55)} · {p['rating']} · {_money(p.get('price_lakh'),currency)}"]
        if not squad:header+=['No players signed yet.']
        if page>1:buttons+=_rows(actor,sid,[[('← Players',f'v:team:{tid}-{page-1}')]])
        if page*8<len(squad):buttons+=_rows(actor,sid,[[('More players →',f'v:team:{tid}-{page+1}')]])
        if manager:
            buttons+=_rows(actor,sid,[[('💰 Adjust purse',f'f:purse:{tid}'),('🆕 Add player',f'f:teamplayer:{tid}')],
                [('Remove player',f'f:removeplayer:{tid}'),('Team Manager IDs',f'role:team_managers:{tid}')],
                [('Owner ID',f'role:owner_tg_id:{tid}'),('Co-owner IDs',f'role:co_owners:{tid}')],
                [('Retention',f'v:preseason:{tid}'),('Expansion picks',f'f:picks:{tid}')],[('RTM cards',f'f:rtm:{tid}'),('Previous team',f'f:previous_team:{tid}')]])
    elif view=='pool':
        page=max(1,int(argument or 1));start=((page-1)%4)*10
        header+=['',f'<b>Player pool · page {page}</b>']
        for p in data.get('players',[])[start:start+10]:header+=[f"#{p['player_id']} · {_e(p['name'],38)} · {p['rating']} · {_e(p['status'])} · {_money(p['base_lakh'],currency)}"]
        nav=[]
        if page>1:nav.append(('← Previous',f'v:pool:{page-1}'))
        if page*10<data.get('total',0):nav.append(('Next →',f'v:pool:{page+1}'))
        if nav:buttons+=_rows(actor,sid,[nav])
        if manager:header+=['','Import fresh players with /importplayers in the bound group.'];buttons+=_rows(actor,sid,[[('Build rating set','f:pool')]])
    elif view=='rules':
        rules=data.get('rules') or {}
        header+=['','<b>Auction rulebook</b>',f"Purse {_money(rules.get('opening_lakh'),currency)} · squad {rules.get('min_squad')}–{rules.get('max_squad')}",
            f"Overseas {rules.get('min_overseas')}–{rules.get('max_overseas')} · opening clock {rules.get('bid_seconds')}s",
            f"Retention {rules.get('min_retentions')}–{rules.get('max_retentions')} · RTM {'on' if rules.get('rtm_enabled') else 'off'}",
            'The server enforces purse reserves, role/rating limits, turn order, stale bids and deadlines.','Use /arules in the auction group for the complete rule tables.']
    elif view in ('settings','fields'):
        header+=['','<b>All auction settings</b>','Common values use guided replies. Money is whole lakh: 100 = 1 Cr.']
        if view=='settings':
            for group,label in GROUP_LABELS.items():buttons+=_rows(actor,sid,[[(label,'v:fields:'+group+'-1')]])
        else:
            parts=(argument or 'general-1').rsplit('-',1);group=parts[0];page=int(parts[1])
            keys=[key for key,field in SETTINGS.items() if field.group==group]
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
        header+=['','<b>Finish & next season</b>','Actions ask for the exact auction name. Existing clock, squad and trade safeguards remain active.']
        buttons+=_rows(actor,sid,[[('Publish squads','f:publish'),('Clone next season','f:clone')],[('Simulate remainder','f:simulate'),('Restart auction','f:restart')],[('Cancel auction','f:cancelauction')]])
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
            'Private guided controls use whole lakh: 100 = 1 Cr. /cancel stops an unfinished input.',
            'Only global auction roles change settings. Team roles bid and answer RTM for their assigned franchises.']
    else:raise A.AuctionError('Open /auction for a fresh menu.')
    if view not in ('home','roles'):buttons+=_rows(actor,sid,[[('🏠 Auction home','v:home')]])
    buttons+=_rows(actor,sid,[[('↻ Refresh',f'v:{view}'+(':'+str(argument) if argument else ''))]])
    return _lines(header),InlineKeyboardMarkup(buttons)


async def _send(update,text,markup=None):
    query=getattr(update,'callback_query',None)
    if query is not None and getattr(query,'message',None) is not None:
        try:
            await query.edit_message_text(text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
            return
        except Exception as exc:
            from telegram.error import BadRequest
            if isinstance(exc,BadRequest) and 'not modified' in str(exc).lower():return
            if not isinstance(exc,BadRequest):raise
    await update.effective_message.reply_text(text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)


async def _show(update,context,view='home',sid=None,argument=''):
    actor=_actor(update);chat_id=update.effective_chat.id
    sid=sid if sid is not None else (_current_sid(context) if chat_id>0 else None)
    private_views={'control','settings','fields','form','teamroles','preseason','draft','trades','finish','leagues'}
    if chat_id<0 and view in private_views:
        # Never show owner IDs, ledgers, private shortlists or forms in a group.
        data=await run_session(get_session,lambda db:read_workspace(db,actor,sid,chat_id,'roles'))
        if not data.get('admin'):raise W.AccessError('Only Auction Managers can use these controls.')
        selected=(data.get('season') or {}).get('id') or sid
        url=_private_url(context,selected)
        rows=[[InlineKeyboardButton('Open private auction controls',url=url)]] if url else []
        await _send(update,'🎛 Open a private chat with this bot and use /auction to manage the competition.',InlineKeyboardMarkup(rows))
        return
    data=await run_session(get_session,lambda db:read_workspace(db,actor,sid,chat_id,view,argument),commit=True)
    actual=(data.get('season') or {}).get('id')
    if actual:context.user_data['franchise_season_id']=actual
    text,markup=render_workspace(data,actor,actual or sid,view,argument,context,chat_id)
    await _send(update,text,markup)


async def menu_handler(update,context):
    _reset_prompt(context)
    args=list(context.args or [])
    sid=None
    if args:
        value=args[0].removeprefix('auction_')
        if value.isdigit() and int(value)>0:sid=int(value)
    try:await _show(update,context,'home',sid)
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
        caption='Example rows only: replace them with your competition’s player list. Then use /importplayers in the auction group and upload the edited CSV.')


async def _start_form(update,context,key,sid,team_id=None,role_key=None):
    if update.effective_chat.id<0:
        await _show(update,context,'control',sid);return
    actor=_actor(update)
    if key in ('new','fromleague'):
        allowed=await run_session(get_session,lambda db:W.admin_for(db,actor))
        if not allowed:raise W.AccessError('Only Auction Managers can create an auction.')
        data={}
    else:data=await run_session(get_session,lambda db:read_workspace(db,actor,sid,update.effective_chat.id,'form'))
    if key in MANAGER_ROLES and not data.get('bot_admin'):raise W.AccessError('Only Bot Owners and Bot Admins appoint Auction Managers.')
    if role_key:
        if role_key not in ('owner_tg_id','co_owners','team_managers'):raise A.AuctionError('Choose a team role.')
        record=dict(kind='role',sid=sid,key=role_key,team_id=int(team_id),payload={},step=0)
        _prompt(context,update,record)
        await _send(update,'Send '+_e(role_key.replace('_',' '))+' Telegram IDs. Use clear to remove this assignment. /cancel keeps the current roles.')
        return
    if key not in FORMS:raise A.AuctionError('Choose a form from /auction.')
    payload={}
    step=0
    if key=='fromleague' and team_id:
        payload['league_id']=int(team_id)
        step=1
    if key in TEAM_FORMS:
        team=next((t for t in data.get('teams',[]) if t['id']==int(team_id or 0)),None)
        if not team:raise A.AuctionError('Choose a franchise first.')
        payload['team_id']=team['id']
        if key=='purse':payload['expected_purse_lakh']=team['purse_lakh']
    record=dict(kind='form',sid=sid,key=key,payload=payload,step=step)
    _prompt(context,update,record)
    await _send(update,'✏️ '+_e(FORMS[key][1][step][1])+'\n\nSend /cancel to stop. Guided money values use whole lakh.')


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
            if not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can bind groups.')
            from models import AuctionSeason
            season=db.get(AuctionSeason,sid)
            if season is None:raise A.AuctionError('No such auction.')
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
            await _send(update,_e(result.get('message','Auction updated.'))+'\nOpen /auction to see the latest state.')
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
    if not season or not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can change settings.')
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
                if not season or not W.admin_for(db,actor):raise W.AccessError('Only Auction Managers can change team roles.')
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
        await _send(update,'✅ '+_e(result.get('message','Auction updated.'))+'\nOpen /auction to continue.')
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
