"""Three-player squads for standalone league Super Overs."""
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _card(draft, side, players):
    from handlers.challenge import _challenge_xi_selection, _mention
    selection = _challenge_xi_selection(draft, side)
    chosen = selection['player_ids']
    captain = draft.get(side) or {}
    team = draft.get('host_team' if side == 'host' else 'target_team') or 'Team'
    lines = ['⚡ <b>SUPER OVER — SELECT 3 PLAYERS</b>',
             f"<b>{html.escape(team)}</b> · {len(chosen)}/3 selected",
             _mention(captain.get('tg_id'), captain.get('name') or 'Captain'),
             'Pick in batting order: two openers and one backup.',
             'One of these same three players will bowl. No full XI required.']
    names = {int(p.id): p.name for p in players}
    lines.extend(f'{i}. {html.escape(names[pid])}' for i, pid in enumerate(chosen, 1) if pid in names)
    if selection.get('confirmed'):
        lines.append('✅ Three-player squad confirmed.')
        return '\n'.join(lines), None
    did = draft['draft_id']
    buttons = [[InlineKeyboardButton(
        f"{'✅ ' if int(p.id) in chosen else ''}{p.name}",
        callback_data=f'sos_pick_{did}_{side}_{p.id}')] for p in players]
    buttons.append([InlineKeyboardButton(f'Confirm 3 players ({len(chosen)}/3)',
                                        callback_data=f'sos_ok_{did}_{side}')])
    buttons.append([InlineKeyboardButton('Cancel', callback_data=f'sos_cancel_{did}_{side}')])
    return '\n'.join(lines), InlineKeyboardMarkup(buttons)


async def send_squad_pickers(context, draft, message_obj):
    from handlers.challenge import (_load_team_players_with_retry, _track_setup_msg,
        _arm_selection_timer, _disarm_selection_timer, _release_draft_chat_lock,
        _challenge_team_draft_key)
    try:
        squads = {side: _load_team_players_with_retry(draft, side)[0]
                  for side in ('host', 'target')}
        if any(len(players) < 3 for players in squads.values()):
            raise ValueError('Both teams need at least three players for a Super Over.')
        for side, players in squads.items():
            text, keyboard = _card(draft, side, players)
            sent = await context.bot.send_message(draft['chat_id'], text,
                parse_mode='HTML', reply_markup=keyboard)
            _track_setup_msg(draft, sent)
        await _arm_selection_timer(context, draft,
            [draft['host_tg_id'], draft['target_tg_id']], 'players')
    except Exception as exc:
        logger.exception('Could not open Super Over squad selection')
        await _disarm_selection_timer(context, draft)
        _release_draft_chat_lock(context.bot_data, draft)
        context.bot_data.pop(_challenge_team_draft_key(draft['draft_id']), None)
        text = str(exc) if isinstance(exc, ValueError) else 'Could not load the squads. Start a new Super Over challenge.'
        await context.bot.send_message(draft['chat_id'], text)


async def super_over_squad_callback(update, context):
    from handlers.challenge import (_challenge_team_draft_key, _challenge_xi_selection,
        _load_team_players_with_retry, _challenge_xi_ready, _touch_selection_timer,
        _arm_selection_timer, _disarm_selection_timer)
    q = update.callback_query
    try:
        _, action, token, side, *rest = q.data.split('_')
        did = int(token)
        if action not in ('pick', 'ok', 'cancel') or side not in ('host', 'target'):
            raise ValueError
        pid = int(rest[0]) if action == 'pick' else None
    except (ValueError, IndexError):
        await q.answer('Invalid squad button.', show_alert=True)
        return
    draft = context.bot_data.get(_challenge_team_draft_key(did))
    if (not draft or not draft.get('standalone_super_over')
            or draft.get('turn') != 'complete' or draft.get('match_launched')):
        await q.answer('This squad selection has ended.', show_alert=True)
        return
    if (q.from_user.id != draft.get(f'{side}_tg_id')
            or q.message.chat_id != draft['chat_id']):
        await q.answer('Only this team’s captain can select these players.', show_alert=True)
        return
    if action == 'cancel':
        from handlers.challenge import _release_draft_chat_lock
        await _disarm_selection_timer(context, draft)
        _release_draft_chat_lock(context.bot_data, draft)
        context.bot_data.pop(_challenge_team_draft_key(did), None)
        await q.answer('Cancelled.')
        await q.edit_message_text('Super Over challenge cancelled.')
        return
    selection = _challenge_xi_selection(draft, side)
    if selection.get('confirmed'):
        await q.answer('Your three players are already confirmed.')
        return
    try:
        players = _load_team_players_with_retry(draft, side)[0]
    except Exception:
        await q.answer('Could not load the squad. Please try again.', show_alert=True)
        return
    available = {int(p.id) for p in players}
    chosen = selection['player_ids']
    chosen[:] = [p for p in chosen if p in available]
    launch = False
    if action == 'pick':
        if pid not in available:
            await q.answer('This player is not available for your team.', show_alert=True)
            return
        if pid in chosen:
            chosen.remove(pid)
        elif len(chosen) < 3:
            chosen.append(pid)
        else:
            await q.answer('Select only three players. Remove a checked player first.', show_alert=True)
            return
        await q.answer(f'{len(chosen)}/3 selected')
        await _touch_selection_timer(context, draft)
    else:
        if len(chosen) != 3:
            await q.answer('Select exactly three players.', show_alert=True)
            return
        selection['confirmed'] = True
        launch = _challenge_xi_ready(draft) and not draft.get('squad_launch_requested')
        if launch:
            draft['squad_launch_requested'] = True
        await q.answer('Three-player squad confirmed!')
        waiting = [draft[f'{s}_tg_id'] for s in ('host', 'target')
                   if not _challenge_xi_selection(draft, s).get('confirmed')]
        await _arm_selection_timer(context, draft, waiting, 'players')
    text, keyboard = _card(draft, side, players)
    try:
        await q.edit_message_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception:
        logger.debug('Could not refresh Super Over squad picker', exc_info=True)
    if launch:
        await _disarm_selection_timer(context, draft)
        from handlers.cipl_play import start_match_launch
        await start_match_launch(context, q, draft, did)
