"""/handover @manager (or reply): receiver accepts temporary match control."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from services import match_handover as svc
from services.match_state_store import (
    A_COMPLETED, CasAbort, REV_KEY, get_match_lock, update_state_cas)
from services.telegram_user_service import resolve_command_target

logger = logging.getLogger(__name__)


def _mutate(context, mid, operation):
    def change(state, session):
        try:
            result = operation(session, state)
            state[REV_KEY] = int(state.get(REV_KEY) or 0) + 1
            return result, None
        except ValueError as exc:
            raise CasAbort({'error': str(exc)})
    return update_state_cas(context, mid, change, with_session=True)


async def _ready(context, mid, chat_id):
    from handlers import cipl_play as cp
    if not await cp._flush_dirty_locked(context, mid):
        raise ValueError('Match progress is still saving. Please try again shortly.')
    state = await cp._gs(context, mid)
    if not state or state.get('chat_id') != chat_id:
        raise ValueError('Use handover in the chat where the match is being played.')
    if cp._super_over_active(context, mid):
        raise ValueError('Finish the active Super Over before changing captains.')
    if await cp._get_next_action(context, mid) == A_COMPLETED:
        raise ValueError('The match has finished.')
    return state


async def handover_handler(update, context):
    from handlers import cipl_play as cp
    msg = update.effective_message
    mid, _ = await cp._find_cipl_match_in_chat(context, update.effective_chat.id)
    if mid is None:
        await msg.reply_text('No live tournament match in this chat.')
        return
    session = get_session()
    try:
        target, _ = resolve_command_target(session, update, context, 'handover')
        target_tg = target.telegram_id if target else None
    finally:
        session.close()
    if target_tg is None:
        await msg.reply_text('Use /handover @manager or /handover <Telegram ID>, '
                             'or reply to your co-owner’s message with /handover.')
        return
    lock = get_match_lock(mid)
    try:
        await asyncio.wait_for(lock.acquire(), 15)
    except asyncio.TimeoutError:
        await msg.reply_text('The current over is still playing. Try handover again shortly.')
        return
    try:
        await _ready(context, mid, update.effective_chat.id)
        result = await asyncio.to_thread(
            _mutate, context, mid,
            lambda db, s: svc.offer(db, s, update.effective_user.id, target_tg))
        if result is None or 'error' in result:
            raise ValueError((result or {}).get('error', 'Could not save the handover. Try again.'))
        token = result['token']
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Accept', callback_data=f'ho_accept_{mid}_{token}'),
            InlineKeyboardButton('Decline', callback_data=f'ho_decline_{mid}_{token}'),
            InlineKeyboardButton('Cancel', callback_data=f'ho_cancel_{mid}_{token}')]])
        await msg.reply_text(
            f'🤝 <b>{html.escape(result["team"])} — captain handover</b>\n'
            f'<a href="tg://user?id={target_tg}">{html.escape(result["target_name"])}</a>, '
            'tap Accept within 60 seconds to continue this match.\n'
            'The current captain stays in control until you accept. The turn clock continues.',
            parse_mode='HTML', reply_markup=kb)
    except ValueError as exc:
        await msg.reply_text(str(exc))
    except Exception:
        logger.exception('Could not offer match handover for %s', mid)
        await msg.reply_text('Could not send the handover. Try again after 60 seconds.')
    finally:
        lock.release()


async def handover_callback(update, context):
    q = update.callback_query
    try:
        _, action, raw_mid, token = q.data.split('_')
        mid = int(raw_mid)
    except (ValueError, AttributeError):
        await q.answer('Invalid handover.', show_alert=True)
        return
    lock = get_match_lock(mid)
    try:
        await asyncio.wait_for(lock.acquire(), 15)
    except asyncio.TimeoutError:
        await q.answer('The over is still playing. Try again shortly.', show_alert=True)
        return
    try:
        await _ready(context, mid, q.message.chat.id)
        result = await asyncio.to_thread(
            _mutate, context, mid,
            lambda db, s: svc.answer(db, s, q.from_user.id, token, action))
        if result is None or 'error' in result:
            raise ValueError((result or {}).get('error', 'Could not save the handover. Try again.'))
        await q.answer('Control transferred.' if action == 'accept' else 'Handover closed.')
        if action == 'accept':
            text = (f'🤝 <b>{html.escape(result["team"])} — control transferred</b>\n'
                    f'<a href="tg://user?id={result["target_tg"]}">'
                    f'{html.escape(result["target_name"])}</a> is now playing.\n'
                    'Continue with the current match buttons or Mini App. '
                    'The score, squad and remaining turn time are preserved.')
        else:
            text = 'Handover declined.' if action == 'decline' else 'Handover cancelled.'
        # Do not re-prompt: that would reset either captain's turn clock.
        await q.edit_message_text(text, parse_mode='HTML')
    except ValueError as exc:
        await q.answer(str(exc), show_alert=True)
    except Exception:
        logger.exception('Could not answer match handover for %s', mid)
        await q.answer('Could not complete the handover. Please try again.', show_alert=True)
    finally:
        lock.release()
