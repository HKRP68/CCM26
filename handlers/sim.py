"""/sim — instant auto-simulated match (your XI vs an auto-generated Sim XI).

Unlike /wpm and /cm (interactive, ball-by-ball via the Mini App), /sim resolves a
whole match server-side and posts the scorecard, a winner announcement, and the
ball-by-ball commentary as a JSON file. Team setup is automatic:
  - batting order = highest batting rating to lowest
  - only Bowlers + All-rounders bowl (rotated, no consecutive overs)
"""

import asyncio
import io
import json
import logging
import random

from sqlalchemy import func
from telegram import Update, InputFile
from telegram.ext import ContextTypes

from database import get_session
from models import Player
from handlers.lineup import _get_ordered_roster, validate_xi
from services.telegram_user_service import sync_telegram_user
from services.config_service import get_config
from services.commentary_service import pick_commentary
from services.sim_match import (
    simulate_match, render_innings_card, render_result, render_match_summary_image,
)
from services.sim_team import append_distinct_base_players, distinct_base_players

logger = logging.getLogger(__name__)

DEFAULT_OVERS = 5
MAX_OVERS = 20
PITCHES = ["Flat", "Hard", "Green", "Dry", "Dead"]


def _player_to_dict(p):
    return {
        "name": p.name,
        "rating": p.rating,
        "bat_rating": p.bat_rating or p.rating,
        "bowl_rating": p.bowl_rating or 0,
        "category": p.category,
        "bowl_style": p.bowl_style,
        "bowl_hand": p.bowl_hand,
        "bat_hand": p.bat_hand,
    }


def _build_bot_xi(session, avg_rating):
    """Assemble a balanced 11 from the player pool near the user's avg rating."""
    lo, hi = max(40, avg_rating - 8), min(100, avg_rating + 8)

    def pick(category, n):
        q = (session.query(Player)
             .filter(Player.category == category, Player.is_active == True,
                     Player.rating.between(lo, hi))
             .order_by(func.random()).limit(n).all())
        if len(q) < n:  # widen the band if the pool is thin
            q = (session.query(Player)
                 .filter(Player.category == category, Player.is_active == True)
                 .order_by(func.random()).limit(n).all())
        return q

    rows = distinct_base_players(
        pick("Batsman", 4) + pick("Wicket Keeper", 1)
        + pick("All-rounder", 2) + pick("Bowler", 4)
    )

    if len(rows) < 11:
        near_pool = (session.query(Player)
                     .filter(Player.is_active == True, Player.rating.between(lo, hi))
                     .order_by(func.random()).all())
        append_distinct_base_players(rows, near_pool)

    if len(rows) < 11:
        full_pool = (session.query(Player)
                     .filter(Player.is_active == True)
                     .order_by(func.random()).all())
        append_distinct_base_players(rows, full_pool)

    return [_player_to_dict(p) for p in rows[:11] if p]


def _team_display_name(user, fallback="User XI"):
    if not user:
        return fallback
    if user.team_name:
        return user.team_name
    if user.username:
        return f"@{user.username}"
    if user.first_name:
        return f"{user.first_name}'s XI"
    return fallback


def _reply_target_user(session, update):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    reply = getattr(message, "reply_to_message", None) if message is not None else None
    tg_user = getattr(reply, "from_user", None) if reply is not None else None
    if tg_user is None or getattr(tg_user, "is_bot", False):
        return None
    return sync_telegram_user(session, tg_user)


def _xi_from_roster(roster):
    return [_player_to_dict(p) for _, p in roster[:11]]


def _parse_overs(args):
    if not args:
        return DEFAULT_OVERS, None
    try:
        n = int(args[0])
    except (ValueError, TypeError):
        return None, "Overs must be a number."
    if n < 1 or n > MAX_OVERS:
        return None, f"Overs must be between 1 and {MAX_OVERS}."
    return n, None


