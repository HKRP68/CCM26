"""Challenge League Tour handlers — /cltour

A CL Tour is a best-of series (3/5/7 matches) between two users using fixed
Challenge-League teams. The league and both teams are chosen once at creation;
each match then reuses the entire /cipl engine (host picks pitch, both pick
Playing XI, toss, over-by-over play).

  /cltour @guest (or reply)  → create a tour:
        host picks league → host picks team → guest picks team →
        host picks match count → guest Accepts → tour info posted
  /cltour                    → show your active tour with a sequential
        "Play Match N" button (only the next pending match is playable)

Callback patterns (owner/viewer telegram id embedded for gating across restarts):
  cltset_lg_<host_tg>_<league_id>     — setup: host picks league
  cltset_ht_<host_tg>_<team_id>       — setup: host picks team
  cltset_gt_<host_tg>_<team_id>       — setup: guest picks team
  cltset_n_<host_tg>_<count>          — setup: host picks match count → create
  cltset_x_<host_tg>                  — setup: cancel
  clt_acc_<tour_id>                   — guest accepts the invite
  clt_dec_<tour_id>                   — guest declines the invite
  cltplay_<tour_id>_<match_no>_<viewer_tg> — play the next match in the series
"""

import html
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import get_session
from models import (
    User, CLTour, ChallengeLeague, ChallengeTeam, ChallengePlayer)
from services.telegram_user_service import resolve_command_target, sync_telegram_user
from handlers.match import _mention
from services.cl_tour_service import (
    create_cl_tour, accept_cl_tour, decline_cl_tour, expire_pending_invite,
    get_active_cl_tour, get_user_cl_tours, get_cl_tour_matches,
    next_pending_match, is_user_in_active_cl_tour, series_leader,
    ALLOWED_MATCH_COUNTS, INVITE_EXPIRE_SECONDS,
)

logger = logging.getLogger(__name__)

_SETUP_KEY = "cltour_setup_{}"


def _esc(value):
    """HTML-escape a dynamic string for parse_mode='HTML' Telegram messages."""
    return html.escape(str(value)) if value is not None else ""


def _u_label(user):
    """Display label for a user (@username or first name), HTML-escaped so a name
    containing <, > or & can't break message delivery or inject formatting."""
    if not user:
        return "User"
    return _esc(f"@{user.username}" if user.username else (user.first_name or "User"))


def _active_leagues(session):
    """Active Challenge Leagues, ordered for the league picker."""
    return (session.query(ChallengeLeague)
            .filter(ChallengeLeague.is_active == True)  # noqa: E712
            .order_by(ChallengeLeague.sort_order, ChallengeLeague.name)
            .all())


def _league_teams(session, league_id):
    """Active teams in a league, ordered for the team picker."""
    return (session.query(ChallengeTeam)
            .filter(ChallengeTeam.league_id == league_id,
                    ChallengeTeam.is_active == True)  # noqa: E712
            .order_by(ChallengeTeam.sort_order, ChallengeTeam.name)
            .all())


# ════════════════════════════════════════════════════════════════════
# /cltour — entry point (create if a target is given, else show your tour)
# ════════════════════════════════════════════════════════════════════

