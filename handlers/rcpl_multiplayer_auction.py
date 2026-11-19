"""Telegram board and auction clock for group RCPL."""

import asyncio
from datetime import datetime, timezone
import logging
import time

from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler

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


def _sale(room):
    state = room.state
    lot = state.get("last_lot")
    if not lot:
        return ""
    player = AL.card(state, lot["pid"])
    player_line = solo._card_line(player)
    if lot["status"] != "sold":
        return (f"○ <b>Unsold · Lot {lot['seq']}</b>\n{player_line}\n"
                f"Base price {AL.money(lot['base'])}")
    winner = lot["winner"]
    team = state["teams"][winner]
    return (f"🔨 <b>Sold · Lot {lot['seq']}</b>\n{player_line}\n"
            f"<b>{solo._esc(winner)}</b> signed the player for <b>{AL.money(lot['price'])}</b>.\n"
            f"{AL.money(team['purse'])} left · {len(team['squad'])}/{AL.SQUAD_MAX} players")


def _board(room):
    state, rid = room.state, room.id
    phase = state["phase"]
    if phase == AL.PHASE_ABANDONED:
        return "This RCPL room has closed. Send /rcpl to open a new room.", []
    if phase != AL.PHASE_AUCTION:
        return ("🏏 <b>RCPL auction complete</b>\n"
                "The squads are ready. Open the season to see fixtures and standings.",
                [[("Season", f"rcpl:s:{rid}:home"), ("Squads", f"rcpl:a:{rid}:teams")]])

    lot = state.get("lot")
    paused = state.get("paused_at") is not None
    clock_at = lot["deadline"] if lot else state["next_lot_at"]
    clock = datetime.fromtimestamp(clock_at, timezone.utc).strftime("%H:%M:%S UTC")
    total = sum(len(group["pids"]) for group in state["sets"])
    text = f"🔨 <b>{solo._esc(state['league']['name'])} · RCPL auction</b>"
    rows = [[("Squads", f"rcpl:a:{rid}:teams"), ("Purses", f"rcpl:a:{rid}:purses")]]
    if lot is None and state.get("last_lot") is None:
        managers = len(state["managers"])
        rules = " · ".join(f"{minimum} {AL.ROLE_SHORT[role]}" for role, minimum in AL.ROLE_MIN.items())
        text += (f"\n\n<b>Auction starts at {clock}</b>" if not paused else "\n\n<b>Start paused</b>")
        text += (f"\n{total} players across {len(state['sets'])} sets"
                 f"\n{managers} human managers · {len(state['team_order']) - managers} AI managers"
                 f"\n{AL.money(AL.PURSE_LAKH)} per franchise · up to {AL.SQUAD_MAX} players"
                 f"\nSquad minimums · {rules}\n"
                 f"\nBid with /rcplbid or tap the quoted price once bidding opens."
                 f"\nEach accepted bid resets the {auction.LOT_SECONDS}s clock.")
    elif lot is None:
        text += "\n\n" + _sale(room)
        text += "\n\n<b>Paused between lots</b>" if paused else f"\n\nNext lot opens at {clock}."
    else:
        player = AL.card(state, lot["pid"])
        price = AL.next_price(lot)
        text += (f"\nLot {lot['seq']}/{total} · {solo._esc(lot['set_name'])}"
                 f"\n\n<b>{solo._card_line(player)}</b>"
                 f"\nBat {player['bat_rating']} · Bowl {player['bowl_rating']}"
                 f"\nBase {AL.money(lot['base'])}")
        if lot["leader"]:
            leader = lot["leader"]
            manager = state["managers"].get(leader)
            owner = manager["name"] if manager else "AI"
            text += (f"\nLeading · <b>{solo._esc(leader)}</b> ({solo._esc(owner)})"
                     f" · <b>{AL.money(lot['price'])}</b>")
        else:
            text += "\nWaiting for the opening bid."
        if lot["trail"]:
            trail = " · ".join(f"{solo._esc(solo._short(state, team))} {AL.money(amount)}"
                               for team, amount in lot["trail"][-3:])
            text += f"\nRecent bids · {trail}"
        if paused:
            text += "\n\n<b>Auction paused by the host.</b>"
        else:
            call = {10: "Going once", 3: "Going twice"}.get(lot.get("closing_call"))
            if call:
                text += f"\n\n<b>{call} · {lot['closing_call']}s or less</b>"
            text += f"\nCloses at {clock} · /rcplbid raises to {AL.money(price)}"
            rows.insert(0, [(f"Bid {AL.money(price)}", f"rcpl:a:{rid}:bid:{lot['seq']}:{price}")])
    action, label = ("resume", "Resume") if paused else ("pause", "Pause")
    rows.append([(label, f"rcpl:a:{rid}:{action}"), ("End auction", f"rcpl:a:{rid}:finish")])
    return text, rows


