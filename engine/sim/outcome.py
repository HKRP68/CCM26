"""The ball outcome engine.

Pseudocode (every constant is in ``config/sim_engine.json``)::

    fs  = factors.compose(conditions, ball, bowler)           # environment
    sit = situation.evaluate(...)                             # match state
    help        = skill-weighted mean of the channels this bowler can use
                  (pace: pace/swing|reverse/seam/bounce, spin: spin)
    bowlerEdge  = (0.45 + 0.55*skill) * (0.8 + 0.4*accuracy) * help * sit.bowler_edge
    batterEdge  = (0.45 + 0.55*(technique+matchup)/2) * form
    shotQuality = batterEdge * bat_ease * bat_speed * sit.shot_quality
    ratio       = bowlerEdge / shotQuality                    # 1.0 = even contest
    Wicket *= ratio^s_w * pace|spin_wkt * sit.wicket * aggression_wkt
    Four   *= (1/ratio)^s_b * aggression_bnd * four_carry
    Six    *= (1/ratio)^s_b * aggression_bnd * six
    Dot    *= ratio^s_d            Extras *= (1/accuracy)^s_e
    each multiplier clamped to bounds, weights renormalised, one draw
    1/2/3 → 4 when a gap shot's power beats 100 - outfieldSpeed
    Caught → dropped with P(base + dew + drama)

``bucket_multipliers`` is shared with the live hook, which calls it twice (real
conditions, neutral conditions) and applies only the ratio.
"""

from dataclasses import dataclass
from typing import Optional

from engine.sim import stadium as stadium_mod
from engine.sim.situation import SituationMods

BUCKETS = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")
RUNS = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6}


@dataclass
class BallResult:
    bucket: str
    runs: int = 0
    extra_type: Optional[str] = None
    extra_runs: int = 0
    wicket_type: Optional[str] = None
    dropped: bool = False
    outfield_four: bool = False
    legal: bool = True
    tags: tuple = ()

    @property
    def is_wicket(self):
        return self.wicket_type is not None

    @property
    def total_runs(self):
        return self.runs + self.extra_runs


def _lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def bowler_help(bowler, fs, cfg):
    """How much of the conditions' help this particular bowler can use."""
    if bowler is None:
        return 1.0
    if bowler.is_spin:
        return fs["spin"]
    from engine.sim.ball_aging import bowler_can_reverse

    mix = cfg["outcome"]["bowlingMix"]["pace"]
    swing = fs["swing"]
    if fs.reverse and bowler_can_reverse(bowler, cfg):
        swing = max(swing, fs.reverse)
    parts = ((mix["pace"] * bowler.pace, fs["pace"]),
             (mix["swing"] * bowler.swing, swing),
             (mix["seam"] * bowler.seam, fs["seam"]))
    wsum = sum(w for w, _ in parts) or 1.0
    h = sum(w * f for w, f in parts) / wsum
    bw = cfg["outcome"]["bowlingMix"]["bounceWeight"]
    h *= 1 + bw * (bowler.pace / 100.0) * (fs["bounce"] - 1)
    return max(0.05, h)


def damping(cfg, fmt=None):
    """The damping exponents, with any per-format overrides applied."""
    d = dict(cfg["outcome"]["damping"])
    d.update((d.pop("byFormat", None) or {}).get(fmt or "", {}))
    return d


def edges(batter, bowler, fs, sit, cfg, fatigue_accuracy=1.0, fmt=None):
    """(bowler_edge, shot_quality, aggression, accuracy) for one delivery."""
    sit = sit or SituationMods()
    oc = cfg["outcome"]
    dmp = damping(cfg, fmt)
    if bowler is not None:
        skill = (bowler.spin if bowler.is_spin
                 else (bowler.pace + bowler.swing + bowler.seam) / 3.0) / 100.0
        acc = min(1.2, bowler.accuracy / 100.0 * sit.accuracy * fs.accuracy * fatigue_accuracy)
        help_ = bowler_help(bowler, fs, cfg) ** dmp["bowlerHelp"]
        bowler_edge = (0.45 + 0.55 * skill) * (0.8 + 0.4 * acc) * help_
        bowler_edge *= sit.bowler_edge
        is_spin = bowler.is_spin
    else:
        bowler_edge, acc, is_spin = 1.0, 0.6, False

    if batter is not None:
        sw = oc["skillWeights"]
        matchup = batter.vs_spin if is_spin else batter.vs_pace
        bskill = (sw["technique"] * batter.technique + sw["matchup"] * matchup) / 100.0
        form = _lerp(oc["formRange"][0], oc["formRange"][1], batter.form / 100.0)
        batter_edge = (0.45 + 0.55 * bskill) * form
        aggression = batter.aggression
    else:
        batter_edge, aggression = 1.0, 50.0
    if sit.aggression_set is not None:
        aggression = sit.aggression_set
    aggression = min(100.0, aggression * sit.aggression_mult)
    ease = fs["bat_ease"] ** dmp["battingEase"]
    shot_quality = batter_edge * ease * fs["bat_speed"] * sit.shot_quality
    return bowler_edge, shot_quality, aggression, acc


