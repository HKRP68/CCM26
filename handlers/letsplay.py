"""/letsplay — a friendly head-to-head match played with each user's OWN roster.

This reuses the entire Challenge League over-by-over "Approach" engine
(services/cipl_match + handlers/cipl_play): the only thing /letsplay adds in
front of it is a personal invitation → pitch → auto Playing-XI → toss flow.
Once the toss is decided we build the same ``cipl_approach`` match state (but
from personal-roster players, with their TRAITS attached) and hand off to the
shared per-over prompts/callbacks, scorecard, completion and Mini App view.

Both sides MUST already hold a legal Playing XI to start — /letsplay validates
each roster's top-11 (same "your XI is not valid" report as /playmatch) before
the invite is even sent, and refuses to start otherwise.

Flow:
  /letsplay  (reply to a user, or "/letsplay @user")
    → both rosters validated as a legal Playing XI (else it won't start)
    → invitation to the guest (Accept / Deny, 30s timer)
    → host picks the pitch
    → each side's XI batting order is auto-set by batting rating (high → low),
      both XIs are shown, and a single Start Toss button appears
    → toss (guest calls heads/tails, winner elects bat/bowl)
    → over-by-over match in the chat (shared with /cipl), traits active.
"""

import html
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match, User, UserRoster, Player, PlayerTrait, Trait
from services.match_constants import PITCH_TYPES, MATCH_EXPIRE, random_match_settings
from services.telegram_user_service import sync_telegram_user, resolve_command_target
from services.match_state_store import save_state, A_PICK_CIPL_BOWLER
from services import cipl_match
from handlers.match import (
    _mention, _active_match_in_chat, _active_match_for_user,
    _cric_lobby_for_user, _chat_busy_message, _user_busy_message)
from handlers.lineup import _get_ordered_roster, validate_xi

logger = logging.getLogger(__name__)

INVITE_TIMEOUT = 30    # seconds the guest has to accept/deny
SETUP_TIMEOUT = 180    # seconds a setup stage (pitch/xi/toss) may stall before
                       # the accepted draft is abandoned and the chat freed
LETSPLAY_OVERS = 20    # /letsplay is always a 20-over contest

_PITCH_EMOJI = {
    "Dry": "🟫", "Dusty": "🟤", "Hard": "🟩",
    "Flat": "⬜", "Green": "🌿", "Bouncy": "🔵",
}


# ════════════════════════════════════════════════════════════════════
# Draft store (one in-memory dict per pending invitation)
# ════════════════════════════════════════════════════════════════════

def _dkey(invite_id):
    return f"lp_{invite_id}"


def _next_id(context):
    n = int(context.bot_data.get("_lp_seq", 0)) + 1
    context.bot_data["_lp_seq"] = n
    return n


def _get_draft(context, invite_id):
    return context.bot_data.get(_dkey(invite_id))


def _drop_draft(context, invite_id):
    context.bot_data.pop(_dkey(invite_id), None)


def _m(info):
    return _mention(info["tg_id"], info["name"])


def _active_letsplay_in_chat(context, chat_id):
    """True if a Lets Play invite/setup or live match is already running here.

    Guards against two concurrent /letsplay matches stepping on each other's
    per-over messages in the same chat. Checks both pending drafts and live
    ``cipl_approach`` states flagged ``is_letsplay`` (a completed match has its
    state cleaned up, so a lingering one means it's still live).
    """
    for k, v in list(context.bot_data.items()):
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        if k.startswith("lp_") and v.get("chat_id") == chat_id \
                and v.get("status") in ("pending", "pitch", "showxi", "toss"):
            return True
        if k.startswith("ms_") and v.get("chat_id") == chat_id \
                and v.get("is_letsplay") and v.get("mode") == "cipl_approach":
            return True
    return False


# ════════════════════════════════════════════════════════════════════
# Roster helpers
# ════════════════════════════════════════════════════════════════════

def _roster_traits(session, roster_id):
    """Active traits for a UserRoster entry, formatted for the trait engine."""
    if not roster_id:
        return []
    try:
        rows = (session.query(PlayerTrait, Trait)
                .join(Trait, PlayerTrait.trait_id == Trait.id)
                .filter(PlayerTrait.roster_id == roster_id,
                        Trait.is_active == True)  # noqa: E712
                .all())
        return [{"effect_key": t.effect_key, "level": pt.level,
                 "display_name": t.name, "emoji": t.emoji,
                 "category": t.category}
                for pt, t in rows]
    except Exception:
        logger.exception("letsplay trait fetch failed for roster %s", roster_id)
        return []


