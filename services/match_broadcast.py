"""Chat-side helpers for Mini-App matches: the post-toss launch message,
the live scorecard broadcast, and per-user "Play Match" buttons.

The launch message lets each participant jump into the right Mini App screen:
the batting side → batting board, the bowling side → bowling board, and
everyone else → spectate. We use a startapp deep link (works in groups) of
the form  t.me/<bot>/<app>?startapp=lm_<match_id>  — the Mini App reads the
start_param and routes to the live-match screen for that id.
"""

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _launch_url(match_id):
    """Build the t.me deep link that opens the live match in the Mini App
    DIRECTLY (no DM, no /start). Both forms use `startapp`, which opens the
    Mini App in place — in groups too. The Mini App reads start_param and
    routes straight to the match."""
    bot_username = (os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")
    miniapp_name = (os.getenv("MINIAPP_NAME", "") or "").strip()
    if not bot_username:
        return None
    if miniapp_name:
        return f"https://t.me/{bot_username}/{miniapp_name}?startapp=lm_{match_id}"
    return f"https://t.me/{bot_username}?startapp=lm_{match_id}"


def play_match_keyboard(match_id):
    """Single 'Play Match' button that deep-links into the Mini App live match.

    One button for all — the Mini App itself detects the user's role
    (batsman / bowler / spectator) from the live state, so a single link
    routes everyone correctly.
    """
    url = _launch_url(match_id)
    if not url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play Match (Mini App)", url=url)
    ]])


async def send_match_ready_message(context, chat_id, match, bat_team, bowl_team,
                                   bat_mention, bowl_mention):
    """Post the 'Match Ready' card with all details + the Play Match button."""
    kb = play_match_keyboard(match.id)
    text = (
        "🏏 <b>MATCH READY!</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏟️ <b>Venue:</b> {match.stadium or 'Neutral'}\n"
        f"🌤️ <b>Pitch:</b> {match.pitch_type or 'Balanced'}\n"
        f"⏱️ <b>Overs:</b> {match.overs}\n"
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


def _stat_lookup(stats, roster_id):
    if not isinstance(stats, dict):
        return {}
    return stats.get(roster_id) or stats.get(str(roster_id)) or {}


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
        st = _stat_lookup(bat_stats, p["roster_id"])
        arrow = "👉 " if on_strike else "• "
        return f"{arrow}{p['name']} : {st.get('runs', 0)} ({st.get('balls', 0)}b)"

    bowler = state.get("current_bowler") or {}
    bws = _stat_lookup(state.get("bowl_stats", {}), bowler.get("roster_id")) if bowler else {}
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
    last_ball = state.get("last_ball") or {}
    if last_ball:
        lines.append("")
        lines.append(f"💬 <i>{last_ball.get('commentary') or last_ball.get('rtxt', '')}</i>")
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
