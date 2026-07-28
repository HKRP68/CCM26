"""/cmumysterybox — subscriber-only 3x3 Mystery Box reveal.

Subscribers open ONE box per cooldown window, at their tier's cadence (Bronze
every 15 days, Silver 8, Platinum 4, Diamond 2 — see
config.SUBSCRIPTION_TIERS["…"]["mysterybox_cooldown_days"]). Picking a box
generates weighted rewards (coins, gems,
quest points and one random OVR-banded player), reveals them in place, and locks
all nine buttons so the box cannot be re-opened until the cooldown resets.
"""

import html
import logging
import random
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserStats, UserRoster
from config import (
    MYSTERYBOX_COIN_BANDS, MYSTERYBOX_CURRENCY_BANDS, MYSTERYBOX_PLAYER_BANDS,
    MYSTERYBOX_REWARD_TYPE_BANDS, MAX_ROSTER, get_sell_value,
)
from services import subscription_service
from services.activity_service import log_activity
from services.cooldown_service import check_cooldown, format_remaining
from services.player_service import get_random_player_by_rating_range

logger = logging.getLogger(__name__)

GRID_SIZE = 9  # 3x3
PROMPT = "Select ONE Mystery Box to reveal your rewards! 🎁"

# Guards against a rapid double-tap on two different boxes of the SAME grid
# before the reveal edit lands. Keyed by (chat_id, message_id). Check-and-add is
# atomic within the event loop (no await between them).
_OPENING: set[tuple[int, int]] = set()


# ── Weighted reward logic (ported 1:1 from the product spec) ────────

def _get_random_int(low: int, high: int) -> int:
    """Inclusive, matching the spec's getRandomInt(min, max)."""
    return random.randint(low, high)


def _weighted_from_bands(bands) -> int:
    """Roll 0..100 and pick getRandomInt(low, high) from the matching band."""
    roll = random.random() * 100.0
    for ceiling, low, high in bands:
        if roll <= ceiling:
            return _get_random_int(low, high)
    # Fallback (last band always has ceiling 100.0).
    _, low, high = bands[-1]
    return _get_random_int(low, high)


def get_weighted_currency(kind: str) -> int:
    """Roll a weighted coin amount, or a gems/questPoints amount otherwise."""
    if kind == "coins":
        return _weighted_from_bands(MYSTERYBOX_COIN_BANDS)
    return _weighted_from_bands(MYSTERYBOX_CURRENCY_BANDS)  # gems / questPoints


def get_weighted_player_band() -> tuple[int, int, str]:
    """Return (ovr_low, ovr_high, label) for the rolled rarity band."""
    roll = random.random() * 100.0
    for ceiling, low, high, label in MYSTERYBOX_PLAYER_BANDS:
        if roll <= ceiling:
            return low, high, label
    _, low, high, label = MYSTERYBOX_PLAYER_BANDS[-1]
    return low, high, label


def get_weighted_reward_type() -> str:
    """Roll which SINGLE reward type this box grants: coins/gems/questPoints/player."""
    roll = random.random() * 100.0
    for ceiling, rtype in MYSTERYBOX_REWARD_TYPE_BANDS:
        if roll <= ceiling:
            return rtype
    return MYSTERYBOX_REWARD_TYPE_BANDS[-1][1]


# ── Keyboards ───────────────────────────────────────────────────────

