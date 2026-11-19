"""Telegram group registration for a shared RCPL auction and season."""

import asyncio
import logging

from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from handlers import auction_league as solo
from services import auction_league_service as AL
from services import rcpl_multiplayer_service as MP

logger = logging.getLogger(__name__)


def group_help():
    return (
        "🏏 <b>Auction League — help</b>\n\n"
        "<b>Multiplayer in a group</b>\n"
        "1. Send /rcpl to choose a league, or <code>/rcpl IPL</code> to open its lobby directly. "
        "The person who opens the room is its host.\n"
        "2. Tap a franchise to manage it. Each manager can take one team. "
        "Use <b>Release my team</b> to give up your seat before the auction.\n"
        "3. The host taps <b>Start auction</b> and confirms. AI managers take every open franchise.\n"
        "4. Send /bid or tap the price to bid from your franchise purse. Each bid resets the 30-second clock. "
        "The host can pause, resume or end the auction. Unsold players may return in one accelerated round. "
        "Use /auction to reopen the board and check squads and purses.\n"
        "5. Follow the season with your auction squad. Tap <b>Ready to play</b> on your fixtures "
        "to simulate them. AI-only fixtures run automatically.\n\n"
        "Send /rcpl again to return to the group’s lobby, auction or season. "
        "For a solo career, send /rcpl in a private chat with the bot.\n\n"
        + solo.HELP_LINE
    )


def _display_name(user):
    return user.first_name or user.username or "Manager"


def _lobby_card(room):
    state = room.state
    managers = state["managers"]
    teams = state["team_order"]
    lines = [f"🏏 <b>{solo._esc(state['league']['name'])} · RCPL multiplayer</b>",
             f"Host · {solo._esc(room.host_name)}",
             f"{len(managers)}/{len(teams)} franchises claimed · {AL.money(AL.PURSE_LAKH)} per team", ""]
    available = []
    for idx, team in enumerate(teams):
        manager = managers.get(team)
        owner = solo._esc(manager["name"]) if manager else "Open"
        lines.append(f"<b>{solo._esc(team)}</b> · {owner}")
        if manager is None:
            available.append((team, f"rcpl:claim:{room.id}:{idx}"))
    lines.extend(["", "Pick a franchise below. The host starts the auction once at least one manager has joined. "
                     "AI managers bid and build squads for all open teams."])
    rows = [available[i:i + 2] for i in range(0, len(available), 2)]
    if managers:
        rows.append([("Release my team", f"rcpl:leave:{room.id}")])
    rows.append([("🔨 Start auction", f"rcpl:start:{room.id}"), ("Close room", f"rcpl:close:{room.id}")])
    return "\n".join(lines), rows


async def _show(update, context, room, *, edit=False):
    phase = room.state["phase"]
    if phase == "lobby":
        text, rows = _lobby_card(room)
        if edit:
            try:
                await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=solo._kb(rows))
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                logger.info("RCPL lobby %s could not be edited: %s", room.id, exc)
        await solo._send(context, room.chat_id, text, rows)
    elif phase == AL.PHASE_AUCTION or (phase == AL.PHASE_ABANDONED and room.board_message_id):
        from handlers.rcpl_multiplayer_auction import show_auction
        await show_auction(update, context, room)
    elif phase in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
        from handlers.rcpl_season import show_season
        await show_season(update, context, room)
    else:
        await solo._send(context, room.chat_id, "This RCPL room has closed. Send /rcpl for the current room.")


async def group_command(update, context, args):
    chat_id = update.effective_chat.id
    room = await asyncio.to_thread(MP.get_room, chat_id)
    if room:
        await _show(update, context, room)
        return
    user = update.effective_user
    if not user or user.is_bot or getattr(update.effective_message, "sender_chat", None):
        await solo._send(context, chat_id, "Use your personal Telegram profile to host a multiplayer room.")
        return
    wanted = " ".join(args).strip()
    if wanted and wanted.lower() != "new":
        league_id = await asyncio.to_thread(solo._find_league_id, wanted)
        if league_id is not None:
            try:
                room = await asyncio.to_thread(MP.create_room, chat_id, user.id, _display_name(user), league_id)
            except AL.AuctionLeagueError as exc:
                await solo._send(context, chat_id, solo._esc(exc))
                return
            await _show(update, context, room)
            return
    leagues = await asyncio.to_thread(solo._league_options)
    if not leagues:
        await solo._send(context, chat_id, "No leagues are available for RCPL yet.")
        return
    rows = [[(name, f"rcpl:create:{user.id}:{league_id}:{chat_id}")] for league_id, name in leagues]
    text = (f"🏏 <b>Host an RCPL multiplayer auction</b>\n{solo._esc(_display_name(user))}, choose a league. "
            "Group members can then claim a franchise. You decide when bidding opens.")
    if wanted and wanted.lower() != "new":
        text = f"League <b>{solo._esc(wanted)}</b> is unavailable.\n\n" + text
    await solo._send(context, chat_id, text, rows)


