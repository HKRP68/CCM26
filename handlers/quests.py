"""/myquest — view and claim quest progress."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User
from utils.idempotency import claim_once, release
from services.quest_service import (
    get_user_quests, claim_quest_reward, claim_all_completed,
)
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


def _progress_bar(percent):
    """Return a Unicode progress bar for the given percent (0-100)."""
    filled = int(percent / 10)
    return "█" * filled + "░" * (10 - filled)


QUESTS_PER_PAGE = 8


def _render_quest_list(quests_data, quest_type, user, owner_tg, *,
                       page: int = 0, filter_mode: str = "all"):
    """Render the message text + keyboard for a quest tab.

    Args:
      quests_data: full list of quest dicts (we paginate inside)
      filter_mode: 'all' | 'ready' | 'progress' (in-progress, not ready)
      page: 0-indexed page
    """
    type_label = {"daily": "DAILY", "weekly": "WEEKLY"}.get(quest_type, "MONTHLY")
    type_icon = {"daily": "📅", "weekly": "🗓"}.get(quest_type, "🗓️")
    reset_label = {"daily": "every 24 hours",
                   "weekly": "every Monday"}.get(quest_type, "every 30 days")

    # Apply filter
    if filter_mode == "ready":
        filtered = [it for it in quests_data
                    if it["completed"] and not it["claimed"]]
    elif filter_mode == "progress":
        filtered = [it for it in quests_data
                    if not it["completed"] and not it["claimed"] and it["progress"] > 0]
    else:
        filtered = quests_data

    # Counters across the WHOLE list (for tab badges)
    total_ready = sum(1 for it in quests_data
                      if it["completed"] and not it["claimed"])
    total_in_progress = sum(1 for it in quests_data
                            if not it["completed"] and not it["claimed"]
                            and it["progress"] > 0)

    # Pagination
    total_pages = max(1, (len(filtered) + QUESTS_PER_PAGE - 1) // QUESTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * QUESTS_PER_PAGE
    end = start + QUESTS_PER_PAGE
    page_items = filtered[start:end]

    lines = [
        f"{type_icon} <b>{type_label} QUESTS</b>"
        + (f" · Page {page + 1}/{total_pages}" if total_pages > 1 else ""),
        f"💎 Quest Points: <b>{user.quest_points or 0}</b>  ·  Resets {reset_label}",
        f"🎁 Ready: <b>{total_ready}</b>  ·  ⏳ In Progress: <b>{total_in_progress}</b>"
        + (f"  ·  Showing: <i>{filter_mode}</i>" if filter_mode != "all" else ""),
        "━━━━━━━━━━━━━━━━━━━",
    ]

    if not page_items:
        if filter_mode == "ready":
            lines.append("\n<i>No quests ready to claim right now.</i>")
        elif filter_mode == "progress":
            lines.append("\n<i>No quests currently in progress.</i>")
        else:
            lines.append(f"\n<i>No active {quest_type} quests.</i>")

    for it in page_items:
        q = it["quest"]
        bar = _progress_bar(it["percent"])
        status = ""
        if it["claimed"]:
            status = " ✅"
        elif it["completed"]:
            status = " 🎁"
        rewards = []
        if q.reward_points: rewards.append(f"+{q.reward_points}pt")
        if q.reward_coins: rewards.append(f"+{q.reward_coins:,}🪙")
        if q.reward_gems: rewards.append(f"+{q.reward_gems}💎")
        reward_str = " ".join(rewards)

        lines.append(
            f"\n{q.emoji} <b>{q.name}</b>{status}\n"
            f"  <i>{q.description}</i>\n"
            f"  {bar} <code>{it['progress']}/{it['target']}</code>"
            f"{'  ' + reward_str if reward_str else ''}"
        )

    # ── Build keyboard
    btns = []

    # Tab row: Daily / Weekly / Monthly. Weekly holds the Career Player quests.
    def _tab(label, code):
        return InlineKeyboardButton(
            f"• {label} •" if quest_type == code else label,
            callback_data=f"qst_tab_{owner_tg}_{code}")
    btns.append([
        _tab("📅 Daily", "daily"),
        _tab("🗓 Weekly", "weekly"),
        _tab("🗓️ Monthly", "monthly"),
    ])

    # Filter row: All / Ready / In Progress
    def _flt_lbl(name, code, count=None):
        prefix = "• " if filter_mode == code else ""
        suffix = " •" if filter_mode == code else ""
        ct = f" ({count})" if count is not None else ""
        return f"{prefix}{name}{ct}{suffix}"
    btns.append([
        InlineKeyboardButton(_flt_lbl("All", "all"),
                             callback_data=f"qst_flt_{owner_tg}_{quest_type}_all"),
        InlineKeyboardButton(_flt_lbl("🎁 Ready", "ready", total_ready or None),
                             callback_data=f"qst_flt_{owner_tg}_{quest_type}_ready"),
        InlineKeyboardButton(_flt_lbl("⏳ In Progress", "progress", total_in_progress or None),
                             callback_data=f"qst_flt_{owner_tg}_{quest_type}_progress"),
    ])

    # Pagination row
    if total_pages > 1:
        prev_p = (page - 1) % total_pages
        next_p = (page + 1) % total_pages
        btns.append([
            InlineKeyboardButton("◀ Prev",
                                 callback_data=f"qst_pg_{owner_tg}_{quest_type}_{filter_mode}_{prev_p}"),
            InlineKeyboardButton(f"{page + 1}/{total_pages}",
                                 callback_data=f"qst_noop_{owner_tg}"),
            InlineKeyboardButton("Next ▶",
                                 callback_data=f"qst_pg_{owner_tg}_{quest_type}_{filter_mode}_{next_p}"),
        ])

    # Per-quest claim buttons (only for completed-unclaimed visible on this page)
    for it in page_items:
        if it["completed"] and not it["claimed"]:
            q = it["quest"]
            label = q.name if len(q.name) <= 24 else q.name[:23] + "…"
            btns.append([InlineKeyboardButton(
                f"🎁 {q.emoji} {label}",
                callback_data=f"qst_claim_{owner_tg}_{q.id}",
            )])

    # Claim All (always reflect TOTAL ready, not page-only)
    if total_ready >= 2:
        btns.append([InlineKeyboardButton(
            f"🎁 CLAIM ALL READY ({total_ready})",
            callback_data=f"qst_claimall_{owner_tg}_{quest_type}",
        )])

    btns.append([InlineKeyboardButton("❌ Close",
                                      callback_data=f"qst_close_{owner_tg}")])

    text = "\n".join(lines)
    # Final safety: if somehow over 4096, truncate trailing items with notice
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>… (truncated, use Next ▶ for more)</i>"
    return text, InlineKeyboardMarkup(btns)


# ════════════════════════════════════════════════════════════════════
# /myquest command
# ════════════════════════════════════════════════════════════════════

async def myquest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Trigger rollover for both quest types so the user sees up-to-date
        # auto-claim totals before the message is rendered.
        from services.quest_service import consume_pending_auto_claims
        auto_claimed = []
        for qt in ("daily", "weekly", "monthly"):
            auto_claimed.extend(consume_pending_auto_claims(session, user.id, qt))
        session.commit()

        if auto_claimed:
            total_pts = sum(a.get("points", 0) for a in auto_claimed)
            total_coins = sum(a.get("coins", 0) for a in auto_claimed)
            total_gems = sum(a.get("gems", 0) for a in auto_claimed)
            lines = [
                "🎁 <b>AUTO-CLAIMED FROM LAST PERIOD!</b>",
                "━━━━━━━━━━━━━━━━━━━",
            ]
            for a in auto_claimed[:8]:  # cap shown to avoid spam
                bits = []
                if a.get("points"): bits.append(f"+{a['points']}pt")
                if a.get("coins"):  bits.append(f"+{a['coins']:,}🪙")
                if a.get("gems"):   bits.append(f"+{a['gems']}💎")
                lines.append(f"✅ {a['name']} — {' '.join(bits)}")
            if len(auto_claimed) > 8:
                lines.append(f"…and {len(auto_claimed) - 8} more")
            lines.append(
                f"\n<b>Total:</b> +{total_pts}pt · "
                f"+{total_coins:,}🪙 · +{total_gems}💎"
            )
            try:
                await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            except Exception:
                pass

        quests_data = get_user_quests(session, user.id, "daily")
        text, kb = _render_quest_list(quests_data, "daily", user, tg.id)

        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
        except Exception:
            pass

    except Exception:
        logger.exception("myquest_handler error")
        await update.message.reply_text("⚠️ Error loading quests.")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Filter callback — qst_flt_<owner_tg>_<type>_<filter_mode>
# ════════════════════════════════════════════════════════════════════

async def quest_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_type = parts[3]
        filter_mode = parts[4]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    if quest_type not in ("daily", "weekly", "monthly") or filter_mode not in ("all", "ready", "progress"):
        await q.answer("Unknown")
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id,
                                       page=0, filter_mode=filter_mode)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        logger.exception("quest_filter_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Page callback — qst_pg_<owner_tg>_<type>_<filter_mode>_<page>
# ════════════════════════════════════════════════════════════════════

async def quest_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_type = parts[3]
        filter_mode = parts[4]
        page = int(parts[5])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id,
                                       page=page, filter_mode=filter_mode)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        logger.exception("quest_page_callback error")
    finally:
        session.close()


async def quest_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Page indicator button — just answer the callback."""
    await update.callback_query.answer()


