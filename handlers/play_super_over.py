"""/superover — invite a friend to a standalone, unranked Super Over."""
import html
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import or_
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from models import Match, User
from services.match_outcome import TYPE_SUPER_OVER, END_AUTO, END_CLEARED_BY_USER, mark_end
from services.telegram_user_service import sync_telegram_user, resolve_command_target

logger = logging.getLogger(__name__)
INVITE_SECONDS = 30
GAME_SECONDS = 20 * 60
ACTIVE = ('pending', 'playing', 'active')
USAGE = ('⚡ <b>Super Over — Unranked</b>\n\n'
         '<code>/superover @username</code> or reply to their message with <code>/superover</code>.\n'
         'Use your Playing XI; choose 3 batters and 1 bowler. Six legal balls, two wickets.\n'
         'For league squads, reply with <code>/sipl</code>, <code>/sbbl</code> or <code>/s[league]</code>.\n'
         'No ranked points, regular rewards or career stats.\n'
         'Cancel without a fine: <code>/superover cancel</code>.')


def _name(user):
    return user.team_name or user.first_name or user.username or 'Player'


def _xi(session, user):
    from handlers.lineup import _get_ordered_roster, validate_xi
    from handlers.letsplay import _xi_to_engine
    pairs = _get_ordered_roster(session, user.id)
    ok, _ = validate_xi(pairs)
    if not ok:
        raise ValueError(f'{_name(user)} needs a valid Playing XI. Use /playingxi, /autobuild or /swap first.')
    return _xi_to_engine(session, pairs[:11])


def _busy(session, context, chat_id, users):
    from handlers.match import _active_match_in_chat, _active_match_for_user, _cric_lobby_for_user, _cric_lobby_key
    from handlers.letsplay import _active_letsplay_in_chat, _draft_is_stale
    from handlers.challenge import _active_draft_in_chat
    # Job queues are best-effort; expired practice games must also release their
    # reservations after a restart or a missed timer.
    session.query(Match).filter(Match.match_type == TYPE_SUPER_OVER, Match.status.in_(ACTIVE),
        Match.expires_at < datetime.utcnow()).update(dict(status='expired', end_reason=END_AUTO), synchronize_session=False)
    session.commit()
    if (_active_match_in_chat(session, chat_id) or _active_letsplay_in_chat(context, chat_id, session)
            or _active_draft_in_chat(context.bot_data, chat_id)
            or context.bot_data.get(_cric_lobby_key(chat_id))):
        return 'A match or lobby is already open in this chat. Finish or cancel it first.'
    ids = {u.id for u in users}
    tgs = {u.telegram_id for u in users}
    for user in users:
        if _active_match_for_user(session, user.id) or _cric_lobby_for_user(context.bot_data, user.id):
            return f'{_name(user)} is already in a match or lobby.'
    for key, draft in list(context.bot_data.items()):
        if not isinstance(draft, dict):
            continue
        if str(key).startswith('lp_') and not draft.get('match_launched') and not _draft_is_stale(draft):
            if any((draft.get(side) or {}).get('tg_id') in tgs for side in ('host', 'guest')):
                return 'One of you already has a Lets Play invitation or setup open.'
        if str(key).startswith('cm_lobby_') and ids.intersection(draft.get('user_ids') or []):
            return 'One of you already has a match lobby open.'
    return None


def _arm_timeout(context, mid, chat_id, status, seconds, message_id=None):
    if getattr(context, 'job_queue', None):
        context.job_queue.run_once(expire_super_over, seconds, name=f'sop_{status}_{mid}',
            data=dict(mid=mid, chat_id=chat_id, status=status, message_id=message_id))


