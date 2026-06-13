"""Tour handlers — /cmtours and /mytours

Commands:
  /cmtours @user2  — Create a tour challenge. 3-step picker (matches → overs → invite)
  /mytours         — List user's tours with action buttons

Callback patterns:
  ctm_<owner_tg>_<count>           — picker step 1 (match count)
  cto_<owner_tg>_<overs>           — picker step 2 (overs)
  cancel_cmt_<owner_tg>            — cancel tour creation
  tac_<tour_id>_<u2_id>            — accept tour invite
  tdc_<tour_id>_<u2_id>            — decline tour invite
  mtp_<tour_id>_<match_no>_<owner_tg> — /mytours: play match N
  mti_<tour_id>_<owner_tg>         — /mytours: info button
  mts_<tour_id>_<owner_tg>         — /mytours: stats button
  mtb_<tour_id>_<owner_tg>         — /mytours: back to tour list
"""

import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Tour, TourMatch, Match
from services.telegram_user_service import resolve_command_target, sync_telegram_user
from services.tour_service import (
    create_tour, accept_tour, decline_tour, expire_pending_invite,
    get_user_tours, get_tour_matches, is_user_in_active_tour,
    get_tour_stats, ALLOWED_MATCH_COUNTS, MIN_OVERS, MAX_OVERS,
    INVITE_EXPIRE_SECONDS, link_match_to_tour,
)
from services.match_constants import random_match_settings

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# /cmtours — create a tour challenge
# ════════════════════════════════════════════════════════════════════