async def cltour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cltour entry point: with a target user it starts tour creation; with no
    target it shows the caller's active tour."""
    tg = update.effective_user
    session = get_session()
    try:
        u1 = sync_telegram_user(session, tg)
        if not u1:
            await update.message.reply_text("❌ Do /debut first!")
            return

        target, target_source = resolve_command_target(session, update, context, "cltour")
        # No @mention / reply → this is the "show my tour" command.
        if not target and target_source == "missing":
            await _show_my_tour(update, session, u1)
            return
        if not target:
            if target_source == "not_mention":
                await update.message.reply_text(
                    "❌ Reply to a user or use a real @username mention.")
            else:
                await update.message.reply_text(
                    "❌ User not found. They need to /debut first; if they have no "
                    "username, reply to their message and run /cltour.")
            return

        # ── Create flow ──
        if update.effective_chat.type == "private":
            await update.message.reply_text(
                "❌ CL Tours can only be created in group chats.")
            return
        if target.id == u1.id:
            await update.message.reply_text("❌ You can't tour yourself.")
            return
        if is_user_in_active_cl_tour(session, u1.id):
            await update.message.reply_text(
                "❌ You're already in a CL tour. Finish it first.")
            return
        if is_user_in_active_cl_tour(session, target.id):
            await update.message.reply_text(
                f"❌ {_u_label(target)} is already in a CL tour.")
            return

        leagues = _active_leagues(session)
        if not leagues:
            await update.message.reply_text("❌ No Challenge Leagues are configured yet.")
            return

        context.bot_data[_SETUP_KEY.format(tg.id)] = {
            "host_uid": u1.id, "host_tg": tg.id, "host_name": _u_label(u1),
            "guest_uid": target.id, "guest_tg": target.telegram_id,
            "guest_name": _u_label(target),
            "chat_id": update.effective_chat.id,
            "league_id": None, "host_team_id": None, "guest_team_id": None,
        }

        rows = [[InlineKeyboardButton(lg.name, callback_data=f"cltset_lg_{tg.id}_{lg.id}")]
                for lg in leagues]
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cltset_x_{tg.id}")])
        await update.message.reply_text(
            f"🏆 <b>NEW CL TOUR</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👑 Host: {_u_label(u1)}\n"
            f"⚔️ Guest: {_u_label(target)}\n\n"
            f"{_u_label(u1)}, choose the <b>league</b> for this tour:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows))
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Setup callbacks
# ════════════════════════════════════════════════════════════════════

def _parse(q, n):
    """Split callback data into n trailing int args after the 2-token prefix."""
    parts = q.data.split("_")
    return [int(p) for p in parts[2:2 + n]]


async def cltset_league_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cltset_lg_<host_tg>_<league_id>"""
    q = update.callback_query
    try:
        host_tg, league_id = _parse(q, 2)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != host_tg:
        await q.answer("Only the host can pick the league!", show_alert=True)
        return
    draft = context.bot_data.get(_SETUP_KEY.format(host_tg))
    if not draft:
        await q.answer("Setup expired. Run /cltour again.", show_alert=True)
        return

    session = get_session()
    try:
        league = session.query(ChallengeLeague).get(league_id)
        if not league:
            await q.answer("League not found", show_alert=True)
            return
        teams = _league_teams(session, league_id)
        if len(teams) < 2:
            await q.answer("This league needs at least 2 teams.", show_alert=True)
            return
        draft["league_id"] = league_id
        draft["league_name"] = _esc(league.name)
        await q.answer()
        rows = _team_rows(teams, f"cltset_ht_{host_tg}")
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cltset_x_{host_tg}")])
        await q.edit_message_text(
            f"🏆 <b>NEW CL TOUR — {_esc(league.name)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👑 {draft['host_name']}, pick <b>your team</b>:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    finally:
        session.close()


async def cltset_hostteam_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cltset_ht_<host_tg>_<team_id>"""
    q = update.callback_query
    try:
        host_tg, team_id = _parse(q, 2)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != host_tg:
        await q.answer("Only the host can pick their team!", show_alert=True)
        return
    draft = context.bot_data.get(_SETUP_KEY.format(host_tg))
    if not draft or not draft.get("league_id"):
        await q.answer("Setup expired. Run /cltour again.", show_alert=True)
        return

    session = get_session()
    try:
        team = session.query(ChallengeTeam).get(team_id)
        if not team or team.league_id != draft["league_id"]:
            await q.answer("Team not in this league", show_alert=True)
            return
        league = session.query(ChallengeLeague).get(draft["league_id"])
        draft["host_team_id"] = team_id
        draft["host_team_name"] = _esc(team.name)
        await q.answer(f"You picked {team.name}")

        teams = _league_teams(session, draft["league_id"])
        # Drop the host's team unless the league lets both pick the same one.
        if not league.same_team_allowed:
            teams = [t for t in teams if t.id != team_id]
        rows = _team_rows(teams, f"cltset_gt_{host_tg}")
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cltset_x_{host_tg}")])
        await q.edit_message_text(
            f"🏆 <b>NEW CL TOUR — {draft['league_name']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👑 Host: {draft['host_name']} — <b>{_esc(team.name)}</b>\n\n"
            f"⚔️ {draft['guest_name']}, pick <b>your team</b>:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    finally:
        session.close()


async def cltset_guestteam_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cltset_gt_<host_tg>_<team_id>"""
    q = update.callback_query
    try:
        host_tg, team_id = _parse(q, 2)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    draft = context.bot_data.get(_SETUP_KEY.format(host_tg))
    if not draft or not draft.get("host_team_id"):
        await q.answer("Setup expired. Run /cltour again.", show_alert=True)
        return
    if q.from_user.id != draft.get("guest_tg"):
        await q.answer("Only the guest can pick their team!", show_alert=True)
        return

    session = get_session()
    try:
        team = session.query(ChallengeTeam).get(team_id)
        if not team or team.league_id != draft["league_id"]:
            await q.answer("Team not in this league", show_alert=True)
            return
        league = session.query(ChallengeLeague).get(draft["league_id"])
        if team_id == draft["host_team_id"] and not league.same_team_allowed:
            await q.answer("That team is taken — pick another.", show_alert=True)
            return
        draft["guest_team_id"] = team_id
        draft["guest_team_name"] = _esc(team.name)
        await q.answer(f"You picked {team.name}")

        rows = [[InlineKeyboardButton(str(n), callback_data=f"cltset_n_{host_tg}_{n}")
                 for n in ALLOWED_MATCH_COUNTS]]
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cltset_x_{host_tg}")])
        await q.edit_message_text(
            f"🏆 <b>NEW CL TOUR — {draft['league_name']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👑 {draft['host_name']} — <b>{draft['host_team_name']}</b>\n"
            f"⚔️ {draft['guest_name']} — <b>{_esc(team.name)}</b>\n\n"
            f"👑 {draft['host_name']}, how many matches in the tour?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    finally:
        session.close()


def _team_xi_issue(session, team_id, team_label, min_overseas, max_overseas):
    """Return a human-readable reason this team can't field a legal XI, else None.

    A CL-tour locks both players (``is_user_in_active_cl_tour``) until it ends,
    so a fixed team that can never satisfy the XI rules would otherwise leave the
    pair stuck — every Play attempt would fail at XI selection. Validating the
    roster up front (mirroring ``_challenge_xi_validation``'s rules at the roster
    level) keeps such a tour from ever being created.
    """
    from handlers.challenge import (
        _challenge_is_wicket_keeper, _challenge_is_bowling_option,
        _challenge_is_overseas)
    players = (session.query(ChallengePlayer)
               .filter(ChallengePlayer.team_id == team_id).all())
    if len(players) < 11:
        return f"{team_label} needs at least 11 players (has {len(players)})."
    if not any(_challenge_is_wicket_keeper(p) for p in players):
        return f"{team_label} has no wicket-keeper — can't field a legal XI."
    if sum(1 for p in players if _challenge_is_bowling_option(p)) < 5:
        return f"{team_label} needs at least 5 bowling options (bowlers/all-rounders)."
    overseas = sum(1 for p in players if _challenge_is_overseas(p))
    domestic = len(players) - overseas
    # The overseas count achievable in any 11-man XI is bounded by
    # [max(0, 11 - domestic), min(11, overseas)]; it must overlap the league cap.
    if max(0, 11 - domestic) > max_overseas or min(11, overseas) < min_overseas:
        return (f"{team_label} can't satisfy the league's overseas rule "
                f"(min {min_overseas} / max {max_overseas}).")
    return None


async def cltset_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cltset_n_<host_tg>_<count> — create the tour and post the invite."""
    q = update.callback_query
    try:
        host_tg, count = _parse(q, 2)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != host_tg:
        await q.answer("Only the host can choose the match count!", show_alert=True)
        return
    draft = context.bot_data.get(_SETUP_KEY.format(host_tg))
    if not draft or not draft.get("guest_team_id"):
        await q.answer("Setup expired. Run /cltour again.", show_alert=True)
        return
    if count not in ALLOWED_MATCH_COUNTS:
        await q.answer("Bad count")
        return

    session = get_session()
    try:
        # Refuse to create a tour with a team that can never field a legal XI —
        # otherwise both players would be locked in an unplayable active tour.
        league = session.query(ChallengeLeague).get(draft["league_id"])
        min_raw = getattr(league, "min_overseas", None) if league else None
        max_raw = getattr(league, "max_overseas", None) if league else None
        min_ov = int(min_raw) if min_raw is not None else 0
        max_ov = int(max_raw) if max_raw is not None else 11
        for team_id, label in ((draft["host_team_id"], draft["host_team_name"]),
                               (draft["guest_team_id"], draft["guest_team_name"])):
            issue = _team_xi_issue(session, team_id, label, min_ov, max_ov)
            if issue:
                await q.answer(issue, show_alert=True)
                return

        tour, err = create_cl_tour(
            session,
            host_id=draft["host_uid"], guest_id=draft["guest_uid"],
            league_id=draft["league_id"],
            host_team_id=draft["host_team_id"],
            guest_team_id=draft["guest_team_id"],
            match_count=count, chat_id=draft["chat_id"])
        if err:
            session.rollback()
            await q.answer(err, show_alert=True)
            return
        session.commit()
        await q.answer()
        context.bot_data.pop(_SETUP_KEY.format(host_tg), None)

        invite_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"clt_acc_{tour.id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"clt_dec_{tour.id}"),
        ]])
        await q.edit_message_text(
            f"🏆 <b>CL TOUR INVITATION</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏟️ League: <b>{draft['league_name']}</b>\n"
            f"👑 {draft['host_name']} — <b>{draft['host_team_name']}</b>\n"
            f"⚔️ {draft['guest_name']} — <b>{draft['guest_team_name']}</b>\n"
            f"📋 Best of <b>{count}</b> matches\n\n"
            f"{_mention(draft['guest_tg'], draft['guest_name'])}, do you accept?\n"
            f"⏳ <i>Expires in {INVITE_EXPIRE_SECONDS}s.</i>",
            parse_mode="HTML", reply_markup=invite_kb)

        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _cltour_invite_auto_expire, INVITE_EXPIRE_SECONDS,
                    name=f"cltourexpire_{tour.id}",
                    data={"tour_id": tour.id, "chat_id": draft["chat_id"]})
        except Exception:
            logger.exception("Failed to schedule CL tour invite expiry")
    except Exception:
        session.rollback()
        logger.exception("cltset_count_callback err")
        await q.answer("⚠️ Error creating tour.", show_alert=True)
    finally:
        session.close()


