"""Shared player card text builder — used by /claim, /buypl, /playerinfo."""

from config import get_buy_value, get_sell_value
from services.flags import get_flag


def format_player_card(player, acquired_date=None, value_mode="both") -> str:
    """Return the standard player info text block.

    ``value_mode`` selects which coin value(s) to show:
      • "buy"  → Buy Value only   (used by /buypl)
      • "sell" → Sell Value only  (used by /playerinfo, /myroster)
      • "both" → Buy + Sell       (default — /claim, /gspin, /daily)

    The Bio block is wrapped in an expandable Telegram quote and each value /
    acquired line sits in its own quote, matching the card layout spec.
    """
    buy_val = get_buy_value(player.rating)
    sell_val = get_sell_value(player.rating)
    flag = get_flag(player.country)

    acq = acquired_date.strftime("%d %b %Y") if acquired_date else "Not Owned"

    header = (
        f"📛 <b>{player.name}</b> ⭐ Rating: {player.rating} OVR\n"
        f"📊 Bat/Bowl Rating: {player.bat_rating}/{player.bowl_rating}"
    )

    bio = (
        "<blockquote expandable>"
        "👤 <b>Bio:</b>\n"
        f"🎯 Category: {player.category}\n"
        f"🏏 Bat Hand: {player.bat_hand}\n"
        f"🎳 Bowl Hand: {player.bowl_hand}\n"
        f"🌀 Bowl Style: {player.bowl_style}\n"
        f"🌍 Country: {player.country} {flag}\n"
        f"📋 Version: {player.version}"
        "</blockquote>"
    )

    value_lines = []
    if value_mode in ("buy", "both"):
        value_lines.append(f"<blockquote>💰 Buy Value: {buy_val:,} 🪙</blockquote>")
        # Elite cards pay a gem rebate on purchase — say so before they buy,
        # not just in the receipt.
        from services.buy_bonus import teaser_line
        teaser = teaser_line(player.rating, buy_val, prefix="")
        if teaser:
            value_lines.append(f"<blockquote>{teaser}</blockquote>")
    if value_mode in ("sell", "both"):
        value_lines.append(f"<blockquote>💰 Sell Value: {sell_val:,} 🪙</blockquote>")
    value_lines.append(f"<blockquote>📅 Acquired: {acq}</blockquote>")

    return header + "\n\n" + bio + "\n" + "\n".join(value_lines)
