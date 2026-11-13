"""Telegram controls for a priced, rotating franchise season draft.

Database work finishes before Telegram delivery. Confirmation tokens belong to
their initiating owner and carry their quoted turn and price in the database.
"""
from __future__ import annotations

import html
import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from models import AuctionFranchise, AuctionLot
from services import auction_service as A, auction_webapp as W
from services import franchise_draft as D, rich_message as R
from services.auction_async import run_session, AuctionWorkerBusy, BUSY_MESSAGE

logger = logging.getLogger(__name__)
GROUP_TYPES = ('group', 'supergroup')


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
            [button('❔ Help', 'help')]]
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
    if A.is_auction_admin(db, actor):
        controls = []
        if season.status in (A.STATUS_SETUP, A.STATUS_PAUSED):
            controls.append(InlineKeyboardButton('▶ Start / resume', callback_data=f'fd_a_{actor}_{sid}_start'))
        if season.status == A.STATUS_LIVE:
            controls.append(InlineKeyboardButton('⏸ Pause draft', callback_data=f'fd_a_{actor}_{sid}_pause'))
        if controls:
            rows.append(controls)
        if season.status in (A.STATUS_SETUP, A.STATUS_PAUSED):
            rows.append([InlineKeyboardButton(f'⏱ {minutes} min', callback_data=f'fd_m_{actor}_{sid}_{minutes}')
                         for minutes in (5, 10, 15)])
    return InlineKeyboardMarkup(rows)


def _rules(db, season):
    config = D.get_draft(db, season)
    lines = [f'📜 <b>{_e(season.name)} · Draft rules</b>',
             f'Rounds / maximum squad: <b>{config.rounds}</b>',
             'Each round moves its first team to the end of the order.',
             'Only the current team’s owner or co-owner may pick.',
             'Every signing requires confirmation at the displayed price.',
             D.clock_text(db, season),
             'Voluntary skips and timeouts enter a catch-up round in skip order after scheduled rounds. Skipping again moves the pick to the back.',
             '<b>Overseas players:</b> No limit.',
             '<b>Role minimums:</b> ' + _e(', '.join(f'{role}: {n}' for role, n in A.role_minimums(season).items()) or 'None'),
             '<b>Role maximums:</b> ' + _e(', '.join(f'{role}: {n}' for role, n in A.role_maximums(season).items()) or 'None'),
             '<b>Rating rules:</b> ' + _e(A.rating_rule_line(season)),
             'Custom player prices override rating prices. A missing rating price blocks that signing.',
             '\nManagers: /dteam adds a new franchise during setup; /dtimer sets whole minutes. Use /settings for purse, role and rating rules; /dprices, /dcustom, /drounds and /dorder for draft setup.']
    return '\n'.join(lines)


def _help():
    return ('🏏 <b>Franchise Draft</b>\n\n'
            '<b>Owner / co-owner</b>\n'
            '<code>/dpick Joe Root</code> — review price, then ✅ Sign or ❌ Cancel\n'
            '<code>/draft</code> — current turn, squads and purses\n'
            '<code>/dpool 1</code> · <code>/dsquad Team name</code>\n\n'
            '<code>/dskip</code> — defer your current pick without spending purse\n'
            '<code>/dskips</code> — catch-up picks in skip order after scheduled rounds\n\n'
            '<b>Managers</b>\n'
            '<code>/dnew Premier Season 2</code> — new draft\n'
            '<code>/dsetup 15</code> — enable draft on the selected empty season\n'
            '<code>/dteam New Team | 123456 | 234567,345678</code> — add a team, its owner and optional co-owners during setup\n'
            '<code>/dtimer 10</code> — minutes per pick; default 10, 0 disables it\n'
            'Add teams, owners and co-owners in /auction → Teams &amp; roles. '
            'Import players using /importplayers, just as for an auction.\n'
            '<code>/dprices\n96:23cr\n95:21cr</code>\n'
            '<code>/dcustom\nJoe Root:25.5cr</code>\n'
            '<code>/dorder Team 1 | Team 2 | Team 3</code> or <code>/dorder random</code>\n'
            '<code>/drounds 15</code> · <code>/daddrounds 2</code>\n'
            '<code>/dstart</code> · <code>/dpause</code> · <code>/dresume</code>\n'
            '<code>/dundo</code> — while paused, undo the latest pick or skip; refund any signing\n'
            'Expired turns are automatically skipped. The next team is tagged and gets a fresh timer. Pause freezes the time remaining.\n'
            'After the final round, publish with /apublish or the finish controls in /auction.\n\n'
            '<b>Order example</b>\nRound 1: 1 → 2 → 3 → 4 → 5\n'
            'Round 2: 2 → 3 → 4 → 5 → 1\nRound 3: 3 → 4 → 5 → 1 → 2')


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
    from services.franchise_draft_delivery import blocks_for_html
    return blocks_for_html(text)


