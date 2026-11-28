"""Telegram controls for the same official campaigns shown in the MiniApp."""

import asyncio
from datetime import datetime
from html import escape
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from models import User
from models_events import StudioGame
from services import cipl_picks as picks, impact_player, rich_message as rich
from services.match_state_store import A_INNINGS_BREAK
from services import event_studio as studio, event_studio_play as play
from services.economy_transactions import reserve_miniapp_action
from services.miniapp_buttons import miniapp_button

KINDS = {"eventplay": None, "events": None, "liveevents": "live", "myevents": "joined",
         "moments": "moment", "bossbattles": "boss", "bossbattle": "boss"}

logger = logging.getLogger(__name__)

STALE_TEXT = "This control has expired. Send /events to open it again."
BROKEN_TEXT = "Something went wrong opening that. It has been logged — send /events to try again."


class StaleControl(Exception):
    """The callback data no longer points at anything we can show.

    Raised only where a control is genuinely out of date — truncated or malformed
    data, an index into a list that has since changed, a game row that is gone. It
    is deliberately NOT what a bug in a panel raises: those are logged with their
    traceback instead of being passed off to the player as an expired button.
    """


def _button(label, *parts):
    return InlineKeyboardButton(label, callback_data="ev:" + ":".join(map(str, parts)))


def _part(parts, index, label="control"):
    """One integer out of the callback data, or a stale control."""
    try:
        return int(parts[index])
    except (IndexError, TypeError, ValueError) as exc:
        raise StaleControl(f"callback data has no valid {label} at {index}") from exc


def _when(stamp, zone):
    """An ISO instant as a short local time, for a fixture window."""
    try:
        return datetime.fromisoformat(stamp).astimezone(ZoneInfo(zone)).strftime("%d %b %H:%M")
    except (AttributeError, TypeError, ValueError, ZoneInfoNotFoundError):
        return stamp


def _user(db, telegram_id):
    user = db.scalar(select(User).filter_by(telegram_id=telegram_id))
    if not user or user.needs_debut or user.is_banned:
        raise ValueError("Complete /debut with an active account to play Events.")
    return user


def _list(events, kind=None):
    shown = [event for event in events if kind is None or event["kind"] == kind
             or kind == "joined" and event["joined"]][:12]
    lines = ["Events Hub"]
    buttons = []
    for event in shown:
        availability = event["availability"].capitalize()
        if event["joined"]:
            availability += " · Joined"
        buttons.append([_button(f'{event["name"][:45]} · {availability}', "show", event["id"])])
    if not shown:
        lines.append("There are no published events or campaigns in this section.")
    hub = miniapp_button("Open Events Hub", "events")
    if hub:
        buttons.append([hub])
    return "\n\n".join(lines), buttons


