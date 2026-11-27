"""Community Arena Telegram commands.

User:  /community /cxp /checkin /gctop /gcgames /gclevels /gcrewards /gchelp
Admin: /gcevent /gcstop /gcsettings

Rich-text with HTML fallback throughout (services.rich_message). Admin
authorization is checked server-side via services.admin_ids.
"""

from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services import community_config_service as C
from services import community_rich as CR
from services import rich_message as R

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────

def _official_group_id():
    try:
        from services.gc_gate import official_group_id
        return official_group_id()
    except Exception:
        return None


def _is_official_chat(update):
    gid = _official_group_id()
    chat = update.effective_chat
    return gid is not None and chat is not None and chat.id == gid


def _group_branding():
    """The configured Official Group's display label + join URL.

    Reads the admin-set Branding / Official Group config (never hardcoded) so
    every Community Arena surface can name the real group, e.g. @cmugames, and
    offer a one-tap Join link.
    """
    try:
        from services import gc_gate
        from services.config_service import get_config
        cfg = get_config()
        label = gc_gate._group_label(cfg)   # "@cmugames", or a sensible fallback
        url = gc_gate.join_url(cfg)
        return label, url
    except Exception:
        return None, None


def _join_and_app_keyboard(update, *, label=None, url=None):
    """A keyboard with a Join-group button (when a link is configured) plus the
    Open-Community-Arena Mini App button."""
    rows = []
    if url:
        rows.append([InlineKeyboardButton(
            f"🔗 Join {label}" if label and label.startswith("@") else "🔗 Join Official Group",
            url=url)])
    app_kb = _community_webapp_kb(update)
    if app_kb is not None:
        rows.extend(app_kb.inline_keyboard)
    return InlineKeyboardMarkup(rows) if rows else None


def _registered_user(session, tg_id):
    from models import User
    return session.query(User).filter(User.telegram_id == tg_id).first()


def _is_admin(tg_id):
    try:
        from services.admin_ids import is_admin
        return is_admin(tg_id)
    except Exception:
        return False


async def _reply(update, blocks, html, *, kb=None):
    try:
        await R.reply_rich(update.effective_message, blocks, html, reply_markup=kb)
    except Exception:
        try:
            await update.effective_message.reply_text(
                html, parse_mode="HTML", reply_markup=kb,
                disable_web_page_preview=True)
        except Exception:
            logger.exception("community reply failed")


def _community_webapp_kb(update):
    """A button that opens the Community Arena Mini App page, when a WEBAPP_URL
    is configured. WebApp buttons work only in private chats; groups get a plain
    https link (Telegram rejects web_app buttons outside DMs)."""
    import os
    import re
    from telegram import WebAppInfo
    base = os.getenv("WEBAPP_URL", "").strip()
    if not base.startswith("https://"):
        return None
    # Derive the community page URL from the Mini App base.
    if re.search(r"/webapp/?$", base):
        url = re.sub(r"/webapp/?$", "/webapp/community", base)
    else:
        url = base.rstrip("/") + "/webapp/community"
    is_private = getattr(update.effective_chat, "type", None) == "private"
    try:
        if is_private:
            return InlineKeyboardMarkup([[InlineKeyboardButton(
                "🏟️ Open Community Arena", web_app=WebAppInfo(url=url))]])
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏟️ Open Community Arena", url=url)]])
    except Exception:
        return None


def _need_registration_text():
    return ("🏏 You need a CMU account first. Send /debut to get started, "
            "then join the Community Arena!")


# ── /checkin ─────────────────────────────────────────────────────────────

async def checkin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not C.is_enabled():
        await update.effective_message.reply_text("🏟️ The Community Arena is currently disabled.")
        return
    if not _is_official_chat(update):
        label, url = _group_branding()
        where = label if label else "the Official CMU Group"
        await update.effective_message.reply_text(
            f"📅 Daily check-in works only in {where}. Join and send /checkin there!",
            reply_markup=_join_and_app_keyboard(update, label=label, url=url))
        return
    from database import get_session
    from services import community_checkin_service as CK
    tg = update.effective_user
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        if user is None:
            await update.effective_message.reply_text(_need_registration_text())
            return
        uid = user.id
        name = f"@{user.username}" if user.username else (user.first_name or "Manager")
    finally:
        session.close()
    res = CK.check_in(uid)
    if res.get("reason") == "suspended":
        await update.effective_message.reply_text(
            "⚠️ Your Community rewards are currently suspended.")
        return
    blocks, html = CR.checkin_card(res, name)
    await _reply(update, blocks, html)


# ── /cxp ──────────────────────────────────────────────────────────────────

async def cxp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_session
    from services import community_profile_service as P
    tg = update.effective_user
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        if user is None:
            await update.effective_message.reply_text(_need_registration_text())
            return
        d = P.cxp_summary(session, user.id, user=user)
    finally:
        session.close()
    blocks, html = CR.cxp_card(d)
    await _reply(update, blocks, html, kb=_community_webapp_kb(update))


