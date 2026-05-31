"""/statduel <bet> — Stat Duel: guess if the next player's stat is Higher or Lower.
Dynamic multiplier: easy guesses earn less, hard guesses earn more.
"""
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity

logger = logging.getLogger(__name__)
MIN_BET, MAX_BET = 50, 20000

# Load hilo stats once at startup
_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "hiloStats.json")
try:
    with open(_DATA_PATH) as f:
        _STATS: List[dict] = json.load(f)
except Exception:
    _STATS = []
    logger.warning("statduel: could not load hiloStats.json")

_STAT_KEYS = [k for k in (_STATS[0].keys() if _STATS else []) if k != "name"]

# user_id -> GameState
GAMES: dict = {}


@dataclass
class GameState:
    user_id: int
    bet: int
    multiplier: float = 1.0
    current: dict = field(default_factory=dict)
    next_player: dict = field(default_factory=dict)
    constraint: str = ""
    seen: List[str] = field(default_factory=list)


def _pick_player(exclude: List[str] = None):
    pool = [p for p in _STATS if (exclude is None or p["name"] not in exclude)]
    if not pool:
        pool = _STATS
    return random.choice(pool)


def _pick_constraint(player: dict) -> str:
    keys = [k for k in player if k != "name"]
    return random.choice(keys)


def _calc_increment(base_val, constraint: str, guess: str) -> float:
    total = len(_STATS)
    if guess == "higher":
        win_count = sum(1 for p in _STATS if p.get(constraint, 0) > base_val)
    else:
        win_count = sum(1 for p in _STATS if p.get(constraint, 0) < base_val)
    prob = win_count / total if total else 0.5
    diff = 1.0 - prob
    return round(0.01 + diff * 0.39, 3)


def _board_text(gs: GameState) -> str:
    base = gs.current
    rival = gs.next_player
    stat = gs.constraint
    bv = base.get(stat, "N/A")
    return (
        f"📊 <b>Stat Duel</b>  ×{gs.multiplier:.2f}  |  Bet: <b>{gs.bet:,}</b>\n\n"
        f"🏏 <b>{base['name']}</b>\n"
        f"   {stat}: <b>{bv:,}</b>\n\n"
        f"🆚\n\n"
        f"❓ <b>{rival['name']}</b>\n"
        f"   {stat}: ???\n\n"
        "Is their <b>" + stat + "</b> Higher or Lower?"
    )


def _action_kb(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📈 Higher", callback_data=f"sd_higher_{uid}"),
        InlineKeyboardButton("📉 Lower", callback_data=f"sd_lower_{uid}"),
    ], [
        InlineKeyboardButton("💰 Cash Out", callback_data=f"sd_cash_{uid}"),
    ]])


def _usage():
    return (
        "📊 <b>Stat Duel</b> — Cricket Stats Showdown\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Usage: <code>/statduel &lt;bet&gt;</code>\n"
        f"Bet: {MIN_BET}–{MAX_BET} coins\n\n"
        "Compare cricket player stats round by round.\n"
        "Correct guess = multiplier increases (harder = bigger boost).\n"
        "Wrong guess = lose your bet.\n"
        "Cash out any time to collect bet × multiplier."
    )


