"""/score21 <bet> — Score 21 (Blackjack vs dealer).
Win = 2× bet. Natural blackjack = 2.5× bet. Push = refund.
"""
import logging
import random
from dataclasses import dataclass, field
from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity

logger = logging.getLogger(__name__)
MIN_BET, MAX_BET = 10, 10000

SUITS = ["♥", "♦", "♣", "♠"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]

# user_id -> GameState
GAMES: dict = {}


@dataclass
class GameState:
    user_id: int
    bet: int
    deck: List[dict] = field(default_factory=list)
    player_hand: List[dict] = field(default_factory=list)
    dealer_hand: List[dict] = field(default_factory=list)


def _new_deck():
    deck = [{"r": r, "s": s} for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def _score(hand):
    total, aces = 0, 0
    for c in hand:
        if c["r"] == "A":
            total += 11; aces += 1
        elif c["r"] in ("J", "Q", "K"):
            total += 10
        else:
            total += int(c["r"])
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total


def _fmt(hand, hide_first=False):
    cards = []
    for i, c in enumerate(hand):
        if hide_first and i == 0:
            cards.append("🎴")
        else:
            cards.append(f"{c['r']}{c['s']}")
    return "  ".join(cards)


def _is_blackjack(hand):
    return len(hand) == 2 and _score(hand) == 21


def _action_kb(uid, bet):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👊 Hit", callback_data=f"s21_hit_{uid}_{bet}"),
        InlineKeyboardButton("✋ Stand", callback_data=f"s21_stand_{uid}_{bet}"),
        InlineKeyboardButton("×2 Double", callback_data=f"s21_double_{uid}_{bet}"),
    ]])


def _board_text(gs: GameState, hide_dealer=True):
    p_score = _score(gs.player_hand)
    d_score = _score(gs.dealer_hand) if not hide_dealer else "?"
    return (
        f"🃏 <b>Score 21</b>  —  Bet: <b>{gs.bet:,}</b> coins\n\n"
        f"👤 Your hand ({p_score}):  {_fmt(gs.player_hand)}\n"
        f"🤖 Dealer ({d_score}):  {_fmt(gs.dealer_hand, hide_first=hide_dealer)}"
    )


def _usage():
    return (
        "🃏 <b>Score 21</b> — Blackjack\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Usage: <code>/score21 &lt;bet&gt;</code>\n"
        f"Bet: {MIN_BET}–{MAX_BET} coins\n\n"
        "• Win → 2× bet\n"
        "• Blackjack (21 on first 2) → 2.5× bet\n"
        "• Push (tie) → refund"
    )


async def score21_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in GAMES:
        await update.message.reply_text("❌ Finish your current game first.")
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

    deck = _new_deck()
    p_hand = [deck.pop(), deck.pop()]
    d_hand = [deck.pop(), deck.pop()]
    gs = GameState(uid, bet, deck, p_hand, d_hand)
    GAMES[uid] = gs

    # Natural blackjack check
    if _is_blackjack(p_hand):
        payout = int(bet * 2.5)
        net = payout - bet
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == uid).first()
            if user:
                user.total_coins += payout
                log_activity(session, user.id, "score21", f"blackjack bet:{bet} net:+{net}")
                session.commit()
            bal = user.total_coins if user else 0
        finally:
            session.close()
        GAMES.pop(uid, None)
        await update.message.reply_text(
            f"🃏 <b>Score 21</b>\n\n"
            f"👤 Your hand (21):  {_fmt(p_hand)}\n"
            f"🤖 Dealer:  {_fmt(d_hand)}\n\n"
            f"🎊 <b>BLACKJACK!</b> Payout 2.5×  (+{net:,} coins)\n"
            f"💵 Balance: <b>{bal:,}</b>",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        _board_text(gs),
        parse_mode="HTML",
        reply_markup=_action_kb(uid, bet),
    )


async def score21_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    action, uid, bet = parts[1], int(parts[2]), int(parts[3])

    if query.from_user.id != uid:
        await query.answer("Not your game.", show_alert=True)
        return
    await query.answer()

    gs = GAMES.get(uid)
    if not gs:
        await query.edit_message_text("❌ No active game found.")
        return

    if action == "hit":
        gs.player_hand.append(gs.deck.pop())
        p_score = _score(gs.player_hand)
        if p_score > 21:
            GAMES.pop(uid, None)
            session = get_session()
            try:
                user = session.query(User).filter(User.telegram_id == uid).first()
                if user:
                    log_activity(session, user.id, "score21", f"bust bet:{bet}")
                    session.commit()
                bal = user.total_coins if user else 0
            finally:
                session.close()
            await query.edit_message_text(
                f"🃏 <b>Score 21</b>\n\n"
                f"👤 Your hand ({p_score}):  {_fmt(gs.player_hand)}\n\n"
                f"💥 <b>BUST!</b> You went over 21.\n💸 Lost: <b>{bet:,}</b> coins\n"
                f"💵 Balance: <b>{bal:,}</b>",
                parse_mode="HTML",
            )
            return
        await query.edit_message_text(
            _board_text(gs),
            parse_mode="HTML",
            reply_markup=_action_kb(uid, bet),
        )
        return

    if action == "double":
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == uid).first()
            if not user or user.total_coins < bet:
                await query.answer("Not enough coins to double.", show_alert=True)
                return
            user.total_coins -= bet
            session.commit()
        finally:
            session.close()
        gs.bet *= 2
        bet = gs.bet
        gs.player_hand.append(gs.deck.pop())
        # Fall through to stand logic

    # STAND or bust-after-double → dealer plays
    if action in ("stand", "double"):
        while _score(gs.dealer_hand) < 17:
            gs.dealer_hand.append(gs.deck.pop())

        p = _score(gs.player_hand)
        d = _score(gs.dealer_hand)

        if p > 21:
            outcome, net = "BUST", -gs.bet
        elif d > 21 or p > d:
            outcome, net = "WIN", gs.bet
        elif p == d:
            outcome, net = "PUSH", 0
        else:
            outcome, net = "LOSS", -gs.bet

        GAMES.pop(uid, None)
        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == uid).first()
            if user:
                if net >= 0:
                    user.total_coins += gs.bet + net  # refund bet + profit
                log_activity(session, user.id, "score21", f"{outcome} bet:{gs.bet} net:{net:+d}")
                session.commit()
            bal = user.total_coins if user else 0
        finally:
            session.close()

        icons = {"WIN": "🎉", "BUST": "💥", "PUSH": "🤝", "LOSS": "💔"}
        await query.edit_message_text(
            f"🃏 <b>Score 21</b>\n\n"
            f"👤 Your hand ({p}):  {_fmt(gs.player_hand)}\n"
            f"🤖 Dealer ({d}):  {_fmt(gs.dealer_hand)}\n\n"
            f"{icons[outcome]} <b>{outcome}!</b>  Net: {net:+,} coins\n"
            f"💵 Balance: <b>{bal:,}</b>",
            parse_mode="HTML",
        )
