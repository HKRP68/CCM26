"""Telegram controls for a priced, rotating franchise season draft.

Database work finishes before Telegram delivery. Confirmation tokens belong to
their initiating owner and carry their quoted turn and price in the database.
"""
from __future__ import annotations

import html
import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from database import get_session
from models import AuctionFranchise, AuctionLot
from services import auction_service as A, auction_webapp as W
from services import franchise_draft as D, rich_message as R
from services import franchise_draft_rich as DR
from services.auction_async import run_session, AuctionWorkerBusy, BUSY_MESSAGE

logger = logging.getLogger(__name__)
GROUP_TYPES = ('group', 'supergroup')


async def dcall_handler(update, context):
    """/dcall [message]: call draft owners/co-owners, with optional images."""
    from handlers.auction import _team_call
    await _team_call(update, context, draft=True, session_factory=get_session)


def _e(value):
    return html.escape(str(value if value is not None else ''))


def _raw(update, context):
    # context.args has already discarded newlines in pasted pricing tables.
    source = getattr(update.effective_message, 'text', None) or ''
    parts = source.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ' '.join(context.args or []).strip()


def _actor(update):
    user = getattr(getattr(update, 'callback_query', None), 'from_user', None) or update.effective_user
    if user is None:
        raise A.AuctionError('Use your Telegram account to open the draft.')
    return int(user.id)


def _whole(raw, label='Number'):
    if not re.fullmatch(r'[0-9]{1,16}', str(raw).strip()):
        raise A.AuctionError(f'{label} must be a whole number.')
    return int(str(raw).strip())


def _resolve(db, actor, chat_id, selected_sid=None, *, expected_sid=None, require_draft=True):
    if chat_id < 0:
        season = A.season_for_chat(db, chat_id)
        if expected_sid and (season is None or season.id != expected_sid):
            raise A.AuctionError('This button belongs to a different draft group. Open /draft here.')
    else:
        season, _ = W.resolve(db, actor, expected_sid or selected_sid)
    if season is None:
        raise A.AuctionError('Select a season in /auction, or create a draft with /dnew Season name.')
    if require_draft and D.get_draft(db, season) is None:
        raise A.AuctionError('This season uses an auction. A manager can enable the draft before signings with /dsetup 15.')
    return season


def _cb(actor, sid, view, arg='0'):
    value = f'fd_v_{actor}_{sid}_{view}_{arg}'
    if len(value.encode('utf-8')) > 64:
        raise ValueError('Draft button exceeds Telegram callback limit.')
    return value


def _keyboard(db, season, actor, *, page=None, skip_page=None):
    sid = season.id
    button = lambda label, view, arg='0': InlineKeyboardButton(label, callback_data=_cb(actor, sid, view, arg))
    rows = [[button('📋 Draft board', 'board'), button('👥 Squads', 'teams')],
            [button('🔎 Player pool', 'pool', '1'), button('🔄 Round order', 'order')],
            [button('⏭ Skipped picks', 'skips'), button('📜 Rules', 'rules')],
            [button('📋 All draft commands', 'commands')]]
    rows.append([button('💰 Purses','purses'),button('📜 History','history'),button('💼 My franchise','me')])
    if page is not None:
        rows.append([button('◀ Previous', 'pool', str(max(1, page - 1))),
                     button('Next ▶', 'pool', str(page + 1))])
    if skip_page is not None:
        rows.append([button('◀ Previous', 'skips', str(max(1, skip_page - 1))),
                     button('Next ▶', 'skips', str(skip_page + 1))])
    config = D.get_draft(db, season)
    current = D.turn(db, season) if season.status == A.STATUS_LIVE else None
    if current is not None and actor in D.owner_ids(current):
        rows.append([InlineKeyboardButton('⏭ Skip my turn', callback_data=f'fd_s_{actor}_{sid}_{config.revision}')])
    if D.is_draft_admin(db, actor):
        rows.append([button('✅ Pre-draft validation','preview')])
        rows.append([InlineKeyboardButton('🎛 Draft Admin panel', callback_data=_cb(actor,sid,'admin')),
                     InlineKeyboardButton('⚙️ Setup & settings', callback_data=f'fa:{actor}:{sid}:v:franchisedraft')])
        controls = []
        if season.status in (A.STATUS_SETUP, A.STATUS_PAUSED):
            controls.append(InlineKeyboardButton('▶ Start / resume', callback_data=f'fd_a_{actor}_{sid}_start'))
        if season.status == A.STATUS_LIVE:
            controls.append(InlineKeyboardButton('⏸ Pause draft', callback_data=f'fd_a_{actor}_{sid}_pause'))
        if controls:
            rows.append(controls)
        if season.status in (A.STATUS_SETUP, A.STATUS_LIVE, A.STATUS_PAUSED):
            rows.append([InlineKeyboardButton(f'⏱ {minutes} min', callback_data=f'fd_m_{actor}_{sid}_{minutes}')
                         for minutes in (5, 10, 15)])
    return InlineKeyboardMarkup(rows)


