"""Shared player card text builder — used by /claim, /buypl, /playerinfo."""

from config import get_buy_value, get_sell_value
from services.flags import get_flag


def format_player_card(player, acquired_date=None) -> str:
    """Return the standard player info text block."""
    buy_val = get_buy_value(player.rating)
    sell_val = get_sell_value(player.rating)
    flag = get_flag(player.country)

    acq = acquired_date.strftime("%d %b %Y") if acquired_date else "Not Owned"

    return (
        f"📛 <b>{player.name}</b>\n"
        f"⭐ Rating: {player.rating} OVR\n"
        f"📊 Batting Rating: {player.bat_rating}\n"
        f"📊 Bowling Rating: {player.bowl_rating}\n\n"
        f"👤 <b>Bio:</b>\n"
        f"🎯 Category: {player.category}\n"
        f"🏏 Bat Hand: {player.bat_hand}\n"
        f"🎳 Bowl Hand: {player.bowl_hand}\n"
        f"🌀 Bowl Style: {player.bowl_style}\n"
        f"🌍 Country: {player.country} {flag}\n"
        f"📋 Version: {player.version}\n\n"
        f"💰 Buy Value: {buy_val:,} 🪙\n"
        f"💸 Sell Value: {sell_val:,} 🪙\n"
        f"📅 Acquired: {acq}"
    )