async def lobby_callback(update, context):
    q = update.callback_query
    chat = update.effective_chat
    user = q.from_user
    if not chat or chat.type not in ("group", "supergroup") or not user or user.is_bot:
        await q.answer("Use the RCPL buttons in the group hosting the league.", show_alert=True)
        return
    parts = (q.data or "").split(":")
    action = parts[1]
    try:
        if action == "create":
            if len(parts) != 5 or int(parts[4]) != chat.id or int(parts[2]) != user.id:
                raise AL.AuctionLeagueError("Only the manager who opened this league picker can use it in the original group.")
            room = await asyncio.to_thread(MP.create_room, chat.id, user.id, _display_name(user), int(parts[3]))
            await q.answer()
            await _show(update, context, room, edit=True)
            return
        rid = int(parts[2])
        room = await asyncio.to_thread(MP.get_room, chat.id, rid)
        if room is None:
            raise AL.AuctionLeagueError("This room is unavailable in this group. Send /rcpl to reopen.")
        if action in ("start", "go", "close", "cancel") and user.id != room.host_id:
            raise AL.AuctionLeagueError("Only the host can do that.")
        if action == "lobby":
            await q.answer()
            await _show(update, context, room)
            return
        if action in ("start", "close"):
            if action == "start":
                if room.state["phase"] != "lobby":
                    raise AL.AuctionLeagueError("Registration has closed. Send /rcpl to open the current board.")
                managers = len(room.state["managers"])
                if not managers:
                    raise AL.AuctionLeagueError("At least one manager must claim a franchise first.")
                ai = len(room.state["team_order"]) - managers
                text = (f"<b>Start the auction?</b>\nCurrently {managers} human-managed and {ai} AI-managed franchises. "
                        "Confirm to lock the teams and start the 45-second countdown.")
                button = ("Start countdown", f"rcpl:go:{rid}")
            else:
                text = "<b>Close this RCPL room?</b> This ends the group’s league."
                button = ("Yes, close room", f"rcpl:cancel:{rid}")
            await q.answer()
            await solo._send(context, chat.id, text, [[button, ("Back to room", f"rcpl:lobby:{rid}")]])
            return
        if action == "claim":
            idx = int(parts[3])
            room = await asyncio.to_thread(MP.claim_team, chat.id, rid, user.id, _display_name(user), idx)
            toast = f"You manage {room.state['team_order'][idx]}."
        elif action == "leave":
            room = await asyncio.to_thread(MP.leave_team, chat.id, rid, user.id)
            toast = "Your franchise is open again."
        elif action == "go":
            room = await asyncio.to_thread(MP.start_room, chat.id, rid, user.id)
            toast = "Auction countdown started."
        elif action == "cancel":
            room = await asyncio.to_thread(MP.cancel_room, chat.id, rid, user.id)
            toast = "Room closed."
        else:
            raise AL.AuctionLeagueError("Unknown room action.")
    except (ValueError, IndexError):
        await q.answer("This button is out of date. Send /rcpl for the current room.", show_alert=True)
        return
    except AL.AuctionLeagueError as exc:
        await q.answer(str(exc)[:195], show_alert=True)
        return
    await q.answer(toast[:195])
    await _show(update, context, room, edit=True)


def register_multiplayer(app):
    if getattr(app, "bot_data", {}).get("franchise_dedicated"):
        return
    from handlers.rcpl_multiplayer_auction import register_auction_handlers
    from handlers.rcpl_season import register_season_handlers
    app.add_handler(CallbackQueryHandler(lobby_callback, pattern=r"^rcpl:(create|claim|leave|start|go|close|cancel|lobby):"))
    register_auction_handlers(app)
    register_season_handlers(app)
