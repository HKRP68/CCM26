"""Bowl-out handler — tiebreaker mechanic + /pbo standalone command.

Flow:
  1. start_bowlout() creates a Bowlout row (status pending_pick)
     and asks the first picker to pick a bowler from their XI.
  2. Each pick callback records the pick, then either:
     - asks the same user for their next pick (1..5), or
     - hands off to the other user, or
     - if all 10 picked, transitions to bowling state and starts the deliveries.
  3. Deliveries are bowled one at a time with a 1.2s delay, each editing
     the same scoreboard message.
  4. After 10 deliveries, declare winner or queue sudden death (one ball each).
"""

import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, Player, Bowlout, BowloutBall

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────

def _xi_bowlers(session, user_id, exclude_player_ids=()):
    """Return the user's 11 roster players that can bowl, excluding any
    already-picked roster_ids."""
    rows = (session.query(UserRoster, Player)
                   .join(Player, Player.id == UserRoster.player_id)
                   .filter(UserRoster.user_id == user_id,
                           UserRoster.order_position <= 11)
                   .order_by(UserRoster.order_position)
                   .all())
    out = []
    for r, p in rows:
        if r.id in exclude_player_ids:
            continue
        out.append({
            "roster_id": r.id,
            "player_id": p.id,
            "name": p.name,
            "rating": p.rating,
            "bowl_rating": p.bowl_rating or 50,
            "category": p.category,
        })
    return out


def _mention_user(u):
    if u is None: return "Player"
    if u.username:
        return f'<a href="tg://user?id={u.telegram_id}">@{u.username}</a>'
    return f'<a href="tg://user?id={u.telegram_id}">{u.first_name or "Player"}</a>'


# ── Public entrypoints ───────────────────────────────────────────────

async def start_bowlout(context, *, chat_id, user1_id, user2_id,
                         user1_team_name="Team 1", user2_team_name="Team 2",
                         first_picker_user_id=None, match_id=None):
    """Create a Bowlout and send the first pick prompt. Returns the
    Bowlout id."""
    from services.bowlout_service import create_bowlout
    from services.message_service import get_msg

    session = get_session()
    try:
        bo = create_bowlout(
            session, match_id=match_id, chat_id=chat_id,
            user1_id=user1_id, user2_id=user2_id,
            user1_team_name=user1_team_name,
            user2_team_name=user2_team_name,
            first_picker_user_id=first_picker_user_id or user1_id,
        )
        session.commit()
        picker = session.query(User).get(bo.current_picker)
        intro_text = get_msg(
            "bowlout_tied_intro",
            first_picker_mention=_mention_user(picker),
        )
        # Build picker buttons
        bowlers = _xi_bowlers(session, bo.current_picker)
        # Sort: bowlers/all-rounders first, then by bowl_rating desc
        bowlers.sort(key=lambda x: (
            0 if x["category"] in ("Bowler", "All-rounder") else 1,
            -(x["bowl_rating"] or 0),
        ))
        kb = [[InlineKeyboardButton(
                  f"{b['name']} · BWL {b['bowl_rating']} ({b['category'][:3]})",
                  callback_data=f"bopick_{bo.id}_{b['roster_id']}")]
              for b in bowlers]

        sent = await context.bot.send_message(
            chat_id, intro_text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb))
        bo.message_id = sent.message_id
        session.commit()
        return bo.id
    finally:
        session.close()


