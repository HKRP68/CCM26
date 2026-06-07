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
from services.commentary_service import pick_commentary
from services.sim_match import (
    simulate_match, render_innings_card, render_result,
)

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

    rows = (pick("Batsman", 4) + pick("Wicket Keeper", 1)
            + pick("All-rounder", 2) + pick("Bowler", 4))
    return [_player_to_dict(p) for p in rows if p]


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

        user_xi = [_player_to_dict(p) for _, p in roster[:11]]
        avg_rating = round(sum(p.rating for _, p in roster[:11]) / 11)
        bot_xi = _build_bot_xi(session, avg_rating)
        if len(bot_xi) < 11:
            await update.message.reply_text(
                "⚠️ Couldn't assemble an opponent from the player pool. Try again later.")
            return

        team_name = user.team_name or (f"@{user.username}" if user.username else "Your XI")
        bot_name = "🤖 Sim XI"
        pitch = random.choice(PITCHES)
        toss_winner = random.choice([team_name, bot_name])

        def commentary(event_key, batsman, bowler, fielder, keeper, runs):
            return pick_commentary(session, event_key, batsman=batsman, bowler=bowler,
                                   fielder=fielder, keeper=keeper, runs=runs)

        match = simulate_match(user_xi, bot_xi, overs, pitch,
                               team_name, bot_name, toss_winner=toss_winner,
                               commentary=commentary)

        # Pre-render everything while the session is alive.
        card1 = render_innings_card(match["innings1"])
        card2 = render_innings_card(match["innings2"])
        result_text = render_result(match)
        feed_payload = {
            "overs": overs,
            "pitch": pitch,
            "toss": f"{toss_winner} won the toss & batted first",
            "teams": {"home": team_name, "away": bot_name},
            "result": match["result"]["text"],
            "player_of_the_match": match["potm"],
            "commentary": match["commentary_feed"],
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

        buf = io.BytesIO(json.dumps(feed_payload, indent=2, ensure_ascii=False).encode("utf-8"))
        buf.seek(0)
        await update.message.reply_document(
            InputFile(buf, filename=f"sim_commentary_{update.effective_user.id}.json"),
            caption="📜 Ball-by-ball commentary (JSON)")
    except Exception:
        logger.exception("sim_handler delivery failed")
