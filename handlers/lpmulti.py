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

Once the league team is picked the host chooses Traits — Yes or No. Yes plays
the host's roster with its traits and gives the league team one role-matched
trait per player (the /wpmbot pattern) at the highest trait level in the host's
XI. No plays the match with no traits on either side.

Every /lpmulti match is UNRANKED: a franchise squad is nobody's collection, so
no career stats, POTM credit, coins, gems or Win/Loss for either side.

Leagues offered are the ones admins tick "Include in Multi" for — the same list
/cmulti (/cipl multi) shows.
"""

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
    _draft_side_xi, _drop_draft, _get_draft, _m, _next_id, _prompt_pitch,
    _rearm_setup_timeout, letsplay_handler)

logger = logging.getLogger(__name__)

AI_ARGS = {"ai", "bot", "cpu"}

USAGE_HTML = (
    "🌐 <b>Lets Play Multi</b> — your own roster vs a league team.\n\n"
    "• <code>/lpmulti AI</code> — you pick League → Team, the AI captains it\n"
    "• <code>/lpmulti @username</code> (or reply with <code>/lpmulti</code>) — "
    "they pick League → Team and captain it\n\n"
    "Shorter match? Add the overs (1-20): <code>/lpmulti AI 5</code> or "
    "<code>/lpmulti 5 @username</code>\n"
    "After the team is picked you choose <b>Traits: Yes / No</b> — Yes gives "
    "the league team traits at your highest trait level.\n"
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


# ════════════════════════════════════════════════════════════════════
# Traits — Yes / No (host only), after the team is picked
# ════════════════════════════════════════════════════════════════════

def host_max_trait_level(xi):
    """The highest trait level carried by any player in an engine-dict XI."""
    best = 0
    for player in xi or []:
        for trait in (player or {}).get("traits") or []:
            if isinstance(trait, dict):
                try:
                    best = max(best, int(trait.get("level") or 0))
                except (TypeError, ValueError):
                    continue
    return best


def apply_league_traits(players, level):
    """Copies of ``players`` with one role-matched trait each at ``level``.

    The same pattern /wpmbot's opponent uses (``handlers.vsbot``): one trait
    per player by category, bowlers rotating through a few keys. Level 0 (the
    host has no traits) leaves every player untraited. Always overwrites, so
    calling it again with a new level never stacks traits.
    """
    from handlers.vsbot import _ADAPTIVE_TRAIT_BY_CATEGORY
    from services.bot_xi_builder import trait_dict
    out, rotation = [], 0
    for player in players or []:
        p = dict(player)
        traits = []
        if level > 0:
            keys = _ADAPTIVE_TRAIT_BY_CATEGORY.get(p.get("category"), ["bat_anchor"])
            effect_key = keys[rotation % len(keys)]
            if len(keys) > 1:
                rotation += 1
            traits = [t for t in (trait_dict(effect_key, level),) if t]
        p["traits"] = traits
        out.append(p)
    return out


def _traits_keyboard(draft):
    iid = draft["invite_id"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Yes — with Traits", callback_data=f"lpm_tr_{iid}_yes"),
         InlineKeyboardButton("🚫 No — without", callback_data=f"lpm_tr_{iid}_no")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"lpm_cancel_{iid}")],
    ])


def _traits_prompt(draft):
    return (f"⚡ <b>Lets Play Multi — Traits</b>\n\n"
            f"{_m(draft['host'])}, play with traits?\n"
            "• <b>Yes</b> — your team keeps its traits and "
            f"<b>{html.escape(draft.get('guest_team') or 'the league team')}</b> "
            "gets one trait per player at your highest trait level\n"
            "• <b>No</b> — no traits on either side")


def _abort(context, draft):
    draft["status"] = "cancelled"
    _cancel_setup_timeout(context, draft["invite_id"])
    _drop_draft(context, draft["invite_id"])


async def _edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        logger.debug("lpmulti: picker edit failed", exc_info=True)


async def lpmulti_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``lpm_lg_{id}_{idx|back}`` / ``lpm_tm_{id}_{idx}`` / ``lpm_tr_{id}_{yes|no}``
    / ``lpm_cancel_{id}``."""
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
    status = (draft or {}).get("status")
    # League/Team buttons belong to the picker stage, Yes/No to the traits
    # stage; Cancel works in either.
    expected = {"lg": ("lpm_pick",), "tm": ("lpm_pick",), "tr": ("lpm_traits",)}
    allowed = expected.get(action, ("lpm_pick", "lpm_traits"))
    if not draft or not draft.get("lpmulti") or status not in allowed:
        await q.answer("This team selection is no longer active.", show_alert=True)
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

    if action == "tr":
        await _choose_traits(q, context, draft, choice)
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
        from handlers.challenge import _league_teams, _team_short_code
        from models import ChallengeLeague
        session = get_session()
        try:
            record = session.get(ChallengeLeague, int(league["id"]))
            teams = _league_teams(session, league["key"], record) if record else []
            codes = {t: (_team_short_code(t, league["key"], session) or t)
                     for t in teams}
        finally:
            session.close()
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
        from models import ChallengeLeague
        from services.bot_xi_builder import (build_league_team_xi,
                                             league_overseas_limits)
        session = get_session()
        limits = (0, 11)
        try:
            xi, bench, error = build_league_team_xi(session, league["id"], team)
            limits = league_overseas_limits(
                session.get(ChallengeLeague, int(league["id"])))
        except Exception:
            logger.exception("lpmulti: building %s's XI failed", team)
            xi, bench, error = [], [], "Couldn't load that team. Try another."
        finally:
            session.close()
        if error:
            await q.answer(error, show_alert=True)
            return
        draft["bot_xi"] = xi
        draft["guest_bench"] = bench
        draft["guest_team"] = team
        draft["guest_league_name"] = league["name"]
        draft["guest_overseas"] = list(limits)
        draft["status"] = "lpm_traits"
        await q.answer(f"Selected {team}")
        side_word = "The AI captains" if draft.get("vs_bot") else \
            f"{_m(draft['guest'])} captains"
        await _edit(q, f"🏟️ {side_word} <b>{html.escape(team)}</b> "
                       f"({html.escape(league['name'])}) — best XI auto-picked.")
        _rearm_setup_timeout(context, invite_id)
        sent = await context.bot.send_message(
            draft["chat_id"], _traits_prompt(draft), parse_mode="HTML",
            reply_markup=_traits_keyboard(draft))
        if sent is not None and getattr(sent, "message_id", None):
            draft["lpm_msg_id"] = sent.message_id
        return

    await q.answer("Invalid selection.", show_alert=True)


