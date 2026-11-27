"""Validate and load a recorded scorecard at a CIPL over boundary."""

from collections import Counter

from services import cipl_match as match
from services.event_studio_rules import integer


def _fields(row, fields, label):
    if not isinstance(row, dict) or set(row) != set(fields):
        raise ValueError(f"Moment {label} must contain {', '.join(fields)}.")


def _rows(value, field, label, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"Moment {label} has too many rows or is not a list.")
    rows = {}
    for row in value:
        _fields(row, field, label)
        slot = integer(row["slot"], f"{label} XI position", 1, 11)
        if slot in rows:
            raise ValueError(f"Moment {label} repeats XI position {slot}.")
        rows[slot] = row
    return rows


def load(state, scorecard, *, complete=False):
    """Load observed figures only. Active checkpoints require six-ball boundaries."""
    _fields(scorecard, ("runs", "wickets", "extras", "batting", "bowling", "overs",
                        "crease", "partnership"), "scorecard")
    runs = integer(scorecard["runs"], "Moment score", 0, 3000)
    wickets = integer(scorecard["wickets"], "Moment wickets", 0, 10)
    extras = integer(scorecard["extras"], "Moment extras", 0, runs)
    overs = scorecard["overs"]
    if not isinstance(overs, list) or len(overs) > state["overs"]:
        raise ValueError("Moment overs exceed the innings quota.")
    crease = scorecard["crease"]
    if not isinstance(crease, list) or len(crease) != (1 if wickets == 10 else 2):
        raise ValueError("Set the active batter slots in striker, non-striker order.")
    crease = [integer(slot, "Moment crease slot", 1, 11) for slot in crease]
    if len(set(crease)) != len(crease):
        raise ValueError("The striker and non-striker must be distinct.")
    bat = _rows(scorecard["batting"], ("slot", "runs", "balls", "fours", "sixes", "dots", "dismissal", "bowler"),
                "batting", 11)
    bowl = _rows(scorecard["bowling"], ("slot", "runs", "balls", "wickets", "dots", "maidens"),
                 "bowling", 11)
    bat_xi, bowl_xi = state["bat_xi"], state["bowl_xi"]
    over_totals, over_wickets = [], []
    spell_balls, spell_runs, maidens, last_wickets = Counter(), Counter(), Counter(), {}
    previous = None
    for index, row in enumerate(overs):
        _fields(row, ("bowler", "balls", "runs", "wickets", "credited_wickets", "maidens"), "over")
        slot = integer(row["bowler"], "Moment over bowler slot", 1, 11)
        minimum = 0 if complete and wickets == 10 and index == len(overs) - 1 else 6
        balls = integer(row["balls"], "Moment over legal balls", minimum, 6)
        over_runs = integer(row["runs"], "Moment runs in over", 0, runs)
        over_outs = integer(row["wickets"], "Moment wickets in over", 0, wickets)
        credited = integer(row["credited_wickets"], "Moment wickets credited in over", 0, over_outs)
        maiden = integer(row["maidens"], "Moment maidens in over", 0, int(balls == 6))
        if slot == previous or slot not in bowl or spell_balls[slot] + balls > match.quota_for(state, bowl_xi[slot - 1]) * 6:
            raise ValueError("Moment bowling history breaks a bowler quota or consecutive-over rule.")
        spell_balls[slot] += balls
        spell_runs[slot] += over_runs
        maidens[slot] += maiden
        last_wickets[slot] = credited
        previous = slot
        over_totals.append(over_runs)
        over_wickets.append(over_outs)
    legal = sum(spell_balls.values())
    if sum(over_totals) != runs or sum(over_wickets) != wickets or wickets > legal + extras:
        raise ValueError("Moment over totals do not reconcile with the score.")
    if complete != (wickets == 10 or legal == match.total_balls(state)):
        raise ValueError("Moment first innings must be complete and the active innings must be playable.")

    dismissed, credited_wickets = [], Counter()
    batter_total = 0
    for slot, row in bat.items():
        bat_runs = integer(row["runs"], "Moment batter runs", 0, runs)
        faced = integer(row["balls"], "Moment batter balls", 0, legal + extras)
        fours = integer(row["fours"], "Moment batter fours", 0, bat_runs // 4)
        sixes = integer(row["sixes"], "Moment batter sixes", 0, bat_runs // 6)
        dots = integer(row["dots"], "Moment batter dots", 0, faced)
        if fours * 4 + sixes * 6 > bat_runs or fours + sixes + dots > faced:
            raise ValueError("Moment batter boundaries or dots exceed their figures.")
        how = row["dismissal"]
        if not isinstance(how, str) or len(how) > 80:
            raise ValueError("Moment dismissal text is invalid.")
        bowler = row["bowler"]
        if bowler is not None:
            if type(bowler) is not int or bowler not in bowl or not how:
                raise ValueError("Moment dismissal must credit a bowler with figures.")
            credited_wickets[bowler] += 1
        if how:
            dismissed.append(slot)
        elif slot not in crease:
            raise ValueError("Only batters who are dismissed or at the crease may have figures.")
        player = bat_xi[slot - 1]
        state["bat_stats"][str(player["roster_id"])].update(
            runs=bat_runs, balls=faced, fours=fours, sixes=sixes, dots=dots, batted=True,
            out=bool(how), how_out=how, dismissal=how,
            bowled_by=bowl_xi[bowler - 1]["name"] if bowler is not None else "")
        batter_total += bat_runs
    if batter_total + extras != runs or len(dismissed) != wickets or set(dismissed) & set(crease):
        raise ValueError("Moment batter totals, dismissals and extras do not reconcile.")
    if legal and set(crease) - bat.keys():
        raise ValueError("Provide scorecard figures for both batters at the crease.")
    if not legal <= sum(row["balls"] for row in bat.values()) <= legal + extras:
        raise ValueError("Moment batter balls do not reconcile with legal balls and extras.")

    claimed_by_over = Counter()
    for row in overs:
        claimed_by_over[row["bowler"]] += row["credited_wickets"]
    conceded = 0
    if any(slot not in spell_balls for slot in bowl) or credited_wickets != claimed_by_over:
        raise ValueError("Moment credited wickets and bowling overs do not reconcile.")
    for slot, row in bowl.items():
        if (type(row["balls"]) is not int or row["balls"] != spell_balls[slot] or
                type(row["wickets"]) is not int or row["wickets"] != credited_wickets[slot] or
                type(row["maidens"]) is not int or row["maidens"] != maidens[slot]):
            raise ValueError("Moment bowler balls, wickets or maidens do not reconcile.")
        charged = integer(row["runs"], "Moment bowler runs conceded",
                          max(0, spell_runs[slot] - extras), spell_runs[slot])
        dots = integer(row["dots"], "Moment bowler dots", 0, row["balls"])
        conceded += charged
        state["bowl_stats"][str(bowl_xi[slot - 1]["roster_id"])].update(
            balls=row["balls"], runs=charged, wickets=row["wickets"], dots=dots,
            overs_done=row["balls"] // 6, maidens=row["maidens"], last_over_wickets=last_wickets[slot])
    if not runs - extras <= conceded <= runs:
        raise ValueError("Moment bowling runs conceded do not reconcile with extras.")

    order = [bat_xi[slot - 1] for slot in [*dismissed, *crease]]
    queued = set(dismissed) | set(crease)
    order.extend(player for slot, player in enumerate(bat_xi, 1) if slot not in queued)
    partnership = scorecard["partnership"]
    _fields(partnership, ("runs", "balls"), "current partnership")
    partnership_runs = integer(partnership["runs"], "Moment partnership runs", 0, runs)
    partnership_balls = integer(partnership["balls"], "Moment partnership balls", 0, legal)
    if not wickets and (partnership_runs != runs or partnership_balls != legal):
        raise ValueError("The opening partnership must reconcile with the innings.")
    state.update(total_runs=runs, total_wickets=wickets, extras_total=extras,
                 current_over=legal // 6 + 1, current_ball=legal % 6,
                 batting_order=order, striker_idx=wickets, non_striker_idx=min(10, wickets + 1),
                 next_batsman_idx=min(11, wickets + 2), over_runs=over_totals, over_wkts=over_wickets,
                 prev_bowler_rid=bowl_xi[previous - 1]["roster_id"] if previous else None,
                 partnership_runs=partnership_runs, partnership_balls=partnership_balls)
    if not complete and not match.eligible_bowlers(state):
        raise ValueError("Moment bowling quotas cannot cover the remaining innings.")
