"""Derived player skill profiles — "hardcore" ratings for the match engines.

A player's card carries two scalars (bat_rating, bowl_rating). Real cricketers
are not scalars: same-rated batters differ in power vs technique, some eat
spin and struggle against pace, some are death-over finishers; same-rated
bowlers differ in wicket threat vs control, new-ball bite vs death-over nerve.

This module expands the scalars into a stable per-player skill profile:

  Batting (each 0-99, centred on bat_rating):
    power       — six-hitting muscle (raises 6s at the cost of nothing —
                  the offsets are zero-sum, see below)
    timing      — four-hitting placement
    technique   — dismissal resistance (edges, gates, pads)
    running     — 2s/3s between the wickets
    vs_pace     — skill against pace bowling (matchup shift)
    vs_spin     — skill against spin bowling (matchup shift)
    finishing   — death-overs composure (boundaries up, panic down)

  Bowling (each 0-99, centred on bowl_rating):
    threat      — raw wicket-taking menace
    control     — dot-ball pressure, fewer wides/no-balls, boundary denial
    death       — death-overs execution (yorkers under fire)
    new_ball    — powerplay bite with the hard new ball

Determinism: offsets are hashed from the player's stable identity
(player_id when present, else name) so "V Kohli 91" has the SAME profile in
every match, on every server — profiles are personality, not dice.

Zero-sum design: each offset is drawn symmetrically around the base rating
and the batting scoring offsets (power/timing/running) are re-centred so
their mean is ~0. A profile redistributes HOW a player scores, it does not
make them secretly better or worse than their card. Matchup offsets
(vs_pace/vs_spin) are a reciprocal pair: strong vs spin ⇒ weaker vs pace.

Role shaping (small, on top of the hash):
    Batsman      → technique +2
    Wicketkeeper → running +2
    All-rounder  → power +1, running +1
    Bowler       → technique -2 (tail technique is genuinely worse)
    Fast bowling styles  → threat +2, new_ball +3, death +2
    Spin bowling styles  → control +2, new_ball -3
"""

import hashlib

# Spread (± rating points) of each hashed offset around the base rating.
BAT_OFFSET_SPREAD = {
    "power": 8.0, "timing": 7.0, "technique": 7.0, "running": 9.0,
    "finishing": 8.0,
}
MATCHUP_SPREAD = 6.0          # vs_pace / vs_spin reciprocal pair
BOWL_OFFSET_SPREAD = {
    "threat": 7.0, "control": 7.0, "death": 8.0, "new_ball": 8.0,
}

_FAST_STYLES = ("fast", "medium")     # substring match on bowl_style
_SPIN_STYLES = ("spin", "orthodox", "chinaman")


def _identity(player):
    pid = player.get("player_id")
    if pid not in (None, "", 0):
        return f"pid:{pid}"
    return f"name:{str(player.get('name') or 'Player').strip().lower()}"


def _unit(player, salt):
    """Deterministic float in [-1, 1) from the player's identity + salt."""
    h = hashlib.md5(f"{_identity(player)}|{salt}".encode()).hexdigest()
    return (int(h[:8], 16) / 0x100000000) * 2.0 - 1.0


def _clamp(v, lo=5.0, hi=99.0):
    return max(lo, min(hi, float(v)))


def batting_profile(player):
    """Return the batter's derived skill dict. Pure + deterministic."""
    base = float(player.get("bat_rating") or player.get("batting_rating")
                 or player.get("rating") or 50)
    category = (player.get("category") or "").lower()

    offs = {k: _unit(player, k) * s for k, s in BAT_OFFSET_SPREAD.items()}
    # Re-centre the scoring trio so a profile can't inflate total output.
    scoring = ("power", "timing", "running")
    drift = sum(offs[k] for k in scoring) / len(scoring)
    for k in scoring:
        offs[k] -= drift

    # Role shaping
    if "batsman" in category or "batter" in category:
        offs["technique"] += 2.0
    elif "keeper" in category:
        offs["running"] += 2.0
    elif "all" in category:
        offs["power"] += 1.0
        offs["running"] += 1.0
    elif "bowler" in category:
        offs["technique"] -= 2.0

    matchup = _unit(player, "matchup") * MATCHUP_SPREAD
    return {
        "base": base,
        "power":     _clamp(base + offs["power"]),
        "timing":    _clamp(base + offs["timing"]),
        "technique": _clamp(base + offs["technique"]),
        "running":   _clamp(base + offs["running"]),
        "finishing": _clamp(base + offs["finishing"]),
        # Reciprocal pair: what one matchup gives, the other takes.
        "vs_pace":   _clamp(base + matchup),
        "vs_spin":   _clamp(base - matchup),
    }