def _fresh_grid() -> InlineKeyboardMarkup:
    """Build the initial 3x3 grid of openable 🎁 buttons."""
    rows = []
    for r in range(3):
        row = [InlineKeyboardButton("🎁", callback_data=f"cmb_open_{r * 3 + c}")
               for c in range(3)]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _locked_grid(opened_index: int) -> InlineKeyboardMarkup:
    """All buttons disabled (callback_data 'cmb_locked'); the opened cell shows
    🎉, the rest 🔒."""
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            emoji = "🎉" if idx == opened_index else "🔒"
            row.append(InlineKeyboardButton(emoji, callback_data="cmb_locked"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ── Command ─────────────────────────────────────────────────────────

async def cmumysterybox_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the Mystery Box grid to a subscriber whose cooldown is ready."""
    uid = update.effective_user.id
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == uid).first()
        if not user:
            await update.message.reply_text("❌ Use /debut first.")
            return
        if not subscription_service.is_subscribed(user):
            await update.message.reply_text(
                subscription_service.premium_required_message("The Mystery Box"),
                parse_mode="HTML")
            return

        stats = _ensure_stats(session, user.id)
        cooldown = subscription_service.mysterybox_cooldown_seconds(user) or 0
        ready, remaining = check_cooldown(stats, "last_mysterybox", cooldown)
        if not ready:
            await update.message.reply_text(
                f"⏳ Your next Mystery Box unlocks in "
                f"<b>{format_remaining(remaining)}</b>.",
                parse_mode="HTML")
            return
    finally:
        session.close()

    await update.message.reply_text(PROMPT, reply_markup=_fresh_grid())


# ── Callback ────────────────────────────────────────────────────────

async def mysterybox_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route a box tap: 'already opened' alert for locked cells, else reveal."""
    query = update.callback_query
    data = query.data or ""

    if data == "cmb_locked":
        await query.answer("You have already opened your box!", show_alert=True)
        return

    try:
        opened_index = int(data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer()
        return

    msg = query.message
    key = (getattr(msg, "chat_id", 0) or 0, getattr(msg, "message_id", 0) or 0)
    if key in _OPENING:
        await query.answer("Opening…")
        return
    _OPENING.add(key)
    try:
        await query.answer()
        await _reveal(query, opened_index)
    finally:
        _OPENING.discard(key)


async def _reveal(query, opened_index: int):
    """Grant weighted rewards for the tapped box, edit the reveal in place, and
    lock the grid. No-op (with an alert) if the box was already opened."""
    uid = query.from_user.id
    session = get_session()
    try:
        # Lock the user row so two concurrent reveals (e.g. horizontally scaled
        # workers) serialize: the second waits, then sees last_mysterybox already
        # stamped below and is refused before it can credit rewards twice.
        # (with_for_update is a no-op on SQLite and a real row lock on Postgres.)
        user = (session.query(User).filter(User.telegram_id == uid)
                .with_for_update().first())
        if not user:
            await query.answer("❌ Use /debut first.", show_alert=True)
            return
        if not subscription_service.is_subscribed(user):
            await query.answer("Your subscription has expired.", show_alert=True)
            return

        stats = _ensure_stats(session, user.id)
        cooldown = subscription_service.mysterybox_cooldown_seconds(user) or 0
        # Idempotency + recurring cooldown in one: once a box is opened,
        # last_mysterybox is recent so a second reveal is refused.
        ready, _ = check_cooldown(stats, "last_mysterybox", cooldown)
        if not ready:
            await query.answer("You have already opened your box!", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=_locked_grid(opened_index))
            except Exception:
                pass
            return

        # Each box grants exactly ONE reward type.
        reward_type = get_weighted_reward_type()
        coins = gems = qp = 0
        reward_line = ""
        log_detail = ""

        if reward_type == "coins":
            coins = get_weighted_currency("coins")
            user.total_coins = (user.total_coins or 0) + coins
            reward_line = f"💰 You won <b>{coins:,}</b> Coins!"
            log_detail = f"+{coins} coins"
        elif reward_type == "gems":
            gems = get_weighted_currency("gems")
            user.total_gems = (user.total_gems or 0) + gems
            reward_line = f"💎 You won <b>{gems}</b> Gems!"
            log_detail = f"+{gems} gems"
        elif reward_type == "questPoints":
            qp = get_weighted_currency("questPoints")
            user.quest_points = (user.quest_points or 0) + qp
            reward_line = f"🎯 You won <b>{qp}</b> Quest Points!"
            log_detail = f"+{qp} QP"
        else:  # player
            low, high, band_label = get_weighted_player_band()
            player = get_random_player_by_rating_range(session, low, high)
            reward_line, extra_coins = _grant_player(session, user, player)
            coins += extra_coins  # squad-full fallback credits coins
            log_detail = (f"player {player.name if player else 'none'} "
                          f"({band_label})")

        stats.last_mysterybox = datetime.utcnow()

        log_activity(session, user.id, "cmumysterybox",
                     f"Mystery Box: {log_detail}",
                     coins_change=coins, gems_change=gems)
        try:
            from services.quest_service import safe_track
            safe_track(session, user.id, "cmumysterybox", 1)
        except Exception:
            pass
        session.commit()

        text = (
            "🎉 <b>Mystery Box Opened!</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"{reward_line}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"💵 Balance: <b>{user.total_coins:,}</b> coins"
        )
    finally:
        session.close()

    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=_locked_grid(opened_index))


def _grant_player(session, user, player):
    """Add the player to the roster, or convert to coins if the squad is full.

    Returns (display_line, extra_coins_credited)."""
    if player is None:
        return ("🃏 Player: <i>none available</i>", 0)
    name = html.escape(player.name)  # names are rendered with parse_mode=HTML
    if (user.roster_count or 0) < MAX_ROSTER:
        entry = UserRoster(user_id=user.id, player_id=player.id,
                           order_position=(user.roster_count or 0) + 1,
                           acquired_date=datetime.utcnow())
        session.add(entry)
        user.roster_count = (user.roster_count or 0) + 1
        return (f"🃏 <b>{name}</b> — {player.rating} OVR added to squad", 0)
    # Squad full → convert to sell value.
    sell_val = get_sell_value(player.rating)
    user.total_coins = (user.total_coins or 0) + sell_val
    return (f"🃏 <b>{name}</b> — {player.rating} OVR (squad full → "
            f"+{sell_val:,} coins)", sell_val)


def _ensure_stats(session, user_id: int) -> UserStats:
    """Return the user's UserStats row, creating it if missing."""
    stats = session.query(UserStats).filter(UserStats.user_id == user_id).first()
    if stats is None:
        stats = UserStats(user_id=user_id)
        session.add(stats)
        session.flush()
    return stats
