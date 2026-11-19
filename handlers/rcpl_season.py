"""Schedule and start live RCPL fixtures in the hosting group."""

import asyncio

from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler

from database import get_session
from handlers import auction_league as solo
from services import auction_league_service as AL
from services import rcpl_multiplayer_service as MP
from services import rcpl_season as season

PAGE_SIZE = 10


def _season_card(room, fixture=None, *, setup=False):
    state, rid = room.state, room.id
    complete = state["phase"] == AL.PHASE_COMPLETED
    text = f"🏏 <b>{solo._esc(state['league']['name'])} · {'Season complete' if complete else 'Season'}</b>"
    rows = []
    if complete:
        text += f"\n🏆 Champions · <b>{solo._esc(state['champion'])}</b>"
    else:
        fixture = fixture or season.next_fixture(state)
        if fixture:
            text += "\n\n<b>Fixture</b>\n" + solo._fixture_line(state, fixture)
            if fixture.get("match_id"):
                text += f"\nMatch #{fixture['match_id']} created. Use /continue if it was saved."
            elif setup:
                text += "\nMatch setup is open in this group."
            else:
                ready = fixture.get("ready", [])
                required = len(season.managers_for(state, fixture))
                text += f"\n{len(ready)}/{required} managers ready."
                rows.append([("Ready to play", f"rcpl:s:{rid}:play:{fixture['no']}")])
    last = next((fx for fx in reversed(state["fixtures"]) if fx["status"] == "done"), None)
    if last:
        text += "\n\n<b>Latest completed fixture</b>\n" + solo._fixture_line(state, last)
    rows.append([("Standings", f"rcpl:s:{rid}:table"), ("Fixtures", f"rcpl:s:{rid}:fixtures:0"),
                 ("Squads", f"rcpl:a:{rid}:teams")])
    if not complete:
        rows.append([("Close room", f"rcpl:close:{rid}")])
    return text, rows


def _open_season(room):
    if room.state["phase"] != AL.PHASE_SEASON:
        return room
    pending = AL.pending_fixtures(room.state)
    if not pending or (not any(fx.get("match_id") for fx in pending)
                       and season.managers_for(room.state, pending[0])):
        return room
    with get_session() as session:
        def prepare(current):
            season.recover_match(session, current.state)
            season.prepare(current.state)

        try:
            updated = MP.change_room(room.chat_id, room.id, prepare, session=session)
            session.commit()
            return updated
        except MP.RoomChanged:
            session.rollback()
            return MP.get_room(room.chat_id, room.id) or room


def _setup_fixture(context, room):
    from handlers.challenge import _active_draft_in_chat

    draft = _active_draft_in_chat(context.bot_data, room.chat_id) or {}
    tag = draft.get("auction_league") or {}
    if tag.get("room_id") == room.id:
        return tag.get("fixture_no")
    return None


async def show_season(update, context, room):
    room = _open_season(room)
    if room.state["phase"] in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
        fixture = season.next_fixture(room.state) if room.state["phase"] == AL.PHASE_SEASON else None
        setup = fixture is not None and _setup_fixture(context, room) == fixture["no"]
        text, rows = _season_card(room, fixture, setup=setup)
        await solo._send(context, room.chat_id, text, rows)