# ── /community ─────────────────────────────────────────────────────────

async def community_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_session
    from models import User
    from services import community_profile_service as P
    msg = update.effective_message
    session = get_session()
    try:
        target = None
        # Reply → that user; else @username arg; else self.
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target = _registered_user(session, msg.reply_to_message.from_user.id)
        elif context.args:
            uname = context.args[0].lstrip("@").strip()
            if uname:
                target = (session.query(User)
                          .filter(User.username.ilike(uname)).first())
        if target is None:
            target = _registered_user(session, update.effective_user.id)
        if target is None:
            await msg.reply_text(_need_registration_text())
            return
        d = P.profile_summary(session, target.id, user=target)
    finally:
        session.close()
    blocks, html = CR.profile_card(d)
    await _reply(update, blocks, html, kb=_community_webapp_kb(update))


# ── /gcgames ─────────────────────────────────────────────────────────────

async def gcgames_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_session
    from services import community_profile_service as P
    from services import community_game_service as G
    tg = update.effective_user
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        if user is None:
            await update.effective_message.reply_text(_need_registration_text())
            return
        g = P.game_summary(session, user.id)
        name = f"@{user.username}" if user.username else (user.first_name or "Manager")
    finally:
        session.close()
    lines = [f"🎮 <b>COMMUNITY GAME STATS</b>", f"Manager: {CR.esc(name)}",
             f"Games played: {g['plays']:,}", f"Total wins: {g['wins']:,}",
             f"Win rate: {g['win_pct']:.0f}%"]
    for lbl, cnt in g["by_type"]:
        lines.append(f"• {lbl}: {cnt:,} wins")
    if g.get("fastest_s"):
        lines.append(f"⚡ Fastest correct answer: {g['fastest_s']:.2f}s")
    if g.get("avg_s"):
        lines.append(f"⏱️ Avg winning time: {g['avg_s']:.2f}s")
    await _reply(update, None, "\n".join(lines))


# ── /gclevels ─────────────────────────────────────────────────────────

async def gclevels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_session
    from services import community_xp_service as X
    tg = update.effective_user
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        current = 1
        if user is not None:
            prof = X.get_profile(session, user.id)
            current = (prof.level if prof else 1) or 1
        ladder = X._ladder(session)
    finally:
        session.close()
    blocks, html = CR.levels_card(ladder, current)
    await _reply(update, blocks, html)


# ── /gcrewards ─────────────────────────────────────────────────────────

async def gcrewards_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_session
    from services import community_level_service as L
    tg = update.effective_user
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        uid = user.id if user else None
        rows = L.milestone_overview(session, uid) if uid else L.milestone_overview(session, -1)
    finally:
        session.close()
    blocks, html = CR.rewards_card(rows)
    await _reply(update, blocks, html)


# ── /gchelp ──────────────────────────────────────────────────────────────

async def gchelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label, url = _group_branding()
    blocks, html = CR.help_card(group_label=label, group_url=url)
    await _reply(update, blocks, html,
                 kb=_join_and_app_keyboard(update, label=label, url=url))


# ── /gctop (leaderboards with inline tabs) ──────────────────────────────

_CAT_BUTTONS = [("cxp", "🏆 XP"), ("wins", "⚡ Wins"), ("streak", "🔥 Streak"),
                ("messages", "💬 Msgs")]
_PERIODS = [("today", "Today"), ("week", "Week"), ("month", "Month"), ("all", "All Time")]
_PAGE_SIZE = 10


def _gctop_keyboard(category, period, page, total):
    rows = []
    rows.append([InlineKeyboardButton(("• " if c == category else "") + lbl,
                                      callback_data=f"gctop:{c}:{period}:0")
                 for c, lbl in _CAT_BUTTONS])
    rows.append([InlineKeyboardButton(("• " if p == period else "") + lbl,
                                      callback_data=f"gctop:{category}:{p}:0")
                 for p, lbl in _PERIODS])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"gctop:{category}:{period}:{page-1}"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"gctop:{category}:{period}:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def _build_leaderboard(session, category, period, page, user_id):
    from services import community_leaderboard_service as LB
    lb = LB.leaderboard(session, category, period, limit=_PAGE_SIZE,
                        offset=page * _PAGE_SIZE, user_id=user_id)
    return lb


async def gctop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_session
    tg = update.effective_user
    category, period, page = "cxp", "all", 0
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        uid = user.id if user else None
        lb = _build_leaderboard(session, category, period, page, uid)
    finally:
        session.close()
    blocks, html = CR.leaderboard_card(lb)
    kb = _gctop_keyboard(category, period, page, lb["total"])
    await _reply(update, blocks, html, kb=kb)


