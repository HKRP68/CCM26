"""Scorecards, filtered player totals and championship awards."""
import json
from collections import defaultdict

from services import tournament_mvp as mvp
from services.t20_championship import series_complete
from services.tournament_service import _apply_line, _collect_player_lines, _legal_balls, player_identity


def capture(state, teams_by_user, winner_name, *, award_potm=True):
    from handlers.cipl_play import _cipl_potm_select

    innings = []
    for prefix, name in (("inn1_", "inn1_team"), ("", "bat_team_name")):
        user = state.get(prefix + "bat_team_id")
        if user not in teams_by_user:
            continue
        runs = "inn1_runs" if prefix else "total_runs"
        wickets = "inn1_wickets" if prefix else "total_wickets"
        innings.append(dict(team_id=teams_by_user[user], team=state.get(name),
                            runs=state.get(runs) or 0, wickets=state.get(wickets) or 0,
                            balls=_legal_balls(state.get(prefix + "bowl_stats"))))
    lines = _collect_player_lines(state)
    fielders = defaultdict(lambda: dict(catches=0, keeper_catches=0, stumpings=0, runouts=0))
    for ball in state.get("record_deliveries") or []:
        if not ball.get("wicket") or not ball.get("fielder"):
            continue
        key = (ball.get("opponent"), ball["fielder"])
        fielding = fielders[key]
        how = ball.get("how") or ""
        if "caught" in how or how in ("c&b", "c & b"):
            fielding["catches"] += 1
            fielding["keeper_catches"] += int(bool(ball.get("keeper")))
        elif "stump" in how:
            fielding["stumpings"] += 1
        elif "run out" in how:
            fielding["runouts"] += 1
            for assist in set(ball.get("assists") or []) - {ball["fielder"]}:
                fielders[ball.get("opponent"), assist]["runouts"] += 1
    for line in lines:
        # Delivery evidence is consumed above. The scorecard stores figures
        # rather than a copy of every ball in the archived match.
        line.pop("record_detail", None)
        line["team_id"] = teams_by_user[line["user_id"]]
        line.update(fielders[line["team_name"], line["name"]])
        line["bowled"] |= bool(line["bowl_wickets"])
    best = _cipl_potm_select(state, winner_name) if award_potm else None
    return dict(innings=innings, players=lines, potm=best["rid"] if best else None)


def _new_totals():
    return dict(user_id=None, player_id=None, roster_id=None, name=None, team_name=None,
                matches=0, bat_innings=0, bat_runs=0, bat_balls=0, bat_fours=0,
                bat_sixes=0, bat_outs=0, highest_score=0, highest_score_not_out=False,
                fifties=0, hundreds=0, ducks=0, bowl_innings=0, bowl_wickets=0,
                bowl_runs=0, bowl_balls=0, best_bowl_wickets=0, best_bowl_runs=-1,
                three_fers=0, five_fers=0, hattricks=0, catches=0, dismissals=0,
                keeper_dismissals=0, potm=0, pots=0, batting=0.0, bowling=0.0, mvp=0.0)


def _performance_key(row):
    return (-row["mvp"], -row["potm"], -row["bat_runs"], -row["bowl_wickets"],
            row["name"].casefold(), row["team_id"])


def aggregate(series_rows, *, team_id=None, series_id=None):
    totals = defaultdict(_new_totals)
    for series in series_rows:
        if series_id is not None and series.id != series_id:
            continue
        series_totals = defaultdict(_new_totals)
        for fixture in series.fixtures:
            if not fixture.scorecard:
                continue
            data = json.loads(fixture.scorecard)
            for line in data["players"]:
                key = line["team_id"], player_identity(line)
                potm = int(data["potm"] is not None and data["potm"] == line["roster_id"])
                batting, bowling = mvp.line_impact(line)
                fielding = line["catches"] + line["stumpings"] + line["runouts"]
                result_bonus = (mvp.TIE_BONUS if fixture.result == "tie"
                                else mvp.WIN_BONUS * (fixture.winner_id == line["team_id"]))
                if not (line["batted"] or line["bowled"] or fielding):
                    result_bonus = 0
                for acc in (totals[key], series_totals[key]):
                    acc["team_id"] = line["team_id"]
                    _apply_line(acc, line)
                    # Everyone in the XI gets an appearance, even DNB.
                    acc["matches"] += int(not (line["batted"] or line["bowled"]))
                    acc["catches"] += line["catches"]
                    acc["dismissals"] += fielding
                    acc["keeper_dismissals"] += line["keeper_catches"] + line["stumpings"]
                    acc["potm"] += potm
                    acc["batting"] += batting
                    acc["bowling"] += bowling
                    acc["mvp"] += batting + bowling + result_bonus + potm * mvp.POTM_BONUS
        if not series.is_final and series_complete(series) and series_totals:
            best = min(series_totals, key=lambda key: _performance_key(series_totals[key]))
            if series_totals[best]["mvp"] > 0:
                totals[best]["pots"] += 1
    return sorted((row for row in totals.values() if team_id is None or row["team_id"] == team_id),
                  key=_performance_key)


def awards(championship, series_rows, totals):
    def pick(field, eligible):
        choices = sorted((row for row in totals if eligible(row)),
                         key=lambda row: (-field(row), _performance_key(row)))
        return player_label(choices[0]) if choices else None

    top = next((row for row in totals if row["mvp"] > 0), None)
    final = next((series for series in series_rows if series.is_final), None)
    final_player = None
    if championship.completed_at and final:
        rows = aggregate([final])
        if championship.final_format == "single":
            best = next((row for row in rows if row["potm"]), None)
        else:
            best = next((row for row in rows if row["mvp"] > 0), None)
        final_player = player_label(best) if best else None
    return {
        "Championship MVP": player_label(top) if top else None,
        "Leading Run Scorer": pick(lambda r: r["bat_runs"], lambda r: r["bat_runs"] > 0),
        "Leading Wicket Taker": pick(lambda r: r["bowl_wickets"], lambda r: r["bowl_wickets"] > 0),
        "Best Batter": pick(lambda r: r["batting"], lambda r: r["batting"] > 0),
        "Best Bowler": pick(lambda r: r["bowling"], lambda r: r["bowl_wickets"] > 0),
        "Best All-rounder": pick(lambda r: min(r["batting"], r["bowling"]),
                               lambda r: r["bat_runs"] > 0 and r["bowl_wickets"] > 0),
        "Best Wicketkeeper": pick(lambda r: r["keeper_dismissals"], lambda r: r["keeper_dismissals"] > 0),
        "Player of the Final": final_player,
        "Champion Captain": championship.champion_captain,
    }


def player_label(row):
    return f'{row["name"]} ({row["team_name"]})'


def safe_rate(number, denominator, multiplier=1):
    return f"{number * multiplier / denominator:.2f}" if denominator else "–"


def player_figures(row):
    return dict(
        average=safe_rate(row["bat_runs"], row["bat_outs"]),
        strike_rate=safe_rate(row["bat_runs"], row["bat_balls"], 100),
        highest=f'{row["highest_score"]}{"*" if row["highest_score_not_out"] else ""}',
        economy=safe_rate(row["bowl_runs"], row["bowl_balls"], 6),
        bowling_average=safe_rate(row["bowl_runs"], row["bowl_wickets"]),
        bowling_strike_rate=safe_rate(row["bowl_balls"], row["bowl_wickets"]),
        best=(f'{row["best_bowl_wickets"]}/{row["best_bowl_runs"]}'
              if row["best_bowl_runs"] >= 0 else "–"),
    )
