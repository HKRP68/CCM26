"""/lpmulti — Lets Play Multi: your own roster vs a Challenge League team.

Two ways to play:

  /lpmulti AI [overs] [chat|app]
      You pick League → Team; that franchise is captained by the AI (the same
      bot captain /lpbot and /ciplbot use).

  /lpmulti [overs] @user   (or reply to them with /lpmulti)
      You play your roster; the invited player accepts, then picks League →
      Team and captains that franchise.

Either way the league team fields its best legal Playing XI (highest OVR under
the league's own rules — ``services.bot_xi_builder.build_league_team_xi``) and
the rest of its squad is the Impact Player bench. From the pitch onward this is
the ordinary /letsplay flow (handlers/letsplay.py): Playing XI card, /change for
the host, toss, over-by-over match.

Every /lpmulti match is UNRANKED: a franchise squad is nobody's collection, so
no career stats, POTM credit, coins, gems or Win/Loss for either side.

Leagues offered are the ones admins tick "Include in Multi" for — the same list
/cmulti (/cipl multi) shows.
"""

import asyncio
import html
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services.bot_ai import BOT_TG_ID
from services.telegram_user_service import sync_telegram_user
from handlers.match import (
    _active_match_in_chat, _active_match_for_user, _cric_lobby_for_user,
    _chat_busy_message, _user_busy_message)
from handlers.lineup import _get_ordered_roster, validate_xi
from handlers.letsplay import (
    LETSPLAY_OVERS, _active_letsplay_in_chat, _cancel_setup_timeout, _dkey,
    _drop_draft, _get_draft, _m, _next_id, _prompt_pitch, _rearm_setup_timeout,
    letsplay_handler)

logger = logging.getLogger(__name__)

AI_ARGS = {"ai", "bot", "cpu"}

USAGE_HTML = (
    "🌐 <b>Lets Play Multi</b> — your own roster vs a league team.\n\n"
    "• <code>/lpmulti AI</code> — you pick League → Team, the AI captains it\n"
    "• <code>/lpmulti @username</code> (or reply with <code>/lpmulti</code>) — "
    "they pick League → Team and captain it\n\n"
    "Shorter match? Add the overs (1-20): <code>/lpmulti AI 5</code> or "
    "<code>/lpmulti 5 @username</code>\n"
    "<i>Unranked — no stats, coins, gems or Win/Loss.</i>")


def split_ai_arg(args):
    """``(is_ai, remaining args)`` — pulls "AI" (any case) out of the args."""
    is_ai, rest = False, []
    for word in list(args or []):
        if not is_ai and str(word).strip().lower() in AI_ARGS:
            is_ai = True
        else:
            rest.append(word)
    return is_ai, rest