def _xi_to_engine(session, pairs):
    """Convert ordered (UserRoster, Player) pairs into engine player dicts.

    ``roster_id`` is the UserRoster id (stable, owns the traits + career stats);
    ``player_id`` is the master Player id (career stats are keyed by
    user+player). Selection order == batting order.
    """
    out = []
    for entry, player in pairs:
        out.append({
            "roster_id": int(entry.id),
            "player_id": int(player.id),
            "name": player.name or "Player",
            "rating": int(player.rating or 50),
            "category": player.category or "Batsman",
            "bat_rating": int(player.bat_rating or 50),
            "bowl_rating": int(player.bowl_rating or 40),
            "bowl_style": player.bowl_style or "",
            "bowl_hand": player.bowl_hand or "Right",
            "bat_hand": player.bat_hand or "Right",
            "traits": _roster_traits(session, entry.id),
        })
    return out


def _format_batting_order(pairs, header):
    """Numbered 1-11 batting order (roster order positions) for the XI card."""
    lines = [header]
    for i, (_entry, player) in enumerate(pairs[:11], start=1):
        cat = player.category or ""
        tag = ""
        if cat == "Wicket Keeper":
            tag = " 🧤"
        elif cat == "All-rounder":
            tag = " ⚡"
        elif cat == "Bowler":
            tag = " 🎯"
        nm = html.escape(str(player.name))
        lines.append(f"{i:>2}. {nm} <i>({player.rating})</i>{tag}")
    return "\n".join(lines)


def _pairs_from_roster_ids(session, user_id, roster_ids):
    """Rebuild ordered (UserRoster, Player) pairs from a snapshot of roster ids.

    Used at launch so each side plays the EXACT XI it confirmed — re-reading the
    live roster order would let a player /swap or /autobuild between confirmation
    and the toss and field an XI the opponent never saw. Any roster row that has
    since been sold simply drops out, leaving fewer than 11 so validate_xi fails
    and the launch is cancelled rather than silently substituting a player.
    """
    if not roster_ids:
        return []
    rows = (session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user_id,
                    UserRoster.id.in_([int(r) for r in roster_ids]))
            .all())
    by_id = {int(e.id): (e, p) for e, p in rows}
    return [by_id[int(rid)] for rid in roster_ids if int(rid) in by_id]


def _xi_preview(pairs):
    """Compact strength preview of a roster's top 11 for the invitation card."""
    top = pairs[:11]
    if not top:
        return None
    total = sum(p.rating for _e, p in top)
    avg = total / len(top)
    stars = sorted(top, key=lambda ep: ep[1].rating, reverse=True)[:3]
    names = ", ".join(html.escape(str(p.name)) for _e, p in stars)
    return {"ovr": total, "avg": avg, "stars": names}


# ════════════════════════════════════════════════════════════════════
# /letsplay — start an invitation
# ════════════════════════════════════════════════════════════════════

