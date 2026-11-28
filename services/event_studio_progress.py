"""Readonly stats, competitive standings and trophies derived from official results."""

from sqlalchemy import case, func, select

from models_events import StudioEnrollment, StudioGame, StudioResult
from services.event_studio_rules import is_moment
from services.event_studio_brackets import effective_final


def contribution(state, team):
    batted_second = state["bat_team_id"] == team
    bat_fields = ("bat_xi", "bat_stats") if batted_second else ("inn1_bat_xi", "inn1_bat_stats")
    bowl_fields = ("inn1_bowl_xi", "inn1_bowl_stats") if batted_second else ("bowl_xi", "bowl_stats")
    rows = {}
    moment = state.get("event_moment")
    prefix = state.get("event_preset_stats") or {}
    for kind, fields in (("batting", bat_fields), ("bowling", bowl_fields)):
        if moment:
            relevant = "batting" if batted_second else "bowling"
            if kind != relevant:
                continue
            fields = ("bat_xi", "bat_stats") if kind == "batting" else ("bowl_xi", "bowl_stats")
        for player in state.get(fields[0], []):
            rid = player["roster_id"]
            stat = (state.get(fields[1]) or {}).get(str(rid), {})
            row = rows.setdefault(rid, {"name": player["name"], "version": player["version"],
                                        "player_id": player["player_id"]})
            keys = ("runs", "balls", "fours", "sixes", "out", "batted") if kind == "batting" else (
                "runs", "balls", "wickets", "maidens", "dots")
            if kind == "bowling" and "dots" not in stat:
                keys = tuple(key for key in keys if key != "dots")
            baseline = prefix.get(kind, {}).get(str(rid), {})
            row[kind] = {key: max(0, int(stat.get(key) or 0) - int(baseline.get(key) or 0)) for key in keys}
    return list(rows.values())


def player_stats(rows):
    batting, bowling = {}, {}
    for _, result in rows:
        if not result or result.kind not in ("completed", "success", "failed"):
            continue
        for player in result.detail.get("performance", []):
            pid = player["player_id"]
            name = {"name": player["name"], "version": player["version"]}
            bat, bowl = player.get("batting", {}), player.get("bowling", {})
            if bat.get("batted") or bat.get("balls") or bat.get("out") or bat.get("runs"):
                entry = batting.setdefault(pid, {**name, "innings": 0, "runs": 0, "balls": 0,
                                                  "fours": 0, "sixes": 0, "out": 0, "high": 0})
                entry["innings"] += 1
                entry["high"] = max(entry["high"], bat["runs"])
                for key in ("runs", "balls", "fours", "sixes", "out"):
                    entry[key] += bat[key]
            if bowl.get("balls") or bowl.get("runs") or bowl.get("wickets"):
                entry = bowling.setdefault(pid, {**name, "innings": 0, "runs": 0, "balls": 0,
                                                  "wickets": 0, "maidens": 0, "best": [0, 0]})
                entry["innings"] += 1
                figures = [bowl["wickets"], bowl["runs"]]
                if entry["innings"] == 1 or (figures[0], -figures[1]) > (entry["best"][0], -entry["best"][1]):
                    entry["best"] = figures
                for key in ("runs", "balls", "wickets", "maidens"):
                    entry[key] += bowl[key]
    for row in batting.values():
        row["average"] = round(row["runs"] / row["out"], 2) if row["out"] else None
        row["strike_rate"] = round(100 * row["runs"] / row["balls"], 2) if row["balls"] else None
    for row in bowling.values():
        row["economy"] = round(6 * row["runs"] / row["balls"], 2) if row["balls"] else None
    return {"batting": sorted(batting.values(), key=lambda row: (-row["runs"], row["name"])),
            "bowling": sorted(bowling.values(), key=lambda row: (-row["wickets"], row["name"]))}


