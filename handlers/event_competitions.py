"""Telegram browser, shared group lobby and fixture views for Live Events."""

import asyncio
from datetime import datetime
from html import escape
import logging

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from database import get_session
from models_events import StudioCompetition, StudioFixtureResult
from services import cipl_picks as picks, event_studio as studio, event_studio_competition as play
from services import event_studio_objectives as goals, event_studio_play as legacy, rich_message as rich
from services.miniapp_buttons import miniapp_button

logger = logging.getLogger(__name__)
SECTIONS = {"eventobjectives": "objectives", "eventtable": "table", "eventstats": "stats", "eventrewards": "rewards"}


def _button(label, *parts):
    return InlineKeyboardButton(label, callback_data="ec:" + ":".join(map(str, parts)))


def _when(stamp, zone):
    from handlers.event_studio import _when as display_time
    return display_time(stamp, zone)


def _browser(db, user, telegram_id, index, now, private):
    rows = play.browser(db, user, now)
    if not rows:
        launch = miniapp_button("Open Events Hub", "events", is_private=private)
        return "No Live Events are open right now.", [[launch]] if launch else [], None
    idx = index % len(rows)
    event, config = rows[idx]
    new = goals.tournament(config)
    joined = bool(play.membership(db, event, user.id)) if new else legacy._enrollment(db, event.id, user.id) is not None
    summary = legacy.event_summary(event, config, joined, now)
    zone = config["timezone"]
    from services.event_studio_schedule import utc_text
    cutoff = _when(utc_text(legacy._cutoff(event, config)), zone)
    mode_names = [m.title() for m in config["competition"]["modes"] if play.mode_enabled(config, m)] if new else ["Solo"]
    days = (event.closes_at - event.opens_at).total_seconds() / 86400
    teams = ", ".join(team["name"] for team in config["snapshot"].values())
    lines = [event.name, f'{config["format"]} · {len(config["fixtures"])} fixtures · {days:.1f} days',
             teams, f'{" / ".join(mode_names) or "Registration unavailable"} · AI {(config.get("difficulty") or "hard").title()}',
             f'Enrollment closes {cutoff} · Event closes {_when(summary["closes_at"], zone)}']
    if joined:
        lines.append("Already joined")
    if new:
        lines.append(f'{goals.max_stars(config)} maximum stars')
        lines.extend(f'{obj.get("title") or goals.METRICS[obj["metric"]]} {obj.get("comparator", "≥")} {obj["target"]} · {obj["stars"]} ★'
                     for obj in config["tournament_objectives"][:4])
    elif config["objectives"]:
        lines.extend(f'{legacy.rules.OBJECTIVES[obj["key"]][0]} · {obj["threshold"]}' for obj in config["objectives"])
    if config["milestones"]:
        lines.append("Reward milestones · " + " / ".join(str(row["stars"]) for row in config["milestones"]) + " ★")
        for milestone in config["milestones"][:4]:
            diamond = ", ".join(f"{amount} {asset.replace('_', ' ')}" for asset, amount in milestone.get("diamond", {}).items() if amount)
            free = ", ".join(f"{amount} {asset.replace('_', ' ')}" for asset, amount in milestone.get("free", {}).items() if amount)
            if free or diamond:
                lines.append(f'{milestone["stars"]} ★ · Free {free or "—"} · Diamond {diamond or "—"}')
    buttons = []
    if joined or summary["can_join"] and mode_names:
        buttons.append(_button("Resume" if joined else "Join", "open", telegram_id, event.id, idx))
    buttons.extend((_button("Next", "browser", telegram_id, idx + 1), _button("Close", "close", telegram_id)))
    blocks = [rich.heading(lines[0]), *(rich.paragraph(line) for line in lines[1:])]
    if config.get("banner_url"):
        blocks.insert(0, {"type": "photo", "photo": {"type": "photo", "media": config["banner_url"]}})
    return "\n".join(escape(line) for line in lines), [buttons], blocks