async def letsplay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        # Reuse the global one-match-per-chat gate shared by /match, /cipl and
        # /wpm: a live DB match in this chat blocks a new Lets Play too.
        chat_busy = _active_match_in_chat(session, chat.id)
        if chat_busy:
            await msg.reply_text(_chat_busy_message(chat_busy), parse_mode="HTML",
                                 disable_web_page_preview=True)
            return

        guest_user, reason = resolve_command_target(session, update, context, "letsplay")
        if not guest_user:
            await msg.reply_text(
                "🏏 <b>Lets Play</b> — challenge another player with your own roster.\n\n"
                "• Reply to their message with <code>/letsplay</code>, or\n"
                "• <code>/letsplay @username</code>",
                parse_mode="HTML")
            return
        if guest_user.id == host.id:
            await msg.reply_text("❌ You can't play a match against yourself.")
            return

        # One match per player (any mode): block if either side is already in a
        # live DB match or a /wpm lobby, matching the other challenge handlers.
        host_busy = _active_match_for_user(session, host.id)
        if host_busy:
            await msg.reply_text(_user_busy_message(host_busy), parse_mode="HTML",
                                 disable_web_page_preview=True)
            return
        guest_busy = _active_match_for_user(session, guest_user.id)
        if guest_busy:
            gname = f"@{guest_user.username}" if guest_user.username else (guest_user.first_name or "They")
            await msg.reply_text(
                f"⚠️ {html.escape(gname)} is already in an active match "
                f"(#{guest_busy.id}). They must finish it first.",
                parse_mode="HTML")
            return
        if _cric_lobby_for_user(context.bot_data, host.id) \
                or _cric_lobby_for_user(context.bot_data, guest_user.id):
            await msg.reply_text(
                "⚠️ One of you is already in a match lobby. Finish or cancel it "
                "first, then try /letsplay again.")
            return

        host_pairs = _get_ordered_roster(session, host.id)
        guest_pairs = _get_ordered_roster(session, guest_user.id)
        session.commit()
        if len(host_pairs) < 11:
            await msg.reply_text(
                f"❌ You need at least 11 players to play. You have "
                f"<b>{len(host_pairs)}</b>. Use /claim, /gspin or /buypack.",
                parse_mode="HTML")
            return
        if len(guest_pairs) < 11:
            gname = f"@{guest_user.username}" if guest_user.username else (guest_user.first_name or "They")
            await msg.reply_text(
                f"❌ {html.escape(gname)} needs at least 11 players to play "
                f"(has {len(guest_pairs)}).", parse_mode="HTML")
            return

        # Fail fast: both sides must have a VALID Playing XI before we even send
        # the invite — without it nobody can start /letsplay. Same "your XI is
        # invalid" report style as /playmatch. The XI is re-validated at launch.
        ok_host, host_errs = validate_xi(host_pairs)
        if not ok_host:
            await msg.reply_text(
                "❌ <b>Your Playing XI is not valid:</b>\n"
                + "\n".join(f"• {e}" for e in host_errs)
                + "\n\nFix it with /autobuild or /swap, then try /letsplay again.",
                parse_mode="HTML")
            return
        ok_guest, guest_errs = validate_xi(guest_pairs)
        if not ok_guest:
            gname = f"@{guest_user.username}" if guest_user.username else (guest_user.first_name or "They")
            await msg.reply_text(
                f"❌ <b>{html.escape(gname)}'s Playing XI is not valid:</b>\n"
                + "\n".join(f"• {e}" for e in guest_errs)
                + "\n\nThey should fix it with /autobuild or /swap, then try "
                "/letsplay again.",
                parse_mode="HTML")
            return

        host_info = {"user_id": host.id, "tg_id": host.telegram_id,
                     "name": host.username or host.first_name or "Host"}
        guest_info = {"user_id": guest_user.id, "tg_id": guest_user.telegram_id,
                      "name": guest_user.username or guest_user.first_name or "Guest"}
        # XI preview so the guest knows what they're up against (host's top 11).
        host_preview = _xi_preview(host_pairs)
    except Exception:
        logger.exception("/letsplay setup failed")
        await msg.reply_text("⚠️ Couldn't start the match. Try again.")
        return
    finally:
        session.close()

    invite_id = _next_id(context)
    draft = {
        "invite_id": invite_id, "chat_id": chat.id,
        "is_private": chat.id > 0,
        "status": "pending",
        "host": host_info, "guest": guest_info,
        "host_preview": host_preview,
        "pitch_type": None,
        "host_confirmed": False, "guest_confirmed": False,
        "created_at": datetime.utcnow().isoformat(),
    }
    context.bot_data[_dkey(invite_id)] = draft

    text = (
        "🏏 <b>LETS PLAY — Match Invitation</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Host:</b> {_m(host_info)}\n"
        f"🎯 <b>Guest:</b> {_m(guest_info)}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎮 <b>Match type:</b> Lets Play\n"
        "⏱️ <b>Format:</b> 20 Overs\n"
        "📋 <b>Roster:</b> Own Roster\n"
        + (f"📊 <b>Host XI:</b> {host_preview['ovr']} OVR "
           f"(avg {host_preview['avg']:.1f})\n"
           f"⭐ {host_preview['stars']}\n" if host_preview else "")
        + "━━━━━━━━━━━━━━━━━━━\n"
        f"{_m(guest_info)}, do you accept? <i>(30s)</i>")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"lp_accept_{invite_id}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"lp_deny_{invite_id}"),
    ]])
    sent = await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
    if sent and getattr(sent, "message_id", None):
        draft["invite_msg_id"] = sent.message_id
    _arm_invite_timeout(context, invite_id)


# ════════════════════════════════════════════════════════════════════
# 30-second invitation timer
# ════════════════════════════════════════════════════════════════════

def _arm_invite_timeout(context, invite_id):
    if not getattr(context, "job_queue", None):
        return
    _cancel_invite_timeout(context, invite_id)
    context.job_queue.run_once(
        _on_invite_timeout, INVITE_TIMEOUT,
        name=f"lp_invite_to_{invite_id}", data={"invite_id": invite_id})


def _cancel_invite_timeout(context, invite_id):
    if not getattr(context, "job_queue", None):
        return
    for j in context.job_queue.get_jobs_by_name(f"lp_invite_to_{invite_id}"):
        j.schedule_removal()


