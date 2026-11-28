"""Telegram controls for the same official campaigns shown in the MiniApp."""

import asyncio
from datetime import datetime
from html import escape
import logging
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from models import User
from models_events import StudioEnrollment, StudioGame
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


def _match_panel(event, game, state, *, manage=False, button=_button, public_id=None):
    from handlers import cipl_play as cipl
    from services import cipl_match, match_controls
    from services.bot_captain import difficulty_label
    from services.live_batting_help import manage_batting_button

    header = f'{event["name"]} · {game["home"]} v {game["away"]}'
    round_label = f'Round {game["round"]}' if game["round"] else f'Attempt {game["attempt"]}'
    lines = [f'{round_label} · {event["format"]} · {difficulty_label(state)}']
    identifiers = (event["id"], game["id"], game["version"])
    buttons = []
    tools = []
    score = game.get("score") or {}
    choices = score.get("choices")
    result = game.get("result")
    text, blocks = "", None

    if state:
        state = {**state, "chat_id": 0, "event_game_version": game["version"]}
        if result and result["kind"] == "completed":
            outcome = cipl_match.compute_result(state)
            text = cipl._completion_text(state, outcome, cipl.match_result_line(outcome))
        elif not result and choices and not game["event_paused"] and game["play_mode"] == "chat":
            stage = choices["stage"]

            def picked(code):
                return lambda label, value: button(label, "pick", *identifiers, code, value)

            if stage == picks.PICK_ACTION[picks.PICK_BOWLER]:
                text, buttons, blocks = cipl.bowler_prompt(state, picked("b"))
            elif stage == picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
                text, buttons = cipl.new_batsman_prompt(state, picked("n"))
            elif stage in (picks.PICK_ACTION[picks.PICK_BAT_APPROACH], picks.PICK_ACTION[picks.PICK_BOWL_APPROACH]):
                batting = stage == picks.PICK_ACTION[picks.PICK_BAT_APPROACH]
                text, buttons, blocks = cipl.approach_prompt(state, batting, picked("a" if batting else "p"))
            if (state["innings"] == 2 and not state.get("event_moment")
                    and cipl_match.balls_bowled(state) == 0):
                break_blocks, break_text = cipl.innings_break_card(state)
                if stage == A_INNINGS_BREAK:  # Checkpoints created before chat advanced automatically.
                    buttons.append([button("Continue", "pick", *identifiers, "c", 0)])
                text = f"{break_text}\n\n{text}"
                if blocks:
                    blocks = break_blocks + blocks
        if not text:
            text, blocks = cipl._approach_card(state), cipl._approach_card_blocks(state)

    if result:
        if result["kind"] != "completed":
            lines.append(result["kind"].replace("_", " ").capitalize())
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
    else:
        lines.append(f'Play until {_when(game["cutoff"], event.get("timezone") or "UTC")}')
        if game["event_paused"] or game["status"] == "under_review":
            lines.append(game["status"].replace("_", " ").capitalize())
        elif game["player_paused"]:
            tools.append([button("Resume in chat", "resume", *identifiers)])
        elif game["play_mode"] == "app":
            tools.append([button("Play in chat", "switch", *identifiers)])
        elif choices:
            if state.get("event_impact") and impact_player.cipl_available_to(
                    state, state["bat_team_id"] if choices["role"] == "bat" else state["bowl_team_id"]):
                tools.append([button("🔄 Impact Player", "impact", *identifiers)])
            batting = manage_batting_button(state)
            if batting:
                tools.append([batting])
            tools.append([button("Pause", "pause", *identifiers)])
        if not game["event_paused"] and game["status"] != "under_review":
            tools.append([button("Abandon…", "confirm", *identifiers)])

    for objective in (result or score).get("objectives", []):
        lines.append(f'{objective["title"]} · {objective["measured"]}/{objective["threshold"]}')
    if score.get("moment"):
        moment = score["moment"]
        lines.append(f'Goal {moment["progress"]}/{moment["goal"]} · {moment["balls_left"]} balls left')
    if state:
        tools.extend(cipl._miniapp_row(state) or [])
    tools.append([button("Back to event", "show", event["id"])])
    if manage:
        return match_controls.TITLE, [*tools, [button("Back to match", "match", *identifiers[:2])]]

    text = "\n\n".join(filter(None, (text, f"<b>{escape(header)}</b>\n" + "\n".join(map(escape, lines)))))
    if blocks:
        blocks += [rich.heading(header, size=3), *(rich.paragraph(line) for line in lines)]
    match_id = public_id or f"event-{game['id']}"
    blocks, text = match_controls.attach(
        {"match_id": match_id, "chat_id": 0}, text, blocks, tools,
        fallback_command=f"/eventplay {match_id} manage")
    return text, buttons, blocks