def event_panel(db, user, event_id, now, *, private=True, section=None, page=0):
    if not private and section == "rewards":
        link = miniapp_button("Open your rewards", f"events_{event_id}", is_private=False)
        return "Open your tournament reward road in private.", [[link]] if link else [], None
    view = play.dashboard(db, user, event_id, now)
    rows, lines = [], [f'<b>{escape(view["name"])}</b>']
    if not view["joined"]:
        if view["registration_open"]:
            rows.extend([_button("Join Solo" if mode == "solo" else "Join Multi", "teams", user.telegram_id, event_id, mode)]
                        for mode in view["modes"])
            rows.append([_button("Join Existing Lobby", "invite_help", user.telegram_id)])
        else:
            lines.append("Enrollment is closed.")
        rows.append([_button("Back", "browser", user.telegram_id, 0)])
    elif view["room"]["status"] == "lobby":
        return _lobby_panel(view["room"], now, private)
    else:
        stand = view["my_standing"]
        team = next(team for team in view["teams"] if team["key"] == view["team"])
        lines.append(f'{escape(team["name"])} · {stand["points"]} points · NRR {stand["nrr"]:+.3f} · {view["remaining"]} left')
        if view["form"]:
            lines.append("Recent form · " + " ".join(view["form"]))
        if view["trophy"]:
            lines.append(f'🏆 {escape(view["trophy"])}')
        if section == "fixtures":
            start = max(0, page) * 8
            for fixture in view["fixtures"][start:start + 8]:
                lines.append(_fixture_line(fixture, view["timezone"]))
                if fixture["personal"] and (fixture["can_start"] or fixture["status"] == "in_progress" or
                                            (fixture["result"] or {}).get("kind") == "completed"):
                    rows.append([_button(f'Round {fixture["round"]} · Open', "fixture", fixture["id"])])
            if start:
                rows.append([_button("Previous", "panel", user.telegram_id, event_id, "fixtures", page - 1)])
            if start + 8 < len(view["fixtures"]):
                rows.append([_button("Next", "panel", user.telegram_id, event_id, "fixtures", page + 1)])
        elif section == "table":
            lines.extend(f'{idx}. {escape(row["name"])} · {row["points"]} pts · NRR {row["nrr"]:+.3f}'
                         for idx, row in enumerate(view["standings"], 1))
        elif section == "squad":
            lines.extend(f'{escape(p["name"])} · {escape(p["category"])} · {p["rating"]}' for p in team["squad"])
        elif section == "stats":
            lines.extend(f'{escape(row["name"])} · {row["runs"]} runs'
                         for row in view["statistics"]["batting"][:8])
            lines.extend(f'{escape(row["name"])} · {row["wickets"]} wickets'
                         for row in view["statistics"]["bowling"][:8])
            lines.append("Solo leaderboard" if view["room"]["kind"] == "solo" else "Multiplayer leaderboard")
            lines.extend(f'{row["rank"]}. {escape(row["name"])} · {row["stars"]} ★' for row in view["leaderboard"][:10])
        elif section == "objectives":
            stars = view["stars"]
            lines.append(f'{stars["earned"]} earned · {stars["unclaimed"]} ready · {stars["claimed"]}/{stars["maximum"]} claimed')
            statuses = {"ready": "Ready to claim", "claimed": "Claimed", "expired": "Expired",
                        "locked": "Locked", "in_progress": "In progress"}
            for obj in stars["objectives"]:
                lines.append(f'{escape(obj["title"])} · {obj["measured"]}/{obj["target"]} · {obj["stars"]} ★ · {statuses[obj["status"]]}')
                if obj["status"] == "ready":
                    lines.append(f'Claim by {_when(obj["expires_at"], view["timezone"])}')
                    rows.append([_button("Claim Star · " + obj["id"], "star", event_id, obj["id"])])
            if stars["unclaimed"]:
                rows.insert(0, [_button("Claim All Stars", "stars", event_id)])
        elif section == "rewards":
            if view["tier"]:
                lines.append(f'{view["tier"].title()} tier · fixed at first eligibility')
            for milestone in view["milestones"]:
                status = "Unlocked" if view["stars"]["claimed"] >= milestone["stars"] else "Locked"
                lines.append(f'{milestone["stars"]} ★ · {status}')
            for reward in view["rewards"][-12:]:
                assets = ", ".join(f"{amount} {key.replace('_', ' ')}" for key, amount in reward["assets"].items())
                lines.append(f'{assets} · {reward["status"].title()} · {_when(reward["expires_at"], view["timezone"])}')
                if reward["status"] == "claimable":
                    rows.append([_button("Claim Reward", "wallet", event_id, reward["id"])])
            if any(r["status"] == "claimable" for r in view["rewards"]):
                rows.insert(0, [_button("Claim All Rewards", "wallet", event_id)])
        else:
            upcoming = next((f for f in view["fixtures"] if f["personal"] and f["result"] is None), None)
            if upcoming:
                lines.append("Next match")
                lines.append(_fixture_line(upcoming, view["timezone"]))
                if upcoming["can_start"] or upcoming["status"] == "in_progress":
                    rows.append([_button("Next Match", "fixture", upcoming["id"])])
            for group in (("Fixtures", "Standings", "Squad"), ("Objectives", "Rewards", "Statistics")):
                section_codes = {"standings": "table", "statistics": "stats"}
                rows.append([_button(name, "panel", user.telegram_id, event_id, section_codes.get(name.lower(), name.lower()), 0)
                             for name in group])
        if section:
            rows.append([_button("Back to event", "panel", user.telegram_id, event_id, "home", 0)])
    hub = miniapp_button("Open in Mini App", f"events_{event_id}", is_private=private)
    if hub:
        rows.append([hub])
    return "\n".join(lines), rows, None