def _help():
    from handlers.franchise_commands import draft_commands_html
    return draft_commands_html()


def _admin_panel(db, actor, chat_id, selected_sid=None):
    D.require_draft_admin(db, actor)
    if chat_id < 0:
        season = A.season_for_chat(db, chat_id)
        if season is not None and D.get_draft(db, season) is None:
            season = None
    else:
        rows = [s for s in W.available_seasons(db,actor) if D.get_draft(db,s) is not None]
        season = next((s for s in rows if s.id == selected_sid), next(iter(rows),None))
    text = ('🎛 <b>Draft Admin</b>\n'
            'Manage draft seasons, teams, player imports, prices, purses, squad rules, timers and publishing.\n'
            'Only Bot Owners and Bot Admins appoint Draft Admins. Picks and skips remain owner/co-owner only.\n\n'
            '<code>/draftcommands</code> sends every draft command together.\n'
            '<code>/dadmins</code> lists appointed Draft Admins.\n')
    if season is not None:
        blocks = DR.blocks_for_html(text) or []
        board_blocks,board_text = DR.board(db,season)
        text += '\n\n' + board_text
        blocks += board_blocks
        rows = [list(row) for row in _keyboard(db,season,actor).inline_keyboard]
    else:
        rows = [[InlineKeyboardButton('📋 Create draft',callback_data=f'fa:{actor}:0:f:newdraft')]]
        text += '\nCreate a draft with <code>/dnew Season name</code>, then open <code>/dadmin</code> for its controls.'
        blocks = DR.blocks_for_html(text)
    from services.admin_ids import is_bot_admin
    if is_bot_admin(actor):
        rows.append([InlineKeyboardButton('Appoint Draft Admin',callback_data=f'fa:{actor}:{season.id if season else 0}:f:adddraftmanager'),
                     InlineKeyboardButton('Remove Draft Admin',callback_data=f'fa:{actor}:{season.id if season else 0}:f:deldraftmanager')])
    return dict(text=text,blocks=blocks,markup=InlineKeyboardMarkup(rows),sid=season.id if season else selected_sid)


def _team(db, season, raw, actor):
    if not raw:
        return A.franchise_for_actor(db, season.id, actor)
    rows = A.franchises(db, season.id)
    matches = [team for team in rows if str(team.id) == raw or team.name.casefold() == raw.casefold()]
    if len(matches) != 1:
        raise A.AuctionError('Use one exact team name or its franchise ID from /draft.')
    return matches[0]


def parse_order(db, season, raw):
    """Resolve a pasted seat list without silently dropping unknown teams."""
    if str(raw).strip().casefold() in ('random', 'auto', 'shuffle'):
        return None
    names = [part.strip() for part in re.split(r'[\n|,;]+', str(raw)) if part.strip()]
    if not names:
        raise A.AuctionError('List each franchise exactly once, separated by | or a newline, or send random.')
    return [_team(db, season, name, None).id for name in names]


def build_blocks(text):
    return DR.blocks_for_html(text)


def _teams_view(db, season, actor):
    rows = []
    for team in A.franchises(db, season.id):
        rows.append([InlineKeyboardButton(team.name, callback_data=_cb(actor, season.id, 'squad', str(team.id)))])
    blocks,text=DR.team_directory(db,season)
    return dict(text=text,blocks=blocks,sid=season.id,
                markup=InlineKeyboardMarkup(rows + list(_keyboard(db, season, actor).inline_keyboard)))


