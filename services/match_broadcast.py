"""Chat-side helpers for Mini-App matches: the post-toss launch message,
the live scorecard broadcast, and the "Play Match" button.

Launch scheme:
  • Private chats → a Telegram Web App button opening
        https://<host>/cricket?match_id=<id>&chat_id=<chat>
  • Group chats   → a deep link with startapp=cricket_<matchId>_<chatId>
    (Web App keyboard buttons aren't allowed in groups, so a t.me deep link
    is used; the Mini App reads start_param and routes to the match.)

Backward-compatible: the Mini App still understands the older lm_/sc_ forms.
"""

import asyncio
import logging
import os
import random

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo)

logger = logging.getLogger(__name__)


def _webapp_host():
    """Base https host for the Mini App, from WEBAPP_URL (strip trailing path)."""
    url = (os.getenv("WEBAPP_URL", "") or "").strip().rstrip("/")
    if not url.startswith("https://"):
        return None
    # WEBAPP_URL may already point at /webapp; reduce to scheme+host.
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def _cricket_webapp_url(match_id, chat_id):
    """Direct Mini App URL with match + chat in the query string."""
    host = _webapp_host()
    if not host:
        return None
    return f"{host}/cricket?match_id={match_id}&chat_id={chat_id}"


def _cricket_deep_link(match_id, chat_id):
    """Group deep link: startapp=cricket_<matchId>_<chatId>."""
    bot_username = (os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")
    miniapp_name = (os.getenv("MINIAPP_NAME", "") or "").strip()
    if not bot_username:
        return None
    param = f"cricket_{match_id}_{chat_id}"
    if miniapp_name:
        return f"https://t.me/{bot_username}/{miniapp_name}?startapp={param}"
    return f"https://t.me/{bot_username}?startapp={param}"


def play_match_url(match_id, chat_id=None):
    """Public group-style launch URL for opening/spectating a live match.

    Uses the cricket_<matchId>_<chatId> deep-link scheme when the originating
    chat id is known, so Telegram users land on the same live board as the
    original /wpm or /cm match.
    """
    return _launch_url(match_id, chat_id)


def _launch_url(match_id, chat_id=None):
    """Group-style deep link (kept for the scorecard/result broadcasts, which
    always post to a group chat). Uses the new cricket_ scheme when chat_id is
    known, else falls back to the match-only form."""
    if chat_id is not None:
        link = _cricket_deep_link(match_id, chat_id)
        if link:
            return link
    bot_username = (os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")
    miniapp_name = (os.getenv("MINIAPP_NAME", "") or "").strip()
    if not bot_username:
        return None
    if miniapp_name:
        return f"https://t.me/{bot_username}/{miniapp_name}?startapp=cricket_{match_id}"
    return f"https://t.me/{bot_username}?startapp=cricket_{match_id}"


def play_match_keyboard(match_id, chat_id=None, is_private=False, label=None):
    """'Play Match' button.

    • Private chat → Telegram Web App button → /cricket?match_id&chat_id
      (opens the Mini App in place; user id resolves from Telegram initData).
    • Group chat → t.me deep link with startapp=cricket_<matchId>_<chatId>.

    The Mini App detects each user's role (batsman / bowler / spectator) from
    the live state, so one button routes everyone correctly.
    """
    if is_private:
        wa_url = _cricket_webapp_url(match_id, chat_id if chat_id is not None else 0)
        if wa_url:
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(label or "🎮 Play Match",
                                     web_app=WebAppInfo(url=wa_url))
            ]])
        # Fall through to deep link if no host configured
    url = _launch_url(match_id, chat_id)
    if not url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label or "🎮 Play Match (Mini App)", url=url)
    ]])


# ════════════════════════════════════════════════════════════════════
# Shared coin-toss UI (heads/tails call → animated flip → result)
# ════════════════════════════════════════════════════════════════════

# Keep the flip short and snappy — a long animation (many frames + a long sleep
# per frame) means several sequential Telegram edit round-trips before the
# result lands, which players read as "the toss is stuck". Three quick frames is
# enough to feel like a coin flip while showing the winner fast.
COIN_TOSS_FRAMES = [
    "🪙 <b>TOSS</b>\n\n     ⬆️\n   ╱  🪙  ╲\n\n<i>The coin is in the air…</i>",
    "🪙 <b>TOSS</b>\n\n     🌀 🪙 🌀\n\n<i>Tumbling end over end…</i>",
    "🪙 <b>TOSS</b>\n\n          ⬇️\n        🪙\n\n<i>Coming down now!</i>",
]

# Seconds to hold each animation frame before editing to the next one.
COIN_TOSS_FRAME_DELAY = 0.25