def _fixture_line(fixture, zone):
    home, away = fixture["home"] or "Qualifier", fixture["away"] or "Qualifier"
    result = fixture["result"]
    outcome = escape(result["winner"]) + " won" if result and result["winner"] else fixture["status"].replace("_", " ")
    return (f'R{fixture["round"]} · {escape(home)} v {escape(away)} · {outcome} · '
            f'Opens {_when(fixture["opens_at"], zone)} · Deadline {_when(fixture["deadline"], zone)}')


def _lobby_panel(lobby, now, private, invite=None):
    from services.event_studio_schedule import utc_instant

    room = lobby["id"]
    lines = [f'<b>{escape(lobby["name"])} · Multiplayer lobby</b>']
    rows = []
    if lobby["status"] == "lobby":
        left = max(0, int((utc_instant(lobby["join_until"], "UTC") - now).total_seconds()))
        lines.append(f'Registration closes {_when(lobby["join_until"], lobby["timezone"])} · {left // 60}m {left % 60}s left')
        lines.append(f'{lobby["ai_count"]} AI managers if started now')
        for team in lobby["teams"]:
            lines.append(f'{escape(team["name"])} · {escape(team["owner"] or "Open")}')
        access = invite or "-"
        if lobby["can_join"]:
            rows.extend([_button(team["name"], "seat", room, idx, access)]
                        for idx, team in enumerate(lobby["teams"]) if team["owner"] is None)
            # Group cards are shared. Authorization for host-only actions is server-side.
            if lobby["my_team"] and not lobby["is_host"] or not private:
                rows.append([_button("Release my team", "release", room, access)])
        if lobby["is_host"] or not private:
            if lobby["can_join"] and lobby["early_start"]:
                rows.append([_button("Start Now", "ask_start", room, lobby["version"])])
            rows.append([_button("Close lobby", "ask_cancel", room, lobby["version"])])
        if lobby["is_host"] and private and lobby["invite"]:
            code = lobby["invite"]
            lines.append(f'Lobby code <code>{code}</code>')
            if lobby["invite_url"]:
                from urllib.parse import urlencode
                share = urlencode({"url": lobby["invite_url"], "text": "Join my CCM26 Live Event"})
                rows.append([InlineKeyboardButton("Invite friends", url=f"https://t.me/share/url?{share}")])
        rows.append([_button("Refresh lobby", "room", room, access)])
    else:
        lines.append("Tournament started" if lobby["status"] in ("running", "completed") else "Lobby closed")
        if lobby["status"] in ("running", "completed"):
            rows.append([_button("Open tournament", "dash", lobby["event_id"])])
    return "\n".join(lines), rows, None