async def cmtours_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    chat = update.effective_chat

    # Group-only — match flow requires both users present
    if chat.type == "private":
        await update.message.reply_text(
            "❌ Tours can only be created in group chats.")
        return

    session = get_session()
    try:
        u1 = sync_telegram_user(session, tg)
        if not u1:
            await update.message.reply_text("❌ Do /debut first!")
            return
        u2, target_source = resolve_command_target(session, update, context, "cmtours")
        if not u2:
            if target_source == "missing":
                await update.message.reply_text(
                    "Usage: <code>/cmtours @user2</code>\n"
                    "Tip: for users without @username, reply to their message and run /cmtours.",
                    parse_mode="HTML")
            elif target_source == "not_mention":
                await update.message.reply_text("❌ Reply to a user or use a real @username mention.")
            else:
                await update.message.reply_text("❌ User not found. If they changed or don't have a username, reply to their message and run /cmtours.")
            return
        if u2.id == u1.id:
            await update.message.reply_text("❌ You can't tour yourself.")
            return

        # Check both users free
        if is_user_in_active_tour(session, u1.id):
            await update.message.reply_text(
                "❌ You're already in a tour. Finish it first.")
            return
        if is_user_in_active_tour(session, u2.id):
            await update.message.reply_text(
                f"❌ @{u2.username} is already in a tour.")
            return

        # Stash u2 + chat in bot_data keyed by initiator's telegram id
        context.bot_data[f"cmt_draft_{tg.id}"] = {
            "u1_id": u1.id, "u2_id": u2.id,
            "u2_username": u2.username,
            "chat_id": chat.id,
        }

        # Step 1 picker: match count
        rows = [[
            InlineKeyboardButton(str(n), callback_data=f"ctm_{tg.id}_{n}")
            for n in ALLOWED_MATCH_COUNTS
        ]]
        rows.append([
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_cmt_{tg.id}"),
        ])

        await update.message.reply_text(
            f"🏆 <b>NEW TOUR vs @{u2.username}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"How many matches?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    finally:
        session.close()


async def cmt_matches_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ctm_<owner_tg>_<count>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1])
        count = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    if count not in ALLOWED_MATCH_COUNTS:
        await q.answer("Bad count")
        return

    draft = context.bot_data.get(f"cmt_draft_{owner_tg}")
    if not draft:
        await q.answer("Tour draft expired. Run /cmtours again.", show_alert=True)
        try:
            await q.edit_message_text("⏰ Tour draft expired.")
        except Exception:
            pass
        return

    draft["match_count"] = count
    context.bot_data[f"cmt_draft_{owner_tg}"] = draft

    await q.answer()

    # Step 2: overs picker — 5, 8, 10, 15, 20
    OVERS_OPTIONS = [5, 8, 10, 15, 20]
    rows = [[
        InlineKeyboardButton(str(o), callback_data=f"cto_{owner_tg}_{o}")
        for o in OVERS_OPTIONS
    ]]
    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_cmt_{owner_tg}"),
    ])

    try:
        await q.edit_message_text(
            f"🏆 <b>NEW TOUR vs @{draft['u2_username']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>{count} matches</b> selected\n\n"
            f"How many overs per match? <i>(min {MIN_OVERS})</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        pass


async def cmt_overs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cto_<owner_tg>_<overs>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1])
        overs = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    if overs < MIN_OVERS or overs > MAX_OVERS:
        await q.answer(f"Overs must be {MIN_OVERS}-{MAX_OVERS}")
        return

    draft = context.bot_data.get(f"cmt_draft_{owner_tg}")
    if not draft:
        await q.answer("Tour draft expired. Run /cmtours again.", show_alert=True)
        try:
            await q.edit_message_text("⏰ Tour draft expired.")
        except Exception:
            pass
        return

    await q.answer()

    # Create the Tour and send the invite
    session = get_session()
    try:
        tour, err = create_tour(
            session,
            user1_id=draft["u1_id"], user2_id=draft["u2_id"],
            match_count=draft["match_count"], overs_per_match=overs,
            chat_id=draft["chat_id"],
        )
        if err:
            session.rollback()
            try:
                await q.edit_message_text(f"❌ {err}")
            except Exception:
                pass
            return
        session.commit()

        u1 = session.query(User).get(draft["u1_id"])
        u2 = session.query(User).get(draft["u2_id"])
        u1_label = f"@{u1.username}" if u1.username else (u1.first_name or "User1")
        u2_label = f"@{u2.username}" if u2.username else (u2.first_name or "User2")

        # Clean up the draft
        context.bot_data.pop(f"cmt_draft_{owner_tg}", None)

        # Edit the picker message to show "Sent"
        try:
            await q.edit_message_text(
                f"✅ Tour invite sent to {u2_label}!\n"
                f"<i>{tour.match_count} matches · {tour.overs_per_match} overs each</i>",
                parse_mode="HTML")
        except Exception:
            pass

        # Send the invite message
        invite_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept",
                                  callback_data=f"tac_{tour.id}_{u2.id}"),
            InlineKeyboardButton("❌ Decline",
                                  callback_data=f"tdc_{tour.id}_{u2.id}"),
        ]])
        await context.bot.send_message(
            chat_id=draft["chat_id"],
            text=(
                f"🏆 <b>TOUR INVITATION</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"From: {u1_label}\n"
                f"To: <b>{u2_label}</b>\n\n"
                f"📋 <b>{tour.match_count} matches</b>\n"
                f"🏏 <b>{tour.overs_per_match} overs</b> each\n"
                f"📅 Lasts <b>{tour.match_count * 2} days</b>\n\n"
                f"⏳ Expires in <b>{INVITE_EXPIRE_SECONDS}s</b>"
            ),
            parse_mode="HTML",
            reply_markup=invite_kb,
        )

        # Schedule auto-expire
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _tour_invite_auto_expire,
                    INVITE_EXPIRE_SECONDS,
                    name=f"tourexpire_{tour.id}",
                    data={"tour_id": tour.id, "chat_id": draft["chat_id"]},
                )
        except Exception:
            logger.exception("Failed to schedule tour invite expiry")

    except Exception:
        session.rollback()
        logger.exception("cmt_overs_callback err")
    finally:
        session.close()


async def cmt_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cancel_cmt_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Cancelled")
    context.bot_data.pop(f"cmt_draft_{owner_tg}", None)
    try:
        await q.edit_message_text("❌ Tour creation cancelled.")
    except Exception:
        pass


async def _tour_invite_auto_expire(ctx):
    """Job: invite not accepted within 30s → expire."""
    data = ctx.job.data
    session = get_session()
    try:
        tour = expire_pending_invite(session, data["tour_id"])
        if tour:
            session.commit()
            try:
                await ctx.bot.send_message(
                    data["chat_id"],
                    "⏰ Tour invite expired.")
            except Exception:
                pass
    except Exception:
        session.rollback()
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Accept / Decline tour invite
# ════════════════════════════════════════════════════════════════════

