"""Trade offers keep both cards, fees and confirmation status visible."""

import html
import time

from config import TRADE_FEE_PERCENT
from services import rich_message as R
from services.cooldown_service import format_remaining


def player_snapshot(player):
    return dict(name=str(player.name), rating=player.rating,
                version=str(getattr(player, "version", "") or ""))


def _card(player):
    version = player.get("version")
    return f"{player['name']}" + (f" · {version}" if version else "") + f" · {player['rating']} OVR"


def _name(user):
    return f"@{user.username}" if user.username else str(user.first_name or "Captain")


def _identity(user):
    label = _name(user)
    return R.mention(label, user.telegram_id), f'<a href="tg://user?id={int(user.telegram_id)}">{html.escape(label)}</a>'


def confirmation(state, user1, user2):
    players = state.get("offer_players") or [
        dict(name=state.get("user1_selected_player_name", "Player"),
             rating=state.get("selected_player_ovr", "—")),
        dict(name=state.get("user2_selected_player_name", "Player"),
             rating=state.get("user2_selected_player_ovr", "—"))]
    fee = int(state.get("trade_fee") or 0)
    confirmed = set(state.get("confirmations") or [])
    blocks, text = [R.heading("🤝 CONFIRM TRADE", 2)], ["🤝 <b>CONFIRM TRADE</b>"]
    for user, give, receive in ((user1, players[0], players[1]), (user2, players[1], players[0])):
        who, who_html = _identity(user)
        status = "✅ Confirmed" if user.id in confirmed else "⏳ Awaiting confirmation"
        rows = [("Gives", _card(give)), ("Receives", _card(receive)),
                ("Fee", f"{fee:,} coins" if fee else "No fee"), ("Status", status)]
        blocks.extend([R.heading(who), R.table([[R.cell(label), R.cell(value)]
                                              for label, value in rows], compact=True)])
        text.extend(["", f"<b>{who_html}</b>"] + [f"{label}: {html.escape(value)}" for label, value in rows])
    progress = f"{len(confirmed & {user1.id, user2.id})}/2 captains confirmed"
    blocks.append(R.paragraph(R.bold(progress)))
    text.extend(["", progress])
    if fee:
        note = f"Fee: {TRADE_FEE_PERCENT}% of the card’s buy value, charged to each captain only when the trade completes."
        blocks.append(R.paragraph(note))
        text.append(note)
    expiry = state.get("expires_at")
    if expiry:
        label = f"Expires in {format_remaining(max(0, int(expiry - time.time())))}"
        blocks.append(R.paragraph(R.date_time(label, expiry)))
        text.append(label)
    note = "Both captains must confirm. No cards or coins move until the trade completes."
    blocks.append(R.footer(note))
    text.append(note)
    return blocks, "\n".join(text)


def completed(result):
    fee = int(result.get("fee") or 0)
    blocks = [R.heading("✅ TRADE COMPLETED", 2)]
    text = ["✅ <b>TRADE COMPLETED</b>"]
    for user, player in ((result["initiator"], result["recv_player"]),
                         (result["receiver"], result["init_player"])):
        who, who_html = _identity(user)
        rows = [("Received", _card(player_snapshot(player))), ("Fee paid", f"{fee:,} coins")]
        if fee:
            rows.append(("Coin balance", f"{user.total_coins:,}"))
        blocks.extend([R.heading(who), R.table([[R.cell(label), R.cell(value)] for label, value in rows], compact=True)])
        text.extend(["", f"<b>{who_html}</b>"] + [f"{label}: {html.escape(value)}" for label, value in rows])
    traits = result.get("traits_returned")
    if traits:
        note = f"{traits} trait(s) returned to their owners’ inventories. Traits stay with the owner; use /traitapply to equip them again."
        blocks.append(R.paragraph(note))
        text.extend(["", note])
    return blocks, "\n".join(text)