async def bowlout_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a bowler-pick button press."""
    q = update.callback_query
    await q.answer()

    try:
        parts = q.data.split("_")
        bowlout_id = int(parts[1])
        roster_id = int(parts[2])
    except (ValueError, IndexError):
        await q.answer("Invalid pick", show_alert=True)
        return

    session = get_session()
    try:
        from services.bowlout_service import (
            record_pick, advance_picker, count_picks, render_scoreboard,
        )
        from services.message_service import get_msg

        bo = session.query(Bowlout).get(bowlout_id)
        if not bo:
            await q.answer("Bowl-out not found", show_alert=True)
            return
        if bo.status != "pending_pick":
            await q.answer("Bowl-out already started.", show_alert=True)
            return

        # Verify it's the right user pressing
        if q.from_user.id != _tg_id(session, bo.current_picker):
            await q.answer("Not your turn to pick!", show_alert=True)
            return

        # Look up the player from roster
        roster_row = (session.query(UserRoster)
                              .filter(UserRoster.id == roster_id,
                                      UserRoster.user_id == bo.current_picker)
                              .first())
        if not roster_row:
            await q.answer("That player isn't in your XI.", show_alert=True)
            return
        # Check not already picked
        already = (session.query(BowloutBall)
                          .filter(BowloutBall.bowlout_id == bowlout_id,
                                  BowloutBall.bowler_player_id == roster_row.player_id,
                                  BowloutBall.bowler_user_id == bo.current_picker)
                          .first())
        if already:
            await q.answer("Already picked this bowler.", show_alert=True)
            return

        player = session.query(Player).get(roster_row.player_id)
        record_pick(session, bowlout_id, bo.current_picker,
                     player.id, player.name, player.bowl_rating or 50)
        session.commit()

        # Now decide what's next
        before_picker = bo.current_picker
        bo_after = advance_picker(session, bowlout_id)
        session.commit()

        if bo_after.status == "bowling":
            # All 10 picks done — kick off bowling animation
            try:
                await q.edit_message_text(
                    "✅ All bowlers selected!\n\n🎯 <b>BOWL-OUT BEGINS</b>\n"
                    "<i>Bowling at the unguarded stumps...</i>",
                    parse_mode="HTML")
            except Exception: pass
            await asyncio.sleep(0.6)
            await _run_bowlout_deliveries(context, bowlout_id)
            return

        # Otherwise, show next pick prompt
        picker_user = session.query(User).get(bo_after.current_picker)
        n_picks = count_picks(session, bowlout_id, bo_after.current_picker) + 1

        # Build next pick keyboard — exclude already-picked roster_ids
        already_picked_rosters = set()
        for b in (session.query(BowloutBall)
                          .filter(BowloutBall.bowlout_id == bowlout_id,
                                  BowloutBall.bowler_user_id == bo_after.current_picker)
                          .all()):
            # Need to find roster_id from player_id
            rr = (session.query(UserRoster)
                          .filter(UserRoster.user_id == bo_after.current_picker,
                                  UserRoster.player_id == b.bowler_player_id)
                          .first())
            if rr: already_picked_rosters.add(rr.id)
        bowlers = _xi_bowlers(session, bo_after.current_picker,
                               exclude_player_ids=already_picked_rosters)
        bowlers.sort(key=lambda x: (
            0 if x["category"] in ("Bowler", "All-rounder") else 1,
            -(x["bowl_rating"] or 0),
        ))
        kb = [[InlineKeyboardButton(
                  f"{b['name']} · BWL {b['bowl_rating']} ({b['category'][:3]})",
                  callback_data=f"bopick_{bo_after.id}_{b['roster_id']}")]
              for b in bowlers]

        prompt = get_msg(
            "bowlout_pick_prompt",
            mention=_mention_user(picker_user), n=n_picks,
        )
        # Also show running picks so users see progress
        running = _picks_summary(session, bowlout_id)
        text = running + "\n\n" + prompt

        try:
            await q.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await context.bot.send_message(bo_after.chat_id, text, parse_mode="HTML",
                                            reply_markup=InlineKeyboardMarkup(kb))

    except Exception:
        session.rollback()
        logger.exception("bowlout_pick_callback failed")
        try:
            await q.answer("⚠️ Error processing pick", show_alert=True)
        except Exception: pass
    finally:
        session.close()


async def _run_bowlout_deliveries(context, bowlout_id):
    """Bowl all 10 (or sudden-death) deliveries one by one, editing the
    same message after each."""
    from services.bowlout_service import (
        bowl_next, is_match_decided_after, render_scoreboard,
    )

    DELAY = 1.2  # seconds between deliveries

    session = get_session()
    try:
        bo = session.query(Bowlout).get(bowlout_id)
        if not bo:
            return
        chat_id = bo.chat_id
        msg_id = bo.message_id
    finally:
        session.close()

    # Bowl all initial 10
    for _ in range(10):
        await asyncio.sleep(DELAY)
        session = get_session()
        try:
            ball = bowl_next(session, bowlout_id)
            if ball is None:
                break
            session.commit()
            bo = session.query(Bowlout).get(bowlout_id)
            text = render_scoreboard(session, bowlout_id)
        finally:
            session.close()

        try:
            if msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=text, parse_mode="HTML")
            else:
                m = await context.bot.send_message(chat_id, text, parse_mode="HTML")
                msg_id = m.message_id
        except Exception as e:
            logger.warning(f"bowl-out edit failed (non-fatal): {e}")

    # After 10 balls: decided or sudden death?
    session = get_session()
    try:
        bo = session.query(Bowlout).get(bowlout_id)
        winner_id = is_match_decided_after(session, bowlout_id)
    finally:
        session.close()

    if winner_id:
        await _finalize_bowlout(context, bowlout_id, winner_id)
        return

    # Sudden death
    await asyncio.sleep(1.5)
    try:
        await context.bot.send_message(
            chat_id,
            "⚡ <b>SUDDEN DEATH</b>\n"
            "Both teams tied at 5 bowls each. Continuing one ball at a time...",
            parse_mode="HTML")
    except Exception: pass

    # ── Sudden death ─────────────────────────────────────────────
    # No ties allowed. Auto-pick the next-best bowler each round.
    # If a team runs out of unique bowlers, we loop back to the top of
    # their order. This guarantees the bowl-out ALWAYS produces a winner.
    MAX_ROUNDS = 999  # effectively infinite; in practice 1-2 rounds resolve it
    sudden_round = 0
    while sudden_round < MAX_ROUNDS:
        sudden_round += 1
        await asyncio.sleep(DELAY)
        session = get_session()
        try:
            bo = session.query(Bowlout).get(bowlout_id)
            u1_all = _xi_bowlers(session, bo.user1_id)
            u2_all = _xi_bowlers(session, bo.user2_id)
            u1_all.sort(key=lambda x: -(x["bowl_rating"] or 0))
            u2_all.sort(key=lambda x: -(x["bowl_rating"] or 0))

            # Count how many sudden-death balls each side has bowled
            sd_balls = (session.query(BowloutBall)
                                .filter(BowloutBall.bowlout_id == bowlout_id,
                                        BowloutBall.ball_index >= 10)
                                .all())
            u1_sd_count = sum(1 for b in sd_balls if b.bowler_user_id == bo.user1_id)
            u2_sd_count = sum(1 for b in sd_balls if b.bowler_user_id == bo.user2_id)

            if not u1_all or not u2_all:
                break  # safety: a team has no bowlers at all

            # Loop back to top of order if pool exhausted
            u1_pick = u1_all[u1_sd_count % len(u1_all)]
            u2_pick = u2_all[u2_sd_count % len(u2_all)]

            from services.bowlout_service import queue_sudden_death
            queue_sudden_death(session, bowlout_id, u1_pick, u2_pick)
            session.commit()
        finally:
            session.close()

        # Bowl both balls of the sudden-death pair
        for _ in range(2):
            await asyncio.sleep(0.9)
            session = get_session()
            try:
                ball = bowl_next(session, bowlout_id)
                if ball is None:
                    break
                session.commit()
                text = render_scoreboard(session, bowlout_id,
                                          max_pairs=max(5,
                                              (session.query(BowloutBall)
                                                       .filter(BowloutBall.bowlout_id == bowlout_id)
                                                       .count() + 1) // 2))
            finally:
                session.close()
            try:
                if msg_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=text, parse_mode="HTML")
            except Exception: pass

        session = get_session()
        try:
            winner_id = is_match_decided_after(session, bowlout_id)
        finally:
            session.close()

        if winner_id:
            await _finalize_bowlout(context, bowlout_id, winner_id)
            return

    # Safety fallback: if we somehow exited the loop without a winner,
    # whoever has more hits wins (mathematically near-impossible after
    # 1000+ rounds; this just keeps the function total).
    session = get_session()
    try:
        bo = session.query(Bowlout).get(bowlout_id)
        winner_id = bo.user1_id if bo.user1_hits >= bo.user2_hits else bo.user2_id
    finally:
        session.close()
    await _finalize_bowlout(context, bowlout_id, winner_id)


async def _finalize_bowlout(context, bowlout_id, winner_user_id, flavor=None):
    """Mark complete + send result message + (if from a tied match) update
    the parent Match's winner record and send both innings scorecards with
    the bowl-out winner declared.
    """
    from services.message_service import get_msg
    session = get_session()
    try:
        bo = session.query(Bowlout).get(bowlout_id)
        bo.status = "completed"
        bo.winner_user_id = winner_user_id
        bo.completed_at = datetime.utcnow()
        winner_team = (bo.user1_team_name if winner_user_id == bo.user1_id
                        else bo.user2_team_name)
        loser_team = (bo.user2_team_name if winner_user_id == bo.user1_id
                       else bo.user1_team_name)
        bowlout_result_text = get_msg(
            "bowlout_result",
            winner_team=winner_team,
            team1=bo.user1_team_name, team2=bo.user2_team_name,
            hits1=bo.user1_hits, hits2=bo.user2_hits,
        )
        if flavor:
            bowlout_result_text += f"\n\n<i>{flavor}</i>"
        chat_id = bo.chat_id
        parent_match_id = bo.match_id
        bo_user1_id = bo.user1_id
        bo_user2_id = bo.user2_id
        u1_hits = bo.user1_hits
        u2_hits = bo.user2_hits

        # If from a tied match, update Match record with real winner
        if parent_match_id:
            try:
                from models import Match
                m = session.query(Match).get(parent_match_id)
                if m:
                    m.winner_id = winner_user_id
                    m.loser_id = (bo_user2_id if winner_user_id == bo_user1_id
                                   else bo_user1_id)
                    m.margin_type = "bowl-out"
                    m.margin_value = abs(u1_hits - u2_hits)
                    m.status = "completed"
                    m.completed_at = datetime.utcnow()
            except Exception:
                logger.exception("Failed to update parent Match winner")

        session.commit()
    finally:
        session.close()

    # 1. Send the bowl-out result message
    try:
        await context.bot.send_message(chat_id, bowlout_result_text,
                                        parse_mode="HTML")
    except Exception:
        logger.exception("Couldn't send bowl-out result message")

    # 2. If from a tied match: send both scorecards + final winner banner,
    #    then clean up the match state.
    if parent_match_id:
        import asyncio as _asyncio
        await _asyncio.sleep(0.6)
        try:
            from handlers.match import _send_innings_scorecards, cleanup_state
            try:
                await _send_innings_scorecards(context, parent_match_id, innings_num=1)
            except Exception:
                logger.exception("1st innings scorecard failed (non-fatal)")
            await _asyncio.sleep(0.4)
            try:
                await _send_innings_scorecards(context, parent_match_id, innings_num=2)
            except Exception:
                logger.exception("2nd innings scorecard failed (non-fatal)")
            await _asyncio.sleep(0.4)

            # Final result banner
            try:
                if winner_user_id == bo_user1_id:
                    margin_str = f"{u1_hits}–{u2_hits}"
                else:
                    margin_str = f"{u2_hits}–{u1_hits}"
                final_banner = (
                    "🏆 <b>MATCH RESULT (via Bowl-out)</b>\n\n"
                    f"<b>{winner_team}</b> beat <b>{loser_team}</b>\n"
                    f"<i>Match tied — won the bowl-out {margin_str}</i>"
                )
                await context.bot.send_message(chat_id, final_banner,
                                                parse_mode="HTML")
            except Exception:
                logger.exception("Final banner failed (non-fatal)")

            try:
                cleanup_state(context, parent_match_id)
            except Exception:
                logger.exception("cleanup_state failed (non-fatal)")
        except Exception:
            logger.exception("Post-bowl-out scorecards routine failed")


# ── /pbo standalone command ─────────────────────────────────────────

async def pbo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate a standalone bowl-out against another user. Usage:
       /pbo @username"""
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/pbo @username</code>\n\n"
            "Challenge another user to a bowl-out — each picks 5 bowlers, "
            "5 deliveries each at unguarded stumps. Highest hits wins.",
            parse_mode="HTML")
        return

    target_username = context.args[0].lstrip("@").strip()
    tg_user = update.effective_user
    cid = update.effective_chat.id

    session = get_session()
    try:
        u1 = (session.query(User)
                      .filter(User.telegram_id == tg_user.id).first())
        if not u1:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("not_debuted"))
            return

        u2 = (session.query(User)
                      .filter(User.username.ilike(target_username)).first())
        if not u2:
            await update.message.reply_text(
                f"❌ User @{target_username} not found.")
            return
        if u2.id == u1.id:
            await update.message.reply_text("❌ You can't bowl-out yourself.")
            return

        # Both need 11 players
        if u1.roster_count < 11 or u2.roster_count < 11:
            await update.message.reply_text(
                "❌ Both players need at least 11 players in roster.")
            return

        # Send invite
        u1_name = u1.username or u1.first_name or "Player 1"
        u2_name = u2.username or u2.first_name or "Player 2"
        u2_mention = _mention_user(u2)
        text = (f"🎯 <b>BOWL-OUT CHALLENGE</b>\n\n"
                f"{_mention_user(u1)} challenges {u2_mention} to a bowl-out!\n\n"
                f"Each team picks 5 bowlers, 5 deliveries each at unguarded "
                f"stumps. Highest hits wins.\n\n"
                f"{u2_mention}, accept within 30s:")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept",
                callback_data=f"pboacc_{u1.id}_{u2.id}_{cid}"),
            InlineKeyboardButton("❌ Decline",
                callback_data=f"pbodec_{u1.id}_{u2.id}"),
        ]])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("pbo_handler failed")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def pbo_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        parts = q.data.split("_")
        u1_id = int(parts[1]); u2_id = int(parts[2]); chat_id = int(parts[3])
    except (ValueError, IndexError):
        await q.answer("Invalid", show_alert=True)
        return

    session = get_session()
    try:
        u2 = session.query(User).get(u2_id)
        if not u2 or q.from_user.id != u2.telegram_id:
            await q.answer("Not your invite to accept.", show_alert=True)
            return
        u1 = session.query(User).get(u1_id)
        if not u1:
            await q.answer("Inviter not found.", show_alert=True)
            return

        await q.answer("Accepted!")
        try:
            await q.edit_message_text(
                f"✅ Bowl-out accepted!\n\n"
                f"{_mention_user(u1)} vs {_mention_user(u2)}",
                parse_mode="HTML")
        except Exception: pass

        t1 = u1.username or u1.first_name or "Team 1"
        t2 = u2.username or u2.first_name or "Team 2"

        # The inviter picks first
        await start_bowlout(
            context,
            chat_id=chat_id,
            user1_id=u1.id, user2_id=u2.id,
            user1_team_name=t1, user2_team_name=t2,
            first_picker_user_id=u1.id,
        )
    except Exception:
        logger.exception("pbo_accept failed")
    finally:
        session.close()