def _setup_panel(panel, selected=()):
    row, version = panel["id"], panel["version"]
    team = panel["team"]
    lines = [f'<b>{escape(team["name"])} v {escape(panel["opponent"])}</b>']
    controls = []
    def control(text, verb, value="-"):
        return _button(text, "setup", row, version, verb, value)
    toss = panel["toss"]
    if not panel["ready"]:
        controls.append([control("Ready", "ready")])
    elif not panel["all_ready"]:
        lines.append("Waiting for the other captain.")
    elif not toss and panel["can_call"]:
        controls.append([control("Heads", "coin", "heads"), control("Tails", "coin", "tails")])
    elif not toss:
        lines.append("Waiting for the coin call.")
    elif not toss["decision"] and panel["won_toss"]:
        lines.append(f'🪙 {toss["coin"].title()} · You won the toss.')
        controls.append([control("🏏 Bat First", "elect", "bat"), control("🎳 Bowl First", "elect", "bowl")])
    elif not toss["decision"]:
        lines.append("Waiting for the toss winner.")
    elif not panel["confirmed_xi"]:
        lines.append(f'Toss winner chose to {toss["decision"]}. Select your Playing XI in batting order.')
        controls.append([control("Confirm XI" if panel["fixed_xi"] else "Use suggested XI", "default")])
        if not panel["fixed_xi"]:
            players = team["squad"]
            choices = [control(f'{"✅ " if p["roster_id"] in selected else ""}{idx + 1}. {p["name"]}',
                               "toggle", p["roster_id"]) for idx, p in enumerate(players)]
            controls.extend(choices[i:i + 2] for i in range(0, len(choices), 2))
            lines.append(f'{len(selected)}/11 selected')
            if len(selected) == 11:
                controls.append([control("✅ Confirm XI", "xi")])
    elif panel["all_confirmed"]:
        controls.append([control("Start Match", "start")])
    else:
        lines.append("Waiting for the other Playing XI.")
    if panel["confirmed_xi"] and not panel["fixed_xi"]:
        controls.append([control("Edit XI", "edit")])
    controls.append([_button("Refresh fixture", "fixture", row)])
    return "\n".join(lines), controls, None


def _match(db, user, fixture_id, now, *, private, manage=False):
    from handlers.event_studio import _match_panel

    row, room, event, config = play._fixture(db, fixture_id)
    actor = play._fixture_player(db, room, row, user)
    if not row.state:
        raise ValueError("This fixture has no live checkpoint.")
    result = db.get(StudioFixtureResult, row.id)
    state = {**row.state, "is_private": private,
             "competition_batting_callback": f"ec:bat:{row.id}:{row.version}:n"}
    status = play._fixture_view(row, result, actor, config, now, event, True)
    turn = play._actor(state)
    allowed = turn in (actor.team_key, None) or not private
    choices = play.cricket.public_choices(state, turn or actor.team_key) if allowed and not state.get("event_paused") and not result else None
    status.update(score={"choices": choices}, player_paused=bool(state.get("event_paused")),
                  event_paused=event.status == "paused", play_mode=state["play_mode"], attempt=1)
    summary = {"name": event.name, "id": event.id, "timezone": config["timezone"], "format": config["format"],
               "games": [], "rewards": []}
    public_id = f"competition-{row.id}"

    def button(label, verb, *parts):
        if verb == "pick":
            return _button(label, "pick", row.id, row.version, *parts[-2:])
        if verb == "show":
            return _button(label, "dash", event.id)
        if verb == "match":
            return _button(label, "fixture", row.id)
        return _button(label, verb, row.id, row.version)

    panel = _match_panel(summary, status, state, button=button, public_id=public_id, manage=manage)
    if manage:
        return panel[0], panel[1], None
    text, controls, blocks = panel
    if not choices and not result and not state.get("event_paused"):
        controls = [[_button("Refresh fixture", "fixture", row.id)]]
    return text, controls, blocks


