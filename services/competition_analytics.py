"""Availability-aware scorecard and tactical summaries shared by both sources."""

from collections import Counter, defaultdict
import csv
from io import StringIO
import json

from sqlalchemy import select

from models import TournamentTeam
from models_events import StudioContribution, StudioParticipant, StudioTeamPerformance
from services import competition_sources as sources


APPROACHES = {"defensive", "rotate", "balanced", "aggressive", "ultra", "mixed", "variation"}
PHASES = ("powerplay", "middle", "death")
LIMIT = 2500


def rate(top, base, scale=1):
    return round(top * scale / base, 2) if top is not None and base else None


def overs(balls):
    return f"{balls // 6}.{balls % 6}" if balls is not None else None


def _label(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("label")
    return value if isinstance(value, str) else None


def extract(state, *, include_approaches=True):
    """Project only committed telemetry, without names or private match choices."""
    recorded = state.get("record_deliveries")
    phases = defaultdict(Counter)
    if recorded:
        for event in recorded:
            if event["phase"] not in PHASES or event["innings"] not in (1, 2):
                continue
            key = f'{event["innings"]}:{event["phase"]}'
            slot = phases[key]
            legal = bool(event["legal"])
            slot.update({"balls": int(legal), "runs": event["team_runs"],
                         "wickets": int(event["bowler_wicket"]),
                         "dots": int(legal and event["team_runs"] == 0)})
    matchup = defaultdict(Counter)
    if include_approaches:
        for entry in (*state.get("inn1_approach_log", []), *state.get("approach_log", [])):
            if entry.get("bat") in APPROACHES and entry.get("bowl") in APPROACHES and entry.get("balls"):
                key = f'{entry["innings"]}:{entry["bat"]}:{entry["bowl"]}'
                matchup[key].update({"overs": 1, "balls": entry["balls"], "runs": entry["runs"],
                                     "wickets": entry["wickets"]})
    conditions = (state.get("conditions") or {}) if isinstance(state.get("conditions"), dict) else {}
    return {"version": 1, "phases": {key: dict(value) for key, value in phases.items()},
            "tactics": {key: dict(value) for key, value in matchup.items()},
            "context": {"pitch": _label(state.get("pitch_type")), "venue": _label(state.get("stadium")),
                        "weather": _label(conditions.get("weather")), "toss": _label(state.get("toss_decision"))}}


def _live_cards(db, room):
    detailed = defaultdict(list)
    from models_events import StudioFixture
    query = (select(StudioTeamPerformance)
             .join(StudioFixture, StudioFixture.id == StudioTeamPerformance.fixture_id)
             .where(StudioFixture.competition_id == room.id))
    by_side = set()
    for row in db.scalars(query):
        by_side.add((row.fixture_id, row.team_key))
        for entry in row.detail.get("players", []):
            detailed[row.fixture_id].append((row.team_key, entry))
    old = (select(StudioContribution, StudioParticipant.team_key).join(
           StudioParticipant, StudioParticipant.id == StudioContribution.participant_id)
           .where(StudioParticipant.competition_id == room.id))
    for row, side in db.execute(old):
        if (row.fixture_id, side) not in by_side:
            detailed[row.fixture_id].extend((side, player) for player in row.performance)
    return detailed


def _card_lines(result, raw, live_player_lines):
    if result.source == sources.LIVE:
        for team, player in live_player_lines.get(result.fixture, ()):
            bat, bowl = player.get("batting", {}), player.get("bowling", {})
            yield team, {"player_id": player.get("player_id"), "edition": player.get("version"),
                         "name": player["name"], "bat_runs": bat.get("runs"), "bat_balls": bat.get("balls"),
                         "bat_out": bat.get("out"), "batted": bat.get("batted"),
                         "bat_fours": bat.get("fours"), "bat_sixes": bat.get("sixes"),
                         "bowl_runs": bowl.get("runs"), "bowl_balls": bowl.get("balls"),
                         "bowl_wickets": bowl.get("wickets"), "bowl_dots": bowl.get("dots"),
                         "maidens": bowl.get("maidens")}
        return
    lines, _ = raw
    for line in lines:
        team = line.get("_certified_side")
        if team in result.sides:
            yield team, line


def _parse(raw):
    try:
        values = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [line for line in values if isinstance(line, dict)] if isinstance(values, list) else []


def build(db, user, source, instance_id, filters):
    if not sources.flag("ANALYTICS"):
        raise ValueError("Tournament Analytics is unavailable.")
    instance = sources.resolve(db, user, source, instance_id)
    if source == sources.LIVE:
        cfg = sources.live_config(db, instance)
        labels = {key: value["name"] for key, value in cfg["snapshot"].items()}
        cards = _live_cards(db, instance)
        effective = {**cfg, "points": {**cfg["points"],
                    "no_result": cfg["competition"]["double_no_show_points"]}}
        from services import event_studio_competition as live, event_studio_play as legacy
        standings = legacy.table(effective, live._rows(db, instance))
    else:
        teams = db.scalars(select(TournamentTeam).filter_by(tournament_id=instance.id)).all()
        labels = {team.id: team.name for team in teams}
        from services import tournament_service as tournament
        standings = [{"team": team.id, "name": team.name, "points": team.points, "nrr": getattr(team, "_nrr", None)}
                     for team in tournament.points_table(db, instance.id)]
        cards = {}
    team = filters.get("team")
    if team is not None and (type(team) not in (int, str) or team not in labels):
        raise ValueError("Choose a team in this competition.")
    mode = filters.get("participation", "all")
    if mode not in ("all", "human", "ai"):
        raise ValueError("Choose a participation filter.")
    fmt = filters.get("format")
    if fmt is not None and (not isinstance(fmt, str) or len(fmt) > 20):
        raise ValueError("Choose a format filter.")
    stage = filters.get("stage")
    if stage is not None and (not isinstance(stage, str) or len(stage) > 30):
        raise ValueError("Choose a stage filter.")
    start, end = (_date(filters.get(name)) for name in ("from", "until"))
    if start and end and end <= start:
        raise ValueError("Choose an end date after the start date.")
    selected = sources.result_rows(db, source, instance, limit=LIMIT + 1)
    if len(selected) > LIMIT:
        raise ValueError("This competition exceeds the analytics scan limit.")
    results, fixtures = [], []
    teams, players, phases, tactics, venue = _aggregate_containers()
    for result, raw in selected:
        if fmt and fmt != result.format or stage and stage != result.stage or team is not None and team not in result.sides:
            continue
        if start and (result.recorded_at is None or result.recorded_at < start) or end and (
                result.recorded_at is None or result.recorded_at >= end):
            continue
        verified = result.eligibility == "verified"
        ai = result.simulated or result.eligibility in ("ai_filled", "solo")
        if mode == "human" and not verified or mode == "ai" and not ai:
            continue
        results.append(result.format)
        lines = _parse(raw) if source == sources.CHALLENGE else []
        for line in lines:
            name = line.get("team_name")
            matched = [side for side in result.sides if labels.get(side) == name and name is not None]
            line["_certified_side"] = matched[0] if len(matched) == 1 else None
        telemetry = result.telemetry or {}
        if source == sources.CHALLENGE:
            if result.eligibility != "corrected_without_evidence":
                telemetry = next((line.get("universe_telemetry") for line in lines if line.get("universe_telemetry")), telemetry)
            official = next((line.get("record_detail") for line in lines if line.get("record_detail")), None)
            if official and not result.simulated and result.eligibility != "corrected_without_evidence":
                from services.record_telemetry import analyse
                try:
                    valid = analyse(official, lines)
                except (KeyError, TypeError, ValueError):
                    valid = {"invalid_reason": "Malformed legacy delivery evidence"}
                if valid.get("invalid_reason"):
                    telemetry = {"context": telemetry.get("context", {})}
                elif valid.get("deliveries"):
                    measured = extract({"record_deliveries": valid["deliveries"]})
                    telemetry = {**measured, **telemetry, "phases": measured["phases"]}
        _accumulate_result(result, labels, teams, phases, tactics, telemetry, venue, team)
        fixtures.append({"id": result.fixture, "format": result.format, "stage": result.stage,
                         "status": result.status, "home": labels.get(result.sides[0]), "away": labels.get(result.sides[1]),
                         "winner": labels.get(result.winner), "eligibility": result.eligibility,
                         "innings": [{**row, "team": labels.get(row["team"]), "overs": overs(row["balls"])}
                                     for row in result.innings],
                         "date": sources.utc_text(result.recorded_at) if result.recorded_at else None})
        if result.status != "completed":
            continue
        for side, line in _card_lines(result, (lines, raw), cards):
            if team is None or side == team:
                _accumulate_player(players, result.format, side, labels[side], line)

    return {"source": source, "instance": instance_id, "teams": list(teams.values()),
            "standings": standings, "team_options": [{"key": key, "name": name} for key, name in labels.items()],
            "format_options": sorted({result.format for result, _ in selected}),
            "stage_options": sorted({result.stage for result, _ in selected if result.stage}),
            "fixtures": fixtures[-40:][::-1],
            "players": _players(players), "phases": _phase_rows(phases), "tactics": _tactic_rows(tactics),
            "available": {"scorecards": bool(players), "phases": bool(phases), "tactics": bool(tactics)},
            "contexts": [{"format": fmt, "condition": kind, "value": value, "recorded_matches": counts["matches"],
                          "bat_first_wins": counts["bat_first_wins"], "chase_wins": counts["chase_wins"]}
                         for (fmt, kind, value), counts in venue.items()],
            "sample": len(results),
            "notice": "Approach comparisons are observational. Fewer than 12 recorded overs is a small sample."}


def _aggregate_containers():
    return {}, {}, defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)


