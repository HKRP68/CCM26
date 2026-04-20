"""Handlers for /playingxi (/pxi), /swapplayers, /setcaptain, bench, XI validation."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.activity_service import log_activity
from services.flags import get_flag
from services.bowling_service import is_spinner as _is_spin, get_bowler_profile_key

logger = logging.getLogger(__name__)


def _ensure_order(session, user_id):
    entries = (session.query(UserRoster).filter(UserRoster.user_id == user_id)
               .order_by(UserRoster.order_position, UserRoster.acquired_date).all())
    for i, e in enumerate(entries, 1):
        if e.order_position != i:
            e.order_position = i
    session.flush()
    return entries


def _get_ordered_roster(session, user_id):
    _ensure_order(session, user_id)
    return (session.query(UserRoster, Player).join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user_id).order_by(UserRoster.order_position).all())


def format_xi_text(roster_list, team_name, captain_rid=None, show_bench=False):
    """Build the 5-section Playing XI text.
    roster_list: list of (UserRoster, Player).
    Only first 11 shown as XI with serial 1-11.
    Bench only shown if show_bench=True.
    """
    top_11 = roster_list[:11]
    bench = roster_list[11:]
    count = len(top_11)

    # First pass: categorize
    batsmen_raw, keepers_raw, allrounders_raw, pacers_raw, spinners_raw = [], [], [], [], []
    total_ovr = 0
    for entry, player in top_11:
        total_ovr += player.rating
        cat = player.category
        pair = (entry, player)
        if cat == "Batsman":
            batsmen_raw.append(pair)
        elif cat == "Wicket Keeper":
            keepers_raw.append(pair)
        elif cat == "All-rounder":
            allrounders_raw.append(pair)
        elif cat == "Bowler":
            if _is_spin(player.bowl_style):
                spinners_raw.append(pair)
            else:
                pacers_raw.append(pair)
        else:
            batsmen_raw.append(pair)

    # Second pass: number in display order (batsmen → keepers → allrounders → pacers → spinners)
    def _fmt(entry, player, serial):
        flag = get_flag(player.country)
        cap = " ©️" if entry.id == captain_rid else ""
        return f"{serial}. {player.name} | {player.rating} | {player.bat_rating} | {player.bowl_rating} | {flag}{cap}"

    batsmen, keepers, allrounders, pacers, spinners = [], [], [], [], []
    serial = 0
    for pair in batsmen_raw:
        serial += 1; batsmen.append(_fmt(pair[0], pair[1], serial))
    for pair in keepers_raw:
        serial += 1; keepers.append(_fmt(pair[0], pair[1], serial))
    for pair in allrounders_raw:
        serial += 1; allrounders.append(_fmt(pair[0], pair[1], serial))
    for pair in pacers_raw:
        serial += 1; pacers.append(_fmt(pair[0], pair[1], serial))
    for pair in spinners_raw:
        serial += 1; spinners.append(_fmt(pair[0], pair[1], serial))

    avg_ovr = round(total_ovr / count, 1) if count else 0

    lines = [
        f"🏏 <b>PLAYING XI</b>\n",
        f"👑 <b>{team_name}</b>",
        f"⭐ Avg Rating: {avg_ovr}\n",
        "━━━━━━━━━━━━━━━━━━━\n",
    ]

    if batsmen:
        lines.append("🏏 <b>BATSMEN</b>")
        lines.append("<blockquote>" + "\n".join(batsmen) + "</blockquote>\n")
    if keepers:
        lines.append("🧤 <b>WICKET-KEEPERS</b>")
        lines.append("<blockquote>" + "\n".join(keepers) + "</blockquote>\n")
    if allrounders:
        lines.append("👥 <b>ALL-ROUNDERS</b>")
        lines.append("<blockquote>" + "\n".join(allrounders) + "</blockquote>\n")
    if pacers:
        lines.append("🔥 <b>PACERS</b>")
        lines.append("<blockquote>" + "\n".join(pacers) + "</blockquote>\n")
    if spinners:
        lines.append("🌀 <b>SPINNERS</b>")
        lines.append("<blockquote>" + "\n".join(spinners) + "</blockquote>\n")

    lines.append("━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"⚡ Total OVR: {total_ovr}")
    lines.append(f"📈 Avg per Player: {avg_ovr}")

    if show_bench and bench:
        lines.append(f"\n📋 <b>Bench ({len(bench)}):</b>")
        for entry, player in bench:
            flag = get_flag(player.country)
            lines.append(f"  {entry.order_position}. {player.name} | {player.rating} | {flag}")

    return "\n".join(lines)


def format_bench_text(roster_list):
    """Format bench players."""
    bench = roster_list[11:]
    if not bench:
        return "📋 <b>BENCH</b>\n\nNo bench players."
    lines = [f"📋 <b>BENCH ({len(bench)} players)</b>\n"]
    for entry, player in bench:
        flag = get_flag(player.country)
        lines.append(f"{entry.order_position}. {player.name} | {player.rating} | {player.bat_rating} | {player.bowl_rating} | {flag}")
    return "\n".join(lines)


# ── XI Validation ────────────────────────────────────────────────────

def validate_xi(roster_list):
    """Validate Playing XI composition for match.
    Returns (valid: bool, errors: list[str])

    Rules:
    - Must have 11 players
    - Min 3, Max 5 Batsmen
    - Min 3, Max 5 Bowlers
    - Min 1, Max 2 Wicket Keepers
    - Min 1, Max 3 All-rounders
    - 3rd ALR must have lower BOWL rating than all pure Bowlers
    """
    if len(roster_list) < 11:
        return False, [f"Need 11 players, have {len(roster_list)}"]

    top_11 = roster_list[:11]
    errors = []

    cats = {"Batsman": [], "Wicket Keeper": [], "All-rounder": [], "Bowler": []}
    for entry, player in top_11:
        cat = player.category
        if cat in cats:
            cats[cat].append(player)
        else:
            cats["Batsman"].append(player)

    batsmen = cats["Batsman"]
    keepers = cats["Wicket Keeper"]
    allrounders = cats["All-rounder"]
    bowlers = cats["Bowler"]

    # Min/Max checks
    if len(batsmen) < 3:
        errors.append(f"Need min 3 Batsmen (have {len(batsmen)})")
    if len(batsmen) > 5:
        errors.append(f"Max 5 Batsmen (have {len(batsmen)})")
    if len(bowlers) < 3:
        errors.append(f"Need min 3 Bowlers (have {len(bowlers)})")
    if len(bowlers) > 5:
        errors.append(f"Max 5 Bowlers (have {len(bowlers)})")
    if len(keepers) < 1:
        errors.append("Need at least 1 Wicket Keeper")
    if len(keepers) > 2:
        errors.append(f"Max 2 Wicket Keepers (have {len(keepers)})")
    if len(allrounders) < 1:
        errors.append("Need at least 1 All-rounder")
    if len(allrounders) > 3:
        errors.append(f"Max 3 All-rounders (have {len(allrounders)})")

    # 3rd ALR rule: 3rd all-rounder must have lower bowl rating than all bowlers
    if len(allrounders) == 3 and bowlers:
        alr_sorted = sorted(allrounders, key=lambda p: p.bowl_rating, reverse=True)
        third_alr = alr_sorted[0]  # highest bowl rating ALR
        min_bowler_bowl = min(b.bowl_rating for b in bowlers)
        if third_alr.bowl_rating >= min_bowler_bowl:
            errors.append(
                f"3rd All-rounder ({third_alr.name}, BOWL {third_alr.bowl_rating}) "
                f"must have lower BOWL rating than all Bowlers (lowest: {min_bowler_bowl})"
            )

    return len(errors) == 0, errors


# ── Handlers ─────────────────────────────────────────────────────────

async def playingxi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        # Check if viewing another user's XI
        target_user = None
        if context.args:
            target_name = context.args[0].lstrip("@").strip()
            target_user = session.query(User).filter(User.username.ilike(target_name)).first()
            if not target_user:
                await update.message.reply_text(f"❌ @{target_name} not found.")
                return

        viewer = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not viewer:
            await update.message.reply_text("❌ Do /debut first!")
            return

        view_user = target_user or viewer
        is_own = (view_user.id == viewer.id)

        roster = _get_ordered_roster(session, view_user.id)
        session.commit()

        if not roster:
            name = f"@{view_user.username}" if target_user else "You"
            await update.message.reply_text(f"❌ {name} has no players!")
            return

        team_name = view_user.team_name or f"@{view_user.username or view_user.first_name}'s XI"
        text = format_xi_text(roster, team_name, view_user.captain_roster_id, show_bench=False)

        # Add bench button only for own XI
        bench = roster[11:]
        if is_own and bench:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📋 View Bench ({len(bench)})", callback_data=f"viewbench_{view_user.id}")
            ]])
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception("PlayingXI error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def bench_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bench players — only for the owner."""
    q = update.callback_query
    tg_user = q.from_user
    parts = q.data.split("_")
    owner_uid = int(parts[1])

    session = get_session()
    try:
        viewer = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not viewer or viewer.id != owner_uid:
            await q.answer("You can only view your own bench!")
            return
        await q.answer()

        roster = _get_ordered_roster(session, owner_uid)
        session.commit()

        text = format_bench_text(roster)
        await q.edit_message_text(
            q.message.text_html + "\n\n" + text if q.message.text_html else text,
            parse_mode="HTML")
    except Exception:
        logger.exception("Bench err")
    finally:
        session.close()


