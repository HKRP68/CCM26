"""Player splits backed by official scorecards and their recorded deliveries."""

import json
from collections import defaultdict
from dataclasses import dataclass
from itertools import product

from engine.pitch_registry import PITCHES


RESULTS = {"win": "Wins", "loss": "Losses", "tie": "Ties", "no result": "No results"}
PHASES = {"powerplay": "Powerplay", "middle": "Middle", "death": "Death"}
PITCH_NAMES = {pitch.casefold(): pitch for pitch in PITCHES}
FILTER_HELP = (
    "Filters: Win, Loss, Tie, No Result · "
    + ", ".join(PITCHES)
    + " · Powerplay, Middle, Death, All Phases. Use commas to compare values and | to combine filters."
)
_ALIASES = {
    "won": "win", "wins": "win", "lost": "loss", "losses": "loss",
    "tied": "tie", "ties": "tie", "draw": "tie", "nr": "no result",
    "no results": "no result", "power play": "powerplay", "pp": "powerplay",
    "middle overs": "middle", "death overs": "death",
}


def key(value):
    return " ".join((value or "").split()).casefold()


def parse_filters(parts):
    """Three sets of allowed values. Values in the same set are alternatives."""
    filters = ([], [], [])
    for part in parts:
        for value in part.split(","):
            token = key(value.replace("-", " ").replace("_", " "))
            token = _ALIASES.get(token, token).removesuffix(" pitch")
            if token == "all phases":
                filters[2][:] = PHASES
                continue
            for allowed, selected in zip((RESULTS, PITCH_NAMES, PHASES), filters):
                if token in allowed:
                    if token not in selected:
                        selected.append(token)
                    break
            else:
                raise ValueError(f'Unknown filter “{value.strip()[:80]}”.' if token
                                 else 'A filter is missing after a separator.')
    return tuple(tuple(values) for values in filters)


def _json(value, kind):
    try:
        decoded = json.loads(value or "null")
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, kind) else None


# This order is shared by the scorecard projection and phase rollups.
_FIELDS = ("bat_runs", "bat_balls", "bat_out", "bowl_wickets", "bowl_runs", "bowl_balls")


def _same_player(event, role, line, team):
    if key(event.get(team)) != key(line.get("team_name")):
        return False
    rid = event.get("rid" if role == "batter" else "bid")
    if rid is not None and line.get("roster_id") is not None:
        return str(rid) == str(line["roster_id"])
    return key(event.get(role)) == key(line.get("name"))


def _phase_stats(line, detail, official):
    """Validate this player's splits against the authoritative scorecard line."""
    if not isinstance(detail, dict) or detail.get("version") != 1:
        return None
    starts = detail.get("innings_starts") or {}
    phases = defaultdict(lambda: [0] * len(_FIELDS))
    player_team = key(line.get("team_name"))
    for ball in detail.get("deliveries") or ():
        phase = ball.get("phase")
        if phase not in PHASES:
            continue
        batting = _same_player(ball, "batter", line, "team")
        bowling = _same_player(ball, "bowler", line, "opponent")
        dismissed = False
        if ball.get("wicket") and key(ball.get("team")) == player_team:
            # A non-striker run-out can occur without the player facing a ball.
            other = ball.get("dismissed_batter")
            dismissed = (key(other) == key(line.get("name"))) if other else batting
        if not (batting or bowling or dismissed):
            continue
        if not starts.get(str(ball.get("innings")), {}).get("complete"):
            return None
        totals = phases[phase]
        if batting:
            totals[0] += int(ball.get("bat_runs") or 0)
            totals[1] += int(ball.get("faced") or 0)
        totals[2] += int(dismissed)
        if bowling:
            totals[3] += int(bool(ball.get("bowler_wicket")))
            totals[4] += int(ball.get("conceded") or 0)
            totals[5] += int(bool(ball.get("legal")))
    if not phases or [sum(values) for values in zip(*phases.values())] != official:
        return None
    return dict(phases)


def _player_result(result, side, winner):
    if not side:
        return None
    if result in ("tie", "no result"):
        return result
    if result == "win" and winner:
        return "win" if side == winner else "loss"
    return None


def _players(lines, result, winner, sides, user_sides, *, phases, name=None):
    detail = next((line["record_detail"] for line in lines if line.get("record_detail")), None)
    players = []
    for line in lines:
        player_name = line.get("name")
        if not player_name or (name and key(player_name) != name):
            continue
        official = [int(line.get(field) or 0) for field in _FIELDS]
        if not (any(official) or line.get("batted") or line.get("bowled")):
            continue
        side = sides.get(key(line.get("team_name"))) or user_sides.get(str(line.get("user_id")))
        players.append(dict(
            name=player_name, result=_player_result(result, side, winner), totals=official,
            phases=_phase_stats(line, detail, official) if phases else None,
        ))
    return players