async def _on_invite_timeout(context: ContextTypes.DEFAULT_TYPE):
    invite_id = context.job.data["invite_id"]
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("status") != "pending":
        return  # already accepted / denied
    draft["status"] = "expired"
    chat_id = draft["chat_id"]
    mid = draft.get("invite_msg_id")
    text = (
        "🏏 <b>LETS PLAY — Match Invitation</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {_m(draft['host'])}  vs  🎯 {_m(draft['guest'])}\n\n"
        "⌛ Match invitation expired.")
    try:
        if mid:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=mid, parse_mode="HTML",
                reply_markup=None)
        else:
            await context.bot.send_message(chat_id, "⌛ Match invitation expired.")
    except Exception:
        pass
    _drop_draft(context, invite_id)


# ════════════════════════════════════════════════════════════════════
# Setup timer — covers the pitch / XI / toss stages after acceptance
# ════════════════════════════════════════════════════════════════════

_SETUP_STATUSES = ("pitch", "showxi", "toss")


def _rearm_setup_timeout(context, invite_id):
    """(Re)start the rolling setup timer. Called on every setup transition so a
    draft that stalls in pitch/XI/toss is abandoned instead of blocking the chat
    forever — the accept path cancels the invite timer but never replaced it."""
    if not getattr(context, "job_queue", None):
        return
    _cancel_setup_timeout(context, invite_id)
    context.job_queue.run_once(
        _on_setup_timeout, SETUP_TIMEOUT,
        name=f"lp_setup_to_{invite_id}", data={"invite_id": invite_id})


def _cancel_setup_timeout(context, invite_id):
    if not getattr(context, "job_queue", None):
        return
    for j in context.job_queue.get_jobs_by_name(f"lp_setup_to_{invite_id}"):
        j.schedule_removal()


async def _on_setup_timeout(context: ContextTypes.DEFAULT_TYPE):
    invite_id = context.job.data["invite_id"]
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("match_launched") \
            or draft.get("status") not in _SETUP_STATUSES:
        return  # launched, cancelled, or already moved on
    draft["status"] = "expired"
    text = (
        "🏏 <b>LETS PLAY</b>\n"
        f"👤 {_m(draft['host'])}  vs  🎯 {_m(draft['guest'])}\n\n"
        "⌛ Match setup timed out — nobody finished the pitch / toss in "
        "time. Start again with /letsplay.")
    try:
        await context.bot.send_message(draft["chat_id"], text, parse_mode="HTML")
    except Exception:
        pass
    _drop_draft(context, invite_id)


# ════════════════════════════════════════════════════════════════════
# Accept / Deny
# ════════════════════════════════════════════════════════════════════

async def letsplay_invite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lp_accept_{id} / lp_deny_{id} — only the guest may respond."""
    q = update.callback_query
    try:
        _, action, invite_id = q.data.split("_")
        invite_id = int(invite_id)
    except Exception:
        await q.answer("Invalid action.", show_alert=True)
        return
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("status") != "pending":
        await q.answer("This invitation is no longer active.", show_alert=True)
        return
    if q.from_user.id != draft["guest"]["tg_id"]:
        await q.answer("Only the invited player can respond.", show_alert=True)
        return
    await q.answer()
    _cancel_invite_timeout(context, invite_id)

    if action == "deny":
        draft["status"] = "cancelled"
        await q.edit_message_text(
            f"❌ {_m(draft['guest'])} denied the match request.",
            parse_mode="HTML")
        _drop_draft(context, invite_id)
        return

    # Accepted → host picks the pitch. Start the rolling setup timer that now
    # guards the pitch/XI/toss stages.
    draft["status"] = "pitch"
    _rearm_setup_timeout(context, invite_id)
    await q.edit_message_text(
        f"✅ {_m(draft['guest'])} accepted the challenge!\n\n"
        f"👤 {_m(draft['host'])} — pick the pitch below.",
        parse_mode="HTML")
    await _prompt_pitch(context, draft)


# ════════════════════════════════════════════════════════════════════
# Pitch selection (host only)
# ════════════════════════════════════════════════════════════════════

async def _prompt_pitch(context, draft):
    invite_id = draft["invite_id"]
    rows, row = [], []
    for p in PITCH_TYPES:
        row.append(InlineKeyboardButton(
            f"{_PITCH_EMOJI.get(p, '🏏')} {p}",
            callback_data=f"lp_pitch_{invite_id}_{p}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    text = (f"🌱 <b>Pitch Selection</b>\n\n"
            f"{_m(draft['host'])}, choose the pitch for this match:")
    sent = await context.bot.send_message(
        draft["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows))
    if sent and getattr(sent, "message_id", None):
        draft["pitch_msg_id"] = sent.message_id


async def letsplay_pitch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lp_pitch_{id}_{pitch} — only the host picks."""
    q = update.callback_query
    try:
        _, _, invite_id, pitch = q.data.split("_")
        invite_id = int(invite_id)
    except Exception:
        await q.answer("Invalid pitch.", show_alert=True)
        return
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("status") != "pitch":
        await q.answer("This match is no longer in pitch selection.", show_alert=True)
        return
    if q.from_user.id != draft["host"]["tg_id"]:
        await q.answer("Only the host picks the pitch.", show_alert=True)
        return
    if pitch not in PITCH_TYPES:
        await q.answer("Unknown pitch.", show_alert=True)
        return
    await q.answer()
    draft["pitch_type"] = pitch
    draft["status"] = "showxi"
    _rearm_setup_timeout(context, invite_id)
    await q.edit_message_text(
        f"🌱 Pitch locked: <b>{_PITCH_EMOJI.get(pitch, '🏏')} {pitch}</b>",
        parse_mode="HTML")
    await _prompt_show_xi(context, draft)