def coin_call_keyboard(heads_cb, tails_cb, prompt_owner=None):
    """Heads/Tails call buttons. heads_cb/tails_cb are full callback_data."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🪙 Heads", callback_data=heads_cb),
        InlineKeyboardButton("🌑 Tails", callback_data=tails_cb),
    ]])


async def run_coin_toss(edit_fn, call_side):
    """Animate a coin toss and return (coin_side, won).

    edit_fn: async callable taking the HTML string to display each frame.
    call_side: 'heads' or 'tails' — what the calling captain chose.
    """
    for fr in COIN_TOSS_FRAMES:
        try:
            await edit_fn(fr)
        except Exception:
            pass
        await asyncio.sleep(COIN_TOSS_FRAME_DELAY)
    coin = random.choice(["heads", "tails"])
    return coin, (coin == call_side)


async def send_match_ready_message(context, chat_id, match, bat_team, bowl_team,
                                   bat_mention, bowl_mention, rules_note=None,
                                   toss_note=None):
    """Post the 'Match Ready' card with all details + the Play Match button."""
    # A private chat with the bot uses a positive user-id chat_id; groups are
    # negative. Web App buttons only work in private chats, so pick the right
    # button type for the chat we're posting into.
    is_private = isinstance(chat_id, int) and chat_id > 0
    kb = play_match_keyboard(match.id, chat_id=chat_id, is_private=is_private)
    text = (
        "🏏 <b>MATCH READY!</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏟️ <b>Venue:</b> {match.stadium or 'Neutral'}\n"
        f"🌤️ <b>Pitch:</b> {match.pitch_type or 'Balanced'}\n"
        f"⏱️ <b>Overs:</b> {match.overs}\n"
        + (f"🪙 <b>Toss:</b> {toss_note}\n" if toss_note else "")
        + (f"🎯 <b>Rules:</b> {rules_note}\n" if rules_note else "") +
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏏 <b>Batting first:</b> {bat_team}\n   {bat_mention}\n"
        f"🎳 <b>Bowling first:</b> {bowl_team}\n   {bowl_mention}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Tap <b>Play Match</b> to open the game board.\n"
        "• Batting side → pick openers & play shots\n"
        "• Bowling side → pick bowler & deliver\n"
        "• Everyone else → spectate live 👁"
    )
    if kb is None:
        text += ("\n\n⚠️ <i>Mini App link unavailable — set BOT_USERNAME"
                 " (and MINIAPP_NAME) to enable.</i>")
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML",
                                       reply_markup=kb,
                                       disable_web_page_preview=True)
    except Exception:
        logger.exception("send_match_ready_message failed")


def build_live_scorecard_text(state, waiting_for_mention=None):
    """Build the improved LIVE SCORECARD broadcast text for the chat."""
    bat_team = state.get("bat_team_name", "Batting")
    runs = state.get("total_runs", 0)
    wkts = state.get("total_wickets", 0)
    over = max(0, state.get("current_over", 1) - 1)
    ball = state.get("current_ball", 0)
    overs_limit = state.get("overs", 0)

    order = state.get("batting_order", [])
    si = state.get("striker_idx", 0)
    nsi = state.get("non_striker_idx", 1)
    bat_stats = state.get("bat_stats", {})

    def _bat_line(idx, on_strike):
        if idx is None or idx < 0 or idx >= len(order):
            return None
        p = order[idx]
        st = bat_stats.get(p["roster_id"], {})
        arrow = "👉 " if on_strike else "• "
        return f"{arrow}{p['name']} : {st.get('runs', 0)} ({st.get('balls', 0)}b)"

    bowler = state.get("current_bowler") or {}
    bws = state.get("bowl_stats", {}).get(bowler.get("roster_id"), {}) if bowler else {}
    b_overs_done = bws.get("overs_done", 0)
    b_this = bws.get("this_over_balls", 0)
    b_ov = f"{b_overs_done}.{b_this}" if b_this else f"{b_overs_done}"

    lines = [
        "🏏 <b>LIVE SCORECARD</b>",
        "══════════════════════════════",
        f"• Batting: {bat_team}",
        f"• Score: {runs}/{wkts} in {over}.{ball}/{overs_limit} overs",
        "══════════════════════════════",
        "🪓 <b>Batsmen:</b>",
    ]
    s_line = _bat_line(si, True)
    n_line = _bat_line(nsi, False)
    if s_line:
        lines.append(s_line)
    if n_line:
        lines.append(n_line)
    lines.append("")
    lines.append("🎳 <b>Bowler:</b>")
    if bowler:
        lines.append(f"• {bowler.get('name', '?')} : "
                     f"{bws.get('wickets', 0)}-{bws.get('runs', 0)} ({b_ov} ov)")
    else:
        lines.append("• —")
    if state.get("innings") == 2 and state.get("target"):
        from services.match_engine import chase_requirements
        chase = chase_requirements(state)
        if chase:
            lines.append(
                f"🎯 Need {chase['runs_required']} runs from "
                f"{chase['balls_remaining']} balls"
            )
    lines.append("══════════════════════════════")
    if waiting_for_mention:
        lines.append(f"🎳 <b>Waiting for {waiting_for_mention} to deliver…</b>")

    return "\n".join(lines)


async def broadcast_scorecard(context, match_id, state, waiting_for_mention=None):
    """Send the live scorecard + Play Match button to the match chat."""
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    text = build_live_scorecard_text(state, waiting_for_mention)
    kb = play_match_keyboard(match_id)
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML",
                                       reply_markup=kb,
                                       disable_web_page_preview=True)
    except Exception:
        logger.exception("broadcast_scorecard failed")