async def _launch(context, room, fixture):
    from handlers.challenge import launch_auction_league_match
    from handlers.vsbot import _get_or_create_bot_user
    from models import ChallengeLeague, User
    from services.tournament_only_service import is_restricted

    if _setup_fixture(context, room) == fixture["no"]:
        return True, None

    state = room.state
    managers = season.managers_for(state, fixture)
    host_team = fixture["home"] if fixture["home"] in managers else fixture["away"]
    opponent_team = fixture["away"] if host_team == fixture["home"] else fixture["home"]
    with get_session() as session:
        users = {user.telegram_id: user for user in session.query(User)
                 .filter(User.telegram_id.in_(managers.values())).all()}
        missing = [team for team, uid in managers.items() if uid not in users]
        if missing:
            return False, f"{', '.join(missing)} must use /debut before playing a live RCPL fixture."
        if any(is_restricted(user) for user in users.values()):
            return False, "A manager has tournament-only access."
        host = users[managers[host_team]]
        vs_bot = opponent_team not in managers
        opponent = _get_or_create_bot_user(session) if vs_bot else users[managers[opponent_team]]
        session.commit()
        league = session.get(ChallengeLeague, state["league"]["id"]) if state["league"].get("id") else None
        tag = {"room_id": room.id, "chat_id": room.chat_id, "fixture_no": fixture["no"],
               "user_id": host.id, "opp_user_id": opponent.id,
               "user_team": host_team, "opp_team": opponent_team}
        return await launch_auction_league_match(
            context, chat_id=room.chat_id, host=host, opponent=opponent,
            league_record=league, league_key=state["league"].get("key"),
            league_name=f"{state['league']['name']} RCPL · {AL.STAGE_LABEL[fixture['stage']]}",
            host_team=host_team, target_team=opponent_team,
            host_squad=AL.match_cards(state, host_team), target_squad=AL.match_cards(state, opponent_team),
            tag=tag, home_team=fixture["home"], pitch=AL.fixture_pitch(state, fixture),
            venue=fixture.get("venue"), session=session, vs_bot=vs_bot,
            team_codes={team: state["teams"][team]["short"] for team in (host_team, opponent_team)})


async def _ready(context, room, user_id, fixture_no):
    updated = await asyncio.to_thread(
        MP.change_room, room.chat_id, room.id,
        lambda current: season.play(current.state, user_id, fixture_no))
    fixture = AL.fixture_by_no(updated.state, fixture_no)
    if not season.ready_to_start(updated.state, fixture):
        waiting = [team for team, uid in season.managers_for(updated.state, fixture).items()
                   if uid not in fixture["ready"]]
        return updated, fixture, False, f"Ready. Waiting for {', '.join(waiting)} to accept."
    started, reason = await _launch(context, updated, fixture)
    return updated, fixture, started, "Match setup opened." if started else reason


async def play_command(update, context, room):
    message = update.effective_message
    user = update.effective_user
    if not user or user.is_bot:
        return
    try:
        if not room or room.state["phase"] != AL.PHASE_SEASON:
            raise AL.AuctionLeagueError("Finish the RCPL auction before playing fixtures.")
        room = _open_season(room)
        state = room.state
        if MP.managed_team(state, user.id) is None:
            raise AL.AuctionLeagueError("Only an RCPL franchise manager can play a fixture.")
        reply = getattr(message, "reply_to_message", None)
        opponent = getattr(reply, "from_user", None)
        if reply is not None:
            if not opponent or opponent.is_bot or opponent.id == user.id or not MP.managed_team(state, opponent.id):
                raise AL.AuctionLeagueError("Reply to the RCPL manager you want to play against.")
        fixture = season.next_fixture(state, user.id, opponent.id if opponent else None)
        if not fixture:
            raise AL.AuctionLeagueError("You have no pending RCPL fixture with that selection.")
        if opponent is None and len(season.managers_for(state, fixture)) == 2:
            raise AL.AuctionLeagueError("Reply to your scheduled opponent with /rcpl play, or use Ready to play.")
        room, fixture, started, feedback = await _ready(context, room, user.id, fixture["no"])
        await message.reply_text(feedback)
        if not started:
            text, rows = _season_card(room, fixture)
            await solo._send(context, room.chat_id, text, rows)
    except AL.AuctionLeagueError as exc:
        await message.reply_text(str(exc))


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
        room = _open_season(room)
        state = room.state
        rows = [[("Season", f"rcpl:s:{rid}:home")]]
        if action == "play":
            room, fixture, started, feedback = await _ready(context, room, q.from_user.id, int(parts[4]))
            text, rows = _season_card(room, fixture, setup=started)
            await q.answer(feedback[:195], show_alert=not started and season.ready_to_start(room.state, fixture))
            try:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=solo._kb(rows))
            except TelegramError:
                if started:
                    from handlers.rcpl_multiplayer import clear_action_buttons
                    await clear_action_buttons(q)
            return
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
            fixture = season.next_fixture(state) if state["phase"] == AL.PHASE_SEASON else None
            setup = fixture is not None and _setup_fixture(context, room) == fixture["no"]
            text, rows = _season_card(room, fixture, setup=setup)
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
