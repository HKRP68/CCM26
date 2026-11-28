"""Read models and snapshots derived from the shared official fixture ledger."""

from collections import defaultdict
from fractions import Fraction
from itertools import product
from types import SimpleNamespace

from sqlalchemy import select

from models_events import StudioAwardMedia, StudioFixture, StudioFixtureResult, StudioHonourSnapshot, StudioTeamPerformance
from services import event_studio_play as legacy, event_studio_progress as stats, event_studio_rules as rules
from services.event_studio_schedule import utc_text
from services.event_studio_brackets import effective_final
from services.tournament_mvp import bat_impact, bowl_impact
from services.team_of_tournament import SHAPE, _role


def dressing_room(config, team, samples, now):
    # Samples are ordered by official result time. A squad member can be in the
    # XI without having a batting or bowling innings.
    form = defaultdict(list)
    overlaps, previous_xi, morale = [], None, 50
    history = []
    for result, sample in samples:
        detail = sample.detail
        code = "W" if result.winner == team else "L" if result.winner else "T"
        morale = min(100, max(0, morale + {"W": 4, "L": -3, "T": 0}[code]))
        xi = set(detail["xi"])
        if xi and previous_xi:
            overlaps.append(len(xi & previous_xi) / len(xi))
        if xi:
            previous_xi = xi
        history.append({"result": code, "at": utc_text(result.recorded_at)})
        for line in detail["players"]:
            form[line["player_id"]].append((result.recorded_at, line, detail["captain"]))
    players = []
    for card in config["snapshot"][team]["squad"]:
        appearances = form[card["player_id"]]
        recent = appearances[-5:]
        runs = sum(line["batting"]["runs"] for _, line, _ in recent)
        balls = sum(line["batting"]["balls"] for _, line, _ in recent)
        wickets = sum(line["bowling"]["wickets"] for _, line, _ in recent)
        bowling_balls = sum(line["bowling"]["balls"] for _, line, _ in recent)
        conceded = sum(line["bowling"]["runs"] for _, line, _ in recent)
        # Workload is a presentation value in legal deliveries, with a 24h
        # half-life. Bowling uses legal deliveries and batting uses balls faced. It never enters the engine or duplicates ODI fatigue.
        workload = sum((line["bowling"]["balls"] + line["batting"]["balls"])
                       * 0.5 ** (max(0, (now - date).total_seconds()) / 86400)
                       for date, line, _ in appearances)
        captained = [line for _, line, captain in recent if captain == card["player_id"]]
        captain_impact = sum(impact(line) for line in captained) / len(captained) if captained else 0
        players.append({"player_id": card["player_id"], "name": card["name"], "appearances": len(recent),
                        "captain_impact": round(captain_impact, 2), "runs": runs, "wickets": wickets,
                        "strike_rate": round(100 * runs / balls, 1) if balls else None,
                        "economy": round(6 * conceded / bowling_balls, 2) if bowling_balls else None,
                        "workload": round(workload, 1), "captained": sum(
                            captain == card["player_id"] for _, _, captain in appearances)})
    return {"morale": morale, "unity": round(100 * sum(overlaps[-5:]) / len(overlaps[-5:])) if overlaps else None,
            "players": players, "recent": history[-5:], "model_version": 2,
            "reputation": sum(3 if row["result"] == "W" else 1 for row in history),
            "fan_support": len(history) + 2 * sum(row["result"] == "W" for row in history)}


def impact(line):
    bat, bowl = line["batting"], line["bowling"]
    return bat_impact(bat["runs"], bat["balls"], bat["fours"], bat["sixes"]) + bowl_impact(
        bowl["wickets"], bowl["runs"], bowl["balls"])


def performances(db, room_id, *, team=None):
    query = (select(StudioFixtureResult, StudioTeamPerformance).join(
        StudioTeamPerformance, StudioTeamPerformance.fixture_id == StudioFixtureResult.fixture_id)
        .join(StudioFixture, StudioFixture.id == StudioFixtureResult.fixture_id)
        .where(StudioFixture.competition_id == room_id, StudioFixtureResult.kind == "completed")
        .order_by(StudioFixtureResult.recorded_at, StudioFixtureResult.fixture_id, StudioTeamPerformance.team_key))
    if team:
        query = query.where(StudioTeamPerformance.team_key == team)
    return db.execute(query).all()


