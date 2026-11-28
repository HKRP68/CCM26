"""Telegram browser, shared group lobby and fixture views for Live Events."""

import asyncio
from datetime import datetime
from hmac import compare_digest, digest as hmac_digest
from html import escape
from secrets import token_bytes
import logging

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from database import get_session
from models_events import StudioCompetition, StudioFixtureResult, StudioParticipant
from services import cipl_picks as picks, event_studio as studio, event_studio_competition as play
from services import event_studio_objectives as goals, event_studio_play as legacy, rich_message as rich
from services.economy_transactions import reserve_miniapp_action
from services.event_studio_award_media import LABELS as AWARDS
from services.miniapp_buttons import miniapp_button

logger = logging.getLogger(__name__)
OWNER_ACTIONS = frozenset({"browser", "open", "close", "teams", "confirm_team", "create", "invite_help",
                           "panel", "joined", "next_fixture"})
_LOCAL_CONTROL_SECRET = token_bytes(32)
SECTIONS = {
    "myevent": "home", "eventfixtures": "fixtures", "eventobjectives": "objectives", "eventtable": "table",
    "eventqualification": "table", "eventstats": "stats", "eventrewards": "rewards", "eventpass": "rewards",
    "eventclaim": "claim", "eventnews": "news", "eventhistory": "history", "eventawards": "history",
    "trophies": "history", "manager": "manager",
}


def _owner_token(telegram_id):
    from config import BOT_TOKEN

    secret = BOT_TOKEN.encode() if BOT_TOKEN else _LOCAL_CONTROL_SECRET
    return hmac_digest(secret, f"live-event-owner:{telegram_id}".encode(), "sha256").hex()[:16]


def _is_owner(control, telegram_id):
    # Previously sent controls carried the numeric owner. Keep them usable.
    return control.isascii() and (compare_digest(control, _owner_token(telegram_id)) or control == str(telegram_id))


