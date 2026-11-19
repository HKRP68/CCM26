"""Telegram board and auction clock for group RCPL."""

import asyncio
from datetime import datetime, timezone
import logging
import time

from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler

from handlers import auction_league as solo
from services import auction_league_service as AL
from services import rcpl_multiplayer_auction as auction
from services import rcpl_multiplayer_service as MP

logger = logging.getLogger(__name__)


def squad_buttons(room):
    return [(solo._short(room.state, name), f"rcpl:a:{room.id}:squad:{index}")
            for index, name in enumerate(room.state["team_order"])]


def _navigation(room):
    if room.state["phase"] == AL.PHASE_AUCTION:
        return [("Auction board", f"rcpl:a:{room.id}:board")]
    return [("Season", f"rcpl:s:{room.id}:home")]


def _team_picker(room):
    buttons = squad_buttons(room)
    return [buttons[i:i + 4] for i in range(0, len(buttons), 4)] + [_navigation(room)]


def _board(room):
    state, rid = room.state, room.id
    last = state.get("last_lot")
    recent = f"\n{AL.lot_result_line({**state, 'user_team': None}, last)}\n" if last else ""
    if state["phase"] != AL.PHASE_AUCTION:
        if state["phase"] == AL.PHASE_ABANDONED:
            return "This RCPL room has closed. Send /rcpl to open a new room.", []
        return ("🏏 <b>RCPL auction complete</b>" + recent +
                "\nThe squads are ready. Open the season to see fixtures and standings.",
                [[("Season", f"rcpl:s:{rid}:home"), ("Squads", f"rcpl:a:{rid}:teams")]])

    lot = state["lot"]
    player = AL.card(state, lot["pid"])
    leader = lot["leader"]
    if leader:
        manager = state["managers"].get(leader)
        owner = manager["name"] if manager else "AI"
        high_bid = f"<b>{solo._esc(leader)}</b> ({solo._esc(owner)}) · {AL.money(lot['price'])}"
    else:
        high_bid = "Awaiting opening bid"
    deadline = datetime.fromtimestamp(lot["deadline"], timezone.utc).strftime("%H:%M:%S UTC")
    price = AL.next_price(lot)
    seq = lot["seq"]
    text = (f"🔨 <b>{solo._esc(state['league']['name'])} · RCPL auction</b>\n"
            f"Lot {seq} · {solo._esc(lot['set_name'])}\n{recent}\n"
            f"{solo._card_line(player)}\n{high_bid}\n"
            f"Closes at {deadline}. A new bid resets the {auction.LOT_SECONDS}s clock.")
    rows = [[(f"Bid {AL.money(price)}", f"rcpl:a:{rid}:bid:{seq}:{price}")],
            [("Squads", f"rcpl:a:{rid}:teams"), ("Purses", f"rcpl:a:{rid}:purses")],
            [("Finish auction", f"rcpl:a:{rid}:finish:{seq}:{price}"),
             ("Close room", f"rcpl:close:{rid}")]]
    return text, rows


async def _publish(context, room, *, repost=False):
    locks = context.bot_data.setdefault("rcpl_board_locks", {})
    lock = locks.setdefault(room.chat_id, asyncio.Lock())
    async with lock:
        current = await asyncio.to_thread(MP.get_room, room.chat_id, room.id)
        if not current:
            return
        text, rows = _board(current)
        message_id = current.board_message_id
        if message_id and not repost:
            try:
                await context.bot.edit_message_text(text, chat_id=room.chat_id, message_id=message_id,
                                                    parse_mode="HTML", reply_markup=solo._kb(rows))
                return
            except BadRequest as exc:
                reason = str(exc).lower()
                if "not modified" in reason:
                    return
                if not any(part in reason for part in ("not found", "can't be edited", "cannot be edited")):
                    raise
        message = await solo._send(context, room.chat_id, text, rows)
        if not message:
            return
        saved = await asyncio.to_thread(MP.set_board_message, room.chat_id, room.id,
                                        message.message_id, expected_old=message_id)
        retired = message_id if saved else message.message_id
        if retired and retired != (message.message_id if saved else message_id):
            try:
                await context.bot.edit_message_reply_markup(chat_id=room.chat_id, message_id=retired,
                                                           reply_markup=None)
            except TelegramError:
                logger.debug("Could not clear the previous RCPL board", exc_info=True)