def _dispatch(telegram_id, chat_id, private, parts, drafts):
    from handlers.event_studio import _user

    now = datetime.utcnow()
    with get_session() as db:
        user = _user(db, telegram_id)
        action = parts[0]
        if action in ("browser", "open", "close", "teams", "confirm_team", "create", "invite_help", "panel"):
            if int(parts[1]) != telegram_id:
                raise ValueError("Open your own /event menu.")
        if action == "browser":
            return _browser(db, user, telegram_id, int(parts[2]), now, private)
        if action == "close":
            return "Live Events closed.", [], None
        if action == "invite_help":
            return "Send /eventlobby followed by the lobby code, or open the invitation link.", [], None
        if action in ("open", "dash", "panel"):
            event_id = int(parts[2] if action != "dash" else parts[1])
            event, config = legacy._published(db, event_id)
            if not goals.tournament(config):
                from handlers.event_studio import _event_panel
                if private:
                    text, buttons = _event_panel(legacy.detail(db, user, event_id, now))
                    return escape(text), buttons, None
                link = miniapp_button("Open in Mini App", f"events_{event_id}", is_private=False)
                return "Open this Live Event in the Mini App or in a private chat.", [[link]] if link else [], None
            return event_panel(db, user, event_id, now, private=private,
                               section=parts[3] if action == "panel" else None,
                               page=int(parts[4]) if action == "panel" else 0)
        if action in ("teams", "confirm_team", "create"):
            event_id, mode = int(parts[2]), parts[3]
            view = play.dashboard(db, user, event_id, now)
            if action == "teams":
                rows = [[_button(team["name"], "confirm_team", telegram_id, event_id, mode, idx)]
                        for idx, team in enumerate(view["teams"]) if team["eligible"]]
                rows.append([_button("Back", "open", telegram_id, event_id, 0)])
                return "Choose your franchise.", rows, None
            index = int(parts[4])
            team = view["teams"][index]
            if action == "confirm_team":
                lines = [f'<b>{escape(team["name"])}</b> · {team["ovr"]} OVR']
                if team["venue"]:
                    lines.append(escape(team["venue"]))
                lines.extend(f'{escape(p["name"])} · {escape(p["category"])} · {p["rating"]}' for p in team["squad"])
                rows = [[_button("Confirm franchise", "create", telegram_id, event_id, mode, index)],
                        [_button("Back", "teams", telegram_id, event_id, mode)]]
                blocks = None
                if team["crest"]:
                    from services.rich_layout import lines_from_html
                    blocks = [{"type": "photo", "photo": {"type": "photo", "media": team["crest"]}}, *(rich.paragraph(line) for line in lines_from_html("\n".join(lines)))]
                return "\n".join(lines), rows, blocks or None
            play.create(db, user, event_id, mode, team["key"], now, chat_id=None if private else chat_id, clock=datetime.utcnow)
            db.commit()
            return event_panel(db, user, event_id, now, private=private)
        if action in ("room", "seat", "release", "invite", "start", "cancel", "ask_start", "ask_cancel"):
            invite = parts[1] if action == "invite" else parts[-1] if action in ("room", "seat", "release") and parts[-1] != "-" else None
            room = play._by_invite(db, invite) if action == "invite" else int(parts[1])
            view = play.lobby(db, user, room, invite=invite, chat_id=chat_id if not private else None, now=now)
            if action.startswith("ask_"):
                if not view["is_host"]:
                    raise ValueError("Only the lobby host can do that.")
                verb = action[4:]
                text = f'Start with {view["ai_count"]} AI managers?' if verb == "start" else "Close registration and release all seats?"
                return text, [[_button("Confirm", verb, room, view["version"]),
                               _button("Back", "room", room, "-")]], None
            if action in ("seat", "release", "start", "cancel"):
                team = view["teams"][int(parts[2])]["key"] if action == "seat" else None
                view = play.change_lobby(db, user, room, "claim" if action == "seat" else action, now,
                                         team=team, invite=invite, chat_id=chat_id if not private else None,
                                         version=int(parts[2]) if action in ("start", "cancel") else None, clock=datetime.utcnow)
                db.commit()
            return _lobby_panel(view, now, private, invite=invite)
        if action in ("stars", "star", "wallet"):
            if action == "wallet" and not private:
                raise ValueError("Claim wallet rewards in your private chat or Mini App.")
            event_id = int(parts[1])
            play.claim(db, user, event_id, now, objective_id=parts[2] if action == "star" else None,
                       reward_id=int(parts[2]) if action == "wallet" and len(parts) > 2 else None, wallet=action == "wallet")
            db.commit()
            return event_panel(db, user, event_id, now, private=private,
                               section="rewards" if action == "wallet" else "objectives")
        fixture = int(parts[1])
        if action in ("fixture", "setup"):
            model, _, _, _ = play._fixture(db, fixture)
            if model.state:
                return _match(db, user, fixture, now, private=private)
            current = play.setup(db, user, fixture, now)
            local = drafts.setdefault(fixture, [])
            if action == "setup":
                version, verb, value = int(parts[2]), parts[3], parts[4]
                if verb == "toggle":
                    rid = int(value)
                    allowed = {p["roster_id"] for p in current["team"]["squad"]}
                    if rid not in allowed:
                        raise ValueError("Choose a player from your franchise.")
                    if rid in local:
                        local.remove(rid)
                    elif len(local) < 11:
                        local.append(rid)
                else:
                    actual = "xi" if verb == "default" else verb
                    selection = current["default_xi"] if verb == "default" else local if verb == "xi" else value
                    try:
                        current = play.setup(db, user, fixture, now, action=actual, version=version,
                                             value=selection, source="chat")
                    except studio.ExecutionUnavailable:
                        db.commit()
                        raise
                    db.commit()
                    if current["status"] == "in_progress":
                        drafts.pop(fixture, None)
                        return _match(db, user, fixture, now, private=private)
            return _setup_panel(current, local)
        version = int(parts[2]) if len(parts) > 2 else None
        if action == "manage":
            return _match(db, user, fixture, now, private=private, manage=True)
        if action == "confirm":
            return "Forfeit this official fixture?", [[_button("Confirm abandon", "abandon", fixture, version),
                                                        _button("Back", "fixture", fixture)]], None
        if action == "bat":
            return _batting(db, user, fixture, version, parts[3:], now, private)
        if action == "impact":
            return _impact(db, user, fixture, version, parts[3:], now, private)
        if action == "pick":
            kinds = {"a": picks.PICK_BAT_APPROACH, "b": picks.PICK_BOWLER, "p": picks.PICK_BOWL_APPROACH,
                     "n": picks.PICK_NEW_BATSMAN, "c": "continue"}
            name, value = kinds[parts[3]], int(parts[4])
        elif action in ("pause", "resume", "switch", "abandon"):
            name, value = action, None
        else:
            raise ValueError("This control is no longer available.")
        try:
            play.action(db, user, fixture, version, name, value, now, source="chat")
            db.commit()
        except studio.ExecutionUnavailable:
            db.commit()
            raise
        return _match(db, user, fixture, now, private=private)