def _impact_panel(user, db, event_id, game_id, version, parts):
    from handlers.cipl_play import _impact_label, _impact_overseas_block, impact_batting_slots

    game, entry, _ = play._ownership(db, user.id, game_id)
    if entry.event_id != event_id or game.status != "in_progress" or game.version != version:
        raise studio.Conflict("This match changed. Reopen the Impact Player picker.")
    state = play._checkpoint(game, entry, user)
    if not state.get("event_impact") or state.get("play_mode") != "chat" or state.get("event_paused"):
        raise ValueError("Impact Player is unavailable on this surface.")
    opts = impact_player.cipl_options(state, entry.team_key, state.get("event_next_action"))
    identifiers = (event_id, game_id, version)
    back = [_button("✖️ Cancel", "match", event_id, game_id)]
    if not opts.get("can_use"):
        return opts["message"], [back]
    title = "🔄 <b>Impact Player</b>"
    side = opts["side"]
    if len(parts) == 4:
        off = [_button(_impact_label(p), "impact", *identifiers, p["roster_id"])
               for p in opts["replaceable_players"]]
        text = (f"{title} — {escape(str(opts['legal_break']))}\n\n"
                f"{_impact_overseas_block(state, side)}Step 1 of 3: who comes <b>off</b>?")
        return text, [*[off[i:i + 2] for i in range(0, len(off), 2)], back], None
    out = _part(parts, 4, "outgoing player")
    outgoing = next((p for p in opts["replaceable_players"] if p["roster_id"] == out), None)
    if outgoing is None:
        raise ValueError("Choose a player from your active XI.")
    legal = impact_player.cipl_incoming_for(state, side, out, opts["incoming_options"])
    if not legal:
        raise ValueError(impact_player.NO_LEGAL_SWAP_MESSAGE)
    recap = f"{title}\n\nOff: <b>{escape(str(outgoing['name']))}</b>\n"
    if len(parts) == 5:
        options = [_button(_impact_label(p), "imp_pos", *identifiers, out, p["roster_id"]) for p in legal]
        text = (recap + _impact_overseas_block(state, side, legal, opts.get("bench_all"))
                + "Step 2 of 3: who comes <b>on</b>?")
    else:
        in_id = _part(parts, 5, "incoming player")
        incoming = next((p for p in legal if p["roster_id"] == in_id), None)
        if incoming is None:
            raise ValueError("Choose an eligible substitute.")
        name = escape(str(incoming["name"]))
        when = "bats" if side == "bat" else "bats next innings"
        text = f"{recap}On: <b>{name}</b>\n\nStep 3 of 3: where {when} <b>{name}</b>?"
        options = [_button(f"#{slot + 1} · {label}", "imp_use", *identifiers, out, in_id, slot)
                   for slot, label in impact_batting_slots(state, side, out)]
    return text, [*[options[i:i + 2] for i in range(0, len(options), 2)], back], None


def _batting_panel(db, user, event_id, game_id, version, parts):
    from handlers.live_batting import _menu
    from services import live_batting

    game, entry, _ = play._ownership(db, user.id, game_id)
    if entry.event_id != event_id or game.status != "in_progress" or game.version != version:
        raise studio.Conflict("This match changed. Reopen Manage batting.")
    state = play._checkpoint(game, entry, user)
    if (state.get("event_paused") or state.get("play_mode") != "chat"
            or state["bat_team_id"] != entry.team_key):
        raise ValueError("Only the batting captain can change batting on the active play surface.")
    options = live_batting.options(state, state["event_next_action"], can_control=True)
    if not options["canManage"]:
        raise ValueError(options["message"])
    action = parts[4]
    if action in ("r", "o"):
        first, second = _part(parts, 5, "player"), _part(parts, 6, "player")
        data = {"token": options["token"]}
        if action == "r":
            kind = "retire_batsman"
            data.update(outRosterId=first, inRosterId=second)
        else:
            kind = "live_batting_order"
            ids = [p["id"] for p in options["waiting"]]
            if min(first, second) < 0 or max(first, second) >= len(ids) or abs(first - second) != 1:
                raise ValueError("Invalid batting position.")
            ids[first], ids[second] = ids[second], ids[first]
            data["rosterIds"] = ids
        play.change_batting(db, user, game_id, version, kind, data, source="chat")
        if game.status != "in_progress":
            return None
        options = live_batting.options(game.state, game.state["event_next_action"], can_control=True)

    def control(label, step, *values):
        if step == "x":
            return _button(label, "match", event_id, game_id)
        return _button(label, "bat", event_id, game_id, game.version, step, *values[1:])

    outgoing = _part(parts, 5, "player") if action == "p" else None
    text, markup = _menu(game_id, options, outgoing=outgoing, button=control)
    return text, [list(row) for row in markup.inline_keyboard], None