async def expire_super_over(context):
    data = context.job.data
    session = get_session()
    try:
        changed = (session.query(Match).filter_by(id=data['mid'], match_type=TYPE_SUPER_OVER, status=data['status'])
                   .filter(Match.expires_at <= datetime.utcnow())
                   .update(dict(status='expired', end_reason=END_AUTO, completed_at=datetime.utcnow()), synchronize_session=False))
        session.commit()
    finally:
        session.close()
    if changed:
        from services.match_state_store import cleanup_state
        cleanup_state(context, data['mid'])
        await context.bot.send_message(data['chat_id'], '⌛ The unranked Super Over invitation expired.'
                                       if data['status'] == 'pending' else '⌛ The practice Super Over timed out. Start again with /superover.')


async def cancel_super_over(update, context, *, quiet=False):
    """Only a participant can cancel; practice never charges an endmatch fine."""
    session = get_session()
    mid = None
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user:
            match = (session.query(Match).filter(Match.match_type == TYPE_SUPER_OVER,
                     Match.status.in_(ACTIVE), or_(Match.user1_id == user.id, Match.user2_id == user.id))
                     .order_by(Match.id.desc()).first())
            if match:
                mid = match.id
                match.status = 'cancelled'
                match.completed_at = datetime.utcnow()
                mark_end(match, END_CLEARED_BY_USER, by_user_id=user.id)
                session.commit()
    finally:
        session.close()
    if mid:
        from services.match_state_store import cleanup_state
        cleanup_state(context, mid)
        await update.message.reply_text('🛑 Practice Super Over cancelled. No fine or ranked penalty.')
        return True
    if not quiet:
        await update.message.reply_text('No active practice Super Over to cancel.')
    return False


async def superover_handler(update, context):
    if list(getattr(context, 'args', None) or []) == ['cancel']:
        await cancel_super_over(update, context)
        return
    if update.effective_chat.id >= 0:
        await update.message.reply_text('Play in a group so both captains can use the buttons.\n\n' + USAGE, parse_mode='HTML')
        return
    mid = None
    session = get_session()
    try:
        host = sync_telegram_user(session, update.effective_user)
        guest, _ = resolve_command_target(session, update, context, 'superover')
        if not host:
            raise ValueError('Use /debut first, then challenge a friend with /superover.')
        if not guest:
            await update.message.reply_text(USAGE, parse_mode='HTML')
            return
        from services.match_rewards import is_ai_user
        if host.id == guest.id or is_ai_user(guest):
            raise ValueError('Choose another human player for your Super Over.')
        busy = _busy(session, context, update.effective_chat.id, (host, guest))
        if busy:
            raise ValueError(busy)
        _xi(session, host)
        _xi(session, guest)
        match = Match(user1_id=host.id, user2_id=guest.id, chat_id=update.effective_chat.id,
                      match_type=TYPE_SUPER_OVER, overs=1, status='pending',
                      expires_at=datetime.utcnow() + timedelta(seconds=INVITE_SECONDS))
        session.add(match)
        session.commit()
        mid = match.id
        text = (f'⚡ <b>UNRANKED SUPER OVER</b>\n\n{html.escape(_name(host))} challenges '
                f'<a href="tg://user?id={guest.telegram_id}">{html.escape(_name(guest))}</a>.\n'
                'Own Playing XIs · 3 batters · 1 bowler · 6 legal balls · 2 wickets.\n'
                'No ranked points, regular rewards or career stats.\n\nAccept within 30 seconds.')
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Accept', callback_data=f'sop_accept_{mid}'),
            InlineKeyboardButton('❌ Decline', callback_data=f'sop_deny_{mid}')]])
        sent = await update.message.reply_text(text, parse_mode='HTML', reply_markup=keyboard)
        _arm_timeout(context, mid, match.chat_id, 'pending', INVITE_SECONDS, getattr(sent, 'message_id', None))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
    except Exception:
        session.rollback()
        if mid:
            session.query(Match).filter_by(id=mid, status='pending').update(dict(status='cancelled'))
            session.commit()
        logger.exception('/superover invitation failed')
        await update.message.reply_text('Could not send the Super Over invitation. Try again.')
    finally:
        session.close()