def _render_view(db, season, actor, view, argument='0'):
    markup = _keyboard(db,season,actor)
    if view == 'board':
        blocks,text = DR.board(db, season)
    elif view == 'pool':
        page = max(1, _whole(argument, 'Page'))
        blocks,text = DR.pool(db,season,page=page)
        markup = _keyboard(db,season,actor,page=page)
        from handlers.franchise_draft_v2 import pick_buttons
        lots=D._available(db,season)
        page=min(page,max(1,(len(lots)+11)//12))
        markup=InlineKeyboardMarkup(pick_buttons(db,season,actor,lots[(page-1)*12:page*12])+list(markup.inline_keyboard))
    elif view == 'order':
        page=max(1,_whole(argument or '1'))
        blocks,text = DR.order(db, season,page=page)
        markup=InlineKeyboardMarkup(list(markup.inline_keyboard)+[[
            InlineKeyboardButton('◀ Earlier rounds',callback_data=_cb(actor,season.id,'order',str(max(1,page-3)))),
            InlineKeyboardButton('Later rounds ▶',callback_data=_cb(actor,season.id,'order',str(min(D.get_draft(db,season).rounds,page+3))))]])
    elif view == 'purses':
        blocks,text=DR.purses(db,season)
    elif view == 'preview':
        D.require_draft_admin(db,actor)
        blocks,text=DR.readiness(db,season)
    elif view == 'history':
        page=max(1,_whole(argument or '1'))
        blocks,text=DR.history(db,season,page=page)
        markup=InlineKeyboardMarkup(list(markup.inline_keyboard)+[[
            InlineKeyboardButton('◀ Previous events',callback_data=_cb(actor,season.id,'history',str(max(1,page-1)))),
            InlineKeyboardButton('More events ▶',callback_data=_cb(actor,season.id,'history',str(page+1)))]])
    elif view == 'me':
        team=A.franchise_for_actor(db,season.id,actor)
        if team is None:raise A.AuctionError('No franchise is assigned to your Telegram account.')
        blocks,text=DR.squad(db,season,team)
    elif view == 'skips':
        page = max(1, _whole(argument, 'Page'))
        blocks,text = DR.skips(db,season,page=page)
        markup = _keyboard(db,season,actor,skip_page=page)
    elif view == 'rules':
        blocks,text = DR.rules(db, season)
    elif view in ('help','commands'):
        text = _help()
        blocks = build_blocks(text)
    elif view == 'admin':
        return _admin_panel(db,actor,season.chat_id or actor,season.id)
    elif view == 'teams':
        return _teams_view(db, season, actor)
    elif view == 'squad':
        team = _team(db, season, argument, actor)
        if team is None:
            return _teams_view(db, season, actor)
        blocks,text = DR.squad(db, season, team)
    else:
        raise A.AuctionError('Open /draft for a fresh menu.')
    return dict(text=text,blocks=blocks,markup=markup,sid=season.id)


def execute(db, actor, chat_id, selected_sid, command, raw=''):
    """Run a command in one caller-owned transaction; return detached display data."""
    if command=='draftpause':command='dpause'
    if command in ('dhelp','draftcommands','dcommands','dcmds'):
        return dict(text=_help(), markup=None, sid=selected_sid)
    if command == 'dadmin':
        return _admin_panel(db,actor,chat_id,selected_sid)
    if command in ('dadminadd','dadminremove','dadmins'):
        if command == 'dadmins':
            D.require_draft_admin(db,actor)
            blocks,text=DR.admin_directory(db)
            return dict(text=text,blocks=blocks,markup=None,sid=selected_sid)
        else:
            from services.admin_ids import is_bot_admin
            if not is_bot_admin(actor):raise A.AuctionError('Only Bot Owners and Bot Admins can appoint or remove Draft Admins.')
            parts=raw.split(None,1)
            if not parts:raise A.AuctionError(f'Use /{command} TelegramID.')
            target=_whole(parts[0],'Telegram ID')
            if command=='dadminadd':D.add_draft_admin(db,target,name=parts[1] if len(parts)>1 else None,by_tg_id=actor)
            else:D.remove_draft_admin(db,target,by_tg_id=actor)
            text=f'🛡 <b>Draft Admin {"appointed" if command=="dadminadd" else "removed"}</b> · <code>{target}</code>'
        return dict(text=text,markup=None,sid=selected_sid)
    if command == 'dbind':
        D.require_draft_admin(db,actor)
        if chat_id>=0:raise A.AuctionError('Use /dbind draftID in the intended draft group.')
        from models import AuctionSeason
        sid=_whole(raw,'Draft ID') if raw else selected_sid
        season=db.get(AuctionSeason,sid) if sid else None
        if season is None or D.get_draft(db,season) is None:raise A.AuctionError('Choose a draft: /dbind draftID.')
        A.bind_chat(db,season,chat_id)
        return dict(text=f'🔗 <b>{_e(season.name)}</b> bound to this group.',markup=_keyboard(db,season,actor),sid=season.id)
    if command == 'dnew':
        D.require_draft_admin(db, actor)
        if not raw:
            raise A.AuctionError('Use /dnew Season name, then add teams and import players in /auction.')
        season = A.create_season(db, raw)
        D.enable(db, season, by_tg_id=actor)
        if chat_id < 0:
            A.bind_chat(db, season, chat_id)
        text = (f'✅ <b>{_e(season.name)}</b> is ready for draft setup.\n'
                'Add teams and assign owners in /auction → Teams &amp; roles; import players with /importplayers.\n'
                'Set /dprices, /drounds and /dorder, then use /dstart. '
                f'For a private setup, use <code>/dbind {season.id}</code> in the intended group.')
        return dict(text=text, markup=_keyboard(db, season, actor), sid=season.id)
    season = _resolve(db, actor, chat_id, selected_sid, require_draft=command != 'dsetup')
    if command!='dsetup':
        from handlers.franchise_draft_v2 import route
        packet=route(db,season,actor,command,raw)
        if packet is not None:return packet
    if command == 'dsetup':
        D.require_draft_admin(db, actor)
        A.require_auction_manager(db, actor)
        D.enable(db, season, rounds=_whole(raw or '15', 'Rounds'), by_tg_id=actor)
        text = '✅ Draft enabled. Add players and teams, then configure /dprices and /dorder.'
    elif command in ('draft', 'dboard', 'dpool', 'dsquad', 'drules', 'dorder', 'dskips') and (command != 'dorder' or not raw):
        view = {'draft': 'board', 'dboard': 'board', 'dpool': 'pool', 'dsquad': 'squad', 'drules': 'rules', 'dorder': 'order', 'dskips': 'skips'}[command]
        argument = raw or ('1' if view in ('pool', 'skips') else '0')
        if view == 'squad':
            team = _team(db, season, raw, actor)
            view, argument = ('squad', str(team.id)) if team else ('teams', '0')
        return _render_view(db, season, actor, view, argument)
    elif command == 'dpurse' and not raw:
        blocks,text=DR.purses(db,season)
        return dict(text=text,blocks=blocks,markup=_keyboard(db,season,actor),sid=season.id)
    elif command == 'dskip':
        if chat_id >= 0 or season.chat_id != chat_id:
            raise A.AuctionError('Skip your turn publicly in the group bound to this draft.')
        team = D.skip_turn(db, season, actor)
        text = f'⏭ <b>{_e(team.name)} skip their turn.</b> Their pick joins the catch-up queue. No purse deducted.'
    elif command == 'dpick':
        if chat_id >= 0 or season.chat_id != chat_id:
            raise A.AuctionError('Make picks publicly in the group bound to this draft.')
        if not raw:
            raise A.AuctionError('Use /dpick Player name, or a player lot ID from /dpool.')
        try:
            quote = D.preview_pick(db, season, actor, raw)
        except D.AmbiguousDraftPlayer as exc:
            from handlers.franchise_draft_v2 import choice_packet
            return choice_packet(db,season,actor,exc.players)
        lot = db.get(AuctionLot, quote.lot_id)
        team = db.get(AuctionFranchise, quote.franchise_id)
        blocks,text = DR.confirmation(db,season,quote,lot,team)
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Confirm Signing', callback_data=f'fd_y_{quote.token}'),
            InlineKeyboardButton('❌ Cancel', callback_data=f'fd_n_{quote.token}')]])
        return dict(text=text,blocks=blocks, markup=markup, sid=season.id)
    else:
        D.require_draft_admin(db, actor)
        if command in ('downer','dco'):
            parts=[part.strip() for part in raw.split('|',1)]
            if len(parts)!=2:raise A.AuctionError(f'Use /{command} Team name | Telegram ID(s).')
            team=_team(db,season,parts[0],actor)
            if command=='downer':D.update_team_roles(db,season,team,owner_tg_id=_whole(parts[1],'Owner ID'),by_tg_id=actor)
            else:
                ids=[] if parts[1].casefold() in ('clear','none','-') else [_whole(i,'Co-owner ID') for i in re.split(r'[,;\s]+',parts[1]) if i]
                D.update_team_roles(db,season,team,co_owners=ids,by_tg_id=actor)
            text=f'🏷 <b>{_e(team.name)}</b> ownership updated.'
        elif command == 'dpurse':
            if '|' not in raw:
                blocks,text=DR.purses(db,season)
                return dict(text=text,blocks=blocks,markup=_keyboard(db,season,actor),sid=season.id)
            name,price=[part.strip() for part in raw.split('|',1)]
            team=_team(db,season,name,actor)
            from services import auction_management as M
            amount=D.parse_money(price)
            M.adjust_purse(db,season,team,mode='set',amount_lakh=amount,
                expected_purse_lakh=team.purse_remaining_lakh,note='Draft Admin /dpurse',by_tg_id=actor)
            text=f'💰 <b>{_e(team.name)}</b> purse: {A.render_money(team.purse_remaining_lakh,season.currency_label)}.'
        elif command in ('drolemin','drolemax','dratingrule'):
            from services.franchise_bot_settings import parse_value
            key={'drolemin':'role_minimums','drolemax':'role_maximums','dratingrule':'rating_rules'}[command]
            try:value=({} if command!='dratingrule' else []) if raw.casefold()=='clear' else parse_value(key,raw)
            except (ValueError,KeyError,TypeError):raise A.AuctionError('Use BAT=4,BOWL=4,AR=2,WK=1 for roles; 83=4 for rating rules; or clear.') from None
            D.update_squad_rules(db,season,**{{'drolemin':'minimums','drolemax':'maximums','dratingrule':'ratings'}[command]:value},by_tg_id=actor)
            return _render_view(db,season,actor,'rules')
        elif command == 'dpooladd':
            match=re.fullmatch(r'(\d{1,3})\s*[-| ]\s*(\d{1,3})',raw)
            if not match:raise A.AuctionError('Use /dpooladd 80-96 to add a catalogue rating range; /importplayers imports CSV.')
            D._editable(db,season)
            D._claim(db,season)
            D._editable(db,season)
            low,high=int(match[1]),int(match[2])
            if not 1<=low<=high<=999:raise A.AuctionError('Use a valid rating range, for example /dpooladd 80-96.')
            added,skipped,_=A.add_rating_range_to_pool(db,season,low,high)
            text=f'📚 <b>{added} players added</b> · {skipped} already in the draft pool. Set /dprices before starting.'
        elif command == 'dpublish':
            from services import league_season_service as LS
            season_link=LS.season_for_auction(db,season.id)
            try:league=LS.publish_season_squads(db,season_link) if season_link else A.publish_to_league(db,season)
            except LS.LeagueSeasonError as exc:raise A.AuctionError(str(exc)) from None
            text=f'📤 <b>Draft squads published</b> · {_e(league.name)}.'
        elif command == 'dcancel':
            if raw!=season.name:raise A.AuctionError('To cancel, use /dcancel followed by the exact draft name.')
            A.cancel(db,season,by_tg_id=actor)
            text='🚫 <b>Draft cancelled.</b> Squads and purse history are preserved.'
        elif command == 'dteam':
            parts = [part.strip() for part in raw.split('|')]
            if len(parts) not in (2, 3) or not parts[0]:
                raise A.AuctionError('Use /dteam Team name | owner Telegram ID | co-owner IDs (optional).')
            co_owners = [_whole(i, 'Co-owner ID') for i in re.split(r'[,;\s]+', parts[2]) if i] if len(parts) == 3 else []
            team = D.add_team(db, season, parts[0], _whole(parts[1], 'Owner ID'), co_owners=co_owners, by_tg_id=actor)
            text = f'🏷 <b>{_e(team.name)}</b> added with a full purse and {D.get_draft(db, season).rounds} draft picks. Review /dorder before starting.'
        elif command == 'dtimer':
            if raw:
                D.set_timer(db, season, _whole(raw, 'Minutes'), by_tg_id=actor)
            text = D.clock_text(db, season) + '\nSet with <code>/dtimer 10</code>, including while live; 0 disables it. The current team gets a new full window.'
        elif command == 'dprices':
            if raw:
                D.set_rating_prices(db, season, raw, by_tg_id=actor)
            blocks,text=DR.prices(db,season)
            return dict(text=text,blocks=blocks,markup=_keyboard(db,season,actor),sid=season.id)
        elif command == 'dcustom':
            if raw:
                D.set_custom_prices(db, season, raw, by_tg_id=actor)
            blocks,text=DR.prices(db,season,custom=True)
            return dict(text=text,blocks=blocks,markup=_keyboard(db,season,actor),sid=season.id)
        elif command in ('drounds', 'daddrounds'):
            if raw:
                rounds = _whole(raw, 'Rounds')
                if command == 'daddrounds':
                    if not rounds:
                        raise A.AuctionError('Add at least one round.')
                    rounds += D.get_draft(db, season).rounds
                D.configure_rounds(db, season, rounds, by_tg_id=actor)
            text = f'✅ Draft has <b>{D.get_draft(db, season).rounds} rounds</b>; each team’s maximum squad size equals the round count.'
        elif command == 'dorder':
            if raw.casefold() in ('random', 'auto', 'shuffle'):
                D.set_order(db, season, randomize=True, by_tg_id=actor)
            else:
                D.set_order(db, season, parse_order(db, season, raw), by_tg_id=actor)
            return _render_view(db,season,actor,'order')
        elif command in ('dstart', 'dresume'):
            D.start(db, season, by_tg_id=actor)
            return _render_view(db,season,actor,'board')
        elif command == 'dpause':
            D.pause(db, season, by_tg_id=actor)
            text = '⏸ <b>Draft paused.</b> Picks are locked until /dresume.'
        elif command == 'dundo':
            D.undo_last(db, season, by_tg_id=actor)
            text = '↩️ <b>Latest turn undone.</b> Any signing was refunded; any skip was restored. Review /draft, then /dresume.'
        else:
            raise A.AuctionError('Open /dhelp for draft commands.')
    return dict(text=text, markup=_keyboard(db, season, actor), sid=season.id)