async def statduel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _STATS:
        await update.message.reply_text("❌ Stat data unavailable.")
        return
    if uid in GAMES:
        await update.message.reply_text("❌ You already have an active Stat Duel. Use the buttons or /endstatduel to quit.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(_usage(), parse_mode="HTML")
        return
    try:
        bet = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid bet.\n\n" + _usage(), parse_mode="HTML")
        return
    if not MIN_BET <= bet <= MAX_BET:
        await update.message.reply_text(f"❌ Bet must be {MIN_BET}–{MAX_BET} coins.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == uid).first()
        if not user:
            await update.message.reply_text("❌ Use /debut first.")
            return
        if user.total_coins < bet:
            await update.message.reply_text(
                f"❌ Not enough coins. Balance: <b>{user.total_coins:,}</b>.", parse_mode="HTML")
            return
        user.total_coins -= bet
        session.commit()
    finally:
        session.close()

    p1 = _pick_player()
    p2 = _pick_player([p1["name"]])
    constraint = _pick_constraint(p1)
    gs = GameState(uid, bet, 1.0, p1, p2, constraint, [p1["name"], p2["name"]])
    GAMES[uid] = gs

    await update.message.reply_text(_board_text(gs), parse_mode="HTML", reply_markup=_action_kb(uid))


async def statduel_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    gs = GAMES.pop(uid, None)
    if not gs:
        await update.message.reply_text("❌ No active Stat Duel.")
        return
    # Refund bet (they quit voluntarily — refund at 0.9× like cash-out penalty)
    refund = int(gs.bet * 0.9)
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == uid).first()
        if user:
            user.total_coins += refund
            session.commit()
        bal = user.total_coins if user else 0
    finally:
        session.close()
    await update.message.reply_text(
        f"📊 Stat Duel ended. Refunded <b>{refund:,}</b> coins (90%).\n"
        f"💵 Balance: <b>{bal:,}</b>",
        parse_mode="HTML",
    )


async def statduel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    action, uid = parts[1], int(parts[2])

    if query.from_user.id != uid:
        await query.answer("Not your game.", show_alert=True)
        return
    await query.answer()

    gs = GAMES.get(uid)
    if not gs:
        await query.edit_message_text("❌ No active Stat Duel found.")
        return

    # Cash out
    if action == "cash":
        payout = int(gs.bet * gs.multiplier)
        net = payout - gs.bet
        GAMES.pop(uid, None)
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == uid).first()
            if user:
                user.total_coins += payout
                log_activity(session, user.id, "statduel", f"cashout bet:{gs.bet} mult:{gs.multiplier:.2f} net:{net:+d}")
                session.commit()
            bal = user.total_coins if user else 0
        finally:
            session.close()
        await query.edit_message_text(
            f"💰 <b>Cashed Out!</b>  ×{gs.multiplier:.2f}\n"
            f"Payout: <b>{payout:,}</b> coins  (net {net:+,})\n"
            f"💵 Balance: <b>{bal:,}</b>",
            parse_mode="HTML",
        )
        return

    # Higher / Lower guess
    base_val = gs.current.get(gs.constraint, 0)
    rival_val = gs.next_player.get(gs.constraint, 0)
    correct = (action == "higher" and rival_val > base_val) or (action == "lower" and rival_val < base_val)

    reveal = (
        f"📊 Revealed: <b>{gs.next_player['name']}</b>  {gs.constraint}: <b>{rival_val:,}</b>\n"
        f"vs <b>{gs.current['name']}</b>  {gs.constraint}: <b>{base_val:,}</b>\n\n"
    )

    if not correct:
        GAMES.pop(uid, None)
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == uid).first()
            if user:
                log_activity(session, user.id, "statduel", f"loss bet:{gs.bet} mult:{gs.multiplier:.2f}")
                session.commit()
            bal = user.total_coins if user else 0
        finally:
            session.close()
        await query.edit_message_text(
            f"❌ <b>Wrong!</b>\n\n{reveal}"
            f"💸 Lost: <b>{gs.bet:,}</b> coins\n"
            f"💵 Balance: <b>{bal:,}</b>",
            parse_mode="HTML",
        )
        return

    # Correct — advance round
    increment = _calc_increment(base_val, gs.constraint, action)
    gs.multiplier = round(gs.multiplier + increment, 3)
    gs.current = gs.next_player
    gs.next_player = _pick_player(gs.seen)
    gs.seen.append(gs.next_player["name"])
    gs.constraint = _pick_constraint(gs.current)

    potential = int(gs.bet * gs.multiplier)
    await query.edit_message_text(
        f"✅ <b>Correct!</b>  +{increment:.3f}×  →  ×<b>{gs.multiplier:.2f}</b>\n\n"
        f"{reveal}"
        + _board_text(gs) + f"\n\nPotential payout: <b>{potential:,}</b> coins",
        parse_mode="HTML",
        reply_markup=_action_kb(uid),
    )
