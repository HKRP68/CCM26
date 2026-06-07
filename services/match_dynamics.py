"""Shared match dynamics — chase pressure, scenario (dramatic finish) steering,
and the super-over tie-breaker.

These helpers are UI-agnostic so every match flow (the auto-sim /sim, the chat
flows /playmatch and /vsbot, and the Mini App /wpm and /wpmbot) can reuse the
same logic. Pressure feeds the existing services.probability_engine
calculate_outcome `pressure` knob; the super-over helpers drive both an
auto-resolved tie-breaker (/sim, bots) and the interactive per-ball flows.
"""

import random

from services.probability_engine import calculate_outcome

SO_WICKET_LIMIT = 2          # a super over ends after 2 wickets
SO_BALLS = 6                 # ...or 6 legal balls
SO_BATSMEN = 3               # each side nominates 3 batsmen
SO_MAX_REPEATS = 3           # repeat tied super overs at most this many times


# ── Pressure & scenario ────────────────────────────────────────────────

def chase_pressure(innings, target, total_runs, balls_bowled, total_overs, wickets=0):
    """Realistic chase pressure in [0,1] from required run rate + death overs.

    Used by ALL flows (including human deliveries). balls_bowled = legal balls
    bowled so far in the current innings.
    """
    if innings != 2 or not target:
        return 0.0
    balls_left = total_overs * 6 - balls_bowled
    if balls_left <= 0:
        return 0.0
    needed = max(0, target - total_runs)
    if needed <= 0:
        return 0.0
    rrr = needed / balls_left * 6.0
    p = 0.0
    if rrr > 9.0:
        p += min((rrr - 9.0) / 9.0, 0.7)        # up to +0.7 around RRR 15+
    death_start = total_overs - max(1, round(total_overs * 0.2))
    if (balls_bowled // 6 + 1) > death_start:
        p += 0.15                                # death-over desperation
    return max(0.0, min(1.0, p))


def scenario_boost(innings, target, total_runs, balls_bowled, total_overs, enabled=True):
    """Extra 'drama' pressure for close finishes — sim & bot turns only.

    Returns a small additive boost (≤0.30) when the 2nd innings is in its last
    couple of overs and the result is genuinely on a knife edge, nudging the
    engine toward last-over thrillers. No-op when disabled or not close.
    """
    if not enabled or innings != 2 or not target:
        return 0.0
    balls_left = total_overs * 6 - balls_bowled
    if balls_left <= 0 or balls_left > 12:
        return 0.0
    needed = target - total_runs
    if 1 <= needed <= balls_left * 2 + 2:        # a plausibly winnable margin
        return min(0.30, 0.30 * (1.0 - balls_left / 12.0))
    return 0.0


def total_pressure(state, *, scenario=False):
    """Convenience: combine chase pressure + optional scenario boost from a
    match-engine state dict (services.match_engine shape)."""
    innings = state.get("innings", 1)
    target = state.get("target")
    runs = state.get("total_runs", 0)
    overs = state.get("overs", 20)
    balls_bowled = (state.get("current_over", 1) - 1) * 6 + state.get("current_ball", 0)
    wkts = state.get("total_wickets", 0)
    p = chase_pressure(innings, target, runs, balls_bowled, overs, wkts)
    if scenario:
        p = min(1.0, p + scenario_boost(innings, target, runs, balls_bowled, overs, True))
    return p


# ── Super over selection ───────────────────────────────────────────────

def super_over_batsmen(xi, n=SO_BATSMEN):
    """Top-n batsmen (by batting rating) nominated for the super over."""
    return sorted(xi, key=lambda p: p.get("bat_rating", 0) or p.get("rating", 0),
                  reverse=True)[:n]


def super_over_bowler(xi):
    """Best eligible (Bowler/All-rounder) bowler for the super over."""
    elig = [p for p in xi if p.get("category") in ("Bowler", "All-rounder")] or list(xi)
    return max(elig, key=lambda p: p.get("bowl_rating", 0))


# ── Auto super over (used by /sim and bot-vs-bot) ──────────────────────

def simulate_super_over_innings(bat_xi, bowl_xi, pitch, run_factor=1.0,
                                target=None, commentary=None, feed=None,
                                label="Super Over"):
    """Auto-simulate one super-over innings (1 over, 2 wickets, 3 batsmen).

    Returns {runs, wickets, balls, boundaries}. Reuses calculate_outcome so
    scoring matches the rest of the engine.
    """
    from services.sim_match import _HOW_TO_EVENT, _RUNS_TO_EVENT, _ATTACK
    batsmen = super_over_batsmen(bat_xi)
    bowler = super_over_bowler(bowl_xi)

    runs = wkts = balls = boundaries = 0
    striker_i, next_i = 0, 2  # batsmen[0] & [1] open; [2] is the reserve
    free_hit = False

    def _emit(event_key, batsman):
        if feed is None:
            return
        line = None
        if commentary:
            try:
                line = commentary(event_key, batsman, bowler["name"],
                                  "the fielder", "the keeper", 0)
            except Exception:
                line = None
        feed.append({"phase": label, "ball": f"{balls}", "score": f"{runs}/{wkts}",
                     "event": event_key, "text": line or ""})

    while balls < SO_BALLS and wkts < SO_WICKET_LIMIT:
        striker = batsmen[striker_i]
        shot = random.choice(_ATTACK)  # super over = all-out attack
        oc = calculate_outcome(
            bowler.get("bowl_style"), bowler.get("bowl_hand"),
            None, None, pitch, 20, 20, shot,
            striker.get("bat_rating", 0) or striker.get("rating", 0),
            bowler.get("bowl_rating", 0),
            free_hit=free_hit, pressure=0.6,
        )
        t = oc["type"]
        if t == "wide":
            runs += 1
            _emit("wide", striker["name"])
            continue
        if t == "noball":
            r = oc.get("runs", 0)
            runs += 1 + r
            if r in (4, 6):
                boundaries += 1
            free_hit = True
            _emit("no_ball", striker["name"])
            if r % 2 == 1:
                striker_i = 1 - striker_i if striker_i < 2 else striker_i
            continue
        balls += 1
        free_hit = False
        if t == "legbye":
            runs += oc.get("runs", 1)
            _emit("extras", striker["name"])
        elif t == "wicket":
            wkts += 1
            how = oc.get("how", "Caught")
            _emit(_HOW_TO_EVENT.get(how, "wicket_caught_fielder"), striker["name"])
            if next_i < len(batsmen):
                striker_i = next_i
                next_i += 1
            else:
                break
        else:
            r = oc.get("runs", 0)
            runs += r
            if r in (4, 6):
                boundaries += 1
            _emit(_RUNS_TO_EVENT.get(r, "general"), striker["name"])
            if r % 2 == 1 and next_i <= len(batsmen):
                # rotate strike between the two batsmen at the crease
                striker_i = 1 if striker_i == 0 else 0
        if target is not None and runs >= target:
            break

    return {"runs": runs, "wickets": wkts, "balls": balls, "boundaries": boundaries}


def resolve_super_over(team_a_xi, team_b_xi, a_name, b_name, pitch,
                       run_factor=1.0, commentary=None, feed=None,
                       first_bat="a"):
    """Auto-resolve a tie via super over(s).

    Returns {winner, loser, text, innings: [..], shared: bool}. On a tied super
    over the side with more boundaries wins; if still level it repeats (capped).
    """
    if first_bat == "a":
        first_xi, first_name, second_xi, second_name = team_a_xi, a_name, team_b_xi, b_name
    else:
        first_xi, first_name, second_xi, second_name = team_b_xi, b_name, team_a_xi, a_name

    innings_log = []
    for attempt in range(SO_MAX_REPEATS):
        i1 = simulate_super_over_innings(first_xi, second_xi, pitch, run_factor,
                                         target=None, commentary=commentary,
                                         feed=feed, label=f"Super Over {first_name}")
        i2 = simulate_super_over_innings(second_xi, first_xi, pitch, run_factor,
                                         target=i1["runs"] + 1, commentary=commentary,
                                         feed=feed, label=f"Super Over {second_name}")
        innings_log.append((first_name, i1, second_name, i2))

        if i2["runs"] > i1["runs"]:
            return _so_result(second_name, first_name, i1, i2, innings_log)
        if i1["runs"] > i2["runs"]:
            return _so_result(first_name, second_name, i1, i2, innings_log)
        # tied super over → more boundaries wins
        if i1["boundaries"] != i2["boundaries"]:
            if i1["boundaries"] > i2["boundaries"]:
                return _so_result(first_name, second_name, i1, i2, innings_log,
                                  note="(more boundaries)")
            return _so_result(second_name, first_name, i1, i2, innings_log,
                              note="(more boundaries)")
        # still tied → repeat

    return {"winner": None, "loser": None, "shared": True,
            "text": "Super Over tied — honours shared", "innings": innings_log}


def _so_result(winner, loser, i1, i2, log, note=""):
    return {
        "winner": winner, "loser": loser, "shared": False,
        "text": f"{winner} won the Super Over {note}".strip(),
        "innings": log,
    }