def _date(value):
    if value is None or value == "":
        return None
    from services.event_studio_schedule import utc_instant
    return utc_instant(value, "UTC")


def _accumulate_result(row, labels, teams, phases, tactics, telemetry, venue, team_filter):
    scores = {line["team"]: line for line in row.innings}
    for side, opponent in (row.sides, row.sides[::-1]):
        if side is None or side not in labels or team_filter is not None and side != team_filter:
            continue
        key = row.format, side
        target = teams.setdefault(key, {"team": labels.get(side), "format": row.format, "played": 0,
            "wins": 0, "losses": 0, "ties": 0, "no_results": 0, "forfeits": 0, "ai_fixtures": 0,
            "runs_for": None, "runs_against": None, "wickets_for": None, "wickets_against": None, "form": []})
        if row.status == "completed":
            target["played"] += 1
            field = "ties" if row.winner is None else "wins" if row.winner == side else "losses"
            target[field] += 1
            target["form"] = (target["form"] + [field[0].upper()])[-5:]
        elif row.status == "no_result":
            target["no_results"] += 1
        elif row.status == "forfeited":
            target["forfeits"] += 1
        target["ai_fixtures"] += row.simulated or row.eligibility in ("ai_filled", "solo")
        if row.status == "completed":
            for key, value in (("runs_for", scores.get(side, {}).get("runs")),
                               ("runs_against", scores.get(opponent, {}).get("runs")),
                               ("wickets_for", scores.get(opponent, {}).get("wickets")),
                               ("wickets_against", scores.get(side, {}).get("wickets"))):
                if value is not None:
                    target[key] = (target[key] or 0) + value
        target["win_rate"] = rate(target["wins"], target["played"], 100)
    if row.status != "completed":
        return
    for sections, target in (("phases", phases), ("tactics", tactics)):
        for coordinate, counts in (telemetry.get(sections) or {}).items():
            inning, *labels_part = coordinate.split(":")
            if inning not in ("1", "2") or len(row.innings) != 2:
                continue
            batting_team = row.innings[int(inning) - 1]["team"]
            if team_filter is None or team_filter == batting_team:
                target[(row.format, labels.get(batting_team), *labels_part)].update(counts)
    for kind, value in telemetry.get("context", {}).items():
        label = _label(value)
        if kind in ("venue", "pitch", "weather", "toss") and label:
            counts = venue[row.format, kind, label]
            counts["matches"] += 1
            counts["bat_first_wins"] += len(row.innings) == 2 and row.winner == row.innings[0]["team"]
            counts["chase_wins"] += len(row.innings) == 2 and row.winner == row.innings[1]["team"]