# ════════════════════════════════════════════════════════════════════
# Auto Playing-XI + Start Toss
#
# /letsplay no longer asks each side to hand-pick an XI: by the time the
# invite is sent both rosters have already been validated as a legal Playing
# XI. After the pitch is locked we simply take each side's confirmed top-11,
# order the batting line-up by batting rating (highest → lowest), show both
# XIs, and offer a single "Start Toss" button.
# ════════════════════════════════════════════════════════════════════

def _ordered_xi_for_side(session, user_id):
    """Return that side's Playing XI as (entry, player) pairs in batting order.

    Batting order = the 11 selected players sorted by BATTING rating, highest
    first (ties fall back to overall rating, then name). Returns (pairs, errors):
    ``pairs`` is None when the side no longer fields a legal XI (e.g. a player
    was sold after the invite went out).
    """
    pairs = _get_ordered_roster(session, user_id)
    if len(pairs) < 11:
        return None, [f"Need 11 players, have {len(pairs)}"]
    top11 = pairs[:11]
    ok, errors = validate_xi(top11)
    if not ok:
        return None, errors
    ordered = sorted(
        top11,
        key=lambda ep: (int(ep[1].bat_rating or 0), int(ep[1].rating or 0),
                        str(ep[1].name or "")),
        reverse=True)
    return ordered, []


def _show_xi_text(draft, host_pairs, guest_pairs):
    """The 'both XIs locked' card shown before the toss."""
    pitch = draft.get("pitch_type", "Hard")
    parts = [
        "🧾 <b>LETS PLAY — Playing XI</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"🌱 <b>Pitch:</b> {_PITCH_EMOJI.get(pitch, '🏏')} {pitch} • 20 overs",
        "<i>Batting order auto-set by batting rating (high → low).</i>",
        "",
        _format_batting_order(host_pairs, f"👤 <b>{html.escape(draft['host']['name'])}</b> (Host)"),
        "",
        _format_batting_order(guest_pairs, f"🎯 <b>{html.escape(draft['guest']['name'])}</b> (Guest)"),
        "",
        "━━━━━━━━━━━━━━━━━━━",
        f"🔒 Both XIs locked — {_m(draft['guest'])}, tap "
        "<b>Start Toss</b> to flip the coin!",
    ]
    return "\n".join(parts)