def _match_panel(event, game, state):
    from handlers.cipl_play import _approach_card, _approach_card_blocks
    from services.bot_captain import difficulty_label
    from services.match_broadcast import play_match_keyboard

    header = f'{event["name"]} · {game["home"]} v {game["away"]}'
    round_label = f'Round {game["round"]}' if game["round"] else f'Attempt {game["attempt"]}'
    lines = [f'{round_label} · {event["format"]} · {difficulty_label(state)}',
             f'{game["status"].replace("_", " ").capitalize()} · {game["cutoff"]}']
    identifiers = (event["id"], game["id"], game["version"])
    buttons = []
    score = game.get("score") or {}
    choices = score.get("choices")
    result = game.get("result")
    if result:
        if result.get("winner"):
            lines.append(f'{result["winner"]} won.')
        if result.get("stars") is not None:
            lines.append(f'{result["stars"]} stars')
        if result.get("points") is not None:
            lines.append(f'{result["points"]} points')
        following = next((row for row in event["games"] if row["personal"]
                          and not row.get("result") and row["status"] == "scheduled"), None)
        if following:
            lines.append(f'Next fixture · {following["home"]} v {following["away"]}')
        if any(row["game_id"] == game["id"] and row["status"] == "claimable"
               for row in event.get("rewards") or []):
            lines.append("Reward available in Events Hub.")
        for objective in result.get("objectives", []):
            lines.append(f'{objective["title"]} · {objective["measured"]}/{objective["threshold"]}')
    elif not game["event_paused"] and game["status"] != "under_review":
        if game["player_paused"]:
            buttons.append([_button("Resume in chat", "resume", *identifiers)])
        elif game["play_mode"] == "app":
            buttons.append([_button("Play in chat", "switch", *identifiers)])
        elif choices:
            stage = choices["stage"]
            if stage == A_INNINGS_BREAK:
                lines.append(f'Innings break · Target {score["target"]}')
                buttons.append([_button("Continue", "pick", *identifiers, "c", 0)])
            elif stage == picks.PICK_ACTION[picks.PICK_BOWLER]:
                lines.append("Choose a bowler.")
                buttons.extend([_button(f'{p["name"][:45]} · {p["overs_left"]} left', "pick", *identifiers, "b", p["id"])]
                               for p in choices["bowlers"])
            elif stage == picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
                lines.append("Choose the incoming batter.")
                buttons.extend([_button(p["name"][:48], "pick", *identifiers, "n", p["id"])]
                               for p in choices["incoming"])
            else:
                if choices["bowler"]:
                    lines.append(f'Bowler · {choices["bowler"]}')
                batting = stage == picks.PICK_ACTION[picks.PICK_BAT_APPROACH]
                lines.append("Choose your batting approach." if batting else "Choose your bowling approach.")
                buttons.extend([_button(row["label"], "pick", *identifiers, "a" if batting else "p", index)]
                               for index, row in enumerate(choices["approaches"]))
            if state.get("event_impact") and impact_player.cipl_available_to(
                    state, state["bat_team_id"] if choices["role"] == "bat" else state["bowl_team_id"]):
                buttons.append([_button("Impact Player", "impact", *identifiers)])
            buttons.append([_button("Pause", "pause", *identifiers)])
        buttons.append([_button("Abandon…", "confirm", *identifiers)])
    for objective in score.get("objectives", []):
        lines.append(f'{objective["title"]} · {objective["measured"]}/{objective["threshold"]}')
    if score.get("moment"):
        moment = score["moment"]
        lines.append(f'Goal {moment["progress"]}/{moment["goal"]} · {moment["balls_left"]} balls left')
    keyboard = play_match_keyboard(f"event-{game['id']}", chat_id=0, is_private=True,
                                   label="Open CIPL Arena" if not result else "View scorecard")
    if keyboard:
        buttons.extend(list(row) for row in keyboard.inline_keyboard)
    buttons.append([_button("Back to event", "show", event["id"])])
    if not state:
        return "\n".join([header, *lines]), buttons
    # CIPL's rich card renders the same strike, bowler, projection, chase and
    # delivery details that ordinary chat matches show.
    text = "\n".join([f"<b>{escape(header)}</b>", *(escape(line) for line in lines), "", _approach_card(state)])
    blocks = _approach_card_blocks(state, [[line] for line in lines])
    if blocks:
        blocks.insert(0, rich.heading(header, size=3))
    return text, buttons, blocks


def _impact_panel(user, db, event_id, game_id, version, parts):
    owned = play.owned_game(db, user, game_id)
    if not owned:
        raise ValueError("Match is unavailable.")
    game, entry = owned
    if entry.event_id != event_id or game.status != "in_progress" or game.version != version:
        raise studio.Conflict("This match changed. Reopen the Impact Player picker.")
    state = game.state
    if not state.get("event_impact") or state.get("play_mode") != "chat" or state.get("event_paused"):
        raise ValueError("Impact Player is unavailable on this surface.")
    opts = impact_player.cipl_options(state, entry.team_key, state.get("event_next_action"))
    identifiers = (event_id, game_id, version)
    back = [_button("Back to match", "match", event_id, game_id)]
    if not opts.get("can_use"):
        return opts["message"], [back]
    if len(parts) == 4:
        return "Choose the player coming off.", [
            *[[_button(p["name"][:45], "impact", *identifiers, p["roster_id"])]
              for p in opts["replaceable_players"]], back]
    out = int(parts[4])
    if len(parts) == 5:
        incoming = impact_player.cipl_incoming_for(state, opts["side"], out, opts["incoming_options"])
        action = "imp_pos" if opts["side"] == "bat" else "imp_use"
        return "Choose an eligible substitute.", [
            *[[_button(p["name"][:45], action, *identifiers, out, p["roster_id"])] for p in incoming], back]
    positions = impact_player.cipl_batting_slots(state, out)
    return "Choose a batting slot for the substitute.", [
        *[[_button(label, "imp_use", *identifiers, out, int(parts[5]), slot)] for slot, label in positions], back]


