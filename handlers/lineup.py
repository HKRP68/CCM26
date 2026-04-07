"""Handlers for /playingxi (/pxi), /swapplayers (/swappl), /setcaptain."""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.activity_service import log_activity

logger = logging.getLogger(__name__)

# Playing XI composition rules
XI_RULES = {
    "Batsman":       {"min": 3, "max": 5},
    "Bowler":        {"min": 3, "max": 5},
    "All-rounder":   {"min": 1, "max": 3},
    "Wicket Keeper": {"min": 1, "max": 2},
}


def _ensure_order(session, user_id):
    """Make sure all roster entries have sequential order_position."""
    entries = (
        session.query(UserRoster)
        .filter(UserRoster.user_id == user_id)
        .order_by(UserRoster.order_position, UserRoster.acquired_date)
        .all()
    )
    for i, e in enumerate(entries, 1):
        if e.order_position != i:
            e.order_position = i
    session.flush()
    return entries


def _get_ordered_roster(session, user_id):
    """Return list of (UserRoster, Player) ordered by position."""
    _ensure_order(session, user_id)
    return (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id)
        .order_by(UserRoster.order_position)
        .all()
    )


def _validate_xi(top_11):
    """Validate Playing XI composition. Returns (is_valid, issues)."""
    counts = {"Batsman": 0, "Bowler": 0, "All-rounder": 0, "Wicket Keeper": 0}
    for _, p in top_11:
        cat = p.category if p.category in counts else "Batsman"
        counts[cat] += 1

    issues = []
    for cat, rules in XI_RULES.items():
        c = counts.get(cat, 0)
        if c < rules["min"]:
            issues.append(f"Need at least {rules['min']} {cat}(s), have {c}")
        if c > rules["max"]:
            issues.append(f"Max {rules['max']} {cat}(s) allowed, have {c}")

    # 3rd all-rounder must have lower bowl rating than all bowlers
    allrounders = sorted(
        [(e, p) for e, p in top_11 if p.category == "All-rounder"],
        key=lambda x: x[1].bowl_rating, reverse=True
    )
    bowlers = [(e, p) for e, p in top_11 if p.category == "Bowler"]
    if len(allrounders) >= 3 and bowlers:
        min_bowler_rating = min(p.bowl_rating for _, p in bowlers)
        third_alr = allrounders[2]
        if third_alr[1].bowl_rating >= min_bowler_rating:
            issues.append(
                f"3rd All-rounder ({third_alr[1].name}, bowl {third_alr[1].bowl_rating}) "
                f"must have lower bowl rating than bowlers (min {min_bowler_rating})"
            )

    return len(issues) == 0, issues


async def playingxi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        roster = _get_ordered_roster(session, user.id)
        session.commit()

        if len(roster) < 11:
            await update.message.reply_text(
                f"❌ You need at least 11 players. You have {len(roster)}.\nUse /claim to get more!"
            )
            return

        top_11 = roster[:11]
        bench = roster[11:]
        is_valid, issues = _validate_xi(top_11)

        # Find captain
        captain_name = None
        if user.captain_roster_id:
            for e, p in roster:
                if e.id == user.captain_roster_id:
                    captain_name = p.name
                    break

        lines = ["🏏 <b>PLAYING XI</b>\n"]
        for i, (entry, player) in enumerate(top_11, 1):
            cap = " ©️" if entry.id == user.captain_roster_id else ""
            lines.append(f"  {i}. {player.name} - {player.rating} OVR | {player.category}{cap}")

        if captain_name:
            lines.append(f"\n👑 Captain: {captain_name}")

        lines.append(f"\n{'✅ Valid XI' if is_valid else '⚠️ Invalid XI'}")
        if issues:
            lines.append("\n<b>Issues:</b>")
            for iss in issues:
                lines.append(f"  • {iss}")

        if bench:
            lines.append(f"\n📋 <b>Bench ({len(bench)}):</b>")
            for i, (entry, player) in enumerate(bench, 12):
                lines.append(f"  {i}. {player.name} - {player.rating} OVR | {player.category}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        logger.exception(f"PlayingXI error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def swapplayers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /swapplayers <pos1> <pos2>\nExample: /swapplayers 9 13")
        return

    try:
        pos1 = int(context.args[0])
        pos2 = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Positions must be numbers. Example: /swapplayers 9 13")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        entries = _ensure_order(session, user.id)

        if pos1 < 1 or pos2 < 1 or pos1 > len(entries) or pos2 > len(entries):
            await update.message.reply_text(f"❌ Positions must be between 1 and {len(entries)}")
            return

        if pos1 == pos2:
            await update.message.reply_text("❌ Can't swap a player with themselves")
            return

        e1 = entries[pos1 - 1]
        e2 = entries[pos2 - 1]

        e1.order_position, e2.order_position = e2.order_position, e1.order_position

        p1 = session.query(Player).get(e1.player_id)
        p2 = session.query(Player).get(e2.player_id)

        log_activity(session, user.id, "swap",
                     f"Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}")
        session.commit()

        xi_note = ""
        if pos1 <= 11 or pos2 <= 11:
            xi_note = "\n🏏 Playing XI updated!"

        await update.message.reply_text(
            f"✅ Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}{xi_note}"
        )

    except Exception:
        session.rollback()
        logger.exception(f"Swap error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def setcaptain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /setcaptain <player name>\nExample: /setcaptain Virat Kohli")
        return

    search_name = " ".join(context.args).strip()

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        result = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search_name}%"))
            .first()
        )

        if not result:
            await update.message.reply_text(f"❌ Player '{search_name}' not in your roster")
            return

        entry, player = result
        user.captain_roster_id = entry.id
        log_activity(session, user.id, "captain",
                     f"Set captain: {player.name}",
                     player_name=player.name, player_rating=player.rating)
        session.commit()

        await update.message.reply_text(
            f"👑 <b>{player.name}</b> is now your team captain!",
            parse_mode="HTML",
        )

    except Exception:
        session.rollback()
        logger.exception(f"SetCaptain error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()