def _accumulate_player(players, fmt, side, team, line):
    ident = line.get("player_id")
    if ident is None and not line.get("roster_id"):
        return
    value = players.setdefault((fmt, side, ident, line.get("edition"), line.get("roster_id") if not ident else None),
                              {"name": line.get("name"), "format": fmt, "team": team, "edition": line.get("edition"),
                               "innings": 0, "dismissals": 0, "fifties": 0, "centuries": 0,
                               "three_fors": 0, "five_fors": 0, "high": None, "best": None, "recent": []})
    for key in ("bat_runs", "bat_balls", "bat_fours", "bat_sixes", "bowl_runs", "bowl_balls", "bowl_wickets",
                "bowl_dots", "maidens"):
        number = line.get(key)
        if type(number) is int and number >= 0:
            value[key] = value.get(key, 0) + number
    runs, wickets = line.get("bat_runs"), line.get("bowl_wickets")
    conceded = line.get("bowl_runs")
    if runs is not None and (line.get("batted") or line.get("bat_balls") or line.get("bat_out") or runs):
        value["innings"] += 1
        if line.get("bat_out") is None:
            value["has_unknown_dismissal"] = True
        else:
            value["dismissals"] += int(bool(line["bat_out"]))
        value["fifties"] += 50 <= runs < 100
        value["centuries"] += runs >= 100
        value["high"] = max(value["high"] or 0, runs)
        value["recent"] = (value["recent"] + [runs])[-5:]
    if wickets is not None and conceded is not None and line.get("bowl_balls"):
        figure = wickets, -conceded
        value["best"] = max(value["best"] or figure, figure)
        value["three_fors"] += wickets >= 3
        value["five_fors"] += wickets >= 5