async def tour_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """tac_<tour_id>_<u2_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        u2_id = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != u2_id:
            await q.answer("Only the invited user can accept!", show_alert=True)
            return

        await q.answer()
        tour, err = accept_tour(session, tour_id, u2_id)
        if err:
            session.rollback()
            try:
                await q.edit_message_text(f"❌ {err}")
            except Exception:
                pass
            return
        session.commit()

        u1 = session.query(User).get(tour.user1_id)
        u1_label = f"@{u1.username}" if u1.username else (u1.first_name or "User1")
        u2_label = f"@{u.username}" if u.username else (u.first_name or "User2")

        try:
            await q.edit_message_text(
                f"✅ <b>TOUR ACCEPTED!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"{u1_label} <b>vs</b> {u2_label}\n"
                f"📋 {tour.match_count} matches · 🏏 {tour.overs_per_match} overs\n"
                f"📅 Tour ends in <b>{tour.match_count * 2} days</b>\n\n"
                f"Either of you can use /mytours to play matches.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except Exception:
        session.rollback()
        logger.exception("tour_accept_callback err")
    finally:
        session.close()


async def tour_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """tdc_<tour_id>_<u2_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        u2_id = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != u2_id:
            await q.answer("Only the invited user can decline!", show_alert=True)
            return
        await q.answer()
        tour, err = decline_tour(session, tour_id, u2_id)
        if err:
            session.rollback()
            try:
                await q.edit_message_text(f"❌ {err}")
            except Exception:
                pass
            return
        session.commit()
        try:
            await q.edit_message_text("❌ Tour declined.")
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("tour_decline_callback err")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# /mytours — list & manage tours
# ════════════════════════════════════════════════════════════════════