def bowling_profile(player):
    """Return the bowler's derived skill dict. Pure + deterministic."""
    base = float(player.get("bowl_rating") or player.get("bowling_rating") or 40)
    style = (player.get("bowl_style") or player.get("bowling_type") or "").lower()

    offs = {k: _unit(player, "bw_" + k) * s for k, s in BOWL_OFFSET_SPREAD.items()}
    if any(s in style for s in _FAST_STYLES):
        offs["threat"] += 2.0
        offs["new_ball"] += 3.0
        offs["death"] += 2.0
    elif any(s in style for s in _SPIN_STYLES):
        offs["control"] += 2.0
        offs["new_ball"] -= 3.0

    return {
        "base": base,
        "threat":   _clamp(base + offs["threat"]),
        "control":  _clamp(base + offs["control"]),
        "death":    _clamp(base + offs["death"]),
        "new_ball": _clamp(base + offs["new_ball"]),
    }


# ─────────────────────────────────────────────────────────────────────
# Effect helpers shared by both engines
# ─────────────────────────────────────────────────────────────────────
# Deltas are (sub_rating - base)/25 → roughly [-0.45, +0.45] at the spread
# extremes, so every multiplier below stays comfortably positive (worst
# stacked case ~0.6x). Coefficients are deliberately punchy — this layer
# exists to make same-rated players FEEL different ball to ball.

def bat_delta(profile, key):
    return (profile.get(key, profile["base"]) - profile["base"]) / 25.0


def bowl_delta(profile, key):
    return (profile.get(key, profile["base"]) - profile["base"]) / 25.0


def profile_multipliers(bat_profile, bowl_profile, *, bowler_is_spin,
                        phase):
    """Outcome-weight multipliers from the two profiles for one delivery.

    phase: "powerplay" | "middle" | "death"
    Returns a dict keyed by canonical outcome names:
      six, four, two, three, dot, wicket, extras
    All values multiplicative, 1.0 = neutral. Engine adapters map these onto
    their own outcome keys.
    """
    m = {"six": 1.0, "four": 1.0, "two": 1.0, "three": 1.0,
         "dot": 1.0, "wicket": 1.0, "extras": 1.0}

    if bat_profile:
        d = lambda k: bat_delta(bat_profile, k)
        m["six"] *= 1.0 + 0.50 * d("power")
        m["four"] *= 1.0 + 0.40 * d("timing")
        m["two"] *= 1.0 + 0.30 * d("running")
        m["three"] *= 1.0 + 0.30 * d("running")
        m["wicket"] *= 1.0 - 0.40 * d("technique")
        # Matchup: this bowler's style vs the batter's pace/spin skill.
        mu = d("vs_spin") if bowler_is_spin else d("vs_pace")
        m["six"] *= 1.0 + 0.25 * mu
        m["four"] *= 1.0 + 0.25 * mu
        m["wicket"] *= 1.0 - 0.30 * mu
        m["dot"] *= 1.0 - 0.15 * mu
        if phase == "death":
            f = d("finishing")
            m["six"] *= 1.0 + 0.35 * f
            m["four"] *= 1.0 + 0.20 * f
            m["wicket"] *= 1.0 - 0.20 * f

    if bowl_profile:
        e = lambda k: bowl_delta(bowl_profile, k)
        m["wicket"] *= 1.0 + 0.45 * e("threat")
        m["dot"] *= 1.0 + 0.30 * e("control")
        m["four"] *= 1.0 - 0.20 * e("control")
        m["extras"] *= 1.0 - 0.35 * e("control")
        if phase == "death":
            dd = e("death")
            m["six"] *= 1.0 - 0.30 * dd
            m["four"] *= 1.0 - 0.25 * dd
            m["wicket"] *= 1.0 + 0.25 * dd
        elif phase == "powerplay":
            nb = e("new_ball")
            m["wicket"] *= 1.0 + 0.25 * nb
            m["dot"] *= 1.0 + 0.12 * nb

    return m