async def show_auction(update, context, room):
    await _publish(context, room, repost=update.callback_query is None)


async def auction_callback(update, context):
    q = update.callback_query
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup") or q.from_user.is_bot:
        await q.answer("Open the board in the group hosting the auction.", show_alert=True)
        return
    try:
        parts = q.data.split(":")
        rid, action = int(parts[2]), parts[3]
        room = await asyncio.to_thread(MP.get_room, chat.id, rid)
        if not room or room.state["phase"] == AL.PHASE_ABANDONED:
            raise AL.AuctionLeagueError("Open the current room with /rcpl.")
        state = room.state
        if action in ("teams", "squad", "purses"):
            if action == "squad":
                index = int(parts[4])
                if not 0 <= index < len(state["team_order"]):
                    raise ValueError
                team = state["team_order"][index]
                text = solo._squad_text(state, team)
            elif action == "purses":
                lines = ["💰 <b>RCPL purses</b>"]
                for team in state["team_order"]:
                    manager = state["managers"].get(team)
                    label = manager["name"] if manager else "AI"
                    slot = state["teams"][team]
                    lines.append(f"{solo._esc(team)} ({solo._esc(label)}) · {AL.money(slot['purse'])}"
                                 f" · {len(slot['squad'])}/{AL.SQUAD_MAX} signed")
                text = "\n".join(lines)
            else:
                text = "📋 <b>RCPL squads</b>\nChoose a franchise to see its signings."
            await q.answer()
            await solo._send(context, chat.id, text, _team_picker(room))
            return
        if action == "board":
            await q.answer()
            await _publish(context, room, repost=True)
            return
        seq, price = int(parts[4]), int(parts[5])
        if action == "bid":
            room = await asyncio.to_thread(MP.bid, chat.id, rid, q.from_user.id, seq, price)
        elif action == "finish":
            if q.from_user.id != room.host_id:
                raise AL.AuctionLeagueError("Only the host can finish the auction.")
            await q.answer()
            await solo._send(context, chat.id,
                             "Finish bidding now? The current high bid stands and remaining squad places "
                             "are filled from available players.",
                             [[("Confirm finish", f"rcpl:a:{rid}:end:{seq}:{price}"), *_navigation(room)]])
            return
        elif action == "end":
            room = await asyncio.to_thread(MP.finish_auction, chat.id, rid, q.from_user.id, seq, price)
        else:
            raise ValueError
    except (ValueError, IndexError):
        await q.answer("This button is out of date. Open /rcpl.", show_alert=True)
        return
    except AL.AuctionLeagueError as exc:
        await q.answer(str(exc)[:195], show_alert=True)
        return
    await q.answer("Bid placed." if action == "bid" else "Auction finished.")
    await _publish(context, room)


async def _clock(context):
    rooms = await asyncio.to_thread(MP.active_auctions, due_before=time.time())
    for room in rooms:
        try:
            updated = await asyncio.to_thread(
                MP.change_room, room.chat_id, room.id,
                lambda current: auction.tick(current.state, now=time.time()))
            if updated.version != room.version or not updated.board_message_id:
                await _publish(context, updated)
        except AL.AuctionLeagueError:
            continue
        except Exception:
            logger.exception("RCPL room %s clock failed", room.id)


def register_auction_handlers(app):
    app.add_handler(CallbackQueryHandler(auction_callback, pattern=r"^rcpl:a:"))
    if getattr(app, "job_queue", None):
        app.job_queue.run_repeating(_clock, interval=1.5, first=1, name="rcpl_auction_clock")