def _setup_panel(event_id, game_id, previous, setup, selected):
    from handlers.challenge import (
        _challenge_xi_player_keyboard, start_match_buttons, toss_call_buttons, toss_decision_buttons,
    )
    from services.xi_rules import _category_is_keeper

    identifiers = (event_id, game_id, previous)
    lines = [f'<b>{escape(setup["team"])} v {escape(setup["opponent"])}</b>',
             escape(" · ".join(filter(None, (setup["format"], setup["pitch"] + " pitch",
                                               setup["venue"], setup["weather"], setup["difficulty"])))),
             f'Start by {escape(_when(setup["cutoff"], setup["timezone"]))}']

    def control(label, action, *values):
        return _button(label, action, *identifiers, *values)

    toss = setup["toss"]
    if not toss:
        lines[:0] = ["🪙 <b>TOSS</b>", "Call the coin."]
        buttons = toss_call_buttons(lambda label, call: control(label, "coin", call))
    elif not toss["decision"]:
        lines = [f'🪙 The coin lands on <b>{toss["coin"].upper()}</b> — you called '
                 f'<b>{toss["call"].upper()}</b>.', "", "🏆 You won the toss. Choose:"]
        buttons = toss_decision_buttons(lambda label, decision: control(label, "elect", decision))
    else:
        winner = setup["team"] if toss["human_won"] else setup["opponent"]
        decision = toss["decision"].upper()
        lines.append(f'🪙 <b>Toss:</b> {escape(winner)} won and elected to <b>{decision}</b>.')
        squad = {p["roster_id"]: p for p in setup["squad"]}
        confirmed = setup["confirmed_xi"]
        if confirmed:
            lines.insert(0, "🏏 <b>MATCH READY!</b>\n")
            lines.append("\n<b>Playing XI</b>")
            buttons = start_match_buttons(lambda label: control(label, "launch"))
            if not setup["fixed_xi"]:
                buttons.append([control("✏️ Edit XI", "xe")])
        else:
            lines.append(f'\n🏏 <b>{escape(setup["team"])} Playing XI Selection</b>')
            if setup["fixed_xi"]:
                selected = setup["default_xi"]
                buttons = [[control("✅ Confirm XI", "xc")]]
            else:
                lines.append("Tap a player to add/remove, or reply with their numbers "
                             "(e.g. <code>1 2 3 4 5 6 7 8 9 10 11</code>).")
                players = [SimpleNamespace(id=p["roster_id"], name=p["name"]) for p in squad.values()]
                actions = {"pick": "xp", "clear": "xz", "confirm": "xc", "useprev": "xq"}
                keyboard = _challenge_xi_player_keyboard(None, None, players, selected,
                    saved_available=True, saved_label="⚡ Use suggested XI",
                    button=lambda label, action, *values: control(label, actions[action], *values))
                buttons = [list(row) for row in keyboard.inline_keyboard]
            playing = [squad[rid] for rid in selected if rid in squad]
            keepers = sum(_category_is_keeper(p["category"]) for p in playing)
            bowlers = sum(p["category"] in ("Bowler", "All-rounder") for p in playing)
            overseas = sum(bool(p["is_overseas"]) for p in playing)
            lower, upper = setup["min_overseas"], setup["max_overseas"]
            checks = [(keepers >= 1, f"1 Wicket Keeper ({keepers}/1)"),
                      (bowlers >= 5, f"At least 5 Bowling Options ({bowlers}/5)")]
            if lower > 0 or upper < 11:
                checks.append((lower <= overseas <= upper, f"Overseas ✈️ {overseas} (min {lower} / max {upper})"))
            lines += [f'\n<b>Selected:</b> {len(playing)}/11', "<b>Rules:</b>",
                      *(f'{"✅" if met else "⬜"} {label}' for met, label in checks),
                      "• Selection order becomes batting order", "\n<b>Batting order:</b>"]
        lines.extend(f'{pos}. {escape(squad[rid]["name"])}' for pos, rid in enumerate(confirmed or selected, 1)
                     if rid in squad)
    buttons.append([_button("Back to event", "show", event_id)])
    lines.extend(f'{escape(goal["title"])} · {goal["threshold"]}' for goal in setup["objectives"])
    return "\n".join(lines), buttons, None


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
        if action == "show":
            event, config = play._published(db, event_id)
            if config.get("objective_scope") == "tournament":
                from handlers.event_competitions import event_panel
                return event_panel(db, user, event_id, now)
        if action == "join":
            event = play.detail(db, user, event_id, now)
            choice = _part(parts, 3, "team")
            if not 0 <= choice < len(event["teams"]):
                raise StaleControl("team index is outside the published team list")
            team = event["teams"][choice]["key"]
            play.join(db, user, event_id, team, parts[2], now)
        elif action in ("setup", "coin", "elect", "xp", "xc", "xq", "xz", "xe", "xn", "launch",
                        "order", "xi", "quick", "captains", "captain"):
            game_id, previous = _part(parts, 2, "game id"), _part(parts, 3, "attempt")
            options = {"call": parts[4]} if action == "coin" else {}
            if action == "elect":
                options["decision"] = parts[4]
            elif action == "xe":
                options["edit"] = True
            setup = play.setup_fixture(db, user, event_id, game_id, previous, now, **options)
            if drafts is None:
                drafts = {}
            key = f"{event_id}:{game_id}:{previous}"
            selected = drafts.get(key, [])
            if setup["editing_xi"]:
                selected = drafts[key] = setup["editing_xi"]
            if action in ("xp", "xc", "xq", "xz", "xn") and setup["toss"] and setup["toss"]["decision"] and not setup["confirmed_xi"]:
                squad = [p["roster_id"] for p in setup["squad"]]
                selected = [rid for rid in selected if rid in squad]
                if action == "xp" and not setup["fixed_xi"]:
                    rid = _part(parts, 4, "player")
                    if rid not in squad:
                        raise ValueError("Choose an available player.")
                    if rid in selected:
                        selected.remove(rid)
                    elif len(selected) < 11:
                        selected.append(rid)
                    else:
                        raise ValueError("You already have 11 players. Remove one first.")
                elif action == "xz" and not setup["fixed_xi"]:
                    selected = []
                elif action == "xn" and not setup["fixed_xi"]:
                    numbers = [int(number) for number in parts[4:]]
                    if len(numbers) > 11 or len(set(numbers)) != len(numbers):
                        raise ValueError("Choose up to 11 different player numbers.")
                    if any(number < 1 or number > len(squad) for number in numbers):
                        raise ValueError(f"Choose player numbers from 1 to {len(squad)}.")
                    selected = [squad[number - 1] for number in numbers]
                elif action in ("xc", "xq"):
                    if action == "xq" or setup["fixed_xi"]:
                        selected = setup["default_xi"]
                    setup = play.setup_fixture(db, user, event_id, game_id, previous, now, xi=selected)
                drafts[key] = selected
            if action != "launch" or not setup["confirmed_xi"]:
                db.commit()
                return _setup_panel(event_id, game_id, previous, setup, selected)
            launch = {"require_toss": True, "require_confirmed": True}
            if game_id:
                game_id = play.start_fixture(db, user, game_id, None, **launch).id
            else:
                game_id = play.start_attempt(db, user, event_id, previous, **launch).id
            drafts.pop(key, None)
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
        elif action == "bat":
            game_id, version = _part(parts, 2, "game id"), _part(parts, 3, "version")
            panel = _batting_panel(db, user, event_id, game_id, version, parts)
            db.commit()
            if panel:
                return panel
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
        if action in ("match", "confirm", "timeout", "manage"):
            game_id = _part(parts, 2, "game id")
        if action in ("table", "stats", "rewards") and event["joined"]:
            return _progress_panel(db, user, event, action)
        if game_id:
            game = next((game for game in event.get("games", []) if game["id"] == game_id and game["personal"]), None)
            if not game:
                raise ValueError("Match is unavailable.")
            if action == "timeout" and (game["version"] != _part(parts, 3, "version")
                                        or game["play_mode"] != "chat" or game["event_paused"]):
                return None
            if action == "confirm":
                return ("Abandon this official game? The result is permanent and no reward will be earned.",
                        [[_button("Confirm abandon", "abandon", event_id, game_id, _part(parts, 3, "version"))],
                         [_button("Resume", "match", event_id, game_id)]])
            row = db.get(StudioGame, game_id)
            if row is None:
                raise StaleControl(f"game {game_id} is no longer in the database")
            state = row.state
            if game["score"] and state and not state.get("event_next_action"):
                entry = db.get(StudioEnrollment, row.enrollment_id)
                state = play._checkpoint(row, entry, user)
            return _match_panel(event, game, state, manage=action == "manage")
        return _event_panel(event)