def qualification(config, rows, board):
    """Safe points bounds for the canonical league/group ranking slots."""
    if config["schedule"]["preset"] not in ("round_robin", "groups"):
        return None
    templates = rules.fixture_rows(config)
    slots = [side.split(":") for row in templates.values() if row["stage"] == "playoff"
             for side in (row["home"], row["away"]) if side.startswith(("rank:", "group_rank:"))]
    if not slots:
        return None
    scopes = {parts[1] if parts[0] == "group_rank" else "": set() for parts in slots}
    for parts in slots:
        scopes[parts[1] if parts[0] == "group_rank" else ""].add(int(parts[-1]))
    if any(ranks != set(range(1, max(ranks) + 1)) for ranks in scopes.values()):
        return None
    remaining = defaultdict(int)
    pending = []
    for fixture, result in rows:
        if result is None and templates[fixture.fixture_key]["stage"] == "league":
            if not fixture.home or not fixture.away:
                return None
            pending.append((fixture.home, fixture.away))
            remaining[fixture.home] += 1
            remaining[fixture.away] += 1
    peak = max(config["points"].values())
    ceiling = {row["team"]: row["points"] + peak * remaining[row["team"]] for row in board}
    output = []
    for group, ranks in scopes.items():
        size = max(ranks)
        ranked = [row for row in board if not group or row["team"] in config["schedule"]["groups"][group]]
        final = all(not remaining[row["team"]] for row in ranked)
        for index, row in enumerate(ranked):
            team = row["team"]
            peers = [other for other in ranked if other["team"] != team]
            if final:
                status = "clinched" if index < size else "eliminated"
            elif sum(ceiling[p["team"]] >= row["points"] for p in peers) < size:
                status = "clinched"
            elif sum(p["points"] > ceiling[team] for p in peers) >= size:
                status = "eliminated"
            else:
                status = "in contention"
            output.append({"team": team, "name": row["name"], "group": group, "slots": size,
                           "remaining": remaining[team], "max_points": ceiling[team], "status": status})
    if 0 < len(pending) <= 4:
        _prove_small_schedule(config["points"], board, pending, output)
    return output



def win_out_scenario(config, rows, board, qualified, team):
    """Explain the points consequence of winning the remaining league games."""
    if not qualified:
        return None
    status = next((row for row in qualified if row["team"] == team), None)
    if not status or not status["remaining"]:
        return None
    fixtures = rules.fixture_rows(config)
    pending = [(fixture.home, fixture.away) for fixture, result in rows if result is None
               and fixtures[fixture.fixture_key]["stage"] == "league"]
    points = next(row["points"] for row in board if row["team"] == team)
    projected = points + status["remaining"] * config["points"]["win"]
    threats = 0
    for peer in qualified:
        if peer["team"] == team or peer["group"] != status["group"]:
            continue
        head_to_head = sum(team in fixture and peer["team"] in fixture for fixture in pending)
        maximum = peer["max_points"] - head_to_head * (max(config["points"].values()) - config["points"]["loss"])
        threats += maximum >= projected
    return {"wins_needed_for_scenario": status["remaining"], "points": projected,
            "guaranteed": threats < status["slots"]}

def _prove_small_schedule(points, board, fixtures, output):
    unresolved = [row for row in output if row["status"] == "in contention"]
    if not unresolved:
        return
    outcomes = {(points["win"], points["loss"]), (points["loss"], points["win"]),
                (points["win"], 0), (0, points["win"]), (points["tie"], points["tie"]),
                (points["no_result"], points["no_result"])}
    peers = {row["team"]: [other["team"] for other in output
                           if row["team"] != other["team"] and row["group"] == other["group"]]
             for row in unresolved}
    initial = {row["team"]: row["points"] for row in board}
    possible, guaranteed = set(), {row["team"] for row in unresolved}
    for scenario in product(outcomes, repeat=len(fixtures)):
        totals = dict(initial)
        for (home, away), (a, b) in zip(fixtures, scenario):
            totals[home] += a
            totals[away] += b
        for row in unresolved:
            key = row["team"]
            if sum(totals[other] > totals[key] for other in peers[key]) < row["slots"]:
                possible.add(key)
            if sum(totals[other] >= totals[key] for other in peers[key]) >= row["slots"]:
                guaranteed.discard(key)
    for row in unresolved:
        if row["team"] in guaranteed:
            row["status"] = "clinched"
        elif row["team"] not in possible:
            row["status"] = "eliminated"


