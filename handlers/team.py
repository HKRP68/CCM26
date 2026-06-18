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

        # Lead with the player's card image only when there's an admin-uploaded
        # custom card; otherwise send the stats as text only (no generated card).
        # The stats block can exceed Telegram's 1024-char caption limit, so it is
        # sent as a follow-up message rather than a caption.
        card_bytes = None
        try:
            from services.player_image_service import has_custom_card, get_custom_image_bytes
            if has_custom_card(player.id, session):
                card_bytes = get_custom_image_bytes(player.id)
        except Exception:
            logger.exception("Stats custom card load failed for %s", player.id)

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


def _find_challenge_players(session, search):
    """Return [(ChallengePlayer, ChallengeTeam, ChallengeLeague), ...] for a name.

    Searches every active challenge league. Prefers exact (case-insensitive)
    name matches; if none are exact, returns the partial matches. The same real
    player can appear in several leagues, so this returns every appearance.
    Returns an empty list when the player is not part of any challenge league.
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
        .order_by(ChallengeLeague.sort_order, ChallengeLeague.name,
                  ChallengeTeam.sort_order, ChallengePlayer.sort_order)
        .all()
    )
    if not rows:
        return []
    needle = search.strip().lower()
    exact = [r for r in rows if (r[0].name or "").strip().lower() == needle]
    return exact or rows


def _aggregate_player_game_stats(session, player_ids):
    """Sum every owner's PlayerGameStats for one or more master player_ids."""
    agg = {
        "potm": 0, "bat_inns": 0, "runs": 0, "fifties": 0, "hundreds": 0,
        "fours": 0, "sixes": 0, "balls_faced": 0, "times_out": 0, "ducks": 0,
        "highest_score": 0, "highest_score_not_out": False,
        "bowl_inns": 0, "wickets_taken": 0, "runs_conceded": 0,
        "balls_bowled": 0, "three_fers": 0, "five_fers": 0, "hattricks": 0,
        "best_bowl_wickets": 0, "best_bowl_runs": 0, "_has_bbf": False,
    }
    ids = [pid for pid in {p for p in (player_ids or []) if p}]
    if not ids:
        return agg
    rows = (session.query(PlayerGameStats)
            .filter(PlayerGameStats.player_id.in_(ids)).all())
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


def _cp_attr(cp, details, master, key, fallback, cast=None):
    """Read an attribute preferring the per-league details, then the master."""
    val = details.get(key)
    if val in (None, ""):
        val = getattr(master, key, None) if master else None
    if val in (None, ""):
        val = fallback
    if cast is not None:
        try:
            return cast(val)
        except (TypeError, ValueError):
            return fallback
    return val


async def statscl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/statscl <player name> — Challenge League player stats. Anyone can view.

    Lists every league the player appears in (with that league's card values,
    which can differ league to league) plus their overall match career.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /statscl <player name>\nExample: /statscl Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        rows = _find_challenge_players(session, search)
        if not rows:
            await update.message.reply_text("❌ No player found")
            return

        # A partial search (e.g. "Singh") can match several different players.
        # Resolve to one: prefer an exact name match, else the player with the
        # most league appearances. Group by normalized name so the same person
        # linked to different master ids across leagues stays together — and
        # unrelated players never get merged into one card.
        needle = search.strip().lower()
        groups = {}
        for r in rows:
            groups.setdefault((r[0].name or "").strip().lower(), []).append(r)
        rows = groups.get(needle) or max(groups.values(), key=len)

        # Display name from the first (exact-preferred) appearance.
        name = rows[0][0].name or search

        # Pick a master Player (for the card art) from any appearance that links
        # one, and collect every linked master id for the career aggregate.
        master = None
        source_ids = []
        primary_details = {}
        for cp, _team, _league in rows:
            d = _cp_details(cp)
            spid = getattr(cp, "source_player_id", None) or d.get("source_player_id")
            if spid:
                source_ids.append(spid)
                if master is None:
                    m = session.get(Player, spid)
                    if m is not None:
                        master, primary_details = m, d
        if not primary_details:
            primary_details = _cp_details(rows[0][0])

        country = (getattr(master, "country", None)
                   or primary_details.get("country") or "")
        flag = get_flag(country) if country else ""
        head_rating = _cp_attr(rows[0][0], primary_details, master,
                               "rating", 0, int)
        head_category = _cp_attr(rows[0][0], primary_details, master,
                                 "category", "Player")

        # ── Per-league appearances ──
        league_lines = []
        for cp, team, league in rows[:8]:
            d = _cp_details(cp)
            rating = _cp_attr(cp, d, master, "rating", 0, int)
            role = _cp_attr(cp, d, master, "category", "Player")
            bat_r = _cp_attr(cp, d, master, "bat_rating", 0, int)
            bowl_r = _cp_attr(cp, d, master, "bowl_rating", 0, int)
            bat_hand = _cp_attr(cp, d, master, "bat_hand", "Right")
            bowl_style = _cp_attr(cp, d, master, "bowl_style", "")
            extra = f" · {bowl_style}" if bowl_style else ""
            league_lines.append(
                f"🏆 <b>{league.name}</b> · {team.name}\n"
                f"   ⭐ {rating} OVR · {role}\n"
                f"   🏏 Bat {bat_r} · 🎯 Bowl {bowl_r}\n"
                f"   {bat_hand}-hand{extra}"
            )
        more = ""
        if len(rows) > 8:
            more = f"\n<i>…and {len(rows) - 8} more league(s)</i>"

        # ── Overall match career (shared across leagues/modes) ──
        agg = _aggregate_player_game_stats(session, source_ids)
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
        played = bool(agg["bat_inns"] or agg["bowl_inns"])

        header = (
            f"📛 <b>{name}</b> {flag}\n"
            f"📋 In <b>{len(rows)}</b> Challenge League{'s' if len(rows) != 1 else ''}\n\n"
        )
        text = header + "\n\n".join(league_lines) + more + "\n\n"
        if played:
            text += (
                f"<b>📊 Match Career</b> (all leagues)\n"
                f"<code>"
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
        else:
            text += "<i>📊 No match stats yet — this player hasn't featured in a completed match.</i>"

        # Lead with the player's card image (master Player art when available).
        card_player = master
        if card_player is None:
            from types import SimpleNamespace
            card_player = SimpleNamespace(
                id=-(rows[0][0].id),
                name=name, rating=head_rating or 50, category=head_category,
                country=country or "",
                bat_hand=primary_details.get("bat_hand") or "Right",
                bowl_hand=primary_details.get("bowl_hand") or "Right",
                bowl_style=primary_details.get("bowl_style") or "",
                bat_rating=int(primary_details.get("bat_rating") or 50),
                bowl_rating=int(primary_details.get("bowl_rating") or 40),
                version=primary_details.get("version") or "Base",
                card_file_id=None,
            )

        # Only show an image when the master player has an admin-uploaded custom
        # card; otherwise send the stats as text only (no generated card).
        card_bytes = None
        try:
            from services.player_image_service import has_custom_card, get_custom_image_bytes
            cp_id = getattr(card_player, "id", None)
            if master is not None and cp_id and cp_id > 0 and has_custom_card(cp_id, session):
                card_bytes = get_custom_image_bytes(cp_id)
        except Exception:
            logger.exception("statscl custom card load failed for %s", name)

        if card_bytes:
            caption = (
                f"📛 <b>{name}</b> {flag}\n"
                f"⭐ {head_rating} OVR | {head_category}\n"
                f"📋 In {len(rows)} Challenge League{'s' if len(rows) != 1 else ''}"
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