async def sim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    overs, err = _parse_overs(context.args)
    if err:
        await update.message.reply_text(f"❌ {err}\nUsage: <code>/sim [overs 1-{MAX_OVERS}]</code>",
                                        parse_mode="HTML")
        return

    # ---- All DB work + the (instant) simulation happen up front, so we don't
    # hold a pooled connection during the suspense delay. ----
    session = get_session()
    try:
        user = sync_telegram_user(session, update.effective_user)
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        roster = _get_ordered_roster(session, user.id)
        session.commit()
        if len(roster) < 11:
            await update.message.reply_text(
                f"❌ You need 11 players to simulate a match (you have {len(roster)}).\n"
                f"Build a squad with /claim, /buypl or /buypack, then /autobuild.")
            return

        valid, errors = validate_xi(roster)
        if not valid:
            await update.message.reply_text(
                "❌ Your Playing XI is invalid:\n• " + "\n• ".join(errors)
                + "\n\nFix it with /pxi, /swap or /autobuild.", parse_mode="HTML")
            return

        user_xi = _xi_from_roster(roster)
        team_name = _team_display_name(user, "Your XI")

        opponent = _reply_target_user(session, update)
        if opponent and opponent.id == user.id:
            await update.message.reply_text("❌ Reply to another real user to sim your XI vs their XI.")
            return

        if opponent:
            opponent_roster = _get_ordered_roster(session, opponent.id)
            if len(opponent_roster) < 11:
                await update.message.reply_text(
                    f"❌ {_team_display_name(opponent)} needs 11 players to simulate "
                    f"(they have {len(opponent_roster)}).")
                return
            opp_valid, opp_errors = validate_xi(opponent_roster)
            if not opp_valid:
                await update.message.reply_text(
                    f"❌ {_team_display_name(opponent)}'s Playing XI is invalid:\n• "
                    + "\n• ".join(opp_errors), parse_mode="HTML")
                return
            opponent_xi = _xi_from_roster(opponent_roster)
            opponent_name = _team_display_name(opponent, "Opponent XI")
        else:
            avg_rating = round(sum(p.rating for _, p in roster[:11]) / 11)
            opponent_xi = _build_bot_xi(session, avg_rating)
            if len(opponent_xi) < 11:
                await update.message.reply_text(
                    "⚠️ Couldn't assemble an opponent from the player pool. Try again later.")
                return
            opponent_name = "🤖 Sim XI"

        pitch = random.choice(PITCHES)
        toss_winner = random.choice([team_name, opponent_name])

        def commentary(event_key, batsman, bowler, fielder, keeper, runs):
            return pick_commentary(session, event_key, batsman=batsman, bowler=bowler,
                                   fielder=fielder, keeper=keeper, runs=runs)

        match = simulate_match(user_xi, opponent_xi, overs, pitch,
                               team_name, opponent_name, toss_winner=toss_winner,
                               commentary=commentary)

        # Pre-render everything while the session is alive.
        card1 = render_innings_card(match["innings1"])
        card2 = render_innings_card(match["innings2"])
        result_text = render_result(match)
        cfg = get_config()
        summary_bytes = render_match_summary_image(
            match,
            text_settings=cfg.get("scorecard_text_settings"),
            stadium=f"SIM • {pitch} pitch",
        )
        feed_payload = {
            "overs": overs,
            "pitch": pitch,
            "toss": f"{toss_winner} won the toss & batted first",
            "teams": {"home": team_name, "away": opponent_name},
            "result": match["result"]["text"],
            "player_of_the_match": match["potm"],
            "innings": [
                {
                    "innings": match["innings1"]["innings"],
                    "batting_team": match["innings1"]["batting_team"],
                    "score": f"{match['innings1']['runs']}/{match['innings1']['wickets']}",
                    "overs": match["innings1"]["overs"],
                    "over_summaries": match["innings1"].get("over_summaries", []),
                },
                {
                    "innings": match["innings2"]["innings"],
                    "batting_team": match["innings2"]["batting_team"],
                    "score": f"{match['innings2']['runs']}/{match['innings2']['wickets']}",
                    "overs": match["innings2"]["overs"],
                    "over_summaries": match["innings2"].get("over_summaries", []),
                },
            ],
            "commentary": match["commentary_feed"],
            "note": "/sim is a friendly simulation and does not update player batting or bowling stats.",
        }
    except Exception:
        logger.exception("sim_handler preparation failed")
        await update.message.reply_text("⚠️ Couldn't start the simulation. Try again.")
        return
    finally:
        session.close()

    # ---- Suspense + delivery (no DB connection held) ----
    try:
        progress = await update.message.reply_text(
            f"🏏 <b>SIM MATCH</b> — {overs} overs\n"
            f"🪙 {toss_winner} won the toss and chose to bat\n"
            f"📍 Pitch: {pitch}\n\n⏳ <i>Match in progress…</i>",
            parse_mode="HTML")
        await asyncio.sleep(10)
        try:
            await progress.edit_text("🏁 <b>Match Ended!</b>", parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(1)

        await update.message.reply_text(card1, parse_mode="HTML")
        await update.message.reply_text(card2, parse_mode="HTML")
        await update.message.reply_text(result_text, parse_mode="HTML")

        if summary_bytes:
            photo = io.BytesIO(summary_bytes)
            photo.name = f"sim_summary_{update.effective_user.id}.png"
            photo.seek(0)
            await update.message.reply_photo(
                photo=InputFile(photo, filename=photo.name),
                caption="🖼️ Match Summary")

        buf = io.BytesIO(json.dumps(feed_payload, indent=2, ensure_ascii=False).encode("utf-8"))
        buf.seek(0)
        await update.message.reply_document(
            InputFile(buf, filename=f"sim_commentary_{update.effective_user.id}.json"),
            caption="📜 Ball-by-ball commentary (JSON)")
    except Exception:
        logger.exception("sim_handler delivery failed")