def _players(players):
    for row in players.values():
        unknown = row.pop("has_unknown_dismissal", False)
        if unknown:
            row["dismissals"] = None
        row.update(average=rate(row.get("bat_runs"), row["dismissals"]),
                   strike_rate=rate(row.get("bat_runs"), row.get("bat_balls"), 100),
                   economy=rate(row.get("bowl_runs"), row.get("bowl_balls"), 6),
                   bowling_average=rate(row.get("bowl_runs"), row.get("bowl_wickets")),
                   bowling_strike_rate=rate(row.get("bowl_balls"), row.get("bowl_wickets")),
                   dot_rate=rate(row.get("bowl_dots"), row.get("bowl_balls"), 100),
                   not_outs=row["innings"] - row["dismissals"] if not unknown else None, overs=overs(row.get("bowl_balls")))
        if "bat_fours" in row and "bat_sixes" in row:
            row["boundaries"] = row["bat_fours"] + row["bat_sixes"]
            row["boundary_rate"] = rate(row["boundaries"], row.get("bat_balls"), 100)
        if row["best"]:
            wickets, runs = row["best"]
            row["best"] = [wickets, -runs]
    return sorted(players.values(), key=lambda row: (-(row.get("bat_runs") or 0), row.get("name") or ""))[:150]


def _phase_rows(phases):
    return [{"format": fmt, "team": team, "phase": ph, **counts,
             "rpo": rate(counts["runs"], counts["balls"], 6),
             "dot_rate": rate(counts["dots"], counts["balls"], 100),
             "wickets_per_100": rate(counts["wickets"], counts["balls"], 100)}
             for (fmt, team, ph), counts in phases.items()]


def _tactic_rows(tactics):
    return [{"format": fmt, "team": team, "bat": batting, "bowl": bowling, **counts,
             "rpo": rate(counts["runs"], counts["balls"], 6), "small_sample": counts["overs"] < 12}
             for (fmt, team, batting, bowling), counts in tactics.items()]


def export(payload):
    output = StringIO()
    columns = ("name", "team", "format", "edition", "innings", "bat_runs", "strike_rate", "overs", "bowl_wickets", "economy")
    writer = csv.DictWriter(output, columns)
    writer.writeheader()
    for row in payload["players"]:
        writer.writerow({key: _csv_cell(row.get(key)) for key in columns})
    return output.getvalue()


def _csv_cell(value):
    return "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\r", "\t")) else value
