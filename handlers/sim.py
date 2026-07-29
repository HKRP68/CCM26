"""/sim — instant auto-simulated match (your XI vs an auto-generated Sim XI).

Unlike /wpm and /cm (interactive, ball-by-ball via the Mini App), /sim resolves a
whole match server-side and posts the scorecard, a winner announcement, and the
ball-by-ball commentary as a JSON file. Team setup is automatic:
  - batting order = the one you saved with /sbo; highest batting rating to
    lowest if you never saved one
  - only Bowlers + All-rounders bowl (rotated, no consecutive overs)
"""

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
from services.commentary_service import build_commentary_picker
from services.sim_match import (
    simulate_match, render_innings_card, render_result, render_match_summary_image,
)
from services.match_formats import resolve_format, get_format, custom_format
from services.ground_conditions import list_pitches, get_pitch_meta
from services.sim_team import append_distinct_base_players, distinct_base_players

logger = logging.getLogger(__name__)

MAX_OVERS = 20


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
                     .order_by(func.random()).limit(64).all())
        append_distinct_base_players(rows, near_pool)

    if len(rows) < 11:
        full_pool = (session.query(Player)
                     .filter(Player.is_active == True)
                     .order_by(func.random()).limit(128).all())
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


def _xi_from_roster(session, user_id, roster):
    """The user's top 11 as engine dicts, in their saved batting order.

    ``roster`` is in ``order_position`` order, so a line-up saved with /sbo is
    already correct and is stamped ``order_locked`` — that marker is what stops
    ``services.sim_match`` from re-sorting it by batting rating. Without a saved
    order the XI gets that rating sort here instead, which is the same line-up
    /sim used to build for everyone.
    """
    from services import batting_order_service as bos
    return bos.ordered_xi_dicts(
        session, user_id, [_player_to_dict(p) for _, p in roster[:11]])


def _parse_format(args):
    """Resolve the /sim argument into a format config.

    Accepts a format name (T10 / T20 / ODI) or a custom over count, defaulting
    to T20. Returns (fmt_dict, error).
    """
    if not args:
        return get_format("T20"), None
    token = args[0]
    fk = resolve_format(token)
    if fk:
        return get_format(fk), None
    try:
        n = int(token)
    except (ValueError, TypeError):
        return None, "Use a format (T10, T20, ODI) or a number of overs."
    if n < 1 or n > MAX_OVERS:
        return None, f"Overs must be between 1 and {MAX_OVERS}."
    return custom_format(n), None