def _button(label, *parts):
    action, *arguments = parts
    if action in OWNER_ACTIONS:
        arguments[0] = _owner_token(arguments[0])
    return InlineKeyboardButton(label, callback_data="ec:" + ":".join(map(str, (action, *arguments))))


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
                     for obj in goals.active_rows(config)[:4])
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
    if not private and section in ("rewards", "claim", "manager"):
        link = miniapp_button("Open your rewards", f"events_{event_id}", is_private=False)
        return "Open this section in a private chat or the Mini App.", [[link]] if link else [], None
    view = play.dashboard(db, user, event_id, now,
                          news_page=max(0, page) if section == "news" else 0, news_limit=6)
    if not private and (view.get("room", {}).get("features", {}).get("visibility") == "private"):
        link = miniapp_button("Open your event", f"events_{event_id}", is_private=False)
        return "Open your private tournament in the Mini App or in a private chat.", [[link]] if link else [], None
    rows, lines = [], [f'<b>{escape(view["name"])}</b>']
    if section == "home":
        section = None
    def section_button(label, section, page=0):
        return _button(label, "panel", user.telegram_id, event_id, section, page)

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
            for series in view.get("tour_series", []):
                status = series["winner_name"] or ("Drawn" if series["complete"] else "Upcoming")
                lines.append(f'{escape(series["country"])} · {escape(status)}')
            if view.get("qualification"):
                lines.append("Playoff qualification")
                prediction = view.get("qualification_estimate")
                for row in view["qualification"]:
                    chance = prediction["teams"].get(row["team"]) if prediction else None
                    hint = f' · {chance["percent"]}% estimated' if chance else ""
                    lines.append(f'{escape(row["name"])} · {row["status"].title()} · max {row["max_points"]} pts{hint}')
                scenario = view.get("qualification_scenario")
                if scenario:
                    status = "guarantees qualification" if scenario["guaranteed"] else "depends on other results or tiebreakers"
                    lines.append(f'Win {scenario["wins_needed_for_scenario"]} remaining league matches for {scenario["points"]} pts · {status}')
                if prediction:
                    lines.append(escape(prediction["model"]))
        elif section == "squad":
            lines.extend(f'{escape(p["name"])} · {escape(p["category"])} · {p["rating"]}' for p in team["squad"])
            dressing = view.get("dressing_room")
            if dressing:
                unity = dressing["unity"]
                lines.append(f'Morale {dressing["morale"]}/100 · Unity {unity if unity is not None else "—"}')
                lines.append("Recent form · last five appearances per player")
                for player in dressing["players"][:12]:
                    if player["appearances"]:
                        lines.append(f'{escape(player["name"])} · {player["appearances"]} apps · {player["runs"]} runs · {player["wickets"]} wkts')
        elif section == "news":
            if "news" not in view:
                lines.append("News is unavailable for this event.")
            elif not view["news"]["items"]:
                lines.append("Match reports appear here once fixtures settle.")
            else:
                feed = view["news"]
                for story in feed["items"]:
                    lines.extend((f'<b>{escape(story["title"])}</b>', escape(story["summary"])))
                if page > 0:
                    rows.append([section_button("Previous", "news", page - 1)])
                if feed["has_more"]:
                    rows.append([section_button("Next", "news", page + 1)])
        elif section == "history":
            honour = view.get("honour_board")
            if honour and honour.get("champion_name"):
                lines.append(f'Champion · {escape(honour["champion_name"])}')
                if honour.get("runner_up"):
                    lines.append(f'Runner-up · {escape(honour["runner_up"])}')
            if honour and honour["awards"]:
                for metric, winners in honour["awards"].items():
                    for player in winners[:3]:
                        lines.append(f'{AWARDS.get(metric, metric)} · {escape(player["name"])} · {escape(str(player["value"]))}')
            else:
                lines.append("No finalized awards are available for this event.")
            if honour and honour.get("media_ready"):
                if private and honour.get("champion_name"):
                    rows.append([_button("View champion graphic", "award_art", event_id)])
                lines.append("Open the Mini App for player award graphics.")
            if private:
                for edition in view.get("dynasty_history", [])[:6]:
                    title = " · Champion" if edition["title"] else ""
                    lines.append(f'{escape(edition["event"])} · {edition["wins"]}/{edition["played"]} wins{title}')
            if private and view.get("trophies"):
                lines.append("Trophy cabinet")
                lines.extend(f'{escape(t["event"])} · {escape(t["team"])} · {t["mode"].title()} {t["format"]}'
                             for t in view["trophies"][:8])
        elif section == "manager":
            profile = view.get("manager")
            if not profile:
                lines.append("Manager profiles are unavailable for this event.")
            else:
                lines.append(f'{profile["rank"]} · {profile["xp"]} Manager XP')
                for mode in profile["modes"]:
                    lines.append(f'{mode["mode"].title()} {mode["format"]} · {mode["wins"]}W {mode["losses"]}L {mode["ties"]}T · {mode["titles"]} titles')
                    if mode["win_rate"] is not None:
                        lines.append(f'{mode["win_rate"]}% wins over {mode["completed_matches"]} completed matches')
                for rating in profile["ratings"]:
                    lines.append(f'{rating["format"]} PvP {rating["strength_band"]} · {rating["rating"]} · {rating["games"]} matches'
                                 + (" · Provisional" if rating["provisional"] else ""))
        elif section == "claim":
            rows.append([section_button("Claim Stars", "objectives"), section_button("Claim Rewards", "rewards")])
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
            start = max(0, page) * 6
            for obj in stars["objectives"][start:start + 6]:
                lines.append(f'{escape(obj["title"])} · {obj["measured"]}/{obj["target"]} · {obj["stars"]} ★ · {statuses[obj["status"]]}')
                if obj["status"] == "ready" and private:
                    lines.append(f'Claim by {_when(obj["expires_at"], view["timezone"])}')
                    rows.append([_button("Claim Star · " + obj["id"], "star", event_id, obj["id"])])
            if start:
                rows.append([section_button("Previous", "objectives", page - 1)])
            if start + 6 < len(stars["objectives"]):
                rows.append([section_button("Next", "objectives", page + 1)])
            if stars["unclaimed"] and private:
                rows.insert(0, [_button("Claim All Stars", "stars", event_id)])
        elif section == "rewards":
            if view["tier"]:
                lines.append(f'{view["tier"].title()} tier · fixed at first eligibility')
            road = view.get("event_pass")
            if road:
                lines.append(f'{road["claimed"]} claimed stars · Claims close {_when(road["expires_at"], view["timezone"])}')
                lines.append("Diamond replaces Free at the time a milestone unlocks.")
            for milestone in (road or view)["milestones"]:
                status = "Unlocked" if view["stars"]["claimed"] >= milestone["stars"] else "Locked"
                lines.append(f'{milestone["stars"]} ★ · {milestone.get("state", status).title()}')
                for tier, items in milestone.get("item_preview", {}).items():
                    if items:
                        lines.append(f'{tier.title()} · ' + escape(", ".join(items)))
            for reward in view["rewards"][-12:]:
                parts = [f"{amount} {key.replace('_', ' ')}" for key, amount in reward["assets"].items()] + reward.get("items", [])
                assets = escape(", ".join(parts))
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
            optional = [section_button(label, code) for key, label, code in (
                ("news", "News", "news"), ("honour_board", "Honour Board", "history"),
                ("manager", "Manager", "manager")) if key in view]
            if "dynasty_history" in view and "honour_board" not in view:
                optional.append(section_button("Franchise history", "history"))
            if optional:
                rows.append(optional)
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
        features = lobby.get("features", {})
        if features.get("ready"):
            lines.append(f'{features["visibility"].title()} · {features["ready_count"]} ready')
            if features["host"]:
                lines.append(f'Host · {escape(features["host"])}')
        if lobby["can_join"]:
            if features.get("mystery"):
                if not lobby["my_team"]:
                    rows.append([_button("Join with random franchise", "mseat", room, access)])
            else:
                rows.extend([_button(team["name"], "seat", room, idx, access)]
                            for idx, team in enumerate(lobby["teams"]) if team["owner"] is None)
            # Group cards are shared. Authorization for host-only actions is server-side.
            if lobby["my_team"] and features.get("ready"):
                mine = next(t for t in lobby["teams"] if t["key"] == lobby["my_team"])
                rows.append([_button("Cancel ready" if mine["ready"] else "Mark ready",
                                     "unready" if mine["ready"] else "ready", room, lobby["version"])])
                if lobby["is_host"] and private:
                    for idx, team in enumerate(lobby["teams"]):
                        if team["owner"] and team["key"] != lobby["my_team"]:
                            rows.append([_button(f'Transfer offer · {team["name"]}', "offer_host", room, lobby["version"], idx)])
                if features["transfer_pending"]:
                    rows.append([_button("Accept host role", "accept_host", room, lobby["version"])])
            if not features.get("mystery") and ((lobby["my_team"] and not lobby["is_host"]) or not private):
                rows.append([_button("Release my team", "release", room, access)])
        if lobby["is_host"] or not private:
            if lobby["can_join"] and lobby["early_start"] and sum(bool(t["owner"]) for t in lobby["teams"]) >= lobby["minimum"]:
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
    if panel.get("ai_manager"):
        profile = panel["ai_manager"]
        lines.append(escape(f'{profile["name"]} · {profile["summary"]}'))
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
        if action in OWNER_ACTIONS and not _is_owner(parts[1], telegram_id):
            raise ValueError("Open your own /event menu.")
        if action in ("create", "seat", "mseat", "release", "start", "cancel", "ready", "unready", "offer_host",
                      "accept_host", "stars", "star", "wallet"):
            delay = reserve_miniapp_action(db, user)
            db.commit()
            if delay:
                raise ValueError(f"Try again in {delay}s.")
        if action == "browser":
            return _browser(db, user, telegram_id, int(parts[2]), now, private)
        if action == "close":
            return "Live Events browser closed.", [], None
        if action == "invite_help":
            rooms = play.public_lobbies(db, user, now)
            if rooms:
                buttons = [[_button(item["name"], "room", item["id"], "-")] for item in rooms[:8]]
                return "Choose a public lobby or send /eventjoin with your invitation code.", buttons, None
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
            if view.get("template") == "mystery":
                if action == "teams":
                    return "Your franchise is assigned when you join and stays fixed.", [
                        [_button("Join with random franchise", "create", telegram_id, event_id, mode, "random")],
                        [_button("Back", "open", telegram_id, event_id, 0)]], None
                if action != "create" or parts[4] != "random":
                    raise ValueError("Confirm the Mystery Tournament assignment.")
                play.create(db, user, event_id, mode, None, now, consent=True,
                            chat_id=None if private else chat_id, clock=datetime.utcnow)
                db.commit()
                return event_panel(db, user, event_id, now, private=private)
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
        if action == "next_fixture":
            if not private:
                raise ValueError("Open your fixtures in a private chat.")
            event_id = int(parts[2])
            view = play.dashboard(db, user, event_id, now)
            fixture = next((f for f in view.get("fixtures", []) if f["personal"] and not f["result"]), None)
            if fixture and fixture["status"] == "in_progress":
                return _match(db, user, fixture["id"], now, private=True)
            return event_panel(db, user, event_id, now)
        if action == "joined":
            entries = [event for event in legacy.list_events(db, user, now)
                       if event["competition"] and event["joined"]]
            if not entries:
                return _browser(db, user, telegram_id, 0, now, private)
            section = parts[2]
            if len(entries) == 1:
                if section == "play":
                    view = play.dashboard(db, user, entries[0]["id"], now)
                    fixture = next((f for f in view.get("fixtures", []) if f["personal"] and not f["result"]), None)
                    if fixture and fixture["status"] == "in_progress" and private:
                        return _match(db, user, fixture["id"], now, private=True)
                    section = "home"
                return event_panel(db, user, entries[0]["id"], now, private=private, section=section)
            return "Choose a joined Live Event.", [
                [_button(entry["name"], "next_fixture", telegram_id, entry["id"]) if section == "play"
                 else _button(entry["name"], "panel", telegram_id, entry["id"], section, 0)]
                for entry in entries[:20]], None
        if action in ("room", "seat", "mseat", "release", "invite", "start", "cancel", "ask_start", "ask_cancel",
                      "ready", "unready", "accept_host", "offer_host"):
            invite = parts[1] if action == "invite" else parts[-1] if action in ("room", "seat", "mseat", "release") and parts[-1] != "-" else None
            room = play._by_invite(db, invite) if action == "invite" else int(parts[1])
            view = play.lobby(db, user, room, invite=invite, chat_id=chat_id if not private else None, now=now)
            if not private and view.get("features", {}).get("visibility") == "private":
                return "Open that invitation in a private chat or the Mini App.", [], None
            if action.startswith("ask_"):
                if not view["is_host"]:
                    raise ValueError("Only the lobby host can do that.")
                verb = action[4:]
                text = f'Start with {view["ai_count"]} AI managers?' if verb == "start" else "Close registration and release all seats?"
                return text, [[_button("Confirm", verb, room, view["version"]),
                               _button("Back", "room", room, "-")]], None
            if action in ("seat", "mseat", "release", "start", "cancel", "ready", "unready", "accept_host", "offer_host"):
                checked = 2 if action == "seat" else 3 if action == "offer_host" else None
                team = view["teams"][int(parts[checked])]["key"] if checked is not None else None
                view = play.change_lobby(db, user, room, "claim" if action in ("seat", "mseat") else action, now,
                                         team=team, invite=invite, chat_id=chat_id if not private else None,
                                         version=int(parts[2]) if action not in ("seat", "mseat", "release") else None,
                                         consent=action == "mseat", clock=datetime.utcnow)
                db.commit()
            return _lobby_panel(view, now, private, invite=invite)
        if action in ("stars", "star", "wallet"):
            if not private:
                raise ValueError("Open your private chat or the Mini App to claim stars and rewards.")
            event_id = int(parts[1])
            receipt = play.claim(db, user, event_id, now, objective_id=parts[2] if action == "star" else None,
                       reward_id=int(parts[2]) if action == "wallet" and len(parts) > 2 else None, wallet=action == "wallet", clock=datetime.utcnow)
            db.commit()
            panel = event_panel(db, user, event_id, now, private=private,
                                section="rewards" if action == "wallet" else "objectives")
            if receipt.get("wallet_receipts"):
                numbers = ", ".join(str(number) for number in receipt["wallet_receipts"][-12:])
                balances = " · ".join(f"{amount:,} {key.replace('_', ' ')}" for key, amount in receipt["balances"].items())
                items = receipt.get("items", [])
                awarded = "\n" + escape(", ".join(item["name"] for item in items[-8:])) if items else ""
                if any(item["kind"] == "pack" for item in items):
                    awarded += "\nOpen earned packs with /openpack."
                return f"{panel[0]}\nReceipts {numbers}\nWallet {balances}{awarded}", panel[1], panel[2]
            return panel
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
        if parts[0] == "award_art":
            if chat.type != "private":
                raise ValueError("Open the graphic in your private chat or Mini App.")
            photo = await asyncio.to_thread(_champion_photo, user.id, int(parts[1]))
            await update.effective_message.reply_photo(photo)
            return
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
    except studio.Conflict as exc:
        await update.effective_message.reply_text(str(exc))
        if parts[0] in ("seat", "start", "cancel", "ready", "unready", "accept_host", "offer_host"):
            await _show(update, context, ["room", parts[1], parts[-1] if parts[0] == "seat" else "-"], edit=edit)
    except (studio.ExecutionUnavailable, ValueError) as exc:
        await update.effective_message.reply_text(str(exc))
    except (IndexError, KeyError):
        logger.exception("Live Events control could not render: %s", parts[0])
        await update.effective_message.reply_text("That control is unavailable. Reopen /event.")
    except Exception:
        logger.exception("Live Events screen failed: %s", parts[0])
        await update.effective_message.reply_text("The event screen could not load. Try /event again.")


