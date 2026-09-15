"""Most Valuable Player — one number for a whole tournament.

Every other board in ``/tournamentstats`` ranks one column. Most Runs finds the
batsman who batted the most, Most Wickets the bowler who bowled the most, and
neither of them can see the all-rounder who won three matches with 40-odd and
two wickets a time. This module answers the question those boards can't: **who
actually mattered most across the tournament?**

The currency is the *impact point*, and it is deliberately the same currency the
match card already awards Player of the Match with (``handlers.match._calc_potm``)
— so a tournament's MVP table is simply every match's POTM race added up, and a
player who keeps finishing second in that race still climbs.

    Batting   runs + 1/four + 2/six, + a strike-rate bonus, + 50/100 milestones
    Bowling   25/wicket, + 2 per over of economy beaten against 8.00,
              + 3-for / 5-for milestones
    Result    a flat bonus for each match the player's team won (half for a tie)
    Award     a bonus for each match they were the best player on the field

Two properties are worth stating because the rest of the module is written to
keep them:

* **It is per innings, not per aggregate.** Three fifties are three milestone
  bonuses; one 150 is one hundred's worth. Scoring off the aggregated
  ``TournamentPlayerStats`` row would silently merge them, which rewards the
  wrong thing — consistency and impact are the point.
* **It is rebuilt from the scorecards every time.** Nothing is stored, so
  deleting or correcting a match moves the MVP table with it, exactly as it
  moves the points table.

Works for both competition families: a Lets Play tournament stores its matches in
the same two tables, so the same table serves ``/lptstats``.
"""

import logging
from types import SimpleNamespace

from models import TournamentTeam
from services import tournament_service as ts

logger = logging.getLogger(__name__)

# ── The scoring constants, all in one place ────────────────────────────
# Named (rather than inlined below) because they are the feature: the card
# prints this rubric verbatim so nobody has to guess why they are 4th.

FOUR_POINTS = 1          # on top of the run itself
SIX_POINTS = 2           # ditto
SR_BONUS_HIGH = 10       # strike rate ≥ 150
SR_BONUS_GOOD = 5        # strike rate ≥ 130
FIFTY_BONUS = 15
HUNDRED_BONUS = 30

WICKET_POINTS = 25
ECONOMY_BASELINE = 8.0   # runs per over you are measured against
ECONOMY_WEIGHT = 2       # points per over, per run/over beaten
THREE_FOR_BONUS = 15
FIVE_FOR_BONUS = 30

WIN_BONUS = 10           # each match your team won
TIE_BONUS = 5            # …and a tie is worth half of one
POTM_BONUS = 25          # each match you were the best player on the field

# A losing side's player only enters the Player of the Match race with a real
# performance behind them — the same rule the live match card applies.
POTM_LOSER_FLOOR = 50


def bat_impact(runs, balls, fours, sixes):
    """Impact points for one batting innings.

    Zero for a player who never faced a ball: a DNB is not a performance, and
    letting it score would put every unused tail-ender on the board.
    """
    balls = int(balls or 0)
    if balls <= 0:
        return 0.0
    runs = int(runs or 0)
    points = runs + int(fours or 0) * FOUR_POINTS + int(sixes or 0) * SIX_POINTS
    sr = runs / balls * 100.0
    if sr >= 150:
        points += SR_BONUS_HIGH
    elif sr >= 130:
        points += SR_BONUS_GOOD
    # A hundred is a hundred, never also a fifty — the scorecard convention.
    if runs >= 100:
        points += HUNDRED_BONUS
    elif runs >= 50:
        points += FIFTY_BONUS
    return max(0.0, float(points))


def bowl_impact(wickets, runs, balls):
    """Impact points for one bowling spell.

    Economy is scored against ``ECONOMY_BASELINE`` and weighted by the overs
    actually sent down, so holding 6.00 for four overs beats holding it for one.
    Going *over* the baseline costs points, but never below zero in total: an
    expensive spell that still took three wickets is a contribution, and a
    negative score would drag a player's whole tournament down for one bad day.
    """
    balls = int(balls or 0)
    if balls <= 0:
        return 0.0
    overs = balls / 6.0
    econ = int(runs or 0) / balls * 6.0
    wickets = int(wickets or 0)
    points = wickets * WICKET_POINTS
    points += (ECONOMY_BASELINE - econ) * overs * ECONOMY_WEIGHT
    if wickets >= 5:
        points += FIVE_FOR_BONUS
    elif wickets >= 3:
        points += THREE_FOR_BONUS
    return max(0.0, float(points))