def _setup_panel(event_id, game_id, previous, setup, draft, position=0, page=0):
    identifiers = (event_id, game_id, previous)
    lines = [f'{setup["team"]} v {setup["opponent"]}',
             " · ".join(filter(None, (setup["format"], setup["pitch"] + " pitch",
                                      setup["venue"], setup["weather"], setup["difficulty"]))),
             f'Start by {setup["cutoff"]}']
    for goal in setup["objectives"]:
        lines.append(f'{goal["title"]} · {goal["threshold"]}')
    buttons = []
    if not setup["toss"]:
        lines.append("Call the toss.")
        buttons.append([_button("Heads", "coin", *identifiers, "heads"),
                        _button("Tails", "coin", *identifiers, "tails")])
    else:
        toss = setup["toss"]
        ruling = "You won the toss." if toss["human_won"] else f'{setup["opponent"]} chose to {toss["decision"]}.'
        lines.append(f'{toss["coin"].title()} · {ruling}')
        squad = {p["roster_id"]: p for p in setup["squad"]}
        xi = draft.get("xi") or setup["default_xi"]
        captain = draft.get("captain")
        if position:
            lines.append(f'Choose batting position {position}.')
            available = [p for p in squad.values() if p["roster_id"] not in xi[:position - 1]]
            buttons.extend([_button(f'{p["name"][:35]} · {p["category"]} · {p["rating"]}',
                                     "xi", *identifiers, position, p["roster_id"])]
                           for p in available[page * 10:page * 10 + 10])
            pages = []
            if page:
                pages.append(_button("Previous", "order", *identifiers, position, page - 1))
            if len(available) > (page + 1) * 10:
                pages.append(_button("Next", "order", *identifiers, position, page + 1))
            if pages:
                buttons.append(pages)
            buttons.append([_button("Quick XI", "quick", *identifiers)])
        else:
            lines.extend(f'{index}. {squad[rid]["name"]}{" (c)" if rid == captain else ""}'
                         for index, rid in enumerate(xi, 1))
            if not setup["fixed_xi"]:
                buttons.append([_button("Set batting order", "order", *identifiers, 1, 0),
                                _button("Quick XI", "quick", *identifiers)])
            buttons.append([_button("Choose captain", "captains", *identifiers)])
            decisions = ("bat", "bowl") if toss["human_won"] else (toss["decision"],)
            buttons.append([_button(f'Start · {decision.title()} first', "launch", *identifiers, decision)
                            for decision in decisions])
    buttons.append([_button("Back to event", "show", event_id)])
    return "\n".join(lines), buttons


def _event_panel(event):
    event_id = event["id"]
    lines = [event["name"], f'{event["format"]} · {event["availability"].capitalize()}']
    buttons = []
    if event["description"]:
        lines.append(event["description"][:600])
    if event.get("moment"):
        moment = event["moment"]
        lines.append(f'Start at {moment["start_runs"]}/{moment["start_wickets"]} after {moment["start_over"]} overs.')
        if moment["goal_type"] == "chase":
            goal = (f'Chase {moment["goal"]} runs' if moment["side"] == "bat"
                    else f'Defend the target of {moment["target"]}')
        else:
            units = "runs" if moment["side"] == "bat" else "wickets"
            goal = f'Goal: {moment["goal"]} {units}'
        lines.append(f'{goal} in {moment["overs"]} overs.')
    if event["chapter"].get("name"):
        lines.append(f'{event["chapter"]["name"]} · Moment {event["chapter"]["position"]}')
    if event.get("chapter_lock"):
        lines.append(event["chapter_lock"])
    if event["boss_goal"]:
        lines.append(f'{event["boss_goal"]["winners"]}/{event["boss_goal"]["target"]} distinct boss winners')
    if not event["joined"] and event["can_join"]:
        lines.append("Your team and track are locked on joining. Choose a track.")
        for mode in event["modes"]:
            buttons.append([_button("Tournament Team · Hard" if mode == "tournament_team" else "Own Roster · Hard++",
                                    "teams", event_id, mode)])
    if event["joined"]:
        lines.append(event["team"])
        personal = [game for game in event.get("games", []) if game["personal"]]
        if event["attempt_limit"] is not None:
            lines.append(f'{event["attempts_used"]}/{event["attempt_limit"]} attempts used · Best stars {event["best_stars"]}')
            if event["can_attempt"]:
                buttons.append([_button("Start official attempt" if event["moment"] else "Match setup",
                                        "attempt" if event["moment"] else "setup", event_id, 0, len(personal))])
        zone = event.get("timezone") or "UTC"
        upcoming = []
        for game in personal[-20:]:
            kind = game["status"]
            label = f'Round {game["round"]}' if game["round"] else f'Attempt {game["attempt"]}'
            if kind in ("in_progress", "grace", "under_review", "paused"):
                buttons.append([_button("Resume official match", "match", event_id, game["id"])])
            elif game["can_start"]:
                buttons.append([_button(f'Set up round {game["round"]}', "setup", event_id, game["id"], 0)])
            elif game.get("result"):
                buttons.append([_button(f'{label} · {kind.replace("_", " ").capitalize()}', "match", event_id, game["id"])])
            else:
                # Scheduled but not open yet, or a playoff slot still waiting on an
                # earlier result. It has no result to show and cannot be started, but
                # the player should still see that it is coming and when.
                upcoming.append((label, game))
        if upcoming:
            lines.append("Upcoming fixtures")
            for label, game in upcoming[:6]:
                sides = (f'{game["home"]} v {game["away"]}' if game["home"] and game["away"]
                         else "Opponent decided by an earlier result")
                lines.append(f'{label} · {sides}\nOpens {_when(game["opens_at"], zone)}'
                             f' · Deadline {_when(game.get("deadline") or game["cutoff"], zone)}')
        if event.get("objectives"):
            lines.append("Objectives\n" + "\n".join(
                f'{row["title"]} · {row["threshold"]}' for row in event["objectives"][:5]))
        rewards = event.get("rewards") or []
        claimable = sum(reward["status"] == "claimable" for reward in rewards)
        if event.get("table"):
            buttons.append([_button("Personal standings", "table", event_id)])
        buttons.append([_button("Stats and leaderboard", "stats", event_id),
                        _button("Rewards", "rewards", event_id)])
        if claimable:
            buttons.append([_button(f"Claim rewards ({claimable})", "claim", event_id)])
        if event["trophy"]:
            lines.append(event["trophy"])
    hub = miniapp_button("Open in MiniApp", f"events_{event_id}")
    if hub:
        buttons.append([hub])
    buttons.append([_button("All events", "list")])
    return "\n\n".join(lines), buttons