async def sim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    progress = None
    fmt, err = _parse_format(context.args)
    if err:
        await update.message.reply_text(
            f"❌ {err}\nUsage: <code>/sim [T10|T20 | overs 1-{MAX_OVERS}]</code>",
            parse_mode="HTML")
        return
    overs = fmt["overs"]

    # Acknowledge the command before the heavier DB reads, match simulation, and
    # image rendering so users get an immediate bot reply in busy chats.
    try:
        progress = await update.message.reply_text(
            f"🏏 <b>SIM MATCH</b> — {fmt['label']} ({overs} ov)\n"
            "⚡ <i>Setting up teams…</i>",
            parse_mode="HTML")
    except Exception:
        progress = None

    # ---- All DB work + the simulation happen up front, so we don't hold a
    # pooled connection while delivering Telegram messages. ----
    session = get_session()
    try:
        from services.command_config_service import is_command_enabled, get_disabled_message
        if not is_command_enabled(session, "sim"):
            if progress:
                await progress.edit_text(get_disabled_message(session, "sim"), parse_mode="HTML")
            else:
                await update.message.reply_text(
                    get_disabled_message(session, "sim"), parse_mode="HTML")
            return

        user = sync_telegram_user(session, update.effective_user)
        if not user:
            if progress:
                await progress.edit_text("❌ Do /debut first!")
            else:
                await update.message.reply_text("❌ Do /debut first!")
            return

        roster = _get_ordered_roster(session, user.id)
        session.commit()
        if len(roster) < 11:
            text = (
                f"❌ You need 11 players to simulate a match (you have {len(roster)}).\n"
                f"Build a squad with /claim, /buypl or /buypack, then /autobuild.")
            if progress:
                await progress.edit_text(text)
            else:
                await update.message.reply_text(text)
            return

        valid, errors = validate_xi(roster)
        if not valid:
            text = ("❌ Your Playing XI is invalid:\n• " + "\n• ".join(errors)
                    + "\n\nFix it with /pxi, /swap or /autobuild.")
            if progress:
                await progress.edit_text(text, parse_mode="HTML")
            else:
                await update.message.reply_text(text, parse_mode="HTML")
            return

        user_xi = _xi_from_roster(session, user.id, roster)
        team_name = _team_display_name(user, "Your XI")

        opponent = _reply_target_user(session, update)
        if opponent and opponent.id == user.id:
            if progress:
                await progress.edit_text("❌ Reply to another real user to sim your XI vs their XI.")
            else:
                await update.message.reply_text("❌ Reply to another real user to sim your XI vs their XI.")
            return

        if opponent:
            opponent_roster = _get_ordered_roster(session, opponent.id)
            if len(opponent_roster) < 11:
                text = (f"❌ {_team_display_name(opponent)} needs 11 players to simulate "
                        f"(they have {len(opponent_roster)}).")
                if progress:
                    await progress.edit_text(text)
                else:
                    await update.message.reply_text(text)
                return
            opp_valid, opp_errors = validate_xi(opponent_roster)
            if not opp_valid:
                text = (f"❌ {_team_display_name(opponent)}'s Playing XI is invalid:\n• "
                        + "\n• ".join(opp_errors))
                if progress:
                    await progress.edit_text(text, parse_mode="HTML")
                else:
                    await update.message.reply_text(text, parse_mode="HTML")
                return
            opponent_xi = _xi_from_roster(session, opponent.id, opponent_roster)
            opponent_name = _team_display_name(opponent, "Opponent XI")
        else:
            avg_rating = round(sum(p.rating for _, p in roster[:11]) / 11)
            opponent_xi = _build_bot_xi(session, avg_rating)
            if len(opponent_xi) < 11:
                if progress:
                    await progress.edit_text(
                        "⚠️ Couldn't assemble an opponent from the player pool. Try again later.")
                else:
                    await update.message.reply_text(
                        "⚠️ Couldn't assemble an opponent from the player pool. Try again later.")
                return
            opponent_name = "🤖 Sim XI"

        pitches = list_pitches() or ["Flat", "Hard", "Green", "Dry", "Dead"]
        pitch = random.choice(pitches)
        pitch_meta = get_pitch_meta(pitch)
        toss_winner = random.choice([team_name, opponent_name])
        toss_decision = random.choice(["bat", "bowl"])

        commentary = build_commentary_picker(session)

        match = simulate_match(user_xi, opponent_xi, overs, pitch,
                               team_name, opponent_name, toss_winner=toss_winner,
                               toss_decision=toss_decision, commentary=commentary,
                               fmt=fmt)

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
        match_intro = [match["toss"]["text"]] + match["innings1"].get("innings_intro", [])
        feed_payload = {
            "format": match["format"],
            "overs": overs,
            "pitch": pitch,
            "pitch_description": pitch_meta.get("description", ""),
            "toss": match["toss"],
            "match_intro": match_intro,
            "teams": {"home": team_name, "away": opponent_name},
            "result": match["result"]["text"],
            "player_of_the_match": match["potm"],
            "innings": [
                {
                    "innings": match["innings1"]["innings"],
                    "batting_team": match["innings1"]["batting_team"],
                    "bowling_team": match["innings1"]["bowling_team"],
                    "openers": match["innings1"].get("openers", []),
                    "opening_striker": match["innings1"].get("opening_striker", ""),
                    "opening_bowler": match["innings1"].get("opening_bowler", ""),
                    "innings_intro": match["innings1"].get("innings_intro", []),
                    "score": f"{match['innings1']['runs']}/{match['innings1']['wickets']}",
                    "overs": match["innings1"]["overs"],
                    "over_summaries": match["innings1"].get("over_summaries", []),
                },
                {
                    "innings": match["innings2"]["innings"],
                    "batting_team": match["innings2"]["batting_team"],
                    "bowling_team": match["innings2"]["bowling_team"],
                    "openers": match["innings2"].get("openers", []),
                    "opening_striker": match["innings2"].get("opening_striker", ""),
                    "opening_bowler": match["innings2"].get("opening_bowler", ""),
                    "innings_intro": match["innings2"].get("innings_intro", []),
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
        if progress:
            try:
                await progress.edit_text("⚠️ Couldn't start the simulation. Try again.")
            except Exception:
                await update.message.reply_text("⚠️ Couldn't start the simulation. Try again.")
        else:
            await update.message.reply_text("⚠️ Couldn't start the simulation. Try again.")
        return
    finally:
        session.close()

    # ---- Suspense + delivery (no DB connection held) ----
    try:
        if progress:
            try:
                await progress.edit_text(
                    f"🏏 <b>SIM MATCH</b> — {match['format']} ({overs} ov)\n"
                    f"🪙 {match['toss']['text']}\n"
                    f"📍 <b>{pitch}</b> — <i>{pitch_meta.get('description', '')}</i>\n\n"
                    "🏁 <b>Match Ended!</b>",
                    parse_mode="HTML")
            except Exception:
                pass
        else:
            await update.message.reply_text(
                f"🏏 <b>SIM MATCH</b> — {match['format']} ({overs} ov)\n"
                f"🪙 {match['toss']['text']}\n"
                f"📍 <b>{pitch}</b> — <i>{pitch_meta.get('description', '')}</i>\n\n"
                "🏁 <b>Match Ended!</b>",
                parse_mode="HTML")

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