async def cltset_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cltset_x_<host_tg>"""
    q = update.callback_query
    try:
        host_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != host_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    context.bot_data.pop(_SETUP_KEY.format(host_tg), None)
    await q.answer("Cancelled")
    try:
        await q.edit_message_text("❌ CL Tour setup cancelled.")
    except TelegramError:
        logger.debug("Failed to edit CL tour setup-cancelled message", exc_info=True)


def _team_rows(teams, prefix):
    """Two team buttons per row; label shows short code when present."""
    rows, row = [], []
    for t in teams:
        label = (t.short_name or t.name) if (t.short_name or "").strip() else t.name
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{t.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


async def _cltour_invite_auto_expire(ctx):
    """JobQueue callback: expire a pending invite that was never answered in time."""
    session = get_session()
    try:
        tour = expire_pending_invite(session, ctx.job.data["tour_id"])
        if tour:
            session.commit()
            try:
                await ctx.bot.send_message(ctx.job.data["chat_id"],
                                           "⏰ CL Tour invite expired.")
            except TelegramError:
                logger.debug("Failed to send CL tour invite-expired message", exc_info=True)
    except Exception:
        session.rollback()
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Accept / Decline
# ════════════════════════════════════════════════════════════════════

async def cltour_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """clt_acc_<tour_id>"""
    q = update.callback_query
    try:
        tour_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        tour = session.query(CLTour).get(tour_id)
        if not tour:
            await q.answer("Tour not found", show_alert=True)
            return
        if not u or u.id != tour.user2_id:
            await q.answer("Only the invited user can accept!", show_alert=True)
            return
        await q.answer()
        tour, err = accept_cl_tour(session, tour_id, u.id)
        if err:
            # accept_cl_tour may have flipped an overdue invite to 'expired' before
            # returning the error — commit (not rollback) so that state persists and
            # both users stop being blocked by the pending-tour guard.
            session.commit()
            await q.edit_message_text(f"❌ {err}")
            return
        session.commit()
        text, _ = _render_cl_tour_view(session, tour, viewer_tg=None)
        try:
            await q.edit_message_text(
                "✅ <b>CL TOUR ACCEPTED!</b>\n\n" + text + "\n\n"
                "<i>Either player can run /cltour to play the next match.</i>",
                parse_mode="HTML")
        except TelegramError:
            logger.debug("Failed to edit CL tour accepted message", exc_info=True)
    except Exception:
        session.rollback()
        logger.exception("cltour_accept_callback err")
    finally:
        session.close()


async def cltour_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """clt_dec_<tour_id>"""
    q = update.callback_query
    try:
        tour_id = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        tour = session.query(CLTour).get(tour_id)
        if not tour:
            await q.answer("Tour not found", show_alert=True)
            return
        if not u or u.id != tour.user2_id:
            await q.answer("Only the invited user can decline!", show_alert=True)
            return
        await q.answer()
        _, err = decline_cl_tour(session, tour_id, u.id)
        if err:
            session.rollback()
            await q.edit_message_text(f"❌ {err}")
            return
        session.commit()
        try:
            await q.edit_message_text("❌ CL Tour declined.")
        except TelegramError:
            logger.debug("Failed to edit CL tour declined message", exc_info=True)
    except Exception:
        session.rollback()
        logger.exception("cltour_decline_callback err")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# View + play
# ════════════════════════════════════════════════════════════════════

async def _show_my_tour(update, session, user):
    """Reply with the caller's active tour view, or recent/empty-state guidance."""
    tour = get_active_cl_tour(session, user.id)
    if not tour:
        # Show the most recent finished tour, if any, else guidance.
        recent = get_user_cl_tours(session, user.id)
        if recent:
            text, kb = _render_cl_tour_view(session, recent[0], update.effective_user.id)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
            return
        await update.message.reply_text(
            "🏆 <b>NO CL TOUR YET</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Start one with <code>/cltour @username</code> in a group.\n\n"
            "<i>A CL Tour is a best-of series (3/5/7) using Challenge-League "
            "teams. Each match plays like /cipl.</i>",
            parse_mode="HTML")
        return
    text, kb = _render_cl_tour_view(session, tour, update.effective_user.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


def _render_cl_tour_view(session, tour, viewer_tg):
    """Return (text, InlineKeyboardMarkup|None) for a CL tour."""
    u1 = session.query(User).get(tour.user1_id)
    u2 = session.query(User).get(tour.user2_id)
    league = session.query(ChallengeLeague).get(tour.league_id)
    host_team = session.query(ChallengeTeam).get(tour.host_team_id)
    guest_team = session.query(ChallengeTeam).get(tour.guest_team_id)
    matches = get_cl_tour_matches(session, tour.id)

    lines = [
        f"🏆 <b>CL TOUR — {_esc(league.name) if league else ''}</b>",
        f"👑 {_u_label(u1)} <b>{_esc(host_team.name) if host_team else ''}</b>"
        f"  📊 <b>{tour.user1_wins}-{tour.user2_wins}</b>  "
        f"<b>{_esc(guest_team.name) if guest_team else ''}</b> {_u_label(u2)} ⚔️",
        f"<i>Best of {tour.match_count}</i>",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    for tm in matches:
        if tm.status == "done":
            w = session.query(User).get(tm.winner_id) if tm.winner_id else None
            lines.append(f"✅ Match {tm.match_number}: "
                         + (f"<b>{_u_label(w)}</b> won" if w else "no result"))
        elif tm.status == "playing":
            lines.append(f"▶️ Match {tm.match_number}: in progress")
        else:
            lines.append(f"⏳ Match {tm.match_number}: pending")

    if tour.status == "completed":
        leader, tie = series_leader(tour)
        if tie:
            lines.append("\n🤝 <b>Series drawn!</b>")
        else:
            w = session.query(User).get(leader)
            lines.append(f"\n🏆 <b>{_u_label(w)} won the tour!</b>")

    kb = None
    if tour.status == "active" and viewer_tg is not None:
        nxt = next_pending_match(session, tour.id)
        if nxt:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                f"▶️ Play Match {nxt.match_number}",
                callback_data=f"cltplay_{tour.id}_{nxt.match_number}_{viewer_tg}")]])
    return "\n".join(lines), kb