async def gctop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, category, period, page = query.data.split(":")
        page = max(0, int(page))
    except Exception:
        await query.answer()
        return
    from database import get_session
    tg = update.effective_user
    session = get_session()
    try:
        user = _registered_user(session, tg.id)
        uid = user.id if user else None
        lb = _build_leaderboard(session, category, period, page, uid)
    finally:
        session.close()
    blocks, html = CR.leaderboard_card(lb)
    kb = _gctop_keyboard(category, period, page, lb["total"])
    try:
        await R.edit_rich(query, blocks, html, reply_markup=kb)
    except Exception:
        try:
            await query.edit_message_text(html, parse_mode="HTML", reply_markup=kb,
                                           disable_web_page_preview=True)
        except Exception:
            pass
    await query.answer()


# ── Admin: /gcevent /gcstop /gcsettings ─────────────────────────────────

async def gcevent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    if not _is_admin(tg.id):
        await update.effective_message.reply_text("⛔ Admins only.")
        return
    if not C.is_enabled():
        await update.effective_message.reply_text(
            "🏟️ The Community Arena is disabled. Enable it with /gcsettings on.")
        return
    if not _is_official_chat(update):
        await update.effective_message.reply_text(
            "ℹ️ Launch community games inside the Official CMU Group.")
        return
    from services import community_game_service as G
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: /gcevent <game> [difficulty]\nGames: "
            + ", ".join(G.GAME_TYPES))
        return
    game_type = args[0].lower()
    if game_type not in G.GAME_TYPES:
        await update.effective_message.reply_text(
            "Unknown game. Choose: " + ", ".join(G.GAME_TYPES))
        return
    difficulty = args[1].lower() if len(args) > 1 else None
    from handlers.community_games import launch_and_announce
    game_id, err = await launch_and_announce(
        context, update.effective_chat.id, game_type, source="manual",
        launched_by=tg.id, difficulty=difficulty)
    if game_id is None:
        msgs = {"active_exists": "A community game is already running here.",
                "no_payload": "Couldn't build that game from the current player pool.",
                "game_disabled": "That game is disabled in settings.",
                "disabled": "The Community Arena is disabled."}
        await update.effective_message.reply_text("⚠️ " + msgs.get(err, f"Could not launch ({err})."))


async def gcstop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    if not _is_admin(tg.id):
        await update.effective_message.reply_text("⛔ Admins only.")
        return
    from database import get_session
    from services import community_game_service as G
    session = get_session()
    try:
        game = G.active_game_for_chat(session, update.effective_chat.id, lock=True)
        if game is None:
            await update.effective_message.reply_text("No active community game here.")
            return
        G.cancel_game(session, game)
        session.commit()
    except Exception:
        session.rollback()
        await update.effective_message.reply_text("Could not cancel the game.")
        return
    finally:
        session.close()
    await update.effective_message.reply_text("🛑 Community game cancelled.")


async def gcsettings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    if not _is_admin(tg.id):
        await update.effective_message.reply_text("⛔ Admins only.")
        return
    args = [a.lower() for a in (context.args or [])]
    from database import get_session

    if args:
        session = get_session()
        try:
            if args[0] in ("on", "enable"):
                C.save(session, enabled=True, updated_by=tg.id)
                await update.effective_message.reply_text("✅ Community Arena enabled.")
                return
            if args[0] in ("off", "disable"):
                C.save(session, enabled=False, updated_by=tg.id)
                await update.effective_message.reply_text("🛑 Community Arena disabled.")
                return
            if args[0] == "auto" and len(args) > 1:
                on = args[1] in ("on", "enable", "true", "1")
                C.save(session, auto_enabled=on, updated_by=tg.id)
                await update.effective_message.reply_text(
                    f"✅ Automatic games {'enabled' if on else 'disabled'}.")
                return
        finally:
            session.close()
        await update.effective_message.reply_text(
            "Usage: /gcsettings [on|off] | /gcsettings auto [on|off]\n"
            "Full settings live on the admin website → Community Engagement.")
        return

    cfg = C.get_config(fresh=True)
    sched = cfg["scheduler"]
    gid = _official_group_id()
    enabled_games = [t for t in cfg["games"] if cfg["games"][t].get("enabled")]
    html = (
        "⚙️ <b>COMMUNITY ARENA SETTINGS</b>\n"
        f"Status: {'🟢 Enabled' if cfg.get('enabled') else '🔴 Disabled'}\n"
        f"Automatic games: {'🟢 On' if cfg.get('auto_enabled') else '🔴 Off'}\n"
        f"Official group: {gid if gid else '⚠️ not configured'}\n"
        f"Max daily CXP: {cfg.get('max_daily_xp')}\n"
        f"Active hours (IST): {sched.get('active_start_hour')}:00–"
        f"{sched.get('active_end_hour')}:00\n"
        f"Max games/day: {sched.get('max_games_per_day')} · "
        f"Min gap: {sched.get('min_gap_minutes')}m · "
        f"Inactivity: {sched.get('inactivity_minutes')}m · "
        f"Interval: {sched.get('interval_minutes') or 'off'}m\n"
        f"Enabled games: {', '.join(enabled_games) or 'none'}\n\n"
        "Toggle: /gcsettings on|off · /gcsettings auto on|off\n"
        "Full configuration: admin website → Community Engagement.")
    await update.effective_message.reply_text(html, parse_mode="HTML")