def match_news(config, rows, samples=(), *, limit=20, offset=0):
    """Project immutable result records after commit. Each story has a stable source ID."""
    names = config["snapshot"]
    samples_by_fixture = defaultdict(list)
    for result, sample in samples:
        samples_by_fixture[result.fixture_id].append(sample)
    stories = []
    best_score, best_figures = None, None
    streaks = {}
    for fixture, result in sorted(((f, r) for f, r in rows if r),
                                  key=lambda row: (row[1].recorded_at, row[0].id)):
        if not fixture.home or not fixture.away:
            continue
        home, away = names[fixture.home]["name"], names[fixture.away]["name"]
        scores = result.detail.get("innings", []) if result.kind == "completed" else []
        if scores:
            summary = " · ".join(f'{names[part["team"]]["name"]} {part["runs"]}/{part["wickets"]}'
                                 for part in scores)
        else:
            summary = {"forfeited": "Forfeit", "no_result": "No result",
                       "cancelled": "Fixture cancelled"}.get(result.kind, "Result recorded")
        headline = f"{home} v {away}"
        if scores:
            headline = f'{names[result.winner]["name"]} won' if result.winner else "Match tied"
            if result.winner and result.detail.get("margin_type") in ("runs", "wickets"):
                headline += f' by {result.detail["margin"]} {result.detail["margin_type"]}'

        def add(kind, title, text, subject=""):
            suffix = f":{subject}" if subject else ""
            stories.append({"id": f"fixture:{fixture.id}:{kind}{suffix}", "fixture_id": fixture.id,
                            "title": title, "summary": text, "kind": kind,
                            "recorded_at": utc_text(result.recorded_at)})

        add("match_report", headline, summary)
        if not scores:
            continue
        facts = result.detail.get("news_facts", {})
        loser = fixture.away if result.winner == fixture.home else fixture.home
        if result.winner:
            strengths = {team: sum(p["rating"] for p in names[team]["default_xi"]) / len(names[team]["default_xi"])
                         for team in (result.winner, loser)}
            if strengths[loser] - strengths[result.winner] >= config["competition"].get("news_upset_gap", 5):
                add("upset", f'{names[result.winner]["name"]} pull off an upset', summary)
        close = result.winner is None or result.detail.get("margin_type") == "wickets" or (
            result.detail.get("margin_type") == "runs" and result.detail.get("margin", 0) <= 6)
        if facts.get("chase_last_over") and close:
            add("last_over", "Last-over thriller", headline + " · " + summary)
        for record in facts.get("hat_tricks", []):
            add("hat_trick", f'{record["name"]} takes a hat-trick',
                names.get(record["team"], {}).get("name", record["team"]), record["delivery"])
        for team in (fixture.home, fixture.away):
            status = "W" if result.winner == team else "L" if result.winner else "T"
            last, length = streaks.get(team, (None, 0))
            streaks[team] = status, length + 1 if last == status else 1
            if status in ("W", "L") and streaks[team][1] >= 3:
                noun = "wins" if status == "W" else "losses"
                add("streak", f'{names[team]["name"]} record {streaks[team][1]} straight {noun}', summary, team)
        for sample in samples_by_fixture[fixture.id]:
            team = names[sample.team_key]["name"]
            for player in sample.detail["players"]:
                bat, bowl = player["batting"], player["bowling"]
                identity = f'{sample.team_key}:{player["player_id"]}'
                if bat["runs"] >= 100:
                    add("century", f'{player["name"]} scores a century',
                        f'{team} · {bat["runs"]} from {bat["balls"]} balls', identity)
                if bowl["wickets"] >= 5:
                    add("five_for", f'{player["name"]} takes {bowl["wickets"]} wickets',
                        f'{team} · {bowl["wickets"]}/{bowl["runs"]}', identity)
        high = max(((player["batting"]["runs"], player["name"]) for sample in samples_by_fixture[fixture.id]
                    for player in sample.detail["players"]), default=(0, ""))
        if best_score is not None and high[0] > best_score:
            add("record", "New tournament batting record", f'{high[1]} scored {high[0]}, surpassing {best_score}.')
        if high[0]:
            best_score = max(best_score or 0, high[0])
        figures = max(((line["bowling"]["wickets"], -line["bowling"]["runs"], line["name"])
                       for sample in samples_by_fixture[fixture.id] for line in sample.detail["players"]
                       if line["bowling"]["balls"] and line["bowling"]["wickets"]), default=None)
        if figures and best_figures and figures[:2] > best_figures:
            add("bowling_record", "New tournament bowling record", f"{figures[2]} took {figures[0]}/{-figures[1]}.")
        if figures:
            best_figures = max(best_figures or figures[:2], figures[:2])
    _postseason_news(config, rows, stories)
    stories.sort(key=lambda row: (row["recorded_at"], row["fixture_id"], row["kind"] == "match_report", row["id"]),
                 reverse=True)
    return {"items": stories[offset:offset + limit], "has_more": len(stories) > offset + limit}


