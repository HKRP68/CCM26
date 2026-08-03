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
# Wides and no-balls are re-bowled, so a pathological run of them would never
# reach 6 legal balls. Cap the total deliveries so an innings always terminates.
SO_MAX_DELIVERIES = 24
# Nerves. Six balls with a match on them is the most pressure a bowler ever
# bowls under, and the engine's base 1.3% wide / 0.4% no-ball made an extra a
# novelty. Doubling the extras weights (and only those — runs and wickets are
# untouched) puts a wide in roughly one super over in seven. Shared with the
# interactive Super Over, which uses the same multiplier.
SO_EXTRAS_MULT = 2.0


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

def _name_set(exclude):
    """Normalise an exclusion argument (a single name, or any collection of
    names) into a set."""
    if not exclude:
        return set()
    if isinstance(exclude, str):
        return {exclude}
    return set(exclude)


def super_over_batsmen(xi, n=SO_BATSMEN, exclude=None):
    """Top-n batsmen (by batting rating) nominated for the super over.

    ``exclude`` is the names that already BATTED in an earlier super over of the
    same tie — they are spent and cannot bat again, so they are skipped. The
    pool is only topped back up with them if the restriction would leave the
    side short (which takes several replays off an XI of eleven).
    """
    ranked = sorted(xi, key=lambda p: p.get("bat_rating", 0) or p.get("rating", 0),
                    reverse=True)
    out = _name_set(exclude)
    pool = [p for p in ranked if p.get("name") not in out]
    for p in ranked:                       # safety valve: a side must always bat
        if len(pool) >= min(n, len(ranked)):
            break
        if p.get("name") in out:
            pool.append(p)
    return pool[:n]


def super_over_bowler(xi, exclude=None):
    """Best eligible (Bowler/All-rounder) bowler for the super over.

    ``exclude`` is every bowler who has already bowled a super over in this tie
    (a name or a collection of them) — a bowler is spent once they have bowled.
    """
    elig = [p for p in xi if p.get("category") in ("Bowler", "All-rounder")] or list(xi)
    out = _name_set(exclude)
    if out:
        fresh = [p for p in elig if p.get("name") not in out]
        if fresh:
            elig = fresh
    return max(elig, key=lambda p: p.get("bowl_rating", 0))


# ── Auto super over (used by /sim and bot-vs-bot) ──────────────────────

def simulate_super_over_innings(bat_xi, bowl_xi, pitch, run_factor=1.0,
                                target=None, commentary=None, feed=None,
                                label="Super Over", exclude_batsmen=None,
                                exclude_bowler=None):
    """Auto-simulate one super-over innings (1 over, 2 wickets, 3 batsmen).

    Both ends of the crease are tracked. That is the whole point: a super over
    nominates three batsmen, so once one is out the pair at the crease is no
    longer "slots 0 and 1" — and rotating strike as if it were put a batsman
    who had already been dismissed back on strike, letting a side keep batting
    with an out player. The striker and non-striker are now real positions and
    a dismissed batsman never returns.

    Returns {runs, wickets, balls, deliveries, boundaries, sixes, fours,
    dismissed, batted, bowler}. ``sixes``/``fours`` feed the boundary countback;
    ``batted``/``bowler`` feed the replay restrictions in
    :func:`resolve_super_over` — ``batted`` is everyone who actually walked out,
    which is what a replay treats as spent (a reserve who was never needed is
    still available). Reuses calculate_outcome so scoring matches the rest of
    the engine.
    """
    from services.sim_match import _HOW_TO_EVENT, _RUNS_TO_EVENT, _ATTACK
    batsmen = super_over_batsmen(bat_xi, exclude=exclude_batsmen)
    bowler = super_over_bowler(bowl_xi, exclude=exclude_bowler)

    runs = wkts = balls = deliveries = 0
    sixes = fours = 0
    dismissed = []
    if not batsmen:
        return {"runs": 0, "wickets": 0, "balls": 0, "deliveries": 0,
                "boundaries": 0, "sixes": 0, "fours": 0, "dismissed": [],
                "batted": [], "bowler": bowler.get("name") if bowler else None}
    # batsmen[0] & [1] open, batsmen[2] is the reserve. ``next_i`` is the next
    # man in — never an index that has already been at the crease.
    striker_i = 0
    non_striker_i = 1 if len(batsmen) > 1 else 0
    next_i = 2
    free_hit = False
    # Everyone who actually walked out. A reserve who is never needed did not
    # bat, so they stay available for a replay.
    batted = [batsmen[striker_i].get("name")]
    if non_striker_i != striker_i:
        batted.append(batsmen[non_striker_i].get("name"))

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

    def _chased():
        return target is not None and runs >= target

    while (balls < SO_BALLS and wkts < SO_WICKET_LIMIT
           and deliveries < SO_MAX_DELIVERIES):
        striker = batsmen[striker_i]
        shot = random.choice(_ATTACK)  # super over = all-out attack
        oc = calculate_outcome(
            bowler.get("bowl_style"), bowler.get("bowl_hand"),
            None, None, pitch, 20, 20, shot,
            striker.get("bat_rating", 0) or striker.get("rating", 0),
            bowler.get("bowl_rating", 0),
            free_hit=free_hit, pressure=0.6, extras_mult=SO_EXTRAS_MULT,
        )
        deliveries += 1
        t = oc["type"]
        # Wides and no-balls are re-bowled, but they still carry the penalty run
        # and can still win a chase — so the target is checked on every delivery,
        # not just the legal ones.
        if t == "wide":
            runs += 1
            _emit("wide", striker["name"])
            if _chased():
                break
            continue
        if t == "noball":
            r = oc.get("runs", 0)
            runs += 1 + r
            if r == 6:
                sixes += 1
            elif r == 4:
                fours += 1
            free_hit = True
            _emit("no_ball", striker["name"])
            if r % 2 == 1:
                striker_i, non_striker_i = non_striker_i, striker_i
            if _chased():
                break
            continue
        balls += 1
        free_hit = False
        if t == "legbye":
            r = oc.get("runs", 1)
            runs += r
            _emit("extras", striker["name"])
            if r % 2 == 1:
                striker_i, non_striker_i = non_striker_i, striker_i
        elif t == "wicket":
            runs += int(oc.get("runs", 0) or 0)   # completed runs on a run out
            wkts += 1
            dismissed.append(striker.get("name"))
            how = oc.get("how", "Caught")
            _emit(_HOW_TO_EVENT.get(how, "wicket_caught_fielder"), striker["name"])
            if wkts >= SO_WICKET_LIMIT or next_i >= len(batsmen):
                break                              # 2 down — the innings is over
            striker_i = next_i                     # new man in at the same end
            batted.append(batsmen[striker_i].get("name"))
            next_i += 1
        else:
            r = oc.get("runs", 0)
            runs += r
            if r == 6:
                sixes += 1
            elif r == 4:
                fours += 1
            _emit(_RUNS_TO_EVENT.get(r, "general"), striker["name"])
            if r % 2 == 1:
                striker_i, non_striker_i = non_striker_i, striker_i
        if _chased():
            break

    return {"runs": runs, "wickets": wkts, "balls": balls,
            "deliveries": deliveries, "boundaries": sixes + fours,
            "sixes": sixes, "fours": fours, "dismissed": dismissed,
            "batted": batted,
            "bowler": bowler.get("name") if bowler else None}