def _progress_panel(db, user, event, section):
    event_id = event["id"]
    buttons = [[_button("Back to event", "show", event_id)]]
    lines = [event["name"]]
    if section == "table":
        lines.append("Personal standings")
        for index, row in enumerate(event["table"], 1):
            lines.append(f'{index}. {row["name"][:40]} · {row["points"]} pts · '
                         f'{row["won"]}W {row["lost"]}L · NRR {row["nrr"]:+.3f}')
    elif section == "rewards":
        lines.append("Rewards")
        labels = {"coins": "Coins", "gems": "Gems", "quest_points": "Quest Points"}
        for row in event["rewards"][-20:]:
            assets = ", ".join(f"{amount:,} {labels.get(asset, asset)}"
                               for asset, amount in row["assets"].items())
            lines.append(f'{row["status"].capitalize()} · {assets}')
        if not event["rewards"]:
            lines.append("Earned wallet rewards will appear here.")
        if any(row["status"] == "claimable" for row in event["rewards"]):
            buttons.insert(0, [_button("Claim all", "claim", event_id)])
    else:
        data = play.progression(db, user, event_id)
        lines.append("Track leaderboard")
        for row in data["leaderboard"][:10]:
            lines.append(f'{row["rank"]}. {row["name"]} · {row["wins"]} wins · {row["stars"]} stars')
        if not data["leaderboard"]:
            lines.append("Official results will appear here.")
        stats = data["stats"]
        if stats:
            if stats["batting"]:
                lines.append("Leading batters")
                for player in stats["batting"][:5]:
                    lines.append(f'{player["name"][:45]} · {player["runs"]} runs')
            if stats["bowling"]:
                lines.append("Leading bowlers")
                for player in stats["bowling"][:5]:
                    lines.append(f'{player["name"][:45]} · {player["wickets"]} wickets')
    return "\n".join(lines), buttons