def line_impact(line):
    """``(batting points, bowling points)`` for one scorecard line."""
    bat = bowl = 0.0
    if line.get("batted"):
        bat = bat_impact(line.get("bat_runs"), line.get("bat_balls"),
                         line.get("bat_fours"), line.get("bat_sixes"))
    if line.get("bowled"):
        bowl = bowl_impact(line.get("bowl_wickets"), line.get("bowl_runs"),
                           line.get("bowl_balls"))
    return bat, bowl


# ── Which side of the match a line was on ──────────────────────────────
#
# A scorecard line knows its player and its team *name*; the match row knows
# which team id won. Matching them up by name alone is fragile (a team renamed
# mid-tournament stops matching, and two tournaments can share a name), so the
# user id is tried first: ``host_user_id`` batted first and ``target_user_id``
# batted second, which is exactly how the fixture's two team ids were filled in.


def _side_resolver(session, match, name_cache):
    """Return ``line -> 'win' | 'loss' | 'tie' | None`` for one match.

    ``None`` means "can't tell", and a line that can't be placed simply earns no
    result bonus rather than being guessed onto a side.
    """
    if match.status != "completed":
        return lambda line: None

    win_id = match.winner_team_id
    # A completed fixture with no winner is a tie (or a no-result); either way
    # nobody on it won, and both sides share the consolation.
    if not win_id:
        return lambda line: "tie"

    def _name_of(team_id):
        if team_id is None:
            return None
        if team_id not in name_cache:
            tt = session.query(TournamentTeam).get(int(team_id))
            name_cache[team_id] = (tt.name or "") if tt else ""
        return name_cache[team_id]

    win_uid = None
    if win_id == match.team1_id:
        win_uid = match.host_user_id
    elif win_id == match.team2_id:
        win_uid = match.target_user_id
    # The two user ids the match actually knows about. A line whose user is
    # neither of them is not "the loser" — it is a line this match can't place,
    # so the team name gets a say rather than a stale id deciding it silently.
    known = {int(uid) for uid in (match.host_user_id, match.target_user_id)
             if uid is not None}
    win_name = (_name_of(win_id) or "").strip().casefold()

    def _side(line):
        uid = line.get("user_id")
        if win_uid is not None and uid is not None:
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                uid = None
            if uid is not None and uid in known:
                return "win" if uid == int(win_uid) else "loss"
        team = (line.get("team_name") or "").strip().casefold()
        if team and win_name:
            return "win" if team == win_name else "loss"
        return None

    return _side


def _match_potm(scored, side_of):
    """The identity of the best player on the field in one match, or ``None``.

    Mirrors the live match card's rule: the award belongs to the winning side,
    and a losing player only enters the race with ``POTM_LOSER_FLOOR`` points
    behind them. When nobody's side is known (an unfinished or unresolvable
    match) the highest score on the field takes it.

    ``scored`` is ``[(identity, line, points), ...]`` for one match.
    """
    eligible = []
    for key, line, points in scored:
        side = side_of(line)
        if side == "win" or side is None or side == "tie" or points >= POTM_LOSER_FLOOR:
            eligible.append((key, points))
    if not eligible:
        eligible = [(key, points) for key, _line, points in scored]
    if not eligible:
        return None
    best_key, best_points = max(eligible, key=lambda kp: kp[1])
    return best_key if best_points > 0 else None