# ════════════════════════════════════════════════════════════════════
# Tab switcher callback — qst_tab_<owner_tg>_<type>
# ════════════════════════════════════════════════════════════════════

async def quest_tab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_type = parts[3]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    if quest_type not in ("daily", "weekly", "monthly"):
        await q.answer("Unknown tab")
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        logger.exception("quest_tab_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Claim single quest — qst_claim_<owner_tg>_<quest_id>
# ════════════════════════════════════════════════════════════════════

async def quest_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_id = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    # Dedup rapid taps on THIS quest's Claim button (per-quest so other quests
    # in the same message can still be claimed).
    key = f"qstc_{q.message.chat_id}_{q.message.message_id}_{quest_id}"
    if not claim_once(key):
        await q.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            release(key)
            await q.answer("Not found")
            return

        ok, msg, reward = claim_quest_reward(session, user.id, quest_id)
        if not ok:
            session.rollback()
            release(key)
            await q.answer(msg, show_alert=True)
            return

        session.commit()

        bits = []
        if reward.get("points"): bits.append(f"+{reward['points']} pts")
        if reward.get("coins"):  bits.append(f"+{reward['coins']:,} 🪙")
        if reward.get("gems"):   bits.append(f"+{reward['gems']} 💎")
        await q.answer(f"🎁 {' · '.join(bits)}")

        # Re-render the current tab — figure out which one we're on
        # by checking which quest type was just claimed
        from models import Quest as Q
        cq = session.query(Q).get(quest_id)
        quest_type = cq.quest_type if cq else "daily"
        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        # Keep the claim (may be post-commit) so a stale tap can't re-claim.
        session.rollback()
        logger.exception("quest_claim_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Claim all — qst_claimall_<owner_tg>_<type>
# ════════════════════════════════════════════════════════════════════

async def quest_claimall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        quest_type = parts[3]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    if quest_type not in ("daily", "weekly", "monthly"):
        await q.answer("Unknown")
        return

    # Dedup rapid taps on the Claim-All button for this quest type.
    key = f"qstca_{q.message.chat_id}_{q.message.message_id}_{quest_type}"
    if not claim_once(key):
        await q.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            release(key)
            return

        count, total = claim_all_completed(session, user.id, quest_type)
        if count == 0:
            release(key)
            await q.answer("Nothing to claim.", show_alert=True)
            return
        session.commit()

        bits = []
        if total["points"]: bits.append(f"+{total['points']} pts")
        if total["coins"]:  bits.append(f"+{total['coins']:,} 🪙")
        if total["gems"]:   bits.append(f"+{total['gems']} 💎")
        await q.answer(f"🎁 Claimed {count}: {' · '.join(bits)}")

        quests_data = get_user_quests(session, user.id, quest_type)
        text, kb = _render_quest_list(quests_data, quest_type, user, tg.id)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        # Keep the claim (may be post-commit) so a stale tap can't re-claim all.
        session.rollback()
        logger.exception("quest_claimall_callback error")
        await q.answer("⚠️ Error", show_alert=True)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Close — qst_close_<owner_tg>
# ════════════════════════════════════════════════════════════════════

async def quest_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    await q.answer("Closed")
    try:
        await q.edit_message_text("✨ <i>Quest panel closed.</i>", parse_mode="HTML")
    except Exception:
        pass