async def command(update, context):
    if update.effective_chat.type != "private":
        hub = miniapp_button("Open Events Hub", "events", is_private=False)
        await update.effective_message.reply_text(
            "Open the Events Hub or send /events to the bot in a private chat.",
            reply_markup=InlineKeyboardMarkup([[hub]]) if hub else None)
        return
    name = update.effective_message.text.split()[0].split("@")[0][1:].lower()
    if name == "eventplay":
        args = getattr(context, "args", None) or []
        action = "manage" if len(args) > 1 and args[1].lower() == "manage" else "match"
        if await handle_match_command(update, context, action):
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
    key = args[0].lower() if isinstance(args, (list, tuple)) and args else ""
    shared = re.fullmatch(r"competition-(\d{1,10})", key)
    if shared:
        from handlers.event_competitions import match_command
        await match_command(update, context, int(shared[1]), action)
        return True
    target = re.fullmatch(r"event-(\d{1,10})", key)
    if not target:
        return False
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Open the official match in a private chat.")
        return True
    game_id = int(target[1])
    try:
        event_id, version = await asyncio.to_thread(_target_match, update.effective_user.id, game_id, action)
        panel_action = {"clear": "confirm", "manage": "manage"}.get(action, "match")
        parts = [panel_action, str(event_id), str(game_id), str(version)]
        panel = await asyncio.to_thread(_dispatch, update.effective_user.id, parts)
        text, buttons, *render = panel
        markup = InlineKeyboardMarkup(buttons) if buttons else None
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