async def superover_invite_callback(update, context):
    q = update.callback_query
    mid = None
    claimed = False
    authorized = False
    session = get_session()
    try:
        _, action, token = q.data.split('_')
        mid = int(token)
        match = session.get(Match, mid)
        if not match or match.match_type != TYPE_SUPER_OVER or action not in ('accept', 'deny'):
            await q.answer('Invitation not found.', show_alert=True)
            return
        guest = session.get(User, match.user2_id)
        if not guest or q.from_user.id != guest.telegram_id or q.message.chat_id != match.chat_id:
            await q.answer('Only the invited player can answer here.', show_alert=True)
            return
        authorized = True
        if match.status != 'pending' or not match.expires_at or match.expires_at <= datetime.utcnow():
            if match.status == 'pending':
                match.status = 'expired'
                mark_end(match, END_AUTO)
                session.commit()
            await q.answer('This invitation has ended.', show_alert=True)
            return
        if action == 'deny':
            match.status = 'cancelled'
            mark_end(match, END_CLEARED_BY_USER, guest.id)
            session.commit()
            await q.answer('Declined.')
            await q.edit_message_text('Super Over invitation declined.')
            return
        host = session.get(User, match.user1_id)
        if not host:
            raise ValueError('The inviting player is no longer available.')
        host_xi, guest_xi = _xi(session, host), _xi(session, guest)
        # Claim before the first await: duplicate Accept taps cannot start two games.
        claimed = bool(session.query(Match).filter_by(id=mid, status='pending')
            .filter(Match.expires_at > datetime.utcnow()).update(
            dict(status='playing', expires_at=datetime.utcnow() + timedelta(seconds=GAME_SECONDS)), synchronize_session=False))
        if not claimed:
            await q.answer('This invitation has already been answered.', show_alert=True)
            return
        from services import cipl_match
        from services.match_constants import PITCH_TYPES
        first, second = random.sample([(host, host_xi), (guest, guest_xi)], 2)
        pitch = random.choice(PITCH_TYPES)
        state = cipl_match.build_cipl_state(match_id=mid, overs=1,
            bat_user_id=first[0].id, bowl_user_id=second[0].id,
            bat_user_tg=first[0].telegram_id, bowl_user_tg=second[0].telegram_id,
            bat_xi=first[1], bowl_xi=second[1], bat_team_name=_name(first[0]),
            bowl_team_name=_name(second[0]), chat_id=match.chat_id, pitch_type=pitch)
        state.update(standalone_super_over=True, stats_disabled=True, unranked=True, is_letsplay=True)
        match.batting_first_id, match.bowling_first_id = first[0].id, second[0].id
        match.pitch_type = pitch
        session.commit()
        await q.answer('Accepted!')
        await q.edit_message_text('⚡ Super Over accepted — unranked.\nCancel without a fine: /superover cancel')
        session.refresh(match)
        if match.status != 'playing':
            return
        from handlers.super_over import start_super_over
        if not await start_super_over(context, mid, state):
            raise ValueError('The Super Over could not start. Please try /superover again.')
        _arm_timeout(context, mid, state['chat_id'], 'playing', GAME_SECONDS)
    except (ValueError, TypeError) as exc:
        if authorized and mid:
            _abandon(context, session, mid)
        await q.answer(str(exc), show_alert=True)
    except Exception:
        if claimed and mid:
            _abandon(context, session, mid)
        logger.exception('Standalone Super Over launch failed')
        await q.answer('Could not start the Super Over. Try again.', show_alert=True)
    finally:
        session.close()


def _abandon(context, session, mid):
    session.rollback()
    session.query(Match).filter(Match.id == mid, Match.status.in_(ACTIVE)).update(dict(status='cancelled'))
    session.commit()
    from services.match_state_store import cleanup_state
    cleanup_state(context, mid)