async def _send(update, packet, *, edit=False):
    blocks = packet.get('blocks') or build_blocks(packet['text'])
    if update.effective_chat.id>0 and packet.get('markup'):
        rows=packet['markup'].inline_keyboard
        allowed=[list(row) for row in rows if not any((button.callback_data or '').startswith('fd_p_') for button in row)]
        if len(allowed)!=len(rows):
            packet=dict(packet,markup=InlineKeyboardMarkup(allowed),text=packet['text']+'\n\n<i>Open the bound draft group to select and confirm a signing.</i>')
            blocks=(blocks or [])+[R.paragraph('Open the bound draft group to select and confirm a signing.')]
    if edit and len(R.html_parts(packet['text']))>1:
        return await R.reply_rich(update.callback_query.message,blocks,packet['text'],reply_markup=packet.get('markup'))
    if edit:
        return await R.edit_rich(update.callback_query, blocks, packet['text'], reply_markup=packet.get('markup'))
    return await R.reply_rich(update.effective_message, blocks, packet['text'], reply_markup=packet.get('markup'))


async def _error(update, text, *, callback=False):
    if callback:
        await update.callback_query.answer(str(text)[:180], show_alert=True)
    else:
        blocks,fallback=DR.Card('⚠️ Draft action unavailable').text(str(text)).footer('Review /draft or /draftcommands for the current options.').pair()
        await _send(update, dict(text=fallback,blocks=blocks))


