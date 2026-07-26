"""/lpbot — play the /letsplay format against the AI captain.

Same match, same engine, same cards as /letsplay: your own roster, 20 overs,
over-by-over Approach cricket with traits live. The only difference is who is in
the other dugout — the bot fields an auto-built XI (see
``services.bot_xi_builder.build_letsplay_bot_xi``) and answers its own prompts
(``services.bot_captain``).

Every /lpbot match is UNRANKED practice: no career stats, no Player of the
Match credit, no coins or gems, no Win/Loss, no streak. Walk away mid-over and
nothing is charged.

Flow:
  /lpbot
    → your roster is validated as a legal Playing XI (else it won't start)
    → you pick the pitch
    → your XI (auto batting order, tweakable with /change) and the bot's XI
      are shown, with a single Start Toss button
    → you call the toss; if the bot wins it elects bat/bowl at random
    → over-by-over match in the chat, shared with /letsplay and /cipl
"""

import html
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from services.bot_ai import BOT_TG_ID
from services.telegram_user_service import sync_telegram_user
from handlers.match import (
    _active_match_in_chat, _active_match_for_user, _cric_lobby_for_user,
    _chat_busy_message, _user_busy_message)
from handlers.lineup import _get_ordered_roster, validate_xi
from handlers.letsplay import (
    _active_letsplay_in_chat, _dkey, _next_id, _prompt_pitch,
    _rearm_setup_timeout)

logger = logging.getLogger(__name__)

BOT_TEAM_NAME = "Bot XI"


async def lpbot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lpbot — start an unranked Lets Play match against the bot."""
    msg = update.effective_message
    tg = update.effective_user
    chat = update.effective_chat
    if msg is None or tg is None or chat is None:
        return

    session = get_session()
    try:
        host = sync_telegram_user(session, tg)
        if not host:
            await msg.reply_text("❌ Do /debut first to get a roster!")
            return

        if _active_letsplay_in_chat(context, chat.id):
            await msg.reply_text(
                "⚠️ A Lets Play match is already in progress in this chat. "
                "Finish it first before starting another.")
            return

        # Same one-game-per-chat / one-match-per-player gates as /letsplay. Only
        # the human is checked: the bot opponent is a single shared User row that
        # is a participant in every concurrent practice match at once.
        chat_busy = _active_match_in_chat(session, chat.id)
        if chat_busy:
            await msg.reply_text(_chat_busy_message(chat_busy), parse_mode="HTML",
                                 disable_web_page_preview=True)
            return
        host_busy = _active_match_for_user(session, host.id)
        if host_busy:
            await msg.reply_text(_user_busy_message(host_busy), parse_mode="HTML",
                                 disable_web_page_preview=True)
            return
        if _cric_lobby_for_user(context.bot_data, host.id):
            await msg.reply_text(
                "⚠️ You're already in a match lobby. Finish or cancel it first, "
                "then try /lpbot again.")
            return

        host_pairs = _get_ordered_roster(session, host.id)
        session.commit()
        if len(host_pairs) < 11:
            await msg.reply_text(
                f"❌ You need at least 11 players to play. You have "
                f"<b>{len(host_pairs)}</b>. Use /claim, /gspin or /buypack.",
                parse_mode="HTML")
            return
        ok_host, host_errs = validate_xi(host_pairs)
        if not ok_host:
            await msg.reply_text(
                "❌ <b>Your Playing XI is not valid:</b>\n"
                + "\n".join(f"• {e}" for e in host_errs)
                + "\n\nFix it with /autobuild or /swap, then try /lpbot again.",
                parse_mode="HTML")
            return

        bot_user = _bot_user(session)
        host_info = {"user_id": host.id, "tg_id": host.telegram_id,
                     "name": host.username or host.first_name or "You"}
        guest_info = {"user_id": bot_user.id, "tg_id": BOT_TG_ID,
                      "name": BOT_TEAM_NAME}
    except Exception:
        logger.exception("/lpbot setup failed")
        await msg.reply_text("⚠️ Couldn't start the match. Try again.")
        return
    finally:
        session.close()

    # Reuse the /letsplay draft state machine wholesale — there is no invitation
    # to accept, so the draft opens straight at pitch selection.
    invite_id = _next_id(context)
    draft = {
        "invite_id": invite_id, "chat_id": chat.id,
        "is_private": chat.id > 0,
        "status": "pitch",
        "vs_bot": True,
        "host": host_info, "guest": guest_info,
        "host_preview": None,
        "pitch_type": None,
        "host_confirmed": False, "guest_confirmed": False,
        "created_at": datetime.utcnow().isoformat(),
    }
    context.bot_data[_dkey(invite_id)] = draft

    await msg.reply_text(
        "🤖 <b>LETS PLAY vs BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>You:</b> {html.escape(host_info['name'])}\n"
        f"🤖 <b>Opponent:</b> {BOT_TEAM_NAME} <i>(auto-picked XI)</i>\n"
        "⏱️ <b>Format:</b> 20 Overs • Own Roster\n"
        "🎯 <b>Unranked practice</b> — no stats, coins, gems or Win/Loss\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Pick the pitch below to get started.",
        parse_mode="HTML")
    _rearm_setup_timeout(context, invite_id)
    await _prompt_pitch(context, draft)


def _bot_user(session):
    """The singleton bot opponent User row (shared with /vsbot)."""
    from handlers.vsbot import _get_or_create_bot_user
    bot_user = _get_or_create_bot_user(session)
    session.commit()
    return bot_user


async def botmatch_again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``botmatch_again_lp`` / ``botmatch_again_cipl[_league]`` — the Rematch
    button on a finished practice match.

    Runs the same handler the command would, so every guard (chat busy, roster
    legality, league config) applies exactly as on a fresh /lpbot or /ciplbot.
    """
    q = update.callback_query
    parts = (q.data or "").split("_")          # botmatch, again, mode[, league]
    mode = parts[2] if len(parts) > 2 else "lp"
    league = parts[3] if len(parts) > 3 else ""
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("rematch: clearing the button failed", exc_info=True)
    if mode == "cipl":
        from handlers.ciplbot import ciplbot_handler
        # Reopen the same league the finished match was played in.
        context.args = [league] if league else []
        await ciplbot_handler(update, context)
    else:
        await lpbot_handler(update, context)