def live_matches(session, tournament_id, *, phases, name=None):
    from models import Match, TournamentMatch, TournamentTeam

    names = dict(session.query(TournamentTeam.id, TournamentTeam.name)
                 .filter_by(tournament_id=tournament_id))
    fixtures = (session.query(TournamentMatch, Match.pitch_type)
                .outerjoin(Match, Match.id == TournamentMatch.match_id)
                .filter(TournamentMatch.tournament_id == tournament_id,
                        TournamentMatch.status == "completed",
                        TournamentMatch.scorecard_json.isnot(None))
                .order_by(TournamentMatch.id))
    matches = []
    for match, played_pitch in fixtures:
        lines = [line for line in _json(match.scorecard_json, list) or []
                 if isinstance(line, dict)]
        if not lines:
            continue
        ids = (match.team1_id, match.team2_id)
        sides = {key(names[tid]): tid for tid in ids if tid in names and names[tid]}
        user_sides = {str(uid): tid for uid, tid in
                      zip((match.host_user_id, match.target_user_id), ids) if uid is not None}
        result = key((match.result_type or "").replace("_", " "))
        if not result:
            if key(match.result_text) in ("no result", "abandoned"):
                result = "no result"
            elif match.winner_team_id is not None:
                result = "win"
            else:
                result = "tie"
        winner = match.winner_team_id if match.winner_team_id in ids else None
        players = _players(lines, result, winner, sides, user_sides, phases=phases, name=name)
        if players:
            pitch = lines[0].get("played_pitch") or played_pitch or match.pitch_type
            matches.append(dict(pitch=key(pitch), players=players))
    return matches


def saved_matches(session, saved, *, phases, name=None):
    from models import LeagueRecordConfig, LeagueSeason, Match

    matches = _json(saved.player_match_stats_json, list)
    if matches is not None:
        return matches

    # League Seasons also archived full scorecards before player splits existed.
    evidence = (session.query(LeagueRecordConfig.archive_json)
                .join(LeagueSeason, LeagueSeason.id == LeagueRecordConfig.season_id)
                .filter(LeagueSeason.stats_season_id == saved.id,
                        LeagueRecordConfig.archive_json.isnot(None))
                .order_by(LeagueSeason.id).first())
    old_matches = _json(evidence[0], list) if evidence else None
    if old_matches is None:
        return None
    match_ids = {m["match_id"] for m in old_matches if m.get("match_id")}
    pitches = dict(session.query(Match.id, Match.pitch_type).filter(Match.id.in_(match_ids)))
    results = (_json(saved.franchise_results_json, dict) or {}).get("matches", [])
    by_fixture = {result["fixture_id"]: result for result in results}
    matches = []
    for match in old_matches:
        sides = {key(match.get(side)): key(match.get(side)) for side in ("team1", "team2")
                 if match.get(side)}
        official = by_fixture.get(match.get("fixture_id"), {})
        winner = key(official.get("winner") or match.get("winner"))
        result = key((official.get("result") or "").replace("_", " "))
        if not result:
            if winner in sides:
                result = "win"
            elif key(match.get("result")) in ("match tied", "tie", "tied"):
                result = "tie"
            elif key(match.get("result")) in ("no result", "abandoned"):
                result = "no result"
        lines = [line for line in match.get("lines") or [] if isinstance(line, dict)]
        players = _players(lines, result, winner if winner in sides else None,
                           sides, {}, phases=phases, name=name)
        if players:
            pitch = lines[0].get("played_pitch") or pitches.get(match.get("match_id"))
            matches.append(dict(pitch=key(pitch), players=players))
    return matches


@dataclass
class Split:
    matches: int = 0
    runs: int = 0
    faced: int = 0
    outs: int = 0
    wickets: int = 0
    conceded: int = 0
    legal: int = 0

    def add_match(self, lines):
        self.matches += 1
        for runs, faced, outs, wickets, conceded, legal in lines:
            self.runs += runs
            self.faced += faced
            self.outs += outs
            self.wickets += wickets
            self.conceded += conceded
            self.legal += legal


class Report:
    def __init__(self):
        self.total = Split()
        self.groups = defaultdict(Split)
        self.unavailable_matches = 0
        self.unavailable_seasons = 0

    def add_season(self, matches, name, filters):
        """Roll up each requested split, counting a fixture once in the total."""
        season = Split()
        if matches is None:
            self.unavailable_seasons += 1
            return season
        results, pitches, phases = filters
        for match in matches:
            pitch = match["pitch"]
            if pitches and pitch and pitch not in pitches:
                continue
            groups = defaultdict(list)
            unknown = False
            for player in match["players"]:
                if key(player["name"]) != name:
                    continue
                result = player["result"]
                if results and result and result not in results:
                    continue
                if (pitches and not pitch) or (results and not result):
                    unknown = True
                    continue
                prefix = (result if results else None, pitch if pitches else None)
                if not phases:
                    groups[prefix + (None,)].append(player["totals"])
                    continue
                if player["phases"] is None:
                    unknown = True
                    continue
                for phase in phases:
                    values = player["phases"].get(phase)
                    if values is not None:
                        groups[prefix + (phase,)].append(values)
            self.unavailable_matches += int(unknown)
            for group, lines in groups.items():
                self.groups[group].add_match(lines)
            if groups:
                totals = [line for lines in groups.values() for line in lines]
                self.total.add_match(totals)
                season.add_match(totals)
        return season


def group_labels(filters):
    for group in product(*(values or (None,) for values in filters)):
        yield group, " · ".join(labels[value] for labels, value in
                                zip((RESULTS, PITCH_NAMES, PHASES), group) if value)