async def _prompt_show_xi(context, draft):
    """Auto-order both XIs, snapshot them, and show them with a Start Toss button.

    The exact roster ids shown here are snapshotted onto the draft so the match
    launches the SAME XI even if someone /swaps afterwards. If either side has
    fallen below a legal XI since the invite, the setup is abandoned.
    """
    invite_id = draft["invite_id"]
    session = get_session()
    text = host_ids = guest_ids = bad = errs = None
    try:
        host_pairs, host_errs = _ordered_xi_for_side(session, draft["host"]["user_id"])
        guest_pairs, guest_errs = _ordered_xi_for_side(session, draft["guest"]["user_id"])
        if host_pairs is None or guest_pairs is None:
            bad = draft["host"] if host_pairs is None else draft["guest"]
            errs = host_errs if host_pairs is None else guest_errs
        else:
            # Build the card and snapshot ids WHILE the session is open:
            # commit()/close() below expires + detaches these ORM rows, so
            # reading player.rating/name afterwards would raise
            # DetachedInstanceError and leave the draft stuck until timeout.
            host_ids = [int(e.id) for e, _p in host_pairs]
            guest_ids = [int(e.id) for e, _p in guest_pairs]
            text = _show_xi_text(draft, host_pairs, guest_pairs)
        session.commit()
    except Exception:
        logger.exception("letsplay: failed to build auto XIs")
        await context.bot.send_message(
            draft["chat_id"], "⚠️ Couldn't read a roster. Try /letsplay again.")
        draft["status"] = "failed"
        _cancel_setup_timeout(context, invite_id)
        _drop_draft(context, invite_id)
        return
    finally:
        session.close()

    if bad is not None:
        await context.bot.send_message(
            draft["chat_id"],
            f"❌ <b>{html.escape(bad['name'])}'s Playing XI is not valid:</b>\n"
            + "\n".join(f"• {e}" for e in errs)
            + "\n\nFix it with /autobuild or /swap, then try /letsplay again.",
            parse_mode="HTML")
        draft["status"] = "failed"
        _cancel_setup_timeout(context, invite_id)
        _drop_draft(context, invite_id)
        return

    # Snapshot the exact XIs (in batting order) so a later /swap can't change
    # what actually launches.
    draft["host_xi_roster_ids"] = host_ids
    draft["guest_xi_roster_ids"] = guest_ids

    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🪙 Start Toss", callback_data=f"lp_starttoss_{invite_id}")]])
    sent = await context.bot.send_message(
        draft["chat_id"], text,
        parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    if sent and getattr(sent, "message_id", None):
        draft["showxi_msg_id"] = sent.message_id


async def letsplay_starttoss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lp_starttoss_{id} — the GUEST starts the toss (guest calls it, like /cipl)."""
    q = update.callback_query
    try:
        _, _, invite_id = q.data.split("_")
        invite_id = int(invite_id)
    except Exception:
        await q.answer("Invalid action.", show_alert=True)
        return
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("status") != "showxi":
        await q.answer("This match is no longer waiting for the toss.", show_alert=True)
        return
    if q.from_user.id != draft["guest"]["tg_id"]:
        await q.answer("Only the guest starts the toss.", show_alert=True)
        return
    # Flip out of "showxi" synchronously (no await before this) so a racing
    # double-tap can't start two tosses.
    draft["status"] = "toss"
    _rearm_setup_timeout(context, invite_id)
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("letsplay: clearing start-toss button failed", exc_info=True)
    await _start_toss(context, draft)


# ════════════════════════════════════════════════════════════════════
# Toss (guest calls, winner elects bat/bowl) — same coin animation as /cipl
# ════════════════════════════════════════════════════════════════════

async def _start_toss(context, draft):
    text = (f"🪙 <b>Toss Time!</b>\n\n"
            f"{_m(draft['guest'])}, call the toss:")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬆️ Heads", callback_data=f"lp_coin_heads_{draft['invite_id']}"),
        InlineKeyboardButton("⬇️ Tails", callback_data=f"lp_coin_tails_{draft['invite_id']}"),
    ]])
    sent = await context.bot.send_message(
        draft["chat_id"], text, parse_mode="HTML", reply_markup=kb)
    if sent and getattr(sent, "message_id", None):
        draft["toss_msg_id"] = sent.message_id


async def letsplay_coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lp_coin_{heads|tails}_{id} — the guest calls the toss."""
    q = update.callback_query
    try:
        _, _, call, invite_id = q.data.split("_")
        invite_id = int(invite_id)
    except Exception:
        await q.answer("Invalid call.", show_alert=True)
        return
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("status") != "toss":
        await q.answer("This toss is no longer active.", show_alert=True)
        return
    if call not in ("heads", "tails"):
        await q.answer("Invalid call.", show_alert=True)
        return
    if draft.get("toss_winner_side") or draft.get("coin_flipping"):
        await q.answer("Toss already in progress.", show_alert=True)
        return
    if q.from_user.id != draft["guest"]["tg_id"]:
        await q.answer("Only the guest calls the toss!", show_alert=True)
        return
    # Lock synchronously BEFORE the async coin animation so a racing double-tap
    # can't spawn a second election keyboard for the wrong side.
    draft["coin_flipping"] = True
    try:
        await q.answer()
        from services.match_broadcast import run_coin_toss, reveal_toss_result
        coin, won = await run_coin_toss(
            lambda t: q.edit_message_text(t, parse_mode="HTML"), call)
    except Exception:
        # The flip never produced a result — release the lock so the guest can
        # call again instead of being stuck behind "Toss already in progress."
        draft["coin_flipping"] = False
        _rearm_setup_timeout(context, invite_id)
        logger.exception("letsplay coin flip failed for invite %s", invite_id)
        await q.answer("Toss failed — call it again.", show_alert=True)
        return
    winner_side = "guest" if won else "host"
    winner = draft[winner_side]
    # The reveal is the critical edit: if it fails the toss is left frozen on a
    # mid-flip frame. Retry it, and only mark the winner once it actually lands —
    # otherwise release the lock so the guest can call the toss again.
    revealed = await reveal_toss_result(lambda: q.edit_message_text(
        f"🪙 The coin lands on <b>{coin.upper()}</b> — guest called "
        f"<b>{call.upper()}</b>.\n\n"
        f"🏆 {_m(winner)} won the toss. Choose:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First",
                                 callback_data=f"lp_toss_bat_{invite_id}_{winner_side}"),
            InlineKeyboardButton("🎳 Bowl First",
                                 callback_data=f"lp_toss_bowl_{invite_id}_{winner_side}"),
        ]])))
    if not revealed:
        draft["coin_flipping"] = False
        _rearm_setup_timeout(context, invite_id)
        logger.warning("letsplay toss reveal failed for invite %s — recoverable",
                       invite_id)
        await q.answer("Toss reveal failed — call it again.", show_alert=True)
        return
    draft["toss_winner_side"] = winner_side
    _rearm_setup_timeout(context, invite_id)