async def command_handler(update, context):
    try:
        actor = _actor(update)
        chat_id = update.effective_chat.id
        selected_sid = context.user_data.get('franchise_season_id')
        source = (getattr(update.effective_message, 'text', '') or '').split(None, 1)[0]
        command = source.removeprefix('/').split('@', 1)[0].casefold()
        raw = _raw(update, context)
        replied = getattr(update.effective_message, 'reply_to_message', None)
        document = getattr(replied, 'document', None)
        file_season_id = None
        if command in ('dcustom', 'dcorrect') and document is not None:
            # Check scope and current authority before fetching Telegram files.
            def check_file_scope(db):
                D.require_draft_admin(db, actor)
                return _resolve(db, actor, chat_id, selected_sid).id
            file_season_id = await run_session(get_session, check_file_scope)
            size = getattr(document, 'file_size', None)
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 2_000_000:
                raise A.AuctionError('Send a UTF-8 CSV/TSV document under 2 MB.')
            file = await document.get_file()
            content = bytes(await file.download_as_bytearray())
            if len(content) > 2_000_000:
                raise A.AuctionError('Custom price files must be under 2 MB.')
            try:
                text = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise A.AuctionError('Save the price file as UTF-8 CSV or TSV.') from None
            if command == 'dcorrect':
                parts = raw.split('|')
                if len(parts) not in (2, 3) or parts[0].strip() != 'custom' or not parts[1].strip():
                    raise A.AuctionError('Reply to the file with /dcorrect custom | correction reason | while paused.')
                raw = 'custom | ' + parts[1].strip() + ' | ' + text
            else:
                raw = text
            selected_sid = file_season_id
        def apply_command(db):
            if file_season_id is not None:
                _resolve(db, actor, chat_id, file_season_id, expected_sid=file_season_id)
            return execute(db, actor, chat_id, selected_sid, command, raw)
        packet = await run_session(get_session, apply_command, commit=True)
    except (A.AuctionError, W.AccessError) as exc:
        await _error(update, str(exc))
        return
    except AuctionWorkerBusy:
        await _error(update, BUSY_MESSAGE)
        return
    except Exception:
        logger.exception('Franchise draft command failed')
        await _error(update, 'The draft could not be updated. Try again.')
        return
    if packet.get('sid'):
        context.user_data['franchise_season_id'] = packet['sid']
    await _send(update, packet)