def world_tour(config, rows):
    """Distinct series standings over the canonical, sequential tour fixtures."""
    if config["schedule"].get("template") != "world_tour":
        return []
    from services.event_studio_play import table

    roster = sorted(rows, key=lambda item: next(row["round"] for row in config["fixtures"]
                                               if row["key"] == item[0].fixture_key))
    offset, output = 0, []
    for index, stop in enumerate(config["schedule"]["tour"], 1):
        selected = roster[offset:offset + stop["games"]]
        offset += stop["games"]
        boards = table(config, selected)
        complete = len(selected) == stop["games"] and all(
            result is not None and result.kind != "cancelled" for _, result in selected)
        ranked = [(row["points"], row["won"], row["nrr"]) for row in boards]
        winner = boards[0]["team"] if complete and ranked[0] > ranked[1] else None
        output.append({"series": index, "country": stop["country"], "games": stop["games"], "complete": complete,
                       "winner": winner, "winner_name": config["snapshot"][winner]["name"] if winner else None,
                       "points": {row["team"]: row["points"] for row in boards}})
    return output


def trophy(config, entry, rows, standings):
    kind = config["kind"]
    if kind != "live":
        for _, result in rows:
            if result and (result.kind == "success" if is_moment(config) else result.winner == entry.team_key):
                return "Moment completed" if kind == "moment" else "Boss defeated"
        return None
    tour = world_tour(config, rows)
    if tour:
        if not all(item["complete"] for item in tour):
            return None
        wins = {row["team"]: sum(item["winner"] == row["team"] for item in tour) for row in standings}
        champion = max(standings, key=lambda row: wins[row["team"]])
        if champion["team"] == entry.team_key and (champion["won"] or wins[entry.team_key]):
            return "World Tour champion"
        return None
    if config["schedule"]["preset"] == "survival":
        if rows and all(result and result.winner == entry.team_key for _, result in rows):
            return "Survival Cup winner"
        return None
    playoff = [row for row in config["fixtures"] if row["stage"] == "playoff" and row["key"] != "third-place"]
    if playoff:
        final = effective_final(config, rows)
        for game, result in rows:
            if game.fixture_key != final or not result:
                continue
            if result.winner == entry.team_key:
                return "Tournament champion"
            if result.kind == "completed" and result.winner is None and entry.team_key in (game.home, game.away):
                seed_order = ([row["team"] for row in standings] if any(
                    row["stage"] == "league" for row in config["fixtures"]) else [row["key"] for row in config["teams"]])
                if min((game.home, game.away), key=seed_order.index) == entry.team_key:
                    return "Tournament champion"
            return None
    elif rows and all(result and result.kind != "cancelled" for _, result in rows) and standings[0]["team"] == entry.team_key and standings[0]["won"]:
        return "League winner"
    return None


def leaderboard(db, entry, config, limit=25):
    live = config["kind"] == "live"
    enrolled = StudioEnrollment
    played = StudioResult.kind == "completed"
    won = case((StudioResult.winner == enrolled.team_key, 1), else_=0)
    success = case((StudioResult.kind == "success", 1), else_=0)
    stars = func.coalesce(StudioResult.detail["stars"].as_integer(), 0)
    wins = success if is_moment(config) else won
    victories = func.sum(wins) if live else func.max(wins)
    star_total = func.sum(stars) if live else func.max(stars)
    stmt = (select(enrolled.id, victories, star_total, func.count(StudioGame.id))
            .join(StudioGame, StudioGame.enrollment_id == enrolled.id)
            .join(StudioResult, StudioResult.game_id == StudioGame.id)
            .where(enrolled.event_id == entry.event_id, enrolled.revision == entry.revision,
                   enrolled.mode == entry.mode,
                   (StudioGame.home == enrolled.team_key) | (StudioGame.away == enrolled.team_key),
                   played if live else StudioResult.kind.in_(("completed", "failed", "success")))
            .group_by(enrolled.id).order_by(victories.desc(), star_total.desc(), enrolled.id).limit(limit))
    rows = []
    before = None
    rank = 0
    for index, (entry_id, wins, value, games) in enumerate(db.execute(stmt), 1):
        if (wins, value) != before:
            rank = index
            before = wins, value
        rows.append({"rank": rank, "name": "You" if entry_id == entry.id else f"Campaign {entry_id}",
                     "wins": wins, "stars": value, "played": games})
    return rows