async def letsplay_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """lp_toss_{bat|bowl}_{id}_{winner_side} — winner elects, match launches."""
    q = update.callback_query
    try:
        _, _, decision, invite_id, winner_side = q.data.split("_")
        invite_id = int(invite_id)
    except Exception:
        await q.answer("Invalid decision.", show_alert=True)
        return
    draft = _get_draft(context, invite_id)
    if not draft or draft.get("status") != "toss":
        await q.answer("This toss is no longer active.", show_alert=True)
        return
    if decision not in ("bat", "bowl") or winner_side not in ("host", "guest"):
        await q.answer("Invalid decision.", show_alert=True)
        return
    # Trust only the toss winner actually recorded after the coin animation — a
    # stale keyboard (e.g. from a racing double-tap) carries the wrong side and
    # must not be allowed to launch the match.
    if winner_side != draft.get("toss_winner_side"):
        await q.answer("That toss result is stale. Use the latest buttons.",
                       show_alert=True)
        return
    if draft.get("match_launched"):
        await q.answer("Match already started.", show_alert=True)
        return
    if q.from_user.id != draft[winner_side]["tg_id"]:
        await q.answer("Toss winner only.", show_alert=True)
        return

    draft["match_launched"] = True
    _cancel_setup_timeout(context, invite_id)
    await q.answer()
    winner = draft[winner_side]
    await q.edit_message_text(
        f"✅ {_m(winner)} elected to "
        f"<b>{'BAT' if decision == 'bat' else 'BOWL'}</b> first.\n\n"
        f"The match begins below — play over by over!",
        parse_mode="HTML")
    try:
        await _launch_match(context, draft, decision, winner_side)
    except Exception:
        logger.exception("letsplay launch failed for invite %s", invite_id)
        await context.bot.send_message(
            draft["chat_id"], "⚠️ Failed to start the match. Please try /letsplay again.")
        draft["status"] = "failed"


# ════════════════════════════════════════════════════════════════════
# Launch — build the shared cipl_approach state and hand off to cipl_play
# ════════════════════════════════════════════════════════════════════

