"""Handlers for /teamname, /purse, /stats."""

import re
import io
import json
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import (
    User, Player, UserRoster, PlayerGameStats,
    ChallengeLeague, ChallengeTeam, ChallengePlayer,
)
from config import get_buy_value, get_sell_value
from services.activity_service import log_activity
from services.flags import get_flag

logger = logging.getLogger(__name__)

TEAM_NAME_REGEX = re.compile(r"^[a-zA-Z0-9 '\-]{3,50}$")


async def teamname_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /teamname <name>\nExample: /teamname Royal Challengers")
        return

    name = " ".join(context.args).strip()
    if not TEAM_NAME_REGEX.match(name):
        await update.message.reply_text(
            "❌ Team name must be 3-50 characters, letters/numbers/spaces only"
        )
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        old = user.team_name or "None"
        user.team_name = name
        log_activity(session, user.id, "teamname", f"Team name: {old} → {name}")
        session.commit()

        await update.message.reply_text(
            f"✅ Team name set to: <b>{name}</b>", parse_mode="HTML"
        )
    except Exception:
        session.rollback()
        logger.exception(f"Teamname error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def purse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        team = user.team_name or "No team name set"
        await update.message.reply_text(
            f"👤 <b>@{user.username or user.first_name}</b>\n"
            f"🏏 Team: {team}\n\n"
            f"💰 <b>Coins:</b> {user.total_coins:,}\n"
            f"💎 <b>Gems:</b> {user.total_gems}\n"
            f"📊 Roster: {user.roster_count}/25",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(f"Purse error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats <player_name> — show per-owner game stats."""
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /stats <player name>\nExample: /stats Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Check user owns this player
        result = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search}%"))
            .first()
        )

        if not result:
            await update.message.reply_text(
                f"❌ Player '{search}' not in your roster. You can only view stats of players you own."
            )
            return

        entry, player = result
        flag = get_flag(player.country)

        # Get or create game stats
        gs = (
            session.query(PlayerGameStats)
            .filter(PlayerGameStats.user_id == user.id, PlayerGameStats.player_id == player.id)
            .first()
        )

        if not gs:
            # No stats yet — never played
            gs = PlayerGameStats(user_id=user.id, player_id=player.id)
            session.add(gs)
            session.commit()

        buy_val = get_buy_value(player.rating)
        owner_name = f"@{user.username}" if user.username else user.first_name

        text = (
            f"📛 <b>{player.name}</b> {flag}\n"
            f"⭐ {player.rating} OVR | {player.category}\n\n"
            f"<code>"
            f"Owner: {owner_name}\n"
            f"Value: {buy_val:,} 🪙\n"
            f"POTM(s): {gs.potm}\n"
            f"\n"
            f"{'🏏 BATTING':<20}{'🎯 BOWLING'}\n"
            f"{'─' * 38}\n"
            f"Inns: {gs.bat_inns:<14}Inns: {gs.bowl_inns}\n"
            f"Runs: {gs.runs:<14}Wickets: {gs.wickets_taken}\n"
            f"50s: {gs.fifties:<15}3-Fers: {gs.three_fers}\n"
            f"100s: {gs.hundreds:<14}5-Fers: {gs.five_fers}\n"
            f"4/6: {gs.fours}/{gs.sixes:<12}Hattricks: {gs.hattricks}\n"
            f"Avg: {gs.bat_avg:<14}Avg: {gs.bowl_avg}\n"
            f"SR: {gs.bat_sr:<15}Economy: {gs.bowl_economy}\n"
            f"Ducks: {gs.ducks:<13}SR: {gs.bowl_sr}\n"
            f"HS: {gs.hs_str:<15}BBF: {gs.bbf_str}\n"
            f"</code>"
        )

        # Lead with the player's card image, then the full stats table. The
        # stats block can exceed Telegram's 1024-char caption limit, so it is
        # sent as a follow-up message rather than a caption.
        card_bytes = None
        try:
            from services.card_generator import generate_card
            card_bytes = await asyncio.to_thread(generate_card, player)
        except Exception:
            logger.exception("Stats card generation failed for %s", player.id)

        if card_bytes:
            caption = (
                f"📛 <b>{player.name}</b> {flag}\n"
                f"⭐ {player.rating} OVR | {player.category}\n"
                f"🏆 POTM(s): {gs.potm}"
            )
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes), caption=caption, parse_mode="HTML")
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception(f"Stats error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def _cp_details(player):
    """Parse a ChallengePlayer.details_json blob into a dict."""
    raw = getattr(player, "details_json", None) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_challenge_player(session, search):
    """Return (ChallengePlayer, ChallengeTeam, ChallengeLeague) for a name.

    Searches every active challenge league. Prefers an exact (case-insensitive)
    name match, then falls back to a partial match. Returns None if the player
    is not part of any challenge league.
    """
    rows = (
        session.query(ChallengePlayer, ChallengeTeam, ChallengeLeague)
        .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
        .join(ChallengeLeague, ChallengeTeam.league_id == ChallengeLeague.id)
        .filter(
            ChallengeLeague.is_active == True,  # noqa: E712
            ChallengeTeam.is_active == True,  # noqa: E712
            ChallengePlayer.name.ilike(f"%{search}%"),
        )
        .order_by(ChallengeLeague.sort_order, ChallengeTeam.sort_order,
                  ChallengePlayer.sort_order)
        .all()
    )
    if not rows:
        return None
    needle = search.strip().lower()
    for cp, team, league in rows:
        if (cp.name or "").strip().lower() == needle:
            return cp, team, league
    return rows[0]


def _aggregate_player_game_stats(session, player_id):
    """Sum every owner's PlayerGameStats for a master player_id into one block."""
    agg = {
        "potm": 0, "bat_inns": 0, "runs": 0, "fifties": 0, "hundreds": 0,
        "fours": 0, "sixes": 0, "balls_faced": 0, "times_out": 0, "ducks": 0,
        "highest_score": 0, "highest_score_not_out": False,
        "bowl_inns": 0, "wickets_taken": 0, "runs_conceded": 0,
        "balls_bowled": 0, "three_fers": 0, "five_fers": 0, "hattricks": 0,
        "best_bowl_wickets": 0, "best_bowl_runs": 0, "_has_bbf": False,
    }
    if not player_id:
        return agg
    rows = (session.query(PlayerGameStats)
            .filter(PlayerGameStats.player_id == player_id).all())
    for gs in rows:
        for key in ("potm", "bat_inns", "runs", "fifties", "hundreds", "fours",
                    "sixes", "balls_faced", "times_out", "ducks", "bowl_inns",
                    "wickets_taken", "runs_conceded", "balls_bowled",
                    "three_fers", "five_fers", "hattricks"):
            agg[key] += getattr(gs, key, 0) or 0
        if (gs.highest_score or 0) > agg["highest_score"]:
            agg["highest_score"] = gs.highest_score or 0
            agg["highest_score_not_out"] = bool(gs.highest_score_not_out)
        # Best bowling = most wickets, fewest runs as a tie-break.
        if gs.bowl_inns:
            bw, br = gs.best_bowl_wickets or 0, gs.best_bowl_runs or 0
            if (not agg["_has_bbf"] or bw > agg["best_bowl_wickets"]
                    or (bw == agg["best_bowl_wickets"] and br < agg["best_bowl_runs"])):
                agg["best_bowl_wickets"], agg["best_bowl_runs"] = bw, br
                agg["_has_bbf"] = True
    return agg


async def statscl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/statscl <player name> — challenge-league player stats. Anyone can view."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /statscl <player name>\nExample: /statscl Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        found = _find_challenge_player(session, search)
        if not found:
            await update.message.reply_text("❌ No player found")
            return

        cp, team, league = found
        details = _cp_details(cp)
        spid = getattr(cp, "source_player_id", None) or details.get("source_player_id")

        # Master Player gives us the correct card art (custom image / rarity
        # template). Fall back to the challenge details for display values.
        master = session.get(Player, spid) if spid else None

        name = cp.name or (master.name if master else search)
        rating = (master.rating if master else None) or int(details.get("rating") or 0)
        category = (master.category if master else None) or details.get("category") or "Player"
        country = (master.country if master else None) or details.get("country") or ""
        flag = get_flag(country) if country else ""

        agg = _aggregate_player_game_stats(session, spid)

        bat_avg = round(agg["runs"] / agg["times_out"], 2) if agg["times_out"] else 0.0
        bat_sr = round(agg["runs"] / agg["balls_faced"] * 100, 2) if agg["balls_faced"] else 0.0
        overs = round(agg["balls_bowled"] / 6, 1) if agg["balls_bowled"] else 0.0
        bowl_avg = round(agg["runs_conceded"] / agg["wickets_taken"], 2) if agg["wickets_taken"] else 0.0
        econ = round(agg["runs_conceded"] / overs, 2) if overs else 0.0
        bowl_sr = round(agg["balls_bowled"] / agg["wickets_taken"], 2) if agg["wickets_taken"] else 0.0
        hs = "-"
        if agg["bat_inns"] or agg["highest_score"]:
            hs = f"{agg['highest_score']}{'*' if agg['highest_score_not_out'] else ''}"
        bbf = f"{agg['best_bowl_wickets']}/{agg['best_bowl_runs']}" if agg["_has_bbf"] else "-"

        text = (
            f"📛 <b>{name}</b> {flag}\n"
            f"⭐ {rating} OVR | {category}\n"
            f"🏆 {league.name} · {team.name}\n\n"
            f"<code>"
            f"Challenge League career\n"
            f"POTM(s): {agg['potm']}\n"
            f"\n"
            f"{'🏏 BATTING':<20}{'🎯 BOWLING'}\n"
            f"{'─' * 38}\n"
            f"Inns: {agg['bat_inns']:<14}Inns: {agg['bowl_inns']}\n"
            f"Runs: {agg['runs']:<14}Wickets: {agg['wickets_taken']}\n"
            f"50s: {agg['fifties']:<15}3-Fers: {agg['three_fers']}\n"
            f"100s: {agg['hundreds']:<14}5-Fers: {agg['five_fers']}\n"
            f"4/6: {str(agg['fours']) + '/' + str(agg['sixes']):<13}Hattricks: {agg['hattricks']}\n"
            f"Avg: {bat_avg:<14}Avg: {bowl_avg}\n"
            f"SR: {bat_sr:<15}Economy: {econ}\n"
            f"Ducks: {agg['ducks']:<13}SR: {bowl_sr}\n"
            f"HS: {hs:<15}BBF: {bbf}\n"
            f"</code>"
        )

        # Lead with the player's card image (master Player art when available).
        card_player = master
        if card_player is None:
            from types import SimpleNamespace
            card_player = SimpleNamespace(
                id=spid or -(cp.id),
                name=name, rating=rating or 50, category=category,
                country=country or "", bat_hand=details.get("bat_hand") or "Right",
                bowl_hand=details.get("bowl_hand") or "Right",
                bowl_style=details.get("bowl_style") or "",
                bat_rating=int(details.get("bat_rating") or 50),
                bowl_rating=int(details.get("bowl_rating") or 40),
                version=details.get("version") or "Base",
                card_file_id=None,
            )

        card_bytes = None
        try:
            from services.card_generator import generate_card
            card_bytes = await asyncio.to_thread(generate_card, card_player)
        except Exception:
            logger.exception("statscl card generation failed for %s", name)

        if card_bytes:
            caption = (
                f"📛 <b>{name}</b> {flag}\n"
                f"⭐ {rating} OVR | {category}\n"
                f"🏆 {league.name} · {team.name}"
            )
            await update.message.reply_photo(
                photo=io.BytesIO(card_bytes), caption=caption, parse_mode="HTML")
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception("statscl error for search '%s'", search)
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()