def resolve_super_over(team_a_xi, team_b_xi, a_name, b_name, pitch,
                       run_factor=1.0, commentary=None, feed=None,
                       first_bat="a"):
    """Auto-resolve a tie via super over(s).

    Returns {winner, loser, text, innings: [..], shared: bool}. Level on runs
    goes to cricket's boundary countback — most sixes, then most fours — before
    a replay, which matches the interactive Super Over (handlers/super_over.py)
    instead of lumping every boundary together.

    A replay carries the real restrictions across: the side that batted second
    bats first, and every player a side has USED is spent — a batsman who walked
    out cannot bat again and a bowler who bowled cannot bowl again, in any later
    super over of the same tie. A reserve who was nominated but never needed was
    not used, so they stay available.
    """
    a = {"xi": team_a_xi, "name": a_name, "out": [], "bowled": set()}
    b = {"xi": team_b_xi, "name": b_name, "out": [], "bowled": set()}
    first, second = (a, b) if first_bat == "a" else (b, a)

    innings_log = []
    for attempt in range(SO_MAX_REPEATS):
        i1 = simulate_super_over_innings(
            first["xi"], second["xi"], pitch, run_factor, target=None,
            commentary=commentary, feed=feed,
            label=f"Super Over {first['name']}",
            exclude_batsmen=first["out"], exclude_bowler=second["bowled"])
        first["out"].extend(i1.get("batted") or [])
        second["bowled"].add(i1.get("bowler"))

        i2 = simulate_super_over_innings(
            second["xi"], first["xi"], pitch, run_factor, target=i1["runs"] + 1,
            commentary=commentary, feed=feed,
            label=f"Super Over {second['name']}",
            exclude_batsmen=second["out"], exclude_bowler=first["bowled"])
        second["out"].extend(i2.get("batted") or [])
        first["bowled"].add(i2.get("bowler"))

        innings_log.append((first["name"], i1, second["name"], i2))

        if i2["runs"] > i1["runs"]:
            return _so_result(second["name"], first["name"], i1, i2, innings_log)
        if i1["runs"] > i2["runs"]:
            return _so_result(first["name"], second["name"], i1, i2, innings_log)
        # Level on runs → boundary countback: most sixes, then most fours.
        for key, note in (("sixes", "(more sixes)"), ("fours", "(more fours)")):
            if i1.get(key, 0) != i2.get(key, 0):
                if i1[key] > i2[key]:
                    return _so_result(first["name"], second["name"], i1, i2,
                                      innings_log, note=note)
                return _so_result(second["name"], first["name"], i1, i2,
                                  innings_log, note=note)
        # Still level → another super over, chasing side batting first.
        first, second = second, first

    return {"winner": None, "loser": None, "shared": True,
            "text": "Super Over tied — honours shared", "innings": innings_log}


def _so_result(winner, loser, i1, i2, log, note=""):
    return {
        "winner": winner, "loser": loser, "shared": False,
        "text": f"{winner} won the Super Over {note}".strip(),
        "innings": log,
    }