def _impact(db, user, fixture, version, values, now, private):
    from services import impact_player
    from handlers.cipl_play import _impact_label, impact_batting_slots
    row, room, _, _ = play._fixture(db, fixture)
    actor = play._fixture_player(db, room, row, user)
    if row.version != version or row.status != "in_progress":
        raise studio.Conflict("The match changed. Reopen the Impact Player picker.")
    opts = impact_player.cipl_options(row.state, actor.team_key, row.state["event_next_action"])
    if not row.state.get("event_impact") or not opts["can_use"]:
        raise ValueError("Impact Player is unavailable at this stage.")
    choose = lambda label, *extra: _button(label, "impact", fixture, version, *extra)
    if not values:
        rows = [[choose(_impact_label(p), p["roster_id"])] for p in opts["replaceable_players"]]
        text = "Who comes off?"
    elif len(values) == 1:
        outgoing = int(values[0])
        eligible = impact_player.cipl_incoming_for(row.state, opts["side"], outgoing, opts["incoming_options"])
        rows = [[choose(_impact_label(p), outgoing, p["roster_id"])] for p in eligible]
        text = "Who comes on?"
    elif len(values) == 2:
        outgoing, incoming = map(int, values)
        rows = [[choose(f"#{index + 1} · {label}", outgoing, incoming, index)]
                for index, label in impact_batting_slots(row.state, opts["side"], outgoing)]
        text = "Where should the substitute bat?"
    else:
        outgoing, incoming, position = map(int, values)
        play.action(db, user, fixture, version, "impact", {"inRosterId": incoming, "outRosterId": outgoing,
                                                          "batPosition": position}, now, source="chat")
        db.commit()
        return _match(db, user, fixture, now, private=private)
    return text, [*rows, [_button("Cancel", "fixture", fixture)]], None