def mvp_rows(session, tournament_id):
    """Every player's MVP season, unsorted.

    Each row is a ``SimpleNamespace`` carrying the total and the parts it was
    made of, because "612 points" on its own tells a player nothing about how to
    climb: the card shows the batting/bowling split, the wins and the awards.
    """
    agg = {}
    name_cache = {}

    for match, lines in ts.match_scorecards(session, tournament_id):
        side_of = _side_resolver(session, match, name_cache)
        scored = []
        for line in lines:
            if not (line.get("batted") or line.get("bowled")):
                continue  # never took the field
            key = ts.player_identity(line)
            bat, bowl = line_impact(line)
            scored.append((key, line, bat + bowl))

            row = agg.get(key)
            if row is None:
                row = agg[key] = {
                    "name": line.get("name"), "team_name": line.get("team_name"),
                    "player_id": line.get("player_id"),
                    "roster_id": line.get("roster_id"),
                    "matches": 0, "bat_points": 0.0, "bowl_points": 0.0,
                    "result_points": 0.0, "award_points": 0.0,
                    "wins": 0, "awards": 0,
                    "bat_runs": 0, "bat_balls": 0, "bowl_wickets": 0,
                    "bowl_runs": 0, "bowl_balls": 0,
                }
            # Latest non-empty display snapshots win (mirrors ``_apply_line``),
            # so a team renamed mid-tournament shows its current name.
            if line.get("name"):
                row["name"] = line["name"]
            if line.get("team_name"):
                row["team_name"] = line["team_name"]
            row["matches"] += 1
            row["bat_points"] += bat
            row["bowl_points"] += bowl
            row["bat_runs"] += int(line.get("bat_runs", 0) or 0)
            row["bat_balls"] += int(line.get("bat_balls", 0) or 0)
            row["bowl_wickets"] += int(line.get("bowl_wickets", 0) or 0)
            row["bowl_runs"] += int(line.get("bowl_runs", 0) or 0)
            row["bowl_balls"] += int(line.get("bowl_balls", 0) or 0)

            side = side_of(line)
            if side == "win":
                row["wins"] += 1
                row["result_points"] += WIN_BONUS
            elif side == "tie":
                row["result_points"] += TIE_BONUS

        potm = _match_potm(scored, side_of)
        if potm is not None and potm in agg:
            agg[potm]["awards"] += 1
            agg[potm]["award_points"] += POTM_BONUS

    rows = []
    for row in agg.values():
        total = (row["bat_points"] + row["bowl_points"]
                 + row["result_points"] + row["award_points"])
        rows.append(SimpleNamespace(points=round(total, 1), **row))
    return rows


def _sort_key(row, field):
    """Rank on one points column, breaking ties the way a selector would."""
    return (getattr(row, field), row.awards, row.wins, -row.matches)


def mvp_table(session, tournament_id, limit=10, board="overall"):
    """The Top-``limit`` MVP rows for one board.

    ``board`` is ``overall`` (everything), ``batting`` (batting points only) or
    ``bowling`` (bowling points only). The batting and bowling boards rank on
    their own column but still carry the full row, so the card can show what
    else that player did.
    """
    field = {"batting": "bat_points", "bowling": "bowl_points"}.get(board, "points")
    rows = [r for r in mvp_rows(session, tournament_id) if getattr(r, field) > 0]
    rows.sort(key=lambda r: _sort_key(r, field), reverse=True)
    return rows[:int(limit)] if limit else rows


def rubric_lines():
    """The scoring rules, as the card prints them.

    A leaderboard nobody can explain gets argued with instead of chased, so the
    numbers here are generated from the constants above — they cannot drift out
    of step with the scoring itself.
    """
    return [
        f"🏏 runs · +{FOUR_POINTS} a four · +{SIX_POINTS} a six · "
        f"+{SR_BONUS_GOOD}/{SR_BONUS_HIGH} SR 130/150 · "
        f"+{FIFTY_BONUS}/{HUNDRED_BONUS} a 50/100",
        f"🎯 +{WICKET_POINTS} a wicket · economy vs {ECONOMY_BASELINE:.2f} "
        f"(×{ECONOMY_WEIGHT} an over) · +{THREE_FOR_BONUS}/{FIVE_FOR_BONUS} a 3/5-for",
        f"🏆 +{WIN_BONUS} a win (+{TIE_BONUS} a tie) · "
        f"⭐ +{POTM_BONUS} a Player of the Match",
    ]