async def _launch_match(context, draft, decision, winner_side):
    host_info, guest_info = draft["host"], draft["guest"]
    winner = host_info if winner_side == "host" else guest_info
    loser = guest_info if winner_side == "host" else host_info
    if decision == "bat":
        bat_info, bowl_info = winner, loser
    else:
        bowl_info, bat_info = winner, loser

    session = get_session()
    try:
        host = session.query(User).get(host_info["user_id"])
        guest = session.query(User).get(guest_info["user_id"])
        if not host or not guest:
            await context.bot.send_message(draft["chat_id"], "⚠️ A player no longer exists.")
            return

        # Launch the EXACT XI each side confirmed (snapshot of roster ids taken
        # at confirm time), not whatever the live roster order is now. Fall back
        # to the current order only if a snapshot is somehow missing.
        host_ids = draft.get("host_xi_roster_ids")
        guest_ids = draft.get("guest_xi_roster_ids")
        host_pairs = (_pairs_from_roster_ids(session, host.id, host_ids)
                      if host_ids else _get_ordered_roster(session, host.id))
        guest_pairs = (_pairs_from_roster_ids(session, guest.id, guest_ids)
                       if guest_ids else _get_ordered_roster(session, guest.id))
        # Final validation — a snapshot player may have been sold mid-setup,
        # leaving fewer than 11 or an illegal composition.
        for pairs, info in ((host_pairs, host_info), (guest_pairs, guest_info)):
            ok, _err = validate_xi(pairs)
            if not ok:
                await context.bot.send_message(
                    draft["chat_id"],
                    f"⚠️ {html.escape(info['name'])}'s XI became invalid — match cancelled.")
                return

        host_xi = _xi_to_engine(session, host_pairs[:11])
        guest_xi = _xi_to_engine(session, guest_pairs[:11])
        session.commit()

        bat_is_host = (bat_info["user_id"] == host_info["user_id"])
        bat_xi = host_xi if bat_is_host else guest_xi
        bowl_xi = guest_xi if bat_is_host else host_xi
        bat_team_name = bat_info["name"]
        bowl_team_name = bowl_info["name"]

        settings = random_match_settings()
        pitch_type = draft.get("pitch_type") or settings["pitch_type"]
        match = Match(
            user1_id=host.id, user2_id=guest.id, status="active",
            overs=LETSPLAY_OVERS,
            toss_winner_id=(host.id if winner_side == "host" else guest.id),
            toss_decision=decision,
            batting_first_id=bat_info["user_id"], bowling_first_id=bowl_info["user_id"],
            stadium=settings["stadium"], pitch_type=pitch_type,
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=draft["chat_id"], created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=MATCH_EXPIRE),
        )
        session.add(match)
        session.commit()
        match_id = match.id
        stadium = match.stadium
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _drop_draft(context, draft["invite_id"])

    state = cipl_match.build_cipl_state(
        match_id=match_id, overs=LETSPLAY_OVERS,
        bat_user_id=bat_info["user_id"], bowl_user_id=bowl_info["user_id"],
        bat_user_tg=bat_info["tg_id"], bowl_user_tg=bowl_info["tg_id"],
        bat_xi=bat_xi, bowl_xi=bowl_xi,
        bat_team_name=bat_team_name, bowl_team_name=bowl_team_name,
        chat_id=draft["chat_id"], pitch_type=pitch_type,
        is_private=draft["chat_id"] > 0, stadium=stadium,
        bat_team_code=_team_code(bat_team_name), bowl_team_code=_team_code(bowl_team_name),
        bat_team_emoji="🏏", bowl_team_emoji="🏏")
    state["user_names"] = {
        str(bat_info["tg_id"]): bat_team_name,
        str(bowl_info["tg_id"]): bowl_team_name,
    }
    state["is_letsplay"] = True
    save_state(context, match_id, state, next_action=A_PICK_CIPL_BOWLER)

    await _announce(context, state, pitch_type)

    from handlers.cipl_play import _prompt_bowler
    await _prompt_bowler(context, match_id, state, first=True)


def _team_code(name):
    s = "".join(ch for ch in (name or "") if ch.isalnum())
    return (s[:4] or "TEAM").upper()


async def _announce(context, state, pitch_type):
    """Pin a 'Lets Play' match-start card carrying the View Match button."""
    bat = html.escape(str(state.get("bat_team_name", "Host")))
    bowl = html.escape(str(state.get("bowl_team_name", "Guest")))
    stadium = html.escape(str(state.get("stadium") or "Neutral Venue"))
    pitch = html.escape(str(pitch_type or "Hard"))
    rule = "━" * 15
    text = (
        f"🏏 <b>LETS PLAY</b> 🏏\n"
        f"{rule}\n"
        f"⚔️ <b>{bat}</b>  🆚  <b>{bowl}</b>\n"
        f"🏟️ {stadium} • 20 overs\n"
        f"🌱 <b>Pitch:</b> {pitch}\n"
        f"🏏 {bat} batting first\n"
        f"{rule}\n"
        f"📺 Live scorecard, commentary &amp; XI inside\n"
        f"👇 Tap <b>View Match</b> to follow live")
    try:
        from handlers.cipl_play import _miniapp_row
        kb = _miniapp_row(state)
    except Exception:
        kb = None
    try:
        sent = await context.bot.send_message(
            state["chat_id"], text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    except Exception:
        logger.exception("letsplay announcement failed")
        return
    if not (sent and getattr(sent, "message_id", None)):
        return
    state["pinned_msg_id"] = sent.message_id
    try:
        await context.bot.pin_chat_message(
            state["chat_id"], sent.message_id, disable_notification=True)
    except Exception:
        logger.info("letsplay announcement pin skipped (no rights)")