async def lpmulti_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lpmulti AI | /lpmulti @user — your roster vs a league team."""
    msg = update.effective_message
    if msg is None or update.effective_user is None:
        return
    is_ai, rest = split_ai_arg(getattr(context, "args", None))
    context.args = rest
    if is_ai:
        await _start_vs_ai(update, context)
        return
    # vs a real user: the /letsplay invitation, with the guest captaining a
    # league team once they accept.
    await letsplay_handler(update, context, multi=True)


async def _start_vs_ai(update, context):
    msg = update.effective_message
    tg = update.effective_user
    chat = update.effective_chat
    if chat is None:
        return

    from handlers.botlevel import (
        PLAY_LABELS, play_mode_for, prompt_difficulty, remember_play_mode,
        split_play_mode, take_pending_choice)
    play_mode, args = split_play_mode(getattr(context, "args", None))
    if play_mode:
        remember_play_mode(context.bot_data, tg.id, play_mode)

    from services.match_formats import extract_overs_arg
    custom, _rest, overs_error = extract_overs_arg(args)
    if overs_error:
        await msg.reply_text(
            f"❌ {overs_error}\nExample: <code>/lpmulti AI 5</code>",
            parse_mode="HTML")
        return
    overs = custom or LETSPLAY_OVERS

    # Difficulty first: the button re-enters here with the choice banked (the
    # over count rides along in the callback so it isn't lost).
    if not take_pending_choice(context):
        await prompt_difficulty(update, context, "lpm",
                                str(custom) if custom else "")
        return

    session = get_session()
    try:
        host = sync_telegram_user(session, tg)
        if not host:
            await msg.reply_text("❌ Do /debut first to get a roster!")
            return
        if _active_letsplay_in_chat(context, chat.id, session):
            await msg.reply_text(
                "⚠️ A Lets Play match is already in progress in this chat. "
                "Finish it first before starting another.")
            return
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
                "then try /lpmulti again.")
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
                + "\n\nFix it with /autobuild or /swap, then try /lpmulti again.",
                parse_mode="HTML")
            return

        from handlers.vsbot import _get_or_create_bot_user
        bot_user = _get_or_create_bot_user(session)
        session.commit()
        host_info = {"user_id": host.id, "tg_id": host.telegram_id,
                     "name": host.username or host.first_name or "You"}
        guest_info = {"user_id": bot_user.id, "tg_id": BOT_TG_ID, "name": "AI"}
    except Exception:
        logger.exception("/lpmulti AI setup failed")
        await msg.reply_text("⚠️ Couldn't start the match. Try again.")
        return
    finally:
        session.close()

    invite_id = _next_id(context)
    draft = {
        "invite_id": invite_id, "chat_id": chat.id,
        "is_private": chat.id > 0,
        "status": "lpm_pick",
        "vs_bot": True, "lpmulti": True,
        "host": host_info, "guest": guest_info,
        "host_preview": None,
        "pitch_type": None,
        "host_confirmed": False, "guest_confirmed": False,
        "created_at": datetime.utcnow().isoformat(),
        "overs": overs,
    }
    context.bot_data[_dkey(invite_id)] = draft

    await msg.reply_text(
        "🌐 <b>LETS PLAY MULTI vs AI</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>You:</b> {html.escape(host_info['name'])} <i>(own roster)</i>\n"
        "🤖 <b>Opponent:</b> a league team of your choice, captained by the AI\n"
        f"⏱️ <b>Format:</b> {overs} Over{'s' if overs != 1 else ''}\n"
        "🎯 <b>Unranked practice</b> — no stats, coins, gems or Win/Loss\n"
        f"🕹️ <b>Play in:</b> {PLAY_LABELS[play_mode_for(context.bot_data, tg.id)]}\n"
        "━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML")
    _rearm_setup_timeout(context, invite_id)
    await prompt_league_picker(context, draft)


# ════════════════════════════════════════════════════════════════════
# League → Team picker
# ════════════════════════════════════════════════════════════════════

def _picker_side(draft):
    """Who picks the league team: the host vs the AI, the guest vs a user."""
    return "host" if draft.get("vs_bot") else "guest"


def _league_keyboard(draft):
    iid = draft["invite_id"]
    rows, row = [], []
    for idx, league in enumerate(draft.get("lpm_leagues") or []):
        label = league.get("short") or league.get("name") or "League"
        row.append(InlineKeyboardButton(label, callback_data=f"lpm_lg_{iid}_{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"lpm_cancel_{iid}")])
    return InlineKeyboardMarkup(rows)


def _team_keyboard(draft):
    iid = draft["invite_id"]
    codes = draft.get("lpm_team_codes") or {}
    rows, row = [], []
    for idx, team in enumerate(draft.get("lpm_teams") or []):
        row.append(InlineKeyboardButton(codes.get(team) or team,
                                        callback_data=f"lpm_tm_{iid}_{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("◀ Leagues", callback_data=f"lpm_lg_{iid}_back"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"lpm_cancel_{iid}"),
    ])
    return InlineKeyboardMarkup(rows)


def _league_prompt(draft):
    picker = draft[_picker_side(draft)]
    who = ("pick the league your AI opponent comes from"
           if draft.get("vs_bot") else "pick the league your team comes from")
    return (f"🌐 <b>Lets Play Multi — League</b>\n\n"
            f"{_m(picker)}, {who}:")


def _team_prompt(draft):
    picker = draft[_picker_side(draft)]
    who = ("pick the team the AI will captain"
           if draft.get("vs_bot") else "pick your team")
    return (f"🌐 <b>Lets Play Multi — {html.escape(draft.get('lpm_league_name') or 'Team')}</b>\n\n"
            f"{_m(picker)}, {who}. Its best XI is auto-picked.")


async def prompt_league_picker(context, draft):
    """Send the League picker for this /lpmulti draft (both modes)."""
    from handlers.challenge import _multi_leagues
    session = get_session()
    try:
        leagues = _multi_leagues(session)
    finally:
        session.close()
    if not leagues:
        await context.bot.send_message(
            draft["chat_id"],
            "❌ No leagues are enabled for Multi yet. An admin can tick "
            "“Include in Multi” on a league in the admin panel.")
        _abort(context, draft)
        return
    draft["lpm_leagues"] = leagues
    sent = await context.bot.send_message(
        draft["chat_id"], _league_prompt(draft), parse_mode="HTML",
        reply_markup=_league_keyboard(draft))
    if sent is not None and getattr(sent, "message_id", None):
        draft["lpm_msg_id"] = sent.message_id


def _abort(context, draft):
    draft["status"] = "cancelled"
    _cancel_setup_timeout(context, draft["invite_id"])
    _drop_draft(context, draft["invite_id"])


async def _edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        logger.debug("lpmulti: picker edit failed", exc_info=True)


def _load_league_teams(league):
    """``(teams, {team: short code})`` for a league — runs in a worker thread."""
    from handlers.challenge import _league_teams, _team_short_code
    from models import ChallengeLeague
    session = get_session()
    try:
        record = session.get(ChallengeLeague, int(league["id"]))
        teams = _league_teams(session, league["key"], record) if record else []
        codes = {t: (_team_short_code(t, league["key"], session) or t)
                 for t in teams}
        return teams, codes
    finally:
        session.close()


def _load_team_xi(league, team):
    """``(xi, bench, error, overseas limits)`` — runs in a worker thread."""
    from models import ChallengeLeague
    from services.bot_xi_builder import build_league_team_xi, league_overseas_limits
    session = get_session()
    try:
        xi, bench, error = build_league_team_xi(session, league["id"], team)
        limits = league_overseas_limits(
            session.get(ChallengeLeague, int(league["id"])))
        return xi, bench, error, limits
    finally:
        session.close()


async def lpmulti_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``lpm_lg_{id}_{idx|back}`` / ``lpm_tm_{id}_{idx}`` / ``lpm_cancel_{id}``."""
    q = update.callback_query
    parts = (q.data or "").split("_")      # lpm, action, id[, choice]
    try:
        action = parts[1]
        invite_id = int(parts[2])
        choice = parts[3] if len(parts) > 3 else ""
    except (IndexError, ValueError):
        await q.answer("Invalid selection.", show_alert=True)
        return
    draft = _get_draft(context, invite_id)
    if not draft or not draft.get("lpmulti"):
        # Timed out, cancelled, or lost to a bot restart (drafts live in
        # memory). Say which, and take the dead buttons away.
        await q.answer("This selection has expired (timed out or the bot "
                       "restarted). Start again with /lpmulti.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("lpmulti: clearing a stale picker failed", exc_info=True)
        return
    if draft.get("status") in ("pitch", "showxi", "toss"):
        # Usually a second tap on a picker that already worked — the first tap
        # moved the draft on to the pitch. Not an error worth an alert.
        await q.answer("✅ Team already picked — carry on below.")
        return
    if draft.get("status") != "lpm_pick":
        await q.answer("This selection is no longer active. Start again "
                       "with /lpmulti.", show_alert=True)
        return

    players = {draft["host"]["tg_id"], draft["guest"]["tg_id"]}
    if action == "cancel":
        if q.from_user.id not in players:
            await q.answer("This button is not for you.", show_alert=True)
            return
        await q.answer("Cancelled.")
        _abort(context, draft)
        await _edit(q, f"❌ Lets Play Multi cancelled by "
                       f"{html.escape(q.from_user.first_name or 'a player')}.")
        return

    if draft.get("lpm_busy"):
        # The first tap is still loading the squad.
        await q.answer("⏳ Setting up your team…")
        return

    picker = draft[_picker_side(draft)]
    if q.from_user.id != picker["tg_id"]:
        await q.answer("Only " + (picker.get("name") or "the picker")
                       + " picks the team.", show_alert=True)
        return

    if action == "lg":
        if choice == "back":
            for key in ("lpm_league", "lpm_league_name", "lpm_teams",
                        "lpm_team_codes"):
                draft.pop(key, None)
            await q.answer("Pick a league.")
            _rearm_setup_timeout(context, invite_id)
            await _edit(q, _league_prompt(draft), _league_keyboard(draft))
            return
        try:
            league = (draft.get("lpm_leagues") or [])[int(choice)]
        except (ValueError, IndexError):
            await q.answer("Invalid league.", show_alert=True)
            return
        draft["lpm_busy"] = True
        try:
            teams, codes = await asyncio.to_thread(_load_league_teams, league)
        except Exception:
            logger.exception("lpmulti: loading %s's teams failed", league.get("name"))
            teams, codes = [], {}
        finally:
            draft.pop("lpm_busy", None)
        if draft.get("status") != "lpm_pick":
            await q.answer("This selection is no longer active.")
            return
        if not teams:
            await q.answer(f"{league['name']} has no teams right now.",
                           show_alert=True)
            return
        draft["lpm_league"] = league
        draft["lpm_league_name"] = league["name"]
        draft["lpm_teams"] = teams
        draft["lpm_team_codes"] = codes
        await q.answer(f"Selected {league['name']}")
        _rearm_setup_timeout(context, invite_id)
        await _edit(q, _team_prompt(draft), _team_keyboard(draft))
        return

    if action == "tm":
        league = draft.get("lpm_league")
        try:
            team = (draft.get("lpm_teams") or [])[int(choice)]
        except (ValueError, IndexError):
            team = None
        if not league or not team:
            await q.answer("Invalid team.", show_alert=True)
            return
        # Loading the squad is real DB work: keep it off the event loop, and
        # mark the draft busy so a second tap while it runs gets "setting up"
        # rather than a second build (or an alarming "no longer active").
        draft["lpm_busy"] = True
        try:
            xi, bench, error, limits = await asyncio.to_thread(
                _load_team_xi, league, team)
        except Exception:
            logger.exception("lpmulti: building %s's XI failed", team)
            xi, bench, error, limits = [], [], "Couldn't load that team. Try another.", (0, 11)
        finally:
            draft.pop("lpm_busy", None)
        if draft.get("status") != "lpm_pick":
            # Cancelled or timed out while the squad was loading.
            await q.answer("This selection is no longer active.")
            return
        if error:
            await q.answer(error, show_alert=True)
            return
        draft["bot_xi"] = xi
        draft["guest_bench"] = bench
        draft["guest_team"] = team
        draft["guest_league_name"] = league["name"]
        draft["guest_overseas"] = list(limits)
        draft["status"] = "pitch"
        await q.answer(f"Selected {team}")
        side_word = "The AI captains" if draft.get("vs_bot") else \
            f"{_m(draft['guest'])} captains"
        await _edit(q, f"🏟️ {side_word} <b>{html.escape(team)}</b> "
                       f"({html.escape(league['name'])}) — best XI auto-picked.")
        _rearm_setup_timeout(context, invite_id)
        await _prompt_pitch(context, draft)
        return

    await q.answer("Invalid selection.", show_alert=True)