async def pbo_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        parts = q.data.split("_")
        u2_id = int(parts[2])
    except (ValueError, IndexError):
        await q.answer("Invalid", show_alert=True)
        return
    session = get_session()
    try:
        u2 = session.query(User).get(u2_id)
        if not u2 or q.from_user.id != u2.telegram_id:
            await q.answer("Not your invite to decline.", show_alert=True)
            return
        await q.answer("Declined")
        try:
            await q.edit_message_text("❌ Bowl-out declined.")
        except Exception: pass
    finally:
        session.close()


# ── Internal helpers ────────────────────────────────────────────────

def _tg_id(session, user_id):
    u = session.query(User).get(user_id)
    return u.telegram_id if u else None


def _picks_summary(session, bowlout_id):
    """A short running-summary for the pick phase."""
    bo = session.query(Bowlout).get(bowlout_id)
    if not bo: return ""
    u1_picks = (session.query(BowloutBall)
                       .filter(BowloutBall.bowlout_id == bowlout_id,
                               BowloutBall.bowler_user_id == bo.user1_id)
                       .order_by(BowloutBall.ball_index).all())
    u2_picks = (session.query(BowloutBall)
                       .filter(BowloutBall.bowlout_id == bowlout_id,
                               BowloutBall.bowler_user_id == bo.user2_id)
                       .order_by(BowloutBall.ball_index).all())
    lines = [
        f"🎯 <b>{bo.user1_team_name}</b> picks: {len(u1_picks)}/5",
    ]
    for p in u1_picks:
        lines.append(f"  • {p.bowler_name} (BWL {p.bowler_rating})")
    lines.append(f"🎯 <b>{bo.user2_team_name}</b> picks: {len(u2_picks)}/5")
    for p in u2_picks:
        lines.append(f"  • {p.bowler_name} (BWL {p.bowler_rating})")
    return "\n".join(lines)