def _dispatch(telegram_id, parts=None, kind=None, drafts=None):
    with get_session() as db:
        user = _user(db, telegram_id)
        now = datetime.utcnow()
        if parts is None or parts[0] == "list":
            return _list(play.list_events(db, user, now), kind)
        action = parts[0]
        if action in ("join", "launch", "attempt", "abandon", "claim"):
            delay = reserve_miniapp_action(db, user)
            if delay:
                raise ValueError(f"Try again in {delay}s.")
            db.commit()
        event_id = _part(parts, 1, "event id")
        game_id = None
        if action == "join":
            event = play.detail(db, user, event_id, now)
            choice = _part(parts, 3, "team")
            if not 0 <= choice < len(event["teams"]):
                raise StaleControl("team index is outside the published team list")
            team = event["teams"][choice]["key"]
            play.join(db, user, event_id, team, parts[2], now)
        elif action in ("setup", "coin", "order", "xi", "quick", "captains", "captain", "launch"):
            game_id, previous = _part(parts, 2, "game id"), _part(parts, 3, "attempt")
            setup = play.setup_fixture(db, user, event_id, game_id, previous, now,
                                       call=parts[4] if action == "coin" else None)
            draft = drafts.setdefault(f"{event_id}:{game_id}:{previous}", {})
            if action == "quick":
                draft.clear()
            if action == "captains":
                xi = set(draft.get("xi") or setup["default_xi"])
                buttons = [[_button(p["name"][:45], "captain", event_id, game_id, previous, p["roster_id"])]
                           for p in setup["squad"] if p["roster_id"] in xi]
                buttons.append([_button("Back", "setup", event_id, game_id, previous)])
                return "Choose your captain.", buttons
            if action == "captain":
                draft["captain"] = int(parts[4])
            position = int(parts[4]) if action in ("order", "xi") else 0
            if action == "xi":
                xi = draft.setdefault("xi", list(setup["default_xi"]))
                selected = int(parts[5])
                if selected in xi[:position - 1] or selected not in {p["roster_id"] for p in setup["squad"]}:
                    raise ValueError("Choose an available player.")
                if selected in xi:
                    former = xi.index(selected)
                    xi[former] = xi[position - 1]
                xi[position - 1] = selected
                if draft.get("captain") not in xi:
                    draft.pop("captain", None)
                position = 0 if position == 11 else position + 1
            if action != "launch":
                db.commit()
                page = int(parts[5]) if action == "order" else 0
                return _setup_panel(event_id, game_id, previous, setup, draft, position, page)
            launch = {"selected": draft.get("xi"), "captain_id": draft.get("captain"),
                      "decision": parts[4], "require_toss": True}
            if game_id:
                game_id = play.start_fixture(db, user, game_id, **launch).id
            else:
                game_id = play.start_attempt(db, user, event_id, previous, **launch).id
        elif action == "attempt":
            game_id = play.start_attempt(db, user, event_id, int(parts[3]), require_toss=True).id
        elif action == "pick":
            codes = {"c": "continue", "b": picks.PICK_BOWLER, "p": picks.PICK_BOWL_APPROACH,
                     "a": picks.PICK_BAT_APPROACH, "n": picks.PICK_NEW_BATSMAN}
            if len(parts) < 5 or parts[4] not in codes:
                raise StaleControl(f"unknown pick code in {parts!r}")
            kind = codes[parts[4]]
            try:
                game_id = play.submit_pick(db, user, int(parts[2]), int(parts[3]),
                                           kind, int(parts[5]), source="chat").id
            except studio.ExecutionUnavailable as exc:
                db.commit()
                raise ValueError(str(exc)) from exc
        elif action in ("pause", "resume", "switch"):
            game_id = play.set_play_state(db, user, int(parts[2]), int(parts[3]), action, "chat").id
        elif action in ("impact", "imp_pos"):
            return _impact_panel(user, db, event_id, int(parts[2]), int(parts[3]), parts)
        elif action == "imp_use":
            position = int(parts[6]) if len(parts) > 6 else None
            game_id = play.use_impact(db, user, int(parts[2]), int(parts[3]),
                                      int(parts[5]), int(parts[4]), position, source="chat").id
        elif action == "abandon":
            game_id = play.abandon(db, user, int(parts[2]), int(parts[3])).id
        elif action == "claim":
            play.claim(db, user, event_id, None, now)
        db.commit()
        event = play.detail(db, user, event_id, datetime.utcnow())
        if action == "teams" and not event["joined"] and event["can_join"] and parts[2] in event["modes"]:
            buttons = [[_button(f'Join as {team["name"][:44]}', "join", event_id, parts[2], idx)]
                       for idx, team in enumerate(event["teams"])]
            buttons.append([_button("Back", "show", event_id)])
            return "Choose a team and confirm enrollment. Your choice is permanent for this campaign.", buttons
        if action in ("match", "confirm"):
            game_id = _part(parts, 2, "game id")
        if action in ("table", "stats", "rewards") and event["joined"]:
            return _progress_panel(db, user, event, action)
        if game_id:
            game = next((game for game in event.get("games", []) if game["id"] == game_id and game["personal"]), None)
            if not game:
                raise ValueError("Match is unavailable.")
            if action == "confirm":
                return ("Abandon this official game? The result is permanent and no reward will be earned.",
                        [[_button("Confirm abandon", "abandon", event_id, game_id, _part(parts, 3, "version"))],
                         [_button("Resume", "match", event_id, game_id)]])
            row = db.get(StudioGame, game_id)
            if row is None:
                raise StaleControl(f"game {game_id} is no longer in the database")
            return _match_panel(event, game, row.state)
        return _event_panel(event)