async def swapplayers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /swapplayers <pos1> <pos2>")
        return
    try:
        pos1, pos2 = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Numbers only.")
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        entries = _ensure_order(session, user.id)
        if pos1 < 1 or pos2 < 1 or pos1 > len(entries) or pos2 > len(entries) or pos1 == pos2:
            await update.message.reply_text(f"❌ Positions 1-{len(entries)}, different.")
            return
        e1, e2 = entries[pos1 - 1], entries[pos2 - 1]
        e1.order_position, e2.order_position = e2.order_position, e1.order_position
        p1 = session.query(Player).get(e1.player_id)
        p2 = session.query(Player).get(e2.player_id)
        log_activity(session, user.id, "swap", f"Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}")
        session.commit()
        xi_note = "\n🏏 Playing XI updated!" if pos1 <= 11 or pos2 <= 11 else ""
        await update.message.reply_text(f"✅ Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}{xi_note}")
    except Exception:
        session.rollback()
        logger.exception("Swap err")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def setcaptain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /setcaptain <player name>")
        return
    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        result = (session.query(UserRoster, Player).join(Player, UserRoster.player_id == Player.id)
                  .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search}%")).first())
        if not result:
            await update.message.reply_text(f"❌ '{search}' not in roster")
            return
        entry, player = result
        user.captain_roster_id = entry.id
        log_activity(session, user.id, "captain", f"Captain: {player.name}", player_name=player.name)
        session.commit()
        await update.message.reply_text(f"👑 <b>{player.name}</b> is now captain!", parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("Captain err")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()