def _postseason_news(config, rows, stories):
    if not rows:
        return
    template = rules.fixture_rows(config)
    names = config["snapshot"]
    league = [(fixture, result) for fixture, result in rows if template[fixture.fixture_key]["stage"] == "league"]

    def append(key, kind, title, summary, stamp):
        stories.append({"id": f"fixture:{key}:{kind}", "fixture_id": key, "kind": kind,
                        "title": title, "summary": summary, "recorded_at": utc_text(stamp)})

    board = legacy.table(config, rows)
    if league and all(result is not None and result.kind != "cancelled" for _, result in league):
        for team in qualification(config, rows, board) or ():
            if team["status"] in ("clinched", "eliminated"):
                last, result = max(league, key=lambda item: (item[1].recorded_at, item[0].id))
                title = "Qualified for the playoffs" if team["status"] == "clinched" else "Eliminated"
                append(last.id, f'qualification:{team["team"]}', title, team["name"], result.recorded_at)
    final_key = effective_final(config, rows)
    final = next(((fixture, result) for fixture, result in rows if fixture.fixture_key == final_key), None)
    if final and final[0].home and final[0].away and (final[1] is None or final[1].kind != "cancelled"):
        fixture, result = final
        append(fixture.id, "final_preview", "Final line-up",
               f'{names[fixture.home]["name"]} v {names[fixture.away]["name"]}', fixture.opens_at)
    if all(result is not None for _, result in rows):
        champion = champion_key(config, rows, board)
        if champion:
            fixture, result = max(rows, key=lambda item: (item[1].recorded_at, item[0].id))
            append(fixture.id, "championship", "Tournament champion", names[champion]["name"], result.recorded_at)


def champion_key(config, rows, board):
    winner = next((key for key in config["snapshot"]
                   if stats.trophy(config, SimpleNamespace(team_key=key), rows, board)), None)
    final_key = effective_final(config, rows)
    if not winner and final_key:
        fixture, result = next((f, r) for f, r in rows if f.fixture_key == final_key)
        if fixture.home and fixture.away and result and result.kind == "no_result":
            order = ([row["team"] for row in board] if any(row["stage"] == "league" for row in config["fixtures"])
                     else list(config["snapshot"]))
            winner = min((fixture.home, fixture.away), key=order.index)
    return winner


