"""Handlers for /playingxi (/pxi), /swapplayers, /setcaptain, bench, XI validation."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.activity_service import log_activity
from services.telegram_user_service import resolve_command_target, sync_telegram_user
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


def _build_display_order(roster_list):
    """Return list of (entry, player) in the same order as displayed by /pxi.

    Display order for top 11: Batsmen → Wicket Keepers → All-rounders → Pacers → Spinners.
    Positions 12+ stay as bench in raw order.

    So display position 1..11 maps to the same categorized-sorted 11 shown in /pxi,
    and display position 12+ keeps roster order.
    """
    top_11 = roster_list[:11]
    bench = roster_list[11:]

    batsmen, keepers, allrounders, pacers, spinners = [], [], [], [], []
    for pair in top_11:
        _, player = pair
        cat = player.category
        if cat == "Batsman":
            batsmen.append(pair)
        elif cat == "Wicket Keeper":
            keepers.append(pair)
        elif cat == "All-rounder":
            allrounders.append(pair)
        elif cat == "Bowler":
            if _is_spin(player.bowl_style):
                spinners.append(pair)
            else:
                pacers.append(pair)
        else:
            batsmen.append(pair)

    # Return in the same order as /pxi displays
    return batsmen + keepers + allrounders + pacers + spinners + bench


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

    # 3rd ALR rule: if you have 3 all-rounders, AT LEAST ONE of them
    # must have a BOWL rating lower than all pure Bowlers' BOWL ratings
    if len(allrounders) == 3 and bowlers:
        min_bowler_bowl = min(b.bowl_rating for b in bowlers)
        # Find the weakest (lowest bowl rating) all-rounder
        weakest_alr = min(allrounders, key=lambda p: p.bowl_rating)
        if weakest_alr.bowl_rating >= min_bowler_bowl:
            errors.append(
                f"At least one All-rounder must have BOWL rating lower than all Bowlers. "
                f"Lowest Bowler BOWL: {min_bowler_bowl}. "
                f"Your weakest All-rounder ({weakest_alr.name}, BOWL {weakest_alr.bowl_rating}) "
                f"doesn't qualify."
            )

    # Duplicate-version rule: each player (regardless of which version) can
    # only appear ONCE in the XI. So if user has the Base + IPL 2026 cards
    # of the same player, only ONE of them can be in the playing XI.
    seen_base_ids = {}  # base_id -> first variant we saw
    duplicate_pairs = []
    for entry, player in top_11:
        base_id = player.parent_player_id or player.id
        if base_id in seen_base_ids:
            duplicate_pairs.append((seen_base_ids[base_id], player))
        else:
            seen_base_ids[base_id] = player
    if duplicate_pairs:
        for first, second in duplicate_pairs:
            label_first = first.version or "Base"
            label_second = second.version or "Base"
            errors.append(
                f"Duplicate of <b>{first.name}</b> in XI: "
                f"<i>{label_first}</i> and <i>{label_second}</i>. "
                f"Pick only one version. Use /swap to swap one out."
            )

    return len(errors) == 0, errors


# ── Handlers ─────────────────────────────────────────────────────────

async def playingxi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        # Check if viewing another user's XI. Reply targeting supports users without @username.
        target_user = None
        if context.args:
            target_user, target_source = resolve_command_target(session, update, context, "xi")
            if not target_user:
                if target_source == "not_mention":
                    await update.message.reply_text("❌ Reply to a user or use a real @username mention.")
                else:
                    await update.message.reply_text("❌ User not found. If they changed or don't have a username, reply to their message and run /xi.")
                return

        viewer = sync_telegram_user(session, tg_user)
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
        await update.message.reply_text("Usage: /swap <pos1> <pos2>\nPositions match the numbers shown in /pxi")
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

        # Get roster in raw order first (to ensure order_position is clean)
        raw_roster = _get_ordered_roster(session, user.id)

        # Build display order (matches /pxi numbering)
        display_order = _build_display_order(raw_roster)
        total = len(display_order)

        if pos1 < 1 or pos2 < 1 or pos1 > total or pos2 > total or pos1 == pos2:
            await update.message.reply_text(
                f"❌ Positions must be 1-{total}, and different.")
            return

        # Get entries at the DISPLAY positions
        e1, p1 = display_order[pos1 - 1]
        e2, p2 = display_order[pos2 - 1]

        # Swap their order_position values in the database
        e1.order_position, e2.order_position = e2.order_position, e1.order_position

        log_activity(session, user.id, "swap", f"Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}")
        session.commit()

        xi_note = "\n🏏 Playing XI updated!" if pos1 <= 11 or pos2 <= 11 else ""
        await update.message.reply_text(
            f"✅ Swapped #{pos1} {p1.name} ↔ #{pos2} {p2.name}{xi_note}")
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


# ════════════════════════════════════════════════════════════════════
# /autobuild — pick best 11 from roster automatically
# ════════════════════════════════════════════════════════════════════

def _build_best_xi(roster_pairs):
    """Given a list of (UserRoster entry, Player) pairs, choose the best 11
    that satisfies validate_xi rules and has the highest total rating.

    Returns:
        (xi_pairs_in_display_order, bench_pairs, error_str_or_None)

    Display order: Batsmen → Wicket Keepers → All-rounders → Pacers → Spinners,
    sorted by rating desc within each.
    """
    # Bucket by category
    buckets = {"Batsman": [], "Wicket Keeper": [], "All-rounder": [], "Bowler": []}
    for pair in roster_pairs:
        _, player = pair
        cat = player.category if player.category in buckets else "Batsman"
        buckets[cat].append(pair)

    # Sort each bucket by rating desc (greedy-pick top N from each)
    for cat in buckets:
        buckets[cat].sort(key=lambda pair: pair[1].rating, reverse=True)

    # Enumerate valid compositions (b, bw, k, alr) summing to 11
    compositions = []
    for b in range(3, 6):
        for bw in range(3, 6):
            for k in range(1, 3):
                for alr in range(1, 4):
                    if b + bw + k + alr == 11:
                        compositions.append((b, bw, k, alr))

    best = None  # (total_rating, xi_pairs)

    for b_cnt, bw_cnt, k_cnt, alr_cnt in compositions:
        # Need enough in each bucket
        if (len(buckets["Batsman"]) < b_cnt
                or len(buckets["Bowler"]) < bw_cnt
                or len(buckets["Wicket Keeper"]) < k_cnt
                or len(buckets["All-rounder"]) < alr_cnt):
            continue

        bat_pick = buckets["Batsman"][:b_cnt]
        bowl_pick = buckets["Bowler"][:bw_cnt]
        keep_pick = buckets["Wicket Keeper"][:k_cnt]
        alr_pick = buckets["All-rounder"][:alr_cnt]

        # 3rd-ALR rule: if 3 ALR, weakest ALR must have bowl_rating
        # less than min(bowl_rating of pure bowlers picked)
        if alr_cnt == 3 and bowl_pick:
            min_bowler_bowl = min(p[1].bowl_rating for p in bowl_pick)
            weakest_alr = min(alr_pick, key=lambda p: p[1].bowl_rating)
            if weakest_alr[1].bowl_rating >= min_bowler_bowl:
                # Try swapping with a lower-bowl-rating ALR from the bench
                lower_alr = None
                for cand in buckets["All-rounder"][alr_cnt:]:
                    if cand[1].bowl_rating < min_bowler_bowl:
                        lower_alr = cand
                        break
                if lower_alr is None:
                    continue  # composition impossible for this roster
                alr_pick = [p for p in alr_pick if p[1].id != weakest_alr[1].id]
                alr_pick.append(lower_alr)

        xi_pairs = bat_pick + bowl_pick + keep_pick + alr_pick
        if len(xi_pairs) != 11:
            continue

        total = sum(p[1].rating for p in xi_pairs)
        if best is None or total > best[0]:
            best = (total, xi_pairs)

    if best is None:
        return None, None, (
            "No valid XI possible from your roster. Need at least: "
            "3 Batsmen, 3 Bowlers, 1 Wicket Keeper, 1 All-rounder."
        )

    _, xi_pairs = best

    # Display-order arrangement
    by_cat = {"Batsman": [], "Wicket Keeper": [], "All-rounder": [],
              "Pacer": [], "Spinner": []}
    for pair in xi_pairs:
        _, player = pair
        cat = player.category
        if cat == "Bowler":
            sub = "Spinner" if _is_spin(player.bowl_style) else "Pacer"
            by_cat[sub].append(pair)
        elif cat in by_cat:
            by_cat[cat].append(pair)
        else:
            by_cat["Batsman"].append(pair)

    for cat in by_cat:
        by_cat[cat].sort(key=lambda pair: pair[1].rating, reverse=True)

    ordered_xi = (
        by_cat["Batsman"] + by_cat["Wicket Keeper"] + by_cat["All-rounder"]
        + by_cat["Pacer"] + by_cat["Spinner"]
    )

    xi_ids = {p[1].id for p in ordered_xi}
    bench = [p for p in roster_pairs if p[1].id not in xi_ids]
    bench.sort(key=lambda pair: pair[1].rating, reverse=True)

    return ordered_xi, bench, None


async def autobuild_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-build the user's playing XI: pick top-rated 11 satisfying XI rules,
    then reorder roster so the chosen 11 occupy positions 1-11."""
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        roster = _get_ordered_roster(session, user.id)
        if len(roster) < 11:
            await update.message.reply_text(
                f"❌ You need at least 11 players to build an XI.\n"
                f"You have <b>{len(roster)}</b>. Get more with /claim, /gspin, "
                f"/buypl, or /buypack.", parse_mode="HTML")
            return

        xi, bench, err = _build_best_xi(roster)
        if err:
            await update.message.reply_text(f"❌ {err}")
            return

        # Apply: reorder roster positions
        for i, (entry, _) in enumerate(xi, start=1):
            entry.order_position = i
        for i, (entry, _) in enumerate(bench, start=12):
            entry.order_position = i
        session.commit()

        # Build the response message
        total_ovr = sum(p.rating for _, p in xi)
        avg_ovr = total_ovr / 11

        bat_count = sum(1 for _, p in xi if p.category == "Batsman")
        wk_count = sum(1 for _, p in xi if p.category == "Wicket Keeper")
        alr_count = sum(1 for _, p in xi if p.category == "All-rounder")
        bowl_count = sum(1 for _, p in xi if p.category == "Bowler")
        pacer_count = sum(1 for _, p in xi
                          if p.category == "Bowler" and not _is_spin(p.bowl_style))
        spinner_count = bowl_count - pacer_count

        lines = [
            "🤖 <b>AUTO-BUILT XI</b>",
            "━━━━━━━━━━━━━━━━━━━",
            f"📊 Total OVR: <b>{total_ovr}</b>  (avg <b>{avg_ovr:.1f}</b>)",
            f"🏏 {bat_count} BAT · 🥅 {wk_count} WK · ⚡ {alr_count} ALR · "
            f"🎯 {pacer_count} PAC · 🌀 {spinner_count} SPIN",
            "━━━━━━━━━━━━━━━━━━━",
        ]
        for i, (_, p) in enumerate(xi, start=1):
            tag = ""
            if p.category == "Wicket Keeper":
                tag = " 🥅"
            elif p.category == "All-rounder":
                tag = " ⚡"
            elif p.category == "Bowler":
                tag = " 🌀" if _is_spin(p.bowl_style) else " 🎯"
            lines.append(f"{i:>2}. {p.name} — <b>{p.rating}</b>{tag}")

        lines.append("\n<i>Use /pxi to view, /swap to adjust manually.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception:
        session.rollback()
        logger.exception("autobuild_handler err")
        await update.message.reply_text("⚠️ Error building XI. Try again.")
    finally:
        session.close()