def _batting(db, user, fixture, version, values, now, private):
    from handlers.live_batting import _menu
    from services import live_batting

    row, room, _, _ = play._fixture(db, fixture)
    actor = play._fixture_player(db, room, row, user)
    if not row.state or row.version != version or row.status != "in_progress":
        raise studio.Conflict("The match changed. Reopen Manage batting.")
    if row.state["bat_team_id"] != actor.team_key or row.state["play_mode"] != "chat" or row.state.get("event_paused"):
        raise ValueError("Only the active batting captain may use this menu.")
    options = live_batting.options(row.state, row.state["event_next_action"], can_control=True)
    if not options["canManage"]:
        raise ValueError(options["message"])
    step = values[0]
    if step in ("r", "o"):
        first, second = map(int, values[1:])
        data = {"token": options["token"]}
        if step == "r":
            name = "retire_batsman"
            data.update(outRosterId=first, inRosterId=second)
        else:
            name = "live_batting_order"
            order = [p["id"] for p in options["waiting"]]
            if min(first, second) < 0 or max(first, second) >= len(order) or abs(first - second) != 1:
                raise ValueError("Invalid batting position.")
            order[first], order[second] = order[second], order[first]
            data["rosterIds"] = order
        row = play.action(db, user, fixture, version, name, data, now, source="chat")
        db.commit()
        options = live_batting.options(row.state, row.state["event_next_action"], can_control=True)
    def picker(label, action, *parts):
        if action == "x":
            return _button(label, "fixture", fixture)
        return _button(label, "bat", fixture, row.version, action, *parts[1:])
    text, keyboard = _menu(fixture, options, outgoing=int(values[1]) if step == "p" else None, button=picker)
    return text, [list(line) for line in keyboard.inline_keyboard], None


