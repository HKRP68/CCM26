"""Show shared RCPL fixtures and let their managers approve simulation."""

import asyncio

from telegram.ext import CallbackQueryHandler

from handlers import auction_league as solo
from services import auction_league_service as AL
from services import rcpl_multiplayer_service as MP
from services import rcpl_season as season

PAGE_SIZE = 10


def _season_card(room):
    state, rid = room.state, room.id
    phases = {AL.PHASE_COMPLETED: "Season complete", AL.PHASE_SEASON: "Season"}
    text = f"🏏 <b>{solo._esc(state['league']['name'])} · {phases[state['phase']]}</b>"
    rows = []
    if state["phase"] == AL.PHASE_COMPLETED:
        text += f"\n🏆 Champions · <b>{solo._esc(state['champion'])}</b>"
    else:
        pending = AL.pending_fixtures(state)
        if pending:
            fixture = pending[0]
            text += "\n\n<b>Next fixture</b>\n" + solo._fixture_line(state, fixture)
            ready = fixture.get("ready", [])
            required = sum(team in state["managers"] for team in (fixture["home"], fixture["away"]))
            text += f"\n{len(ready)}/{required} managers ready. The bot simulates the match when they’re ready."
            rows.append([("Ready to play", f"rcpl:s:{rid}:play:{fixture['no']}")])
    last = next((fx for fx in reversed(state["fixtures"]) if fx["status"] == "done"), None)
    if last:
        text += "\n\n<b>Latest fixture</b>\n" + solo._fixture_line(state, last)
    rows.append([("Standings", f"rcpl:s:{rid}:table"), ("Fixtures", f"rcpl:s:{rid}:fixtures:0"),
                 ("Squads", f"rcpl:a:{rid}:teams")])
    if state["phase"] == AL.PHASE_SEASON:
        rows.append([("Close room", f"rcpl:close:{rid}")])
    return text, rows


def _open_season(room):
    if room.state["phase"] == AL.PHASE_SEASON:
        pending = AL.pending_fixtures(room.state)
        if pending and not any(pending[0][side] in room.state["managers"] for side in ("home", "away")):
            try:
                return MP.change_room(room.chat_id, room.id, lambda current: season.prepare(current.state))
            except MP.RoomChanged:
                return MP.get_room(room.chat_id, room.id)
    return room


async def show_season(update, context, room):
    room = _open_season(room)
    if room.state["phase"] in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
        text, rows = _season_card(room)
        await solo._send(context, room.chat_id, text, rows)


async def season_callback(update, context):
    q = update.callback_query
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup") or q.from_user.is_bot:
        await q.answer("Use this button in the group hosting the season.", show_alert=True)
        return
    try:
        parts = q.data.split(":")
        rid, action = int(parts[2]), parts[3]
        room = await asyncio.to_thread(MP.get_room, chat.id, rid)
        if not room or room.state["phase"] not in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
            raise AL.AuctionLeagueError("This season is unavailable.")
        state = room.state
        rows = [[("Season", f"rcpl:s:{rid}:home")]]
        if action == "play":
            fixture_no = int(parts[4])
            # The RCPL simulator temporarily seeds the process RNG, so keep
            # synchronous simulation on the event loop like the solo path.
            room = MP.change_room(chat.id, rid, lambda current: season.play(current.state, q.from_user.id, fixture_no))
            text, rows = _season_card(room)
        elif action == "table":
            text = solo._table_text({**state, "user_team": None})
        elif action == "fixtures":
            page = int(parts[4])
            fixtures = state["fixtures"]
            start = page * PAGE_SIZE
            if page < 0 or start >= len(fixtures):
                raise ValueError
            lines = [solo._fixture_line(state, fx) for fx in fixtures[start:start + PAGE_SIZE]]
            text = "📅 <b>RCPL fixtures</b>\n" + "\n".join(lines)
            navigation = []
            if page:
                navigation.append(("Previous", f"rcpl:s:{rid}:fixtures:{page - 1}"))
            if start + PAGE_SIZE < len(fixtures):
                navigation.append(("Next", f"rcpl:s:{rid}:fixtures:{page + 1}"))
            if navigation:
                rows.insert(0, navigation)
        elif action == "home":
            room = _open_season(room)
            if room.state["phase"] not in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
                raise AL.AuctionLeagueError("This season is unavailable.")
            text, rows = _season_card(room)
        else:
            raise ValueError
    except AL.AuctionLeagueError as exc:
        await q.answer(str(exc)[:195], show_alert=True)
        return
    except (ValueError, IndexError):
        await q.answer("This button is out of date. Open /rcpl.", show_alert=True)
        return
    await q.answer()
    await solo._send(context, chat.id, text, rows)


def register_season_handlers(app):
    app.add_handler(CallbackQueryHandler(season_callback, pattern=r"^rcpl:s:"))