def _teams_view(db, season, actor):
    rows = []
    for team in A.franchises(db, season.id):
        rows.append([InlineKeyboardButton(team.name, callback_data=_cb(actor, season.id, 'squad', str(team.id)))])
    return (f'👥 <b>{_e(season.name)} · Squads</b>\nChoose a franchise to see its players, purse and rules progress.',
            InlineKeyboardMarkup(rows + list(_keyboard(db, season, actor).inline_keyboard)))


def _render_view(db, season, actor, view, argument='0'):
    if view == 'board':
        text = D.render_board(db, season)
    elif view == 'pool':
        page = max(1, _whole(argument, 'Page'))
        return D.render_pool(db, season, page=page), _keyboard(db, season, actor, page=page)
    elif view == 'order':
        text = D.render_order(db, season)
    elif view == 'skips':
        page = max(1, _whole(argument, 'Page'))
        return D.render_skips(db, season, page=page), _keyboard(db, season, actor, skip_page=page)
    elif view == 'rules':
        text = _rules(db, season)
    elif view == 'help':
        text = _help()
    elif view == 'teams':
        return _teams_view(db, season, actor)
    elif view == 'squad':
        team = _team(db, season, argument, actor)
        if team is None:
            return _teams_view(db, season, actor)
        text = D.render_squad(db, season, team)
    else:
        raise A.AuctionError('Open /draft for a fresh menu.')
    return text, _keyboard(db, season, actor)


def _price_list(db, season, *, custom=False):
    config = D.get_draft(db, season)
    prices = json.loads((config.custom_prices_json if custom else config.rating_prices_json) or '{}')
    title = 'Custom player prices' if custom else 'Rating prices'
    lines = [f'💰 <b>{title}</b>']
    lot_names = {str(lot.id): lot.name for lot in A.lots(db, season.id)} if custom else {}
    for key, value in sorted(prices.items(), key=lambda item: item[0], reverse=not custom):
        label = lot_names.get(str(key), key)
        lines.append(f'{_e(label)}: <b>{A.render_money(value, season.currency_label)}</b>')
    if not prices:
        lines.append('No prices configured yet.')
    command = '/dcustom\nJoe Root:25.5cr' if custom else '/dprices\n96:23cr\n95:21cr'
    lines.append('\nUpdate using <code>' + command + '</code>. Pause a live draft before changing prices.')
    return '\n'.join(lines)


def execute(db, actor, chat_id, selected_sid, command, raw=''):
    """Run a command in one caller-owned transaction; return detached display data."""
    if command == 'dhelp':
        return dict(text=_help(), markup=None, sid=selected_sid)
    if command == 'dnew':
        A.require_auction_manager(db, actor)
        if not raw:
            raise A.AuctionError('Use /dnew Season name, then add teams and import players in /auction.')
        season = A.create_season(db, raw)
        D.enable(db, season, by_tg_id=actor)
        if chat_id < 0:
            A.bind_chat(db, season, chat_id)
        text = (f'✅ <b>{_e(season.name)}</b> is ready for draft setup.\n'
                'Add teams and assign owners in /auction → Teams &amp; roles; import players with /importplayers.\n'
                'Set /dprices, /drounds and /dorder, then use /dstart. '
                'For a private setup, bind its group with /bind.')
        return dict(text=text, markup=_keyboard(db, season, actor), sid=season.id)
    season = _resolve(db, actor, chat_id, selected_sid, require_draft=command != 'dsetup')
    if command == 'dsetup':
        A.require_auction_manager(db, actor)
        D.enable(db, season, rounds=_whole(raw or '15', 'Rounds'), by_tg_id=actor)
        text = '✅ Draft enabled. Add players and teams, then configure /dprices and /dorder.'
    elif command in ('draft', 'dboard', 'dpool', 'dsquad', 'drules', 'dorder', 'dskips') and (command != 'dorder' or not raw):
        view = {'draft': 'board', 'dboard': 'board', 'dpool': 'pool', 'dsquad': 'squad', 'drules': 'rules', 'dorder': 'order', 'dskips': 'skips'}[command]
        argument = raw or ('1' if view in ('pool', 'skips') else '0')
        if view == 'squad':
            team = _team(db, season, raw, actor)
            view, argument = ('squad', str(team.id)) if team else ('teams', '0')
        text, markup = _render_view(db, season, actor, view, argument)
        return dict(text=text, markup=markup, sid=season.id)
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
        quote = D.preview_pick(db, season, actor, raw)
        lot = db.get(AuctionLot, quote.lot_id)
        team = db.get(AuctionFranchise, quote.franchise_id)
        money = lambda price: A.render_money(price, season.currency_label)
        text = (f'✍️ <b>Sign {_e(lot.name)}?</b>\n'
                f'Team: <b>{_e(team.name)}</b>\n'
                f'{_e(lot.category)} · Rating <b>{lot.rating}</b>\n'
                f'Price: <b>{money(quote.price_lakh)}</b>\n'
                f'Purse after signing: <b>{money(team.purse_remaining_lakh - quote.price_lakh)}</b>\n'
                f'Squad after signing: <b>{team.squad_size + 1}/{D.get_draft(db, season).rounds}</b>\n\n'
                f'{D.clock_text(db, season)}\n'
                'Confirm this signing or cancel to choose another player.')
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Sign', callback_data=f'fd_y_{quote.token}'),
            InlineKeyboardButton('❌ Cancel', callback_data=f'fd_n_{quote.token}')]])
        return dict(text=text, markup=markup, sid=season.id)
    else:
        A.require_auction_manager(db, actor)
        if command == 'dteam':
            parts = [part.strip() for part in raw.split('|')]
            if len(parts) not in (2, 3) or not parts[0]:
                raise A.AuctionError('Use /dteam Team name | owner Telegram ID | co-owner IDs (optional).')
            co_owners = [_whole(i, 'Co-owner ID') for i in re.split(r'[,;\s]+', parts[2]) if i] if len(parts) == 3 else []
            team = D.add_team(db, season, parts[0], _whole(parts[1], 'Owner ID'), co_owners=co_owners, by_tg_id=actor)
            text = f'🏷 <b>{_e(team.name)}</b> added with a full purse and {D.get_draft(db, season).rounds} draft picks. Review /dorder before starting.'
        elif command == 'dtimer':
            if raw:
                D.set_timer(db, season, _whole(raw, 'Minutes'), by_tg_id=actor)
            text = D.clock_text(db, season) + '\nSet with <code>/dtimer 10</code>. Pause before editing the timer; 0 disables it.'
        elif command == 'dprices':
            if raw:
                D.set_rating_prices(db, season, raw, by_tg_id=actor)
            text = _price_list(db, season)
        elif command == 'dcustom':
            if raw:
                D.set_custom_prices(db, season, raw, by_tg_id=actor)
            text = _price_list(db, season, custom=True)
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
            text = D.render_order(db, season)
        elif command in ('dstart', 'dresume'):
            D.start(db, season, by_tg_id=actor)
            text = D.render_board(db, season)
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
    blocks = build_blocks(packet['text'])
    if edit:
        return await R.edit_rich(update.callback_query, blocks, packet['text'], reply_markup=packet.get('markup'))
    return await R.reply_rich(update.effective_message, blocks, packet['text'], reply_markup=packet.get('markup'))