async def _pin(context, chat_id, message_id, *, live):
    try:
        if live:
            await context.bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        else:
            await context.bot.unpin_chat_message(chat_id, message_id)
    except TelegramError:
        logger.debug("RCPL board pin could not be updated for %s", chat_id, exc_info=True)


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
                if current.state["phase"] != AL.PHASE_AUCTION:
                    await _pin(context, room.chat_id, message_id, live=False)
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
        if saved and current.state["phase"] == AL.PHASE_AUCTION:
            await _pin(context, room.chat_id, message.message_id, live=True)
        retired = message_id if saved else message.message_id
        if retired and retired != (message.message_id if saved else message_id):
            if saved:
                await _pin(context, room.chat_id, retired, live=False)
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
        if action == "bid":
            room = await asyncio.to_thread(MP.bid, chat.id, rid, q.from_user.id, int(parts[4]), int(parts[5]))
            feedback = "Bid accepted."
        elif action in ("pause", "resume"):
            room = await asyncio.to_thread(MP.control_auction, chat.id, rid, q.from_user.id, action)
            feedback = "Auction paused." if action == "pause" else "Auction resumed."
        elif action == "finish":
            if q.from_user.id != room.host_id:
                raise AL.AuctionLeagueError("Only the host can end the auction.")
            if state["phase"] != AL.PHASE_AUCTION:
                raise AL.AuctionLeagueError("The auction has finished.")
            await q.answer()
            await solo._send(context, chat.id,
                             "End the auction? The current high bid stands. The remaining lots close "
                             "and the bot fills any gaps in the squads.",
                             [[("Confirm end", f"rcpl:a:{rid}:end"), *_navigation(room)]])
            return
        elif action == "end":
            # Older boards included a quote with the end confirmation.
            quote = (int(parts[4]), int(parts[5])) if len(parts) > 4 else ()
            room = await asyncio.to_thread(MP.finish_auction, chat.id, rid, q.from_user.id, *quote)
            feedback = "Auction finished."
        else:
            raise ValueError
    except (ValueError, IndexError):
        await q.answer("This button is out of date. Open /auction.", show_alert=True)
        return
    except AL.AuctionLeagueError as exc:
        await q.answer(str(exc)[:195], show_alert=True)
        return
    await q.answer(feedback)
    if action == "end":
        from handlers.rcpl_multiplayer import clear_action_buttons
        await clear_action_buttons(q)
    await _show_update(context, state, room)


async def _show_update(context, before, room):
    previous = before.get("last_lot") or {}
    latest = room.state.get("last_lot") or {}
    if latest and latest.get("seq") != previous.get("seq"):
        await solo._send(context, room.chat_id, _sale(room))
    await _publish(context, room)


async def auction_command(update, context):
    chat = update.effective_chat
    if chat is None:
        return
    room = await asyncio.to_thread(MP.get_room, chat.id) if chat.type in ("group", "supergroup") else None
    if room is None:
        await solo._send(context, chat.id, "Open /rcpl in your group to host or join an auction.")
        return
    from handlers.rcpl_multiplayer import _show
    await _show(update, context, room)


async def bid_command(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or user.is_bot:
        return
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Send /rcplbid in the group hosting the auction.")
        return
    room = await asyncio.to_thread(MP.get_room, chat.id)
    if room is None or room.state["phase"] != AL.PHASE_AUCTION:
        await message.reply_text("There is no live RCPL auction in this group. Use /rcpl to see the room.")
        return
    if context.args:
        await message.reply_text("Send /rcplbid without an amount to take the next price.")
        return
    try:
        updated = await asyncio.to_thread(MP.bid, chat.id, room.id, user.id)
    except AL.AuctionLeagueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    lot = updated.state["lot"]
    team = updated.state["teams"][lot["leader"]]["short"]
    await update.effective_message.reply_text(f"Bid accepted · {team} leads at {AL.money(lot['price'])}.")
    await _publish(context, updated)


async def _clock(context):
    rooms = await asyncio.to_thread(MP.active_auctions, due_before=time.time())
    for room in rooms:
        before = None

        def advance(current):
            nonlocal before
            last_lot = current.state.get("last_lot")
            changed = auction.tick(current.state, now=time.time())
            if changed:
                before = {"last_lot": last_lot}
            return changed

        try:
            updated = await asyncio.to_thread(MP.change_room, room.chat_id, room.id, advance)
            if before is not None:
                await _show_update(context, before, updated)
            elif not updated.board_message_id:
                await _publish(context, updated)
        except AL.AuctionLeagueError:
            continue
        except Exception:
            logger.exception("RCPL room %s clock failed", room.id)


def register_auction_handlers(app):
    app.add_handler(CommandHandler("auction", auction_command))
    app.add_handler(CommandHandler(["rcplbid", "bid"], bid_command))
    app.add_handler(CallbackQueryHandler(auction_callback, pattern=r"^rcpl:a:"))
    if getattr(app, "job_queue", None):
        app.job_queue.run_repeating(_clock, interval=1.5, first=1, name="rcpl_auction_clock")