def bucket_multipliers(batter, bowler, fs, cfg, sit=None, fatigue_accuracy=1.0, fmt=None):
    """``{bucket: multiplier}`` (unclamped) for one delivery."""
    sit = sit or SituationMods()
    oc = cfg["outcome"]
    s = oc["sensitivity"]
    bowler_edge, shot_quality, aggression, acc = edges(
        batter, bowler, fs, sit, cfg, fatigue_accuracy, fmt)
    ratio = max(0.05, bowler_edge / max(0.05, shot_quality))
    ag = oc["aggression"]
    agg_t = aggression / 100.0
    agg_bnd = _lerp(ag["boundaryLow"], ag["boundaryHigh"], agg_t)
    agg_wkt = _lerp(ag["wicketLow"], ag["wicketHigh"], agg_t)
    d = damping(cfg, fmt)
    wkt_ch = fs["spin_wkt"] if (bowler is not None and bowler.is_spin) else fs["pace_wkt"]
    wkt_ch **= d["wicketChance"]

    bnd = (1.0 / ratio) ** s["boundary"] * agg_bnd
    return {
        "Dot": ratio ** s["dot"],
        "Single": 1.0,
        "Double": 1.0,
        "Three": 1.0,
        "Four": bnd * fs["four_carry"] ** d["fourCarry"],
        "Six": bnd * (fs["six"] * fs["four_carry"]) ** d["six"],
        "Wicket": ratio ** s["wicket"] * wkt_ch * sit.wicket * agg_wkt,
        "Extras": (0.6 / max(0.1, acc)) ** s["extras"],
    }


def clamp_mults(mults, lo, hi):
    return {k: max(lo, min(hi, v)) for k, v in mults.items()}


def apply_mults(base, mults):
    w = {k: base.get(k, 0.0) * mults.get(k, 1.0) for k in BUCKETS}
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def _wicket_type(bowler, fs, rng, cfg):
    kind = "spin" if (bowler is not None and bowler.is_spin) else "pace"
    table = dict(cfg["outcome"]["wicketTypes"][kind])
    if kind == "pace":
        mv = (fs["swing"] + fs["seam"]) / 2.0
        for k in ("Bowled", "LBW", "Caught Behind"):
            if k in table:
                table[k] *= max(0.3, mv)
    else:
        for k in ("Stumped", "LBW"):
            if k in table:
                table[k] *= max(0.3, fs["spin"])
    wt = rng.weighted(table)
    return "Caught" if wt == "Caught Behind" else wt


def resolve(base, mults, rng, cfg, *, bowler=None, fs=None, shot_quality=1.0,
            drop_chance=0.0, bounds=None):
    """Draw one delivery from ``base`` scaled by ``mults``."""
    lo, hi = bounds or (cfg["meta"]["standalone_bounds"]["min"],
                        cfg["meta"]["standalone_bounds"]["max"])
    weights = apply_mults(base, clamp_mults(mults, lo, hi))
    bucket = rng.weighted(weights)

    if bucket == "Extras":
        et = rng.weighted(cfg["outcome"]["extrasSplit"])
        if et in ("Wide", "No Ball"):
            extra = 1
            bat_runs = 0
            if et == "Wide" and rng.chance(0.04):
                extra = 5
            if et == "No Ball":
                bat_runs = RUNS.get(rng.weighted(
                    {k: weights[k] for k in RUNS}), 0)
            return BallResult("Extras", runs=bat_runs, extra_type=et, extra_runs=extra, legal=False)
        return BallResult("Extras", runs=0, extra_type=et, extra_runs=1 if rng.chance(0.92) else 4)

    if bucket == "Wicket":
        wt = _wicket_type(bowler, fs, rng, cfg)
        if wt == "Caught" and rng.chance(drop_chance):
            return BallResult("Dot", runs=rng.choice([0, 0, 1, 2]), dropped=True)
        return BallResult("Wicket", runs=0, wicket_type=wt)

    runs = RUNS[bucket]
    if runs in (1, 2, 3) and fs is not None:
        p4 = stadium_mod.outfield_four_prob(runs, fs.outfield, cfg, shot_quality)
        if rng.chance(p4):
            return BallResult("Four", runs=4, outfield_four=True)
    return BallResult(bucket, runs=runs)