async def send_timeout_prompt(bot, fixture):
    from models import User
    from models_events import StudioParticipant

    def load():
        with get_session() as db:
            row, room, event, _ = play._fixture(db, fixture)
            if row.state["play_mode"] != "chat" or row.state.get("event_paused") or event.status == "paused":
                return None
            team = play._actor(row.state)
            entrant = db.scalar(select(StudioParticipant).filter_by(competition_id=room.id, team_key=team))
            if not entrant or not entrant.user_id:
                return None
            captain = db.get(User, entrant.user_id)
            if captain.is_banned or captain.dm_blocked and room.chat_id is None:
                return None
            destination = room.chat_id or captain.telegram_id
            return destination, _match(db, captain, fixture, datetime.utcnow(), private=room.chat_id is None)

    try:
        response = await asyncio.to_thread(load)
        if response:
            chat, (text, buttons, blocks) = response
            await rich.send_rich_message(bot, chat, blocks, text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        logger.exception("Unable to send the next prompt for competition fixture %s", fixture)

async def _show(update, context, parts, *, edit=False):
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user:
        return
    try:
        text, buttons, blocks = await asyncio.to_thread(
            _dispatch, user.id, chat.id, chat.type == "private", parts, context.user_data.setdefault("competition_xi", {}))
        markup = InlineKeyboardMarkup(buttons)
        room_button = next((b.callback_data for row in buttons for b in row
                            if (b.callback_data or "").startswith("ec:room:")), None)
        if chat.type != "private" and room_button and parts[0] == "create":
            sent = await rich.send_rich_message(context.bot, chat.id, blocks, text, reply_markup=markup)
            if sent is not None:
                await asyncio.to_thread(_save_message, int(room_button.split(":")[2]), chat.id, sent.message_id)
            return
        if edit:
            message = update.callback_query.message
            if parts[0] == "setup" and parts[3] == "coin":
                from services.match_broadcast import animate_coin_toss
                await animate_coin_toss(lambda frame: update.callback_query.edit_message_text(frame, parse_mode="HTML"))
            if blocks and await rich.edit_rich_message(context.bot, chat.id, message.message_id, blocks,
                                                       fallback_text=text, reply_markup=markup):
                return
            try:
                await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    if "no text" not in str(exc).lower():
                        raise
                    await rich.send_rich_message(context.bot, chat.id, blocks, text, reply_markup=markup)
        else:
            sent = await rich.send_rich_message(context.bot, chat.id, blocks, text, reply_markup=markup)
            if parts[0] == "room" and chat.type != "private" and sent is not None:
                await asyncio.to_thread(_save_message, int(parts[1]), chat.id, sent.message_id)
    except (studio.Conflict, studio.ExecutionUnavailable, ValueError) as exc:
        await update.effective_message.reply_text(str(exc))
    except (IndexError, KeyError):
        logger.exception("Live Events control could not render: %r", parts)
        await update.effective_message.reply_text("That control is unavailable. Reopen /event.")
    except Exception:
        logger.exception("Live Events screen failed: %r", parts)
        await update.effective_message.reply_text("The event screen could not load. Try /event again.")


def _save_message(room, chat_id, message_id):
    with get_session() as db:
        play.save_lobby_message(db, room, chat_id, message_id)
        db.commit()


async def command(update, context):
    name = update.effective_message.text.split()[0].split("@")[0].lstrip("/").lower()
    args = context.args or []
    telegram_id = update.effective_user.id
    if name == "eventlobby":
        if args:
            await _show(update, context, ["invite", args[0]])
        else:
            def hosting_room():
                with get_session() as db:
                    return db.scalar(select(StudioCompetition.id).where(
                        StudioCompetition.chat_id == update.effective_chat.id,
                        StudioCompetition.status.in_(("lobby", "running"))).order_by(StudioCompetition.id.desc()))
            room = await asyncio.to_thread(hosting_room) if update.effective_chat.type != "private" else None
            if room:
                await _show(update, context, ["room", str(room), "-"])
            else:
                await update.effective_message.reply_text("Use /eventlobby followed by your lobby code, or open /event.")
    elif name in SECTIONS:
        def latest():
            from handlers.event_studio import _user
            from models_events import StudioParticipant

            with get_session() as db:
                user = _user(db, telegram_id)
                return db.scalar(select(StudioParticipant.event_id).where(
                    StudioParticipant.user_id == user.id).order_by(StudioParticipant.joined_at.desc()).limit(1))
        try:
            event_id = int(args[0]) if args and args[0].isdigit() else await asyncio.to_thread(latest)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        if event_id:
            await _show(update, context, ["panel", str(telegram_id), str(event_id), SECTIONS[name], "0"])
        else:
            await _show(update, context, ["browser", str(telegram_id), "0"])
    else:
        await _show(update, context, ["browser", str(telegram_id), "0"])


async def deep_link(update, context, code):
    await _show(update, context, ["invite", code])


async def match_command(update, context, match, action):
    row = str(match)
    if action in ("match", "manage"):
        await _show(update, context, ["fixture" if action == "match" else "manage", row])
    else:
        def version():
            with get_session() as db:
                fixture, _, _, _ = play._fixture(db, match)
                return fixture.version
        try:
            current = await asyncio.to_thread(version)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        verb = {"clear": "confirm", "chat": "switch"}.get(action, action)
        await _show(update, context, [verb, row, str(current)])


async def callback(update, context):
    query = update.callback_query
    await query.answer()
    await _show(update, context, query.data.split(":")[1:], edit=True)