def honour(config, rows, board, samples):
    if not rows or any(result is None for _, result in rows):
        return None
    champion = champion_key(config, rows, board)
    final_key = effective_final(config, rows)
    final = next((f for f, result in rows if f.fixture_key == final_key and result.kind == "completed"), None)
    final_pair = next((f for f, result in rows if f.fixture_key == final_key and result.kind != "cancelled"), None)
    runner_up = (next((team for team in (final_pair.home, final_pair.away) if team != champion), None)
                 if champion and final_pair else next((row["team"] for row in board if row["team"] != champion), None)
                 if champion and config["schedule"]["preset"] != "survival" else None)
    players = defaultdict(list)
    for _, sample in samples:
        for line in sample.detail["players"]:
            players[(sample.team_key, line["player_id"], line["version"])].append(line)

    def candidates(metric):
        for (team, _, edition), lines in players.items():
            bat, bowl = [line["batting"] for line in lines], [line["bowling"] for line in lines]
            played = [line for line in bat if line["balls"] or line["runs"] or line["out"] or line.get("batted")]
            spells = [line for line in bowl if line["balls"] or line["runs"] or line["wickets"]]
            if metric == "runs" and played:
                yield sum(row["runs"] for row in played), team, edition, lines[0]["name"]
            elif metric == "wickets" and spells:
                yield sum(row["wickets"] for row in spells), team, edition, lines[0]["name"]
            elif metric in ("fours", "sixes", "fifties", "centuries") and played:
                if metric in ("fours", "sixes"):
                    value = sum(row[metric] for row in played)
                else:
                    value = sum((50 <= row["runs"] < 100 if metric == "fifties" else row["runs"] >= 100)
                                for row in played)
                if value:
                    yield value, team, edition, lines[0]["name"]
            elif metric == "high_score" and played:
                yield max(row["runs"] for row in played), team, edition, lines[0]["name"]
            elif metric == "figures" and spells:
                yield max((row["wickets"], -row["runs"]) for row in spells), team, edition, lines[0]["name"]
            elif metric == "strike_rate" and sum(row["balls"] for row in played) >= 30:
                yield Fraction(100 * sum(row["runs"] for row in played), sum(row["balls"] for row in played)), team, edition, lines[0]["name"]
            elif metric == "economy" and sum(row["balls"] for row in spells) >= 24:
                yield -Fraction(6 * sum(row["runs"] for row in spells), sum(row["balls"] for row in spells)), team, edition, lines[0]["name"]

    awards = {}
    for metric in ("runs", "wickets", "high_score", "figures", "fours", "sixes",
                   "fifties", "centuries", "strike_rate", "economy"):
        rankings = list(candidates(metric))
        if not rankings:
            continue
        best = max(row[0] for row in rankings)
        value = ([best[0], -best[1]] if metric == "figures" else round(abs(float(best)), 2)
                 if isinstance(best, Fraction) else best)
        awards[metric] = [{"name": name, "team": config["snapshot"][team]["name"], "edition": edition,
                           "value": value} for score, team, edition, name in rankings if score == best]
    ranked = []
    categories = {card["player_id"]: card.get("category") for side in config["snapshot"].values() for card in side["squad"]}
    for (team, pid, edition), lines in players.items():
        value = round(sum(impact(line) for line in lines), 2)
        if value <= 0:
            continue
        first = lines[0]
        role = _role(SimpleNamespace(category=categories[pid]), SimpleNamespace(bat_points=0, bowl_points=0))
        ranked.append({"name": first["name"], "team": config["snapshot"][team]["name"], "edition": edition,
                       "value": value, "player_id": pid, "role": role, "matches": len(lines)})
    ranked.sort(key=lambda row: (-row["value"], row["player_id"]))
    if ranked:
        awards["player_of_tournament"] = [row for row in ranked if row["value"] == ranked[0]["value"]]
        qualified = [row for row in ranked if row["matches"] >= max(1, round(max(r["matches"] for r in ranked) * .4))]
        picked = [r for role, size in SHAPE for r in [p for p in qualified if p["role"] == role][:size]]
        picked.extend([row for row in qualified if row not in picked][:11-len(picked)])
        awards["team_of_tournament"] = picked
    final_lines = [(round(impact(line), 2), sample.team_key, line) for result, sample in samples
                   if final and result.fixture_id == final.id for line in sample.detail["players"]]
    if final_lines:
        best = max(points for points, _, _ in final_lines)
        if best > 0:
            awards["player_of_final"] = [
                {"name": line["name"], "team": config["snapshot"][team]["name"], "value": points, "edition": line["version"]}
                for points, team, line in final_lines if points == best]
    captains = [(result, sample.detail["captain"]) for result, sample in samples
                if champion and champion == sample.team_key and sample.detail.get("captain")]
    if captains:
        captain_id = captains[-1][1]
        captain = next(card for card in config["snapshot"][champion]["squad"] if card["player_id"] == captain_id)
        awards["winning_captain"] = [{"name": captain["name"], "team": config["snapshot"][champion]["name"],
                                     "edition": captain["version"], "value": "Captain"}]
    return {"champion": champion, "champion_name": config["snapshot"][champion]["name"] if champion else None,
            "runner_up": config["snapshot"][runner_up]["name"] if runner_up else None, "runner_up_key": runner_up,
            "awards": awards, "source_fixtures": [fixture.id for fixture, _ in rows],
            "standings": board, "rules_version": 2}


def finalize_honours(db, room, config, rows, board, now):
    existing = db.get(StudioHonourSnapshot, (room.id, 1))
    if existing:
        return existing.detail
    snapshot = honour(config, rows, board, performances(db, room.id))
    if snapshot:
        db.add(StudioHonourSnapshot(competition_id=room.id, version=1, detail=snapshot, created_at=now))
        db.add(StudioAwardMedia(competition_id=room.id, due_at=now))
    return snapshot
