"""/start card, /commands, and the buttons of the onboarding + retention flows.

* :func:`send_start_card`: what a bare /start answers with. A newcomer gets
  the short welcome (join GC → /debut → first match), a player mid-journey
  gets their checklist, everyone else gets a status card of what's ready.
* :func:`commands_handler`: /commands, the full catalogue that /start used
  to print in one wall.
* :func:`gcjoin_check_callback`: "✅ I've joined" on the Official GC prompt.
* :func:`onboarding_callback`: the ``onb_*`` buttons on the journey cards.
* :func:`comeback_claim_callback`: "🎁 Claim" on a comeback DM.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User

logger = logging.getLogger(__name__)

_IST = timedelta(hours=5, minutes=30)
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(html_text):
    """Strip tags for the rich rendering of an HTML snippet."""
    import html as _h
    return _h.unescape(_TAG_RE.sub("", html_text or "")).strip()


# ── Funnel telemetry ────────────────────────────────────────────────

_VISIT_SEEN = {}


def record_start_visit(telegram_id):
    """One ``StartVisit`` row per Telegram user per IST day (worker thread).

    Unlike ``activity_service.record_start`` this also counts people with no
    account yet, which is the top of the onboarding funnel.
    """
    if not telegram_id:
        return
    day = (datetime.utcnow() + _IST).strftime("%Y-%m-%d")
    if _VISIT_SEEN.get(telegram_id) == day:
        return
    if len(_VISIT_SEEN) > 50000:
        _VISIT_SEEN.clear()
    _VISIT_SEEN[telegram_id] = day
    try:
        from models import StartVisit
        s = get_session()
        try:
            exists = (s.query(StartVisit.id)
                      .filter(StartVisit.telegram_id == telegram_id,
                              StartVisit.day == day).first())
            if not exists:
                s.add(StartVisit(telegram_id=telegram_id, day=day))
                s.commit()
        except Exception:
            s.rollback()
        finally:
            s.close()
    except Exception:
        logger.debug("start visit record failed", exc_info=True)


# ── /start ──────────────────────────────────────────────────────────

def _load_start_state(telegram_id):
    """Worker-thread read of everything the /start card shows."""
    from models import UserStats
    s = get_session()
    try:
        user = s.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return None, [], 0
        stats = s.query(UserStats).filter(UserStats.user_id == user.id).first()
        ready = []
        streak = 0
        try:
            from services.cooldown_notifier import ready_now
            ready = ready_now(s, user, stats)
        except Exception:
            logger.debug("ready_now failed", exc_info=True)
        if stats is not None:
            streak = int(getattr(stats, "login_streak", 0) or 0)
        s.expunge(user)
        return user, ready, streak
    finally:
        s.close()


async def send_start_card(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          extra_html: str = ""):
    """Answer a bare /start with the right card for this user."""
    from services import gc_gate, onboarding_rich, onboarding_service
    from services.config_service import get_config
    from services.rich_message import paragraph, reply_rich

    tg_user = update.effective_user
    cfg = get_config()
    asyncio.get_running_loop().run_in_executor(None, record_start_visit, tg_user.id)

    user, ready, streak = await asyncio.to_thread(_load_start_state, tg_user.id)

    if user is None:
        in_gc = None
        gid = gc_gate.official_group_id(cfg)
        if gid and onboarding_service.gc_step_applies(cfg):
            in_gc = await gc_gate.check_membership(context.bot, gid, tg_user.id)
        blocks, html_text, kb = onboarding_rich.welcome_card(
            tg_user.first_name, in_gc=in_gc, cfg=cfg, extra_html=extra_html)
    elif onboarding_service.is_in_onboarding(user, cfg):
        blocks, html_text, kb = onboarding_rich.journey_card(user, cfg=cfg)
        html_text += extra_html
    else:
        blocks, html_text, kb = onboarding_rich.status_card(
            user, ready=ready, streak=streak, extra_html=extra_html)

    if blocks is not None and extra_html:
        blocks.append(paragraph(_plain(extra_html)))
    await reply_rich(update.effective_message, blocks, html_text, reply_markup=kb)


# ── /commands ───────────────────────────────────────────────────────

async def commands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services import onboarding_rich
    from services.rich_message import reply_rich
    blocks, html_text, _ = onboarding_rich.commands_card()
    await reply_rich(update.effective_message, blocks, html_text)


# ── "✅ I've joined" ─────────────────────────────────────────────────

def _has_account(telegram_id):
    s = get_session()
    try:
        return s.query(User.id).filter(User.telegram_id == telegram_id).first() is not None
    finally:
        s.close()


async def gcjoin_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services import gc_gate, onboarding_service
    query = update.callback_query
    user = query.from_user
    gid = gc_gate.official_group_id()
    if not gid:
        await query.answer("✅ You're all set!")
        return

    cache = context.bot_data.get("gc_member_cache")
    if isinstance(cache, dict):
        cache.pop(user.id, None)
    is_member = await gc_gate.check_membership(context.bot, gid, user.id)

    if is_member is False:
        await query.answer(
            "❌ Not yet! Tap 🔗 Join Official GC, join the group, then come back "
            "and tap ✅ I've joined.", show_alert=True)
        return

    if is_member and isinstance(cache, dict):
        import time
        cache[user.id] = (time.monotonic() + 600, True)

    # A failed lookup (None) still lets the player through — the gate fails
    # open — but the journey reward is only paid for a confirmed membership.
    result = None
    if is_member is True:
        result = await asyncio.to_thread(onboarding_service.complete_gc_step_for, user.id)
    if result:
        onboarding_service.discard_pending(user.id)
    has_account = await asyncio.to_thread(_has_account, user.id)
    await query.answer("✅ Verified! Welcome to the community.")

    reward = ""
    if result:
        reward = (f"\n🎁 Journey reward: <b>+{onboarding_service.reward_label(result['coins'], result['gems'])}</b>")
    if has_account:
        text = ("✅ <b>You're in!</b> Every command is unlocked." + reward
                + "\n\nSee what to do next with /start.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🧭 What's next?", callback_data="onb_view")]])
    else:
        text = ("✅ <b>You're in!</b> Welcome to the community.\n\n"
                "Next step: create your team and claim your free starting squad.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🎁 Start — /debut", callback_data="onb_debut")]])
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                      disable_web_page_preview=True)
    except Exception:
        try:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("gcjoin confirmation failed", exc_info=True)


# ── Journey buttons ─────────────────────────────────────────────────

def _load_user(telegram_id):
    s = get_session()
    try:
        u = s.query(User).filter(User.telegram_id == telegram_id).first()
        if u is not None:
            s.expunge(u)
        return u
    finally:
        s.close()


async def onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services import onboarding_rich, onboarding_service
    from services.rich_message import edit_rich, reply_rich
    query = update.callback_query
    data = query.data or ""
    tg_user = query.from_user

    if data == "onb_debut":
        await query.answer()
        from handlers.debut import debut_handler
        # debut_handler answers through effective_message / effective_user;
        # point them at this chat and at whoever pressed the button.
        shim = SimpleNamespace(
            effective_user=tg_user, effective_chat=query.message.chat,
            effective_message=query.message, message=query.message,
            callback_query=None)
        await debut_handler(shim, context)
        return

    if data == "onb_cmds":
        await query.answer()
        blocks, html_text, _ = onboarding_rich.commands_card()
        await reply_rich(query.message, blocks, html_text)
        return

    if data == "onb_gc":
        # Same check as the gate's button; it also ticks the journey step.
        await gcjoin_check_callback(update, context)
        return

    if data.startswith("onb_do_"):
        key = data[len("onb_do_"):]
        step = onboarding_service.STEP_BY_KEY.get(key)
        await query.answer()
        if step and step.command:
            await query.message.reply_text(
                f"👉 Tap to run it: {step.command}\n<i>{step.how}</i>",
                parse_mode="HTML")
        return

    # onb_view (and anything unknown): redraw the journey / status card.
    user = await asyncio.to_thread(_load_user, tg_user.id)
    if user is None:
        await query.answer("Send /debut first to create your team!", show_alert=True)
        return
    await query.answer()
    if onboarding_service.is_in_onboarding(user) or user.onboarding_done_at:
        blocks, html_text, kb = onboarding_rich.journey_card(user)
    else:
        u, ready, streak = await asyncio.to_thread(_load_start_state, tg_user.id)
        blocks, html_text, kb = onboarding_rich.status_card(u, ready=ready, streak=streak)
    if not await edit_rich(query, blocks, html_text, reply_markup=kb):
        await reply_rich(query.message, blocks, html_text, reply_markup=kb)


# ── Comeback claim ──────────────────────────────────────────────────

def _claim_comeback(telegram_id, tier):
    from services import comeback_service
    s = get_session()
    try:
        user = s.query(User).filter(User.telegram_id == telegram_id).first()
        if user is None:
            return None
        reward = comeback_service.claim(s, user, tier)
        if reward:
            s.commit()
        return reward
    except Exception:
        s.rollback()
        logger.exception("comeback claim failed")
        return None
    finally:
        s.close()


async def comeback_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from services.onboarding_service import reward_label
    query = update.callback_query
    tier = (query.data or "").rsplit("_", 1)[-1]
    reward = await asyncio.to_thread(_claim_comeback, query.from_user.id, tier)
    if not reward:
        await query.answer("This gift was already claimed (or has expired).",
                           show_alert=True)
        return
    await query.answer(f"🎁 +{reward_label(reward['coins'], reward['gems'])} added!")
    text = (f"🎉 <b>Welcome back!</b> <b>+{reward_label(reward['coins'], reward['gems'])}</b> "
            "added to your purse.\n\nHere's what's waiting for you:")
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except Exception:
        pass
    # Follow with the status card: what's ready, what to do next.
    u, ready, streak = await asyncio.to_thread(_load_start_state, query.from_user.id)
    if u is not None:
        from services import onboarding_rich
        from services.rich_message import reply_rich
        blocks, html_text, kb = onboarding_rich.status_card(u, ready=ready, streak=streak)
        await reply_rich(query.message, blocks, html_text, reply_markup=kb)