async def command(update, context):
    if update.effective_chat.type != "private":
        hub = miniapp_button("Open Events Hub", "events", is_private=False)
        await update.effective_message.reply_text(
            "Open the Events Hub or send /events to the bot in a private chat.",
            reply_markup=InlineKeyboardMarkup([[hub]]) if hub else None)
        return
    name = update.effective_message.text.split()[0].split("@")[0][1:].lower()
    if name == "eventplay" and await handle_match_command(update, context, "match"):
        return
    try:
        text, buttons = await asyncio.to_thread(_dispatch, update.effective_user.id, kind=KINDS.get(name))
    except ValueError as exc:
        text, buttons = str(exc), []
    except StaleControl as exc:
        logger.info("events: stale control for %s: %s", update.effective_user.id, exc)
        text, buttons = STALE_TEXT, []
    except Exception:
        logger.exception("events: could not render /%s for %s", name, update.effective_user.id)
        text, buttons = BROKEN_TEXT, []
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


def _target_match(telegram_id, game_id, action):
    with get_session() as db:
        user = _user(db, telegram_id)
        owned = play.owned_game(db, user, game_id)
        if owned is None:
            raise ValueError("Match is unavailable.")
        game, entry = owned
        event_id, version = entry.event_id, game.version
        if action in ("pause", "resume", "chat", "app"):
            source = "app" if action == "app" else "chat"
            play.set_play_state(db, user, game_id, version,
                                "switch" if action in ("app", "chat") else action, source)
            db.commit()
        return event_id, version


async def handle_match_command(update, context, action):
    args = getattr(context, "args", None)
    target = re.fullmatch(r"event-(\d{1,10})", args[0].lower()) if isinstance(args, (list, tuple)) and args else None
    if not target:
        return False
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Open the official match in a private chat.")
        return True
    game_id = int(target[1])
    try:
        event_id, version = await asyncio.to_thread(_target_match, update.effective_user.id, game_id, action)
        parts = ["confirm" if action == "clear" else "match", str(event_id), str(game_id), str(version)]
        panel = await asyncio.to_thread(_dispatch, update.effective_user.id, parts)
        text, buttons, *render = panel
        markup = InlineKeyboardMarkup(buttons)
        if render:
            await rich.send_rich_message(context.bot, update.effective_chat.id, render[0], text, reply_markup=markup)
        else:
            await update.effective_message.reply_text(text, reply_markup=markup)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
    except (StaleControl, IndexError) as exc:
        logger.info("events: stale match control for %s: %s", update.effective_user.id, exc)
        await update.effective_message.reply_text(STALE_TEXT)
    except Exception:
        logger.exception("events: could not open match %s for %s", game_id, update.effective_user.id)
        await update.effective_message.reply_text(BROKEN_TEXT)
    return True


async def callback(update, context):
    query = update.callback_query
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer("Use /events in private chat.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")[1:]
    try:
        panel = await asyncio.to_thread(_dispatch, update.effective_user.id, parts,
                                        drafts=context.user_data.setdefault("event_xi", {}))
        text, buttons, *render = panel
    except ValueError as exc:
        await query.message.reply_text(str(exc))
        return
    except StaleControl as exc:
        logger.info("events: stale control %r for %s: %s", query.data, update.effective_user.id, exc)
        await query.message.reply_text(STALE_TEXT)
        return
    except Exception:
        # A panel that fails to render is a bug here, not a button the player sat on
        # too long. Record it with its traceback and say so, rather than quietly
        # blaming the control and leaving nothing to debug from.
        logger.exception("events: could not render %r for %s", query.data, update.effective_user.id)
        await query.message.reply_text(BROKEN_TEXT)
        return
    from telegram.error import BadRequest
    try:
        markup = InlineKeyboardMarkup(buttons)
        if render and await rich.edit_rich_message(context.bot, query.message.chat_id,
                                                    query.message.message_id, render[0],
                                                    reply_markup=markup, fallback_text=text):
            return
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML" if render else None)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