async def _choose_traits(q, context, draft, choice):
    """``lpm_tr_{id}_{yes|no}`` — the host decides whether traits play."""
    if q.from_user.id != draft["host"]["tg_id"]:
        await q.answer("Only " + (draft["host"].get("name") or "the host")
                       + " chooses traits.", show_alert=True)
        return
    if choice not in ("yes", "no"):
        await q.answer("Invalid choice.", show_alert=True)
        return
    team = html.escape(draft.get("guest_team") or "The league team")
    if choice == "no":
        draft["traits_enabled"] = False
        text = "🚫 <b>Traits: No</b> — this match plays without traits."
    else:
        draft["traits_enabled"] = True
        session = get_session()
        try:
            level = host_max_trait_level(_draft_side_xi(session, draft, "host"))
        finally:
            session.close()
        draft["lpm_trait_level"] = level
        draft["bot_xi"] = apply_league_traits(draft.get("bot_xi"), level)
        draft["guest_bench"] = apply_league_traits(draft.get("guest_bench"), level)
        if level:
            text = (f"⚡ <b>Traits: Yes</b> — your traits play, and <b>{team}</b> "
                    f"gets one trait per player at <b>Lv.{level}</b> "
                    "(your highest).")
        else:
            text = ("⚡ <b>Traits: Yes</b> — but your XI has no traits equipped, "
                    f"so <b>{team}</b> plays without them too.")
    await q.answer("Traits: " + ("Yes" if choice == "yes" else "No"))
    await _edit(q, text)
    draft["status"] = "pitch"
    _rearm_setup_timeout(context, draft["invite_id"])
    await _prompt_pitch(context, draft)