async def mytours_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u:
            await update.message.reply_text("❌ Do /debut first!")
            return

        tours = get_user_tours(session, u.id, include_finished=True)
        if not tours:
            await update.message.reply_text(
                "🏆 <b>NO TOURS YET</b>\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "You haven't joined any tour. Create one with:\n\n"
                "<code>/cmtours @username</code>\n\n"
                "<i>Tours are multi-match challenges (3-6 matches, ≥5 overs each). "
                "Each match has its own stadium and pitch. Winner is decided "
                "by who wins more matches.</i>",
                parse_mode="HTML")
            return

        # Show the most recent active tour as the main view; older ones as a list.
        active = next((t for t in tours if t.status == "active"), None)
        if active:
            text, kb = _render_tour_view(session, active, u, tg.id)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
            return

        # No active — show summary list of past tours
        lines = ["📋 <b>YOUR TOURS</b>", "━━━━━━━━━━━━━━━━━━━"]
        for t in tours[:5]:
            opp = session.query(User).get(t.user2_id if t.user1_id == u.id else t.user1_id)
            opp_label = f"@{opp.username}" if opp and opp.username else (opp.first_name if opp else "?")
            status_emoji = {
                "pending": "⏳", "active": "▶️", "completed": "🏁",
                "expired": "⌛", "declined": "❌",
            }.get(t.status, "•")
            you_w = t.user1_wins if t.user1_id == u.id else t.user2_wins
            opp_w = t.user2_wins if t.user1_id == u.id else t.user1_wins
            lines.append(
                f"{status_emoji} vs {opp_label} — {you_w}-{opp_w} "
                f"<i>({t.match_count}M·{t.overs_per_match}O)</i>"
            )
        lines.append("\n<i>No active tour. Use /cmtours @user to start one!</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception("mytours_handler err")
        await update.message.reply_text("⚠️ Error loading tours.")
    finally:
        session.close()


def _render_tour_view(session, tour, viewer_user, viewer_tg):
    """Return (text, kb) for the active tour view."""
    u1 = session.query(User).get(tour.user1_id)
    u2 = session.query(User).get(tour.user2_id)
    u1_label = f"@{u1.username}" if u1.username else (u1.first_name or "User1")
    u2_label = f"@{u2.username}" if u2.username else (u2.first_name or "User2")

    matches = get_tour_matches(session, tour.id)

    # Header
    lines = [
        f"🏆 <b>TOUR — {u1_label} vs {u2_label}</b>",
        f"📊 <b>{tour.user1_wins} — {tour.user2_wins}</b>"
        f"  <i>({tour.match_count}M · {tour.overs_per_match}O)</i>",
        "━━━━━━━━━━━━━━━━━━━",
    ]

    # Match list with status icons
    for tm in matches:
        if tm.status == "done":
            winner = session.query(User).get(tm.winner_id) if tm.winner_id else None
            wlabel = f"@{winner.username}" if (winner and winner.username) else (
                winner.first_name if winner else "?")
            lines.append(f"✅ Match {tm.match_number}: <b>{wlabel}</b> won "
                          f"<i>· {tm.stadium}</i>")
        elif tm.status == "playing":
            lines.append(f"▶️ Match {tm.match_number}: in progress "
                          f"<i>· {tm.stadium}</i>")
        elif tm.status == "expired":
            lines.append(f"⌛ Match {tm.match_number}: expired")
        elif tm.status == "forfeit":
            lines.append(f"⚠️ Match {tm.match_number}: forfeit")
        else:  # pending
            lines.append(f"⏳ Match {tm.match_number} <i>· {tm.stadium} · {tm.pitch_type}</i>")

    # Footer with lifetime
    if tour.lifetime_expires_at:
        remaining = tour.lifetime_expires_at - datetime.utcnow()
        if remaining.total_seconds() > 0:
            days = remaining.days
            hours = remaining.seconds // 3600
            if days >= 1:
                lines.append(f"\n⏰ Ends in <b>{days}d {hours}h</b>")
            else:
                mins = (remaining.seconds % 3600) // 60
                lines.append(f"\n⏰ Ends in <b>{hours}h {mins}m</b>")

    # Buttons: Play N for the next pending match + Info + Stats
    next_match = next((tm for tm in matches if tm.status == "pending"), None)
    btn_rows = []
    if next_match and tour.status == "active":
        btn_rows.append([
            InlineKeyboardButton(
                f"▶️ Play Match {next_match.match_number}",
                callback_data=f"mtp_{tour.id}_{next_match.match_number}_{viewer_tg}",
            )
        ])
    btn_rows.append([
        InlineKeyboardButton("ℹ️ Info",
                              callback_data=f"mti_{tour.id}_{viewer_tg}"),
        InlineKeyboardButton("📈 Tour Stats",
                              callback_data=f"mts_{tour.id}_{viewer_tg}"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(btn_rows)


async def mytours_play_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mtp_<tour_id>_<match_no>_<owner_tg>

    The 'owner' is whoever ran /mytours and saw the button.
    Only they can tap it. Tapping creates a Match record linked to the
    TourMatch, then fires the normal /playmatch invite flow.
    """
    q = update.callback_query
    tg = q.from_user
    chat_id = q.message.chat_id
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        match_no = int(parts[2])
        owner_tg = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Only the person who ran /mytours can play this!",
                       show_alert=True)
        return

    session = get_session()
    try:
        u_tapper = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u_tapper:
            await q.answer("Do /debut first", show_alert=True)
            return
        tour = session.query(Tour).get(tour_id)
        if not tour:
            await q.answer("Tour not found", show_alert=True)
            return
        if tour.status != "active":
            await q.answer(f"Tour is {tour.status}", show_alert=True)
            return
        if u_tapper.id not in (tour.user1_id, tour.user2_id):
            await q.answer("Not your tour!", show_alert=True)
            return

        # Get the specific TourMatch
        tm = (session.query(TourMatch)
              .filter(TourMatch.tour_id == tour_id,
                      TourMatch.match_number == match_no)
              .first())
        if not tm:
            await q.answer("Match not found", show_alert=True)
            return
        if tm.status not in ("pending",):
            await q.answer(f"Match {match_no} is {tm.status}", show_alert=True)
            return

        # Verify XIs are valid for both users (same check /playmatch does)
        from models import UserRoster
        from handlers.lineup import validate_xi, _get_ordered_roster

        # The tapper becomes user1 (the initiator); other user is user2
        u1 = u_tapper
        u2_id = tour.user2_id if u_tapper.id == tour.user1_id else tour.user1_id
        u2 = session.query(User).get(u2_id)

        r1_count = session.query(UserRoster).filter(UserRoster.user_id == u1.id).count()
        r2_count = session.query(UserRoster).filter(UserRoster.user_id == u2.id).count()
        if r1_count < 11:
            await q.answer(f"You need 11+ players (have {r1_count})", show_alert=True)
            return
        if r2_count < 11:
            await q.answer(f"@{u2.username} needs 11+ players", show_alert=True)
            return

        roster1 = _get_ordered_roster(session, u1.id)
        ok1, errs1 = validate_xi(roster1)
        if not ok1:
            await q.answer("Your XI is invalid — fix it via /myroster", show_alert=True)
            try:
                await context.bot.send_message(
                    chat_id,
                    f"❌ <b>Your XI is invalid:</b>\n" + "\n".join(f"• {e}" for e in errs1),
                    parse_mode="HTML")
            except Exception:
                pass
            return
        roster2 = _get_ordered_roster(session, u2.id)
        ok2, errs2 = validate_xi(roster2)
        if not ok2:
            await q.answer(f"@{u2.username}'s XI is invalid", show_alert=True)
            return

        await q.answer()

        # One-match-per-chat: tour matches respect the same rule
        from handlers.match import _active_match_in_chat, _chat_busy_message
        existing = _active_match_in_chat(session, chat_id)
        if existing:
            await context.bot.send_message(
                chat_id, _chat_busy_message(existing), parse_mode="HTML")
            return

        # A tour match must not collide with an open /wpm-style lobby either.
        from handlers.match import (
            _cric_lobby_key, _expire_lobby,
            _user_label, _mention, LOBBY_EXPIRE,
        )
        if context.bot_data.get(_cric_lobby_key(chat_id)):
            await context.bot.send_message(
                chat_id, "⚠️ There is already a match lobby waiting in this chat!")
            return

        # ── /wpm-style launch ──
        # Tour matches now play in the Cricket Arena Mini App exactly like /wpm:
        # a directed lobby the opponent accepts, then a coin toss, then the
        # interactive Mini-App match. The Match row is created (and linked to the
        # TourMatch) when the toss winner elects bat/bowl, inside
        # handlers.match.cric_decision_callback — which honours the tour_match_id
        # and pre-decided venue we stash on the lobby here.
        lobby = {
            "host_user_id": u1.id,
            "host_tg_id": u1.telegram_id,
            "host_label": _user_label(u1),
            "overs": tour.overs_per_match,
            "original_lobby_chat_id": chat_id,
            "target_user_id": u2.id,
            "target_tg_id": u2.telegram_id,
            "target_label": _user_label(u2),
            # Tour wiring honoured by cric_decision_callback:
            "tour_match_id": tm.id,
            "tour_match_no": match_no,
            "tour_match_count": tour.match_count,
            "stadium": tm.stadium,
            "pitch_type": tm.pitch_type,
        }
        context.bot_data[_cric_lobby_key(chat_id)] = lobby

        t1 = u1.team_name or f"@{u1.username}'s XI"
        t2 = u2.team_name or f"@{u2.username}'s XI"
        lobby_msg = await context.bot.send_message(
            chat_id,
            f"🏏 <b>TOUR MATCH {match_no}/{tour.match_count}</b> 🏏\n"
            "═════════════════════════════\n"
            f"• <b>Host:</b> {_user_label(u1)} ({t1})\n"
            f"• <b>Invited:</b> {_mention(u2)} ({t2})\n"
            f"• <b>Length:</b> {tour.overs_per_match} Over(s)\n"
            f"• 📍 {tm.pitch_type} • 🏟️ {tm.stadium}\n\n"
            f"{_mention(u2)}, tap below to accept — the match plays in the "
            f"Cricket Arena Mini App.\n"
            f"⏳ <i>Expires in {LOBBY_EXPIRE // 60} min if not accepted.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept Match", callback_data="cric_join"),
                InlineKeyboardButton("❌ Cancel", callback_data="cric_cancel_lobby"),
            ]]))
        context.bot_data[_cric_lobby_key(chat_id)]["lobby_msg_id"] = lobby_msg.message_id

        # Auto-cancel the lobby if the invite is never accepted.
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _expire_lobby, LOBBY_EXPIRE, name=f"lobby_{chat_id}",
                    data={"chat_id": chat_id, "lobby_msg_id": lobby_msg.message_id})
        except Exception:
            logger.exception("Failed to schedule tour lobby expiry")

    except Exception:
        session.rollback()
        logger.exception("mytours_play_callback err")
        try:
            await context.bot.send_message(chat_id, "⚠️ Error starting match.")
        except Exception:
            pass
    finally:
        session.close()


async def mytours_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mti_<tour_id>_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        owner_tg = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        tour = session.query(Tour).get(tour_id)
        if not tour:
            await q.answer("Not found", show_alert=True)
            return
        u1 = session.query(User).get(tour.user1_id)
        u2 = session.query(User).get(tour.user2_id)
        u1_label = f"@{u1.username}" if u1.username else (u1.first_name or "U1")
        u2_label = f"@{u2.username}" if u2.username else (u2.first_name or "U2")

        # Build venue list
        matches = get_tour_matches(session, tour.id)
        venue_lines = []
        for tm in matches:
            venue_lines.append(
                f"• Match {tm.match_number}: {tm.stadium} <i>({tm.pitch_type})</i>"
            )

        lines = [
            f"ℹ️ <b>TOUR INFO</b>",
            f"━━━━━━━━━━━━━━━━━━━",
            f"<b>{u1_label}</b> vs <b>{u2_label}</b>",
            f"📋 {tour.match_count} matches · 🏏 {tour.overs_per_match} overs",
            f"📅 Created: {tour.created_at.strftime('%d %b %Y, %H:%M UTC')}",
        ]
        if tour.accepted_at:
            lines.append(f"✅ Accepted: {tour.accepted_at.strftime('%d %b, %H:%M UTC')}")
        if tour.lifetime_expires_at:
            rem = tour.lifetime_expires_at - datetime.utcnow()
            if rem.total_seconds() > 0:
                lines.append(f"⏰ Ends: {tour.lifetime_expires_at.strftime('%d %b, %H:%M UTC')} "
                              f"<i>({rem.days}d {rem.seconds // 3600}h left)</i>")
        lines.append(f"\n🏟️ <b>Venues</b>")
        lines.extend(venue_lines)

        # Back button
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Back",
                                  callback_data=f"mtb_{tour.id}_{owner_tg}"),
        ]])
        try:
            await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    finally:
        session.close()


async def mytours_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mts_<tour_id>_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        owner_tg = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        tour = session.query(Tour).get(tour_id)
        if not tour:
            return
        stats = get_tour_stats(session, tour_id, top_n=3)
        lines = [
            f"📈 <b>TOUR STATS</b>",
            f"━━━━━━━━━━━━━━━━━━━",
            f"Matches played: <b>{stats['total_matches_played']}/{tour.match_count}</b>",
            f"Total runs scored: <b>{stats['total_runs']}</b>",
            f"Total wickets: <b>{stats['total_wickets']}</b>",
        ]

        if stats["most_runs"]:
            lines.append("\n🏏 <b>MOST RUNS</b>")
            for rank, e in enumerate(stats["most_runs"], 1):
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
                lines.append(
                    f"{medal} {e['player_name']} — <b>{e['value']}</b> runs"
                    f" <i>({e['team_name']})</i>"
                )
        else:
            lines.append("\n<i>No batting stats yet.</i>")

        if stats["most_wickets"]:
            lines.append("\n🎯 <b>MOST WICKETS</b>")
            for rank, e in enumerate(stats["most_wickets"], 1):
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
                lines.append(
                    f"{medal} {e['player_name']} — <b>{e['value']}</b> wkts"
                    f" <i>({e['team_name']})</i>"
                )
        else:
            lines.append("\n<i>No bowling stats yet.</i>")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Back",
                                  callback_data=f"mtb_{tour.id}_{owner_tg}"),
        ]])
        try:
            await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    finally:
        session.close()


async def mytours_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mtb_<tour_id>_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        owner_tg = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        tour = session.query(Tour).get(tour_id)
        if not (u and tour):
            return
        text, kb = _render_tour_view(session, tour, u, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    finally:
        session.close()