def _champion_photo(telegram_id, event_id):
    from base64 import b64decode
    from handlers.event_studio import _user
    from models_events import StudioAwardMedia

    with get_session() as db:
        user = _user(db, telegram_id)
        event = play._event(db, event_id)
        member = play.membership(db, event, user.id)
        media = db.get(StudioAwardMedia, member.competition_id) if member else None
        if not media or not media.images or "champion" not in media.images:
            raise ValueError("This championship graphic is not available.")
        return b64decode(media.images["champion"])


def _save_message(room, chat_id, message_id):
    with get_session() as db:
        play.save_lobby_message(db, room, chat_id, message_id)
        db.commit()


async def command(update, context):
    name = update.effective_message.text.split()[0].split("@")[0].lstrip("/").lower()
    args = context.args or []
    telegram_id = update.effective_user.id
    if name in ("eventlobby", "eventroom", "eventjoin"):
        if args:
            await _show(update, context, ["invite", args[0]])
        else:
            def hosting_room():
                with get_session() as db:
                    if update.effective_chat.type == "private":
                        from handlers.event_studio import _user
                        user = _user(db, telegram_id)
                        return db.scalar(select(StudioCompetition.id).join(StudioParticipant)
                            .where(StudioParticipant.user_id == user.id, StudioCompetition.status == "lobby")
                            .order_by(StudioCompetition.created_at.desc()).limit(1))
                    return db.scalar(select(StudioCompetition.id).where(
                        StudioCompetition.chat_id == update.effective_chat.id,
                        StudioCompetition.status == "lobby").order_by(StudioCompetition.id.desc()).limit(1))
            try:
                room = await asyncio.to_thread(hosting_room) if name != "eventjoin" else None
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc))
                return
            await _show(update, context, ["room", str(room), "-"] if room else
                        ["browser" if name == "eventjoin" else "invite_help", str(telegram_id), "0"])
    elif name == "eventhelp":
        await update.effective_message.reply_text(
            "/event — browse Live Events\n/myevent — open a joined event\n"
            "/eventjoin [code] — join an event or use an invite\n/eventroom — view your lobby\n"
            "/eventplay — open or resume a fixture\n"
            "/eventobjectives — view and claim stars\n/eventclaim — open claims in private\n"
            "Open your event dashboard to see which other sections are available.")
    elif name in SECTIONS:
        if args and args[0].isdigit():
            await _show(update, context, ["panel", str(telegram_id), args[0], SECTIONS[name], "0"])
        else:
            await _show(update, context, ["joined", str(telegram_id), SECTIONS[name]])
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