async def cltour_play_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cltplay_<tour_id>_<match_no>_<viewer_tg>"""
    q = update.callback_query
    try:
        parts = q.data.split("_")
        tour_id = int(parts[1])
        match_no = int(parts[2])
        viewer_tg = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != viewer_tg:
        await q.answer("Only the person who ran /cltour can start this!", show_alert=True)
        return

    session = get_session()
    try:
        tapper = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        tour = session.query(CLTour).get(tour_id)
        if not tour or not tapper:
            await q.answer("Tour not found", show_alert=True)
            return
        if tour.status != "active":
            await q.answer(f"Tour is {tour.status}", show_alert=True)
            return
        if tapper.id not in (tour.user1_id, tour.user2_id):
            await q.answer("Not your tour!", show_alert=True)
            return

        nxt = next_pending_match(session, tour.id)
        if not nxt or nxt.match_number != match_no:
            await q.answer("Play the matches in order.", show_alert=True)
            return

        from handlers.challenge import (
            launch_cl_tour_match, normalize_challenge_league)
        host = session.query(User).get(tour.user1_id)
        guest = session.query(User).get(tour.user2_id)
        # FKs may be NULL after an admin deleted the league/team (ondelete SET
        # NULL) — guard the lookups so we don't query on a NULL primary key.
        host_team = session.query(ChallengeTeam).get(tour.host_team_id) if tour.host_team_id else None
        guest_team = session.query(ChallengeTeam).get(tour.guest_team_id) if tour.guest_team_id else None
        league = session.query(ChallengeLeague).get(tour.league_id) if tour.league_id else None
        # If an admin deleted the league/team mid-tour, the SET NULL FKs leave the
        # series unplayable. Retire the tour so both players are unblocked rather
        # than leaving them stuck in a permanently-active tour they can't play.
        if not (league and host_team and guest_team):
            tour.status = "expired"
            tour.completed_at = datetime.utcnow()
            session.commit()
            await q.answer(
                "This tour's league or team data was removed — the tour has been "
                "cancelled.", show_alert=True)
            try:
                await q.edit_message_text(
                    "❌ <b>CL Tour cancelled</b> — its league or team data was "
                    "removed by an admin.", parse_mode="HTML")
            except TelegramError:
                logger.debug("Failed to edit CL tour data-removed message", exc_info=True)
            return
        if not (host and guest):
            await q.answer("Tour data missing", show_alert=True)
            return

        league_key = normalize_challenge_league(league.short_code or league.name) if league else None
        # Use the tour's actual league row (active or not) — a league deactivated
        # mid-tour must still resolve the correct league-scoped roster/format.
        league_record = league
        league_name = league.name if league else "Challenge League"

        await q.answer()
        ok, err = await launch_cl_tour_match(
            context, message_obj=q.message, chat_id=tour.chat_id or q.message.chat_id,
            host=host, target=guest,
            league_key=league_key, league_name=league_name, league_record=league_record,
            host_team=host_team.name, target_team=guest_team.name, session=session,
            cl_tour_id=tour.id, cl_tour_match_id=nxt.id,
            cl_tour_match_no=nxt.match_number, cl_tour_match_count=tour.match_count)
        if not ok and err:
            try:
                await context.bot.send_message(q.message.chat_id, err, parse_mode="HTML")
            except TelegramError:
                logger.debug("Failed to send CL tour launch-error message", exc_info=True)
    except Exception:
        logger.exception("cltour_play_callback err")
        try:
            await context.bot.send_message(q.message.chat_id, "⚠️ Error starting match.")
        except TelegramError:
            logger.debug("Failed to send CL tour generic-error message", exc_info=True)
    finally:
        session.close()