async def _error(update, text, *, callback=False):
    if callback:
        await update.callback_query.answer(str(text)[:180], show_alert=True)
    else:
        await _send(update, dict(text='⚠️ ' + _e(text)))


async def command_handler(update, context):
    try:
        actor = _actor(update)
        chat_id = update.effective_chat.id
        source = (getattr(update.effective_message, 'text', '') or '').split(None, 1)[0]
        command = source.removeprefix('/').split('@', 1)[0].casefold()
        packet = await run_session(get_session, lambda db: execute(
            db, actor, chat_id, context.user_data.get('franchise_season_id'), command, _raw(update, context)), commit=True)
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
            A.require_auction_manager(db, actor)
            D.set_timer(db, season, _whole(parts[4], 'Minutes'), by_tg_id=actor)
        text, markup = _render_view(db, season, actor, 'board')
        return dict(text=text, markup=markup, sid=season.id)
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
            text = f'✅ <b>{_e(lot.name)} signed.</b> The squad and purse have been updated.'
        else:
            D.cancel_pick(db, season, token, actor)
            text = '❌ <b>Signing cancelled.</b> Your team can choose another player with /dpick.'
        return dict(text=text, markup=_keyboard(db, season, actor), sid=season.id)
    parts = data.split('_', 5)
    if len(parts) < 5 or parts[0] != 'fd' or parts[1] not in ('v', 'a'):
        raise A.AuctionError('Open /draft for a fresh menu.')
    owner, sid = _whole(parts[2], 'Owner ID'), _whole(parts[3], 'Season ID')
    if owner != actor:
        raise A.AuctionError('This menu belongs to another user. Open your own /draft.')
    season = _resolve(db, actor, chat_id, selected_sid, expected_sid=sid)
    if parts[1] == 'a':
        A.require_auction_manager(db, actor)
        if parts[4] == 'start':
            D.start(db, season, by_tg_id=actor)
        elif parts[4] == 'pause':
            D.pause(db, season, by_tg_id=actor)
        else:
            raise A.AuctionError('Open /draft for current controls.')
        text, markup = _render_view(db, season, actor, 'board')
    else:
        text, markup = _render_view(db, season, actor, parts[4], parts[5] if len(parts) > 5 else '0')
    return dict(text=text, markup=markup, sid=season.id)


async def callback_handler(update, context):
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
    await update.callback_query.answer()
    await _send(update, packet, edit=True)