async def send_timeout_prompt(bot, telegram_id, event_id, game_id, version):
    try:
        panel = await asyncio.to_thread(
            _dispatch, telegram_id, ["timeout", str(event_id), str(game_id), str(version)])
        if panel:
            text, buttons, *render = panel
            await rich.send_rich_message(bot, telegram_id, render[0] if render else None, text,
                                         reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    except Exception:
        logger.exception("events: could not refresh the chat picker for match %s", game_id)


async def callback(update, context):
    query = update.callback_query
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer("Use /events in private chat.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")[1:]
    toss = parts[0] == "coin"
    toss_key = (query.message.chat_id, query.message.message_id) if toss else None
    flipping = context.user_data.setdefault("event_flips", set())
    if toss and toss_key in flipping:
        return
    if toss:
        flipping.add(toss_key)
    try:
        try:
            panel = await asyncio.to_thread(_dispatch, update.effective_user.id, parts,
                                            drafts=context.user_data.setdefault("event_xi", {}))
        except studio.Conflict:
            if parts[0] not in ("pick", "pause", "resume", "switch", "bat", "imp_use"):
                raise
            panel = await asyncio.to_thread(_dispatch, update.effective_user.id,
                                            ["match", *parts[1:3]])
        text, buttons, *render = panel
        if toss:
            from services.match_broadcast import animate_coin_toss, reveal_toss_result
            markup = InlineKeyboardMarkup(buttons)
            await animate_coin_toss(lambda frame: query.edit_message_text(frame, parse_mode="HTML"))
            if not await reveal_toss_result(lambda: query.edit_message_text(
                    text, reply_markup=markup, parse_mode="HTML")):
                await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return
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
    finally:
        if toss:
            flipping.discard(toss_key)
    from telegram.error import BadRequest
    try:
        markup = InlineKeyboardMarkup(buttons)
        if render and render[0] and await rich.edit_rich_message(context.bot, query.message.chat_id,
                                                    query.message.message_id, render[0],
                                                    reply_markup=markup, fallback_text=text):
            return
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML" if render else None)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def quickselect_reply(update, context):
    """Route a reply to a numbered Event Studio XI picker."""
    message = update.effective_message
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return False
    target = getattr(message, "reply_to_message", None)
    markup = getattr(target, "reply_markup", None)
    if not markup:
        return False
    data = next((button.callback_data for row in markup.inline_keyboard for button in row
                 if (button.callback_data or "").startswith("ev:xp:")), None)
    tokens = re.split(r"[\s,]+", (message.text or "").strip())
    if not data or len(tokens) < 2 or not all(token.isdecimal() for token in tokens):
        return False
    try:
        parts = ["xn", *data.split(":")[2:5], *tokens]
        text, buttons, _ = await asyncio.to_thread(
            _dispatch, update.effective_user.id, parts, drafts=context.user_data.setdefault("event_xi", {}))
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    except ValueError as exc:
        await message.reply_text(str(exc))
    return True