def execute_callback(db, actor, chat_id, selected_sid, data):
    from models import FranchiseDraftConfirmation
    if data.startswith('fd_b_'):
        _,_,sid,view=data.split('_',3)
        season=_resolve(db,actor,chat_id,selected_sid,expected_sid=_whole(sid))
        if view not in ('board','pool','purses','teams','order','history','me'):raise A.AuctionError('Open /draft.')
        return _render_view(db,season,actor,view,'1')
    if data.startswith('fd_p_'):
        parts=data.split('_')
        if len(parts)!=6 or _whole(parts[2])!=actor:raise A.AuctionError('Open your own /dsearch or /dpool.')
        season=_resolve(db,actor,chat_id,selected_sid,expected_sid=_whole(parts[3]))
        if D.get_draft(db,season).revision!=_whole(parts[5]):raise A.AuctionError('This player button is stale. Search again.')
        return execute(db,actor,chat_id,season.id,'dpick','#'+str(_whole(parts[4])))
    if data.startswith('fd_c_'):
        from models import FranchiseDraftAction
        from services import franchise_draft_admin as M
        parts=data.split('_')
        if len(parts)!=4 or parts[2] not in ('y','n'):raise A.AuctionError('Invalid configuration preview.')
        action=db.get(FranchiseDraftAction,parts[3])
        if action is None:raise A.AuctionError('This configuration preview expired.')
        season=_resolve(db,actor,chat_id,selected_sid,expected_sid=action.season_id)
        M.answer(db,season,actor,parts[3],accept=parts[2]=='y')
        packet=_render_view(db,season,actor,'board')
        subject={'adminpick':'Administrator signing','adminskip':'Administrator skip'}.get(action.kind,'Configuration')
        blocks,text=DR.Card('✅ '+subject+' confirmed' if parts[2]=='y' else '❌ '+subject+' cancelled').text(
            'The change was validated and recorded in the audit log.' if parts[2]=='y' else 'No configuration, squad or purse was changed.').pair()
        packet['blocks']=blocks+packet['blocks'];packet['text']=text+'\n\n'+packet['text']
        return packet
    if data.startswith(('fd_s_', 'fd_m_')):
        parts = data.split('_')
        if len(parts) != 5 or _whole(parts[2], 'Owner ID') != actor:
            raise A.AuctionError('This control belongs to another user. Open your own /draft.')
        season = _resolve(db, actor, chat_id, selected_sid, expected_sid=_whole(parts[3], 'Season ID'))
        if parts[1] == 's':
            if chat_id >= 0 or season.chat_id != chat_id:
                raise A.AuctionError('Skip the turn in the draft’s bound group.')
            D.skip_turn(db, season, actor, expected_revision=_whole(parts[4], 'Turn revision'))
        else:
            D.require_draft_admin(db, actor)
            D.set_timer(db, season, _whole(parts[4], 'Minutes'), by_tg_id=actor)
        return _render_view(db, season, actor, 'board')
    if data.startswith(('fd_y_', 'fd_n_')):
        operation, token = data[3], data[5:]
        if not re.fullmatch(r'[0-9a-f]{16,64}', token):
            raise A.AuctionError('This confirmation is invalid. Use /dpick again.')
        quote = db.get(FranchiseDraftConfirmation, token)
        if quote is None or quote.actor_tg_id != actor:
            raise A.AuctionError('Only the owner who requested this signing can answer it.')
        season = _resolve(db, actor, chat_id, selected_sid, expected_sid=quote.season_id)
        if chat_id >= 0 or season.chat_id != chat_id:
            raise A.AuctionError('Confirm the pick in the draft’s bound group.')
        if operation == 'y':
            lot = D.confirm_pick(db, season, token, actor)
            team = db.get(AuctionFranchise, quote.franchise_id)
            blocks,text = DR.receipt(db,season,lot,team)
        else:
            D.cancel_pick(db, season, token, actor)
            footer='No purse deducted.'
            if D.get_draft(db,season).pick_minutes:
                footer+=' The pick timer keeps running.'
            blocks,text = DR.Card('❌ Signing cancelled').text('Your team can choose another player with /dpick.').footer(footer).pair()
        return dict(text=text,blocks=blocks, markup=_keyboard(db, season, actor), sid=season.id)
    parts = data.split('_', 5)
    if len(parts) < 5 or parts[0] != 'fd' or parts[1] not in ('v', 'a'):
        raise A.AuctionError('Open /draft for a fresh menu.')
    owner, sid = _whole(parts[2], 'Owner ID'), _whole(parts[3], 'Season ID')
    if owner != actor:
        raise A.AuctionError('This menu belongs to another user. Open your own /draft.')
    season = _resolve(db, actor, chat_id, selected_sid, expected_sid=sid)
    if parts[1] == 'a':
        D.require_draft_admin(db, actor)
        if parts[4] == 'start':
            return execute(db,actor,chat_id,season.id,'dstart')
        elif parts[4] == 'pause':
            D.pause(db, season, by_tg_id=actor)
        else:
            raise A.AuctionError('Open /draft for current controls.')
        return _render_view(db, season, actor, 'board')
    return _render_view(db, season, actor, parts[4], parts[5] if len(parts) > 5 else '0')


async def callback_handler(update, context):
    # Stop the client's spinner before waiting for the database worker.
    try:
        await asyncio.wait_for(update.callback_query.answer(),2)
    except (TelegramError,asyncio.TimeoutError):
        pass
    try:
        actor = _actor(update)
        packet = await run_session(get_session, lambda db: execute_callback(
            db, actor, update.effective_chat.id, context.user_data.get('franchise_season_id'), update.callback_query.data), commit=True)
    except (A.AuctionError, W.AccessError) as exc:
        await _error(update, str(exc), callback=True)
        return
    except AuctionWorkerBusy:
        await _error(update, BUSY_MESSAGE, callback=True)
        return
    except Exception:
        logger.exception('Franchise draft callback failed')
        await _error(update, 'The draft could not be updated. Reopen /draft.', callback=True)
        return
    context.user_data['franchise_season_id'] = packet['sid']
    await _send(update, packet, edit=True)
