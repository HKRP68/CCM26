"""Old engine vs realism engine vs the real-data anchors.

    # side-by-side on the metrics §12 of the brief asks for
    python -m tools.compare_realism --n 120 --pitch Even

    # rating sensitivity: does a better card actually win more?
    python -m tools.compare_realism --ratings --n 60

    # calibrate the rating curve, then the downstream gain (in that order)
    python -m tools.compare_realism --calibrate-curve --n 120
    python -m tools.compare_realism --calibrate-gain  --n 120

    # shadow-mode divergence report (no live outcomes changed)
    python -m tools.compare_realism --shadow --n 40

``--n`` is matches per arm; each match is two innings, so ``--n 120`` is 240
innings per engine. The brief asks for 10,000+ innings in the main validation
suite: that is ``--n 5000``, which takes hours, so the default is small and
the number is a knob rather than a promise.

WHICH PIPELINE THIS MEASURES
----------------------------
Every arm drives **/cipl** (``services/cipl_match.simulate_over``). That is
the primary pilot and the pipeline ``config/realism.json``'s correction
factors are calibrated against. ``/sim`` shares the ball model but keeps
fewer of the layers those factors compensate for — no approach modifiers,
none of ``services/match_drama``'s hooks — so the same gain over-corrects
there and ``/sim`` lands below its anchor. Numbers in
``docs/realism_engine_v1.md``, limitation 12. Calibrating ``/sim`` properly
needs a second arm here that drives ``services.sim_match.simulate_match``;
v1 does not have one.

WHAT THIS DOES AND DOES NOT MEASURE
-----------------------------------
The "real" column is the anchor set the reference was fitted to — measured
IPL aggregates for scoring, dismissal and extras rates, published aggregate
shares for the ball-by-ball split. Two of those three are genuine
measurements; the shape is a documented prior. A row where the realism engine
matches the *shape* prior is therefore evidence that the engine implements its
prior faithfully, not independent evidence about cricket.

Proper held-out validation needs the Cricsheet archive: build a reference
with ``--holdout-from 2024`` and the held-out shares land in the reference's
``holdout`` block for this tool to score against. Without the download that
block is absent, and this tool says so rather than inventing a figure.
"""

import argparse
import collections
import json
import os
import random
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")
MARK_TO_OUTCOME = {"0": "Dot", "1": "Single", "2": "Double", "3": "Three",
                   "4": "Four", "6": "Six", "W": "Wicket"}


def _set_env(realism, shadow=False):
    os.environ["SIM_REALISM_ENABLED"] = "1" if realism else "0"
    os.environ["SIM_REALISM_SHADOW_MODE"] = "1" if shadow else "0"
    from engine.realism import config as rcfg
    rcfg.clear_cache()


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def _collect(pitch, n, seed, overs=20, bat_bias=0, bowl_bias=0):
    """Run n matches and return the metric bundle for §12.

    ``build_cipl_state``'s first two XI arguments are ``bat_xi`` and
    ``bowl_xi``, so team A always bats first. ``bat_bias`` therefore shifts
    the FIRST-INNINGS BATTING side's batting ratings and ``bowl_bias`` shifts
    the FIRST-INNINGS BOWLING side's bowling ratings — which makes the
    first-innings metrics a clean read of "this batting against that
    bowling". Biasing one XI's bowling (as an earlier version did) moved only
    the second innings and made the arms unreadable.

    ``tools.pitch_calibration.make_xi`` builds every XI from the same fixed
    rating tables, so two unbiased sides are exactly equal in strength. That
    is what makes the symmetry check meaningful, and it is also why ``upset%``
    is undefined unless one of the biases is non-zero.
    """
    from tools import pitch_calibration as pc
    from tools import realism_audit as ra
    import services.cipl_match as cm

    random.seed(seed)
    S = ra._new_stats()
    S["phase_runs"] = collections.Counter()
    S["phase_balls"] = collections.Counter()
    S["phase_marks"] = collections.Counter()
    S["phase_extras"] = collections.Counter()
    S["chases"] = 0
    S["chases_won"] = 0
    S["close"] = 0
    S["matches"] = 0
    S["upsets"] = 0
    S["stronger_won"] = 0
    S["first_innings"] = []
    S["bowler_wkts_all"] = []

    for i in range(n):
        with pc._muted_logging():
            a, b = pc.make_xi("A", seed * 1000 + i), pc.make_xi("B", seed * 1000 + 500 + i)
            if bat_bias:
                a = _bias_xi(a, bat_bias, 0)
            if bowl_bias:
                b = _bias_xi(b, 0, bowl_bias)
            state = cm.build_cipl_state(1, overs, 10, 20, 111, 222, a, b,
                                        "A", "B", -1, pitch, False,
                                        ball_format="T20")
            strength_a = _xi_strength(a)
            strength_b = _xi_strength(b)
            totals = []
            for innings in (1, 2):
                before = len(S["totals"])
                _play_innings_phased(state, S, cm, overs)
                ra._record_innings(state, S)
                totals.append(S["totals"][-1] if len(S["totals"]) > before
                              else state["total_runs"])
                if innings == 1:
                    S["first_innings"].append(totals[0])
                    cm.end_first_innings(state)
            S["matches"] += 1
            if len(totals) == 2:
                S["chases"] += 1
                if totals[1] > totals[0]:
                    S["chases_won"] += 1
                if abs(totals[0] - totals[1]) <= 10:
                    S["close"] += 1
                # Team A bats first in build_cipl_state's ordering.
                winner_a = totals[0] > totals[1]
                if strength_a != strength_b:
                    a_stronger = strength_a > strength_b
                    if winner_a == a_stronger:
                        S["stronger_won"] += 1
                    else:
                        S["upsets"] += 1
    return S


def _xi_strength(xi):
    """A single number for "how good is this side", for the upset metric."""
    return round(sum(float(p.get("bat_rating") or p.get("rating") or 50)
                     + float(p.get("bowl_rating") or 0)
                     for p in xi) / max(1, len(xi)), 6)


def _bias_xi(xi, bat_bias, bowl_bias):
    """Shift an XI's ratings for the strong-vs-weak arm.

    Ratings are changed only on the in-memory copy this harness simulates
    with. Nothing is written back, and data/players.json is never touched —
    ``tests/test_realism_reference.py`` asserts the file's hash is stable.
    """
    out = []
    for p in xi:
        q = dict(p)
        for key in ("bat_rating", "rating"):
            if q.get(key) is not None:
                q[key] = max(30, min(100, float(q[key]) + bat_bias))
        if q.get("bowl_rating") is not None:
            q["bowl_rating"] = max(30, min(100, float(q["bowl_rating"]) + bowl_bias))
        out.append(q)
    return out


def _pick(mix):
    """Draw an approach from a ``((key, weight), ...)`` mix."""
    keys = [k for k, _w in mix]
    weights = [w for _k, w in mix]
    return random.choices(keys, weights)[0]


def _play_innings_phased(state, S, cm, overs):
    """Like realism_audit._play_innings, but also splits runs by phase.

    Approaches are drawn from ``pitch_calibration.REAL_BAT_MIX`` /
    ``REAL_BOWL_MIX`` — the mix read off ``/pitchstats`` — rather than
    ``realism_audit``'s own hand-set weights, which are a different
    distribution (balanced 39% / ultra 17% against the measured ultra 32% /
    balanced 18%).

    That difference is not cosmetic. ``engine/realism/approaches.py`` is
    mean-normalised on the MEASURED mix, so benchmarking it against a
    different one makes the approach layer look systematically below par:
    measured, it dropped the powerplay wicket rate to 2.69% against a 4.63%
    baseline and pushed dots to 43%. The benchmark has to play the mix the
    model is calibrated for, or it is measuring the mismatch rather than the
    model.
    """
    from tools import pitch_calibration as pc
    from tools import realism_audit as ra  # noqa: F401  (kept for _new_stats)
    guard = 0
    while not cm.is_innings_over(state) and guard < 40:
        guard += 1
        eligible = cm.eligible_bowlers(state)
        if not eligible:
            break
        over_no = state.get("current_over", 1)
        state["current_bowler"] = random.choice(eligible[:4])
        state["batting_approach"] = _pick(pc.REAL_BAT_MIX)
        state["bowling_approach"] = _pick(pc.REAL_BOWL_MIX)
        runs_before = state["total_runs"]
        summary = cm.simulate_over(state) or {}
        marks = summary.get("over_timeline") or []
        for mark in marks:
            S["balls"][mark] += 1
        legal = sum(1 for m in marks if m not in ("WD", "NB"))
        S["legal"] += legal
        S["overs"] += legal / 6.0
        if summary.get("over_runs") == 0 and legal == 6:
            S["wicket_maidens" if "W" in marks else "pure_maidens"] += 1
        frac = (over_no - 1) / float(max(1, overs))
        phase = "powerplay" if frac < 0.30 else ("middle" if frac < 0.80 else "death")
        S["phase_runs"][phase] += state["total_runs"] - runs_before
        S["phase_balls"][phase] += legal
        for mark in marks:
            if mark in ("WD", "NB"):
                S["phase_extras"][phase] += 1
            else:
                S["phase_marks"][(phase, mark)] += 1


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def metrics(S):
    legal = max(1, S["legal"])
    inn = max(1, S["innings"])
    scores = S["scores"] or [0]
    firsts = S["first_innings"] or [0]
    econ = sorted(S["econ"]) or [0]
    m = {
        "innings": S["innings"],
        "legal_balls": S["legal"],
        "mean_first_innings": statistics.mean(firsts),
        "median_first_innings": statistics.median(firsts),
        "stdev_first_innings": statistics.pstdev(firsts) if len(firsts) > 1 else 0.0,
        "pct_under_120": 100.0 * sum(1 for t in firsts if t < 120) / len(firsts),
        "pct_over_200": 100.0 * sum(1 for t in firsts if t > 200) / len(firsts),
        "pct_over_250": 100.0 * sum(1 for t in firsts if t > 250) / len(firsts),
        "wickets_per_innings": statistics.mean(S["wickets"] or [0]),
        "extras_per_innings": statistics.mean(S["extras"] or [0]),
        "fifties_per_innings": sum(1 for s in scores if s >= 50) / inn,
        "centuries_per_innings": sum(1 for s in scores if s >= 100) / inn,
        "econ_median": statistics.median(econ),
        "five_wkt_per_innings": sum(1 for w in S["bowler_wickets"] if w >= 5) / inn,
        "three_wkt_per_innings": sum(1 for w in S["bowler_wickets"] if w >= 3) / inn,
        "chase_success_pct": 100.0 * S["chases_won"] / max(1, S["chases"]),
        "close_match_pct": 100.0 * S["close"] / max(1, S["matches"]),
        "upset_pct": (100.0 * S["upsets"] / max(1, S["upsets"] + S["stronger_won"])
                      if (S["upsets"] + S["stronger_won"]) else float("nan")),
        "median_individual_score": statistics.median(scores),
    }
    for mark, name in MARK_TO_OUTCOME.items():
        m["pct_%s" % name] = 100.0 * S["balls"][mark] / legal
    m["pct_Extras"] = 100.0 * (S["balls"]["WD"] + S["balls"]["NB"]) / legal
    for phase in ("powerplay", "middle", "death"):
        b = S["phase_balls"][phase]
        m["rpo_%s" % phase] = (6.0 * S["phase_runs"][phase] / b) if b else 0.0
    return m


# Real-world reference column. Scoring/dismissal/extras levels are the
# MEASURED IPL anchors (static/ipl16/ipl_master_calibrated.csv, modern era);
# ball-split shares and the softer gameplay figures are published aggregate
# reference values, flagged accordingly.
def real_reference(competition=None):
    """The reference column, with each row labelled by how it was obtained.

    The scoring/dismissal/extras levels are derived from the MEASURED IPL
    anchors for the baseline's own era. ``mean_first_innings`` is
    ``(off-bat rpo + extras rpo) * 20``, lightly discounted for innings that
    end early — a side bowled out inside 20 overs scores less than its rate
    implies. First innings run a few runs above the pooled figure because
    second innings are truncated by completed chases, which is why the
    discount is 0.98 rather than lower.

    The ball-split shares are an ALL-T20 prior. The default baseline is
    modern IPL, which genuinely hits harder than the all-T20 average — about
    7-8% of balls go for six in a recent IPL season against ~5.5% across T20
    cricket as a whole. So a realism figure above the prior on sixes, or
    below it on dots, is not necessarily an error. The baseline column is the
    like-for-like comparison; use --calibrate-gain for the authoritative one.
    """
    from tools.build_realism_reference import measure_ipl_anchors
    from engine.realism import baselines
    anchors = measure_ipl_anchors()
    doc = baselines.load(format_name="T20", reload=True)
    comp = competition or (baselines.competition_key(doc) if doc else "ipl_modern")
    a = anchors.get(comp) or anchors.get("ipl_modern") or {}
    rpo = a.get("runs_per_ball_faced", 0) * 6
    extras_rpo = a.get("extras_runs_per_legal", 0) * 6
    return {
        "mean_first_innings": ((rpo + extras_rpo) * 20 * 0.98, "measured-derived"),
        "wickets_per_innings": (a.get("wickets_per_ball", 0) * 120, "measured-derived"),
        # Wides and no-balls cost two runs each in this engine (see
        # build_realism_reference.EXTRAS_RUNS); byes and leg byes add ~55%
        # more extras on top of the bowler-charged figure.
        "extras_per_innings": (a.get("extras_runs_per_legal", 0) * 120 * 1.55,
                               "measured-derived"),
        "pct_Dot": (35.5, "prior all-T20"), "pct_Single": (33.0, "prior all-T20"),
        "pct_Double": (6.5, "prior all-T20"), "pct_Three": (0.5, "prior all-T20"),
        "pct_Four": (11.5, "prior all-T20"), "pct_Six": (5.5, "prior all-T20"),
        "pct_Wicket": (a.get("wickets_per_ball", 0) * 100, "measured"),
        "fifties_per_innings": (0.75, "prior"),
        "centuries_per_innings": (0.035, "prior"),
        "econ_median": (rpo + extras_rpo, "measured-derived"),
        "chase_success_pct": (50.0, "prior"),
        "close_match_pct": (22.0, "prior"),
        "rpo_powerplay": (8.8, "prior"), "rpo_middle": (8.0, "prior"),
        "rpo_death": (11.0, "prior"),
    }


def baseline_targets():
    """What the ACTIVE reference says each per-ball share should be.

    This is the like-for-like column: the realism engine is implementing this
    reference, so a gap here is a calibration error, whereas a gap against the
    all-T20 prior may just mean the chosen era differs from the T20 average.
    """
    from engine.realism import baselines
    doc = baselines.load(format_name="T20", reload=True)
    if not doc:
        return {}
    comp = baselines.competition_key(doc)
    base = baselines.smoothed(doc, "T20/%s" % comp)
    legal = 1.0 - 0.65 * base["Extras"]
    out = {"pct_%s" % o: base[o] / legal * 100.0 for o in OUTCOMES}
    out["pct_Extras"] = base["Extras"] * 0.65 / legal * 100.0
    return out


ROWS = [
    ("mean_first_innings", "mean 1st-inn total", "%8.1f"),
    ("median_first_innings", "median 1st-inn total", "%8.1f"),
    ("stdev_first_innings", "stdev 1st-inn total", "%8.1f"),
    ("pct_under_120", "1st inn under 120 %", "%8.1f"),
    ("pct_over_200", "1st inn over 200 %", "%8.1f"),
    ("pct_over_250", "1st inn over 250 %", "%8.2f"),
    ("pct_Dot", "dot %", "%8.2f"),
    ("pct_Single", "single %", "%8.2f"),
    ("pct_Double", "double %", "%8.2f"),
    ("pct_Three", "three %", "%8.2f"),
    ("pct_Four", "four %", "%8.2f"),
    ("pct_Six", "six %", "%8.2f"),
    ("pct_Wicket", "wicket %", "%8.2f"),
    ("pct_Extras", "wide+nb % of legal", "%8.2f"),
    ("wickets_per_innings", "wickets / innings", "%8.2f"),
    ("extras_per_innings", "extras runs / innings", "%8.2f"),
    ("rpo_powerplay", "powerplay rpo", "%8.2f"),
    ("rpo_middle", "middle rpo", "%8.2f"),
    ("rpo_death", "death rpo", "%8.2f"),
    ("fifties_per_innings", "50s / innings", "%8.3f"),
    ("centuries_per_innings", "100s / innings", "%8.3f"),
    ("median_individual_score", "median indiv score", "%8.1f"),
    ("econ_median", "median economy", "%8.2f"),
    ("three_wkt_per_innings", "3+ hauls / innings", "%8.3f"),
    ("five_wkt_per_innings", "5+ hauls / innings", "%8.3f"),
    ("chase_success_pct", "chase success %", "%8.1f"),
    ("close_match_pct", "close match (<=10) %", "%8.1f"),
    ("upset_pct", "upset %", "%8.1f"),
]


def print_table(old, new, ref, targets=None):
    targets = targets or {}
    print("\n%-24s %8s %8s %9s %10s %s"
          % ("metric", "OLD", "REALISM", "baseline", "real", "source"))
    print("-" * 82)
    for key, label, fmt in ROWS:
        r = ref.get(key)
        rv = (fmt % r[0]) if r else "         -"
        src = r[1] if r else ""
        t = targets.get(key)
        tv = (("%9.2f" % t) if t is not None else "        -")
        print("%-24s %s %s %s %s  %s"
              % (label, fmt % old[key], fmt % new[key], tv, rv, src))
    print("-" * 82)
    print("baseline  = what the ACTIVE reference says this should be; the")
    print("            like-for-like check on the realism column.")
    print("measured  = from real IPL aggregates committed to this repository.")
    print("measured-derived = a measured anchor scaled to an innings.")
    print("prior     = published aggregate figure, NOT measured here.")
    print("prior all-T20 = an all-T20 average; the default baseline is modern")
    print("            IPL, which genuinely hits harder, so a gap on sixes or")
    print("            dots against this column is not necessarily an error.")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def run_compare(args):
    _set_env(False)
    old = metrics(_collect(args.pitch, args.n, args.seed, args.overs))
    _set_env(True)
    new = metrics(_collect(args.pitch, args.n, args.seed, args.overs))
    print_table(old, new, real_reference(), baseline_targets())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"old": old, "realism": new, "pitch": args.pitch,
                       "matches_per_arm": args.n, "seed": args.seed}, fh, indent=1)
        print("\nwrote %s" % args.json)
    _holdout_note()
    return 0


def _holdout_note():
    from engine.realism import baselines
    doc = baselines.load(format_name="T20", reload=True)
    if doc and doc.get("holdout"):
        h = doc["holdout"]
        print("\nheld-out real data (from year %s, %d deliveries):"
              % (h.get("from_year"), h.get("deliveries", 0)))
        print("   " + "  ".join("%s=%.4f" % (o, h["shares"].get(o, 0.0))
                                for o in OUTCOMES))
    else:
        print("\nNo held-out real deliveries in this reference: it was built in "
              "'anchors' mode from aggregates, so there is no per-ball holdout "
              "to score against. Build with\n   python -m tools.build_realism_"
              "reference cricsheet --zip <t20s_json.zip> --holdout-from 2024\n"
              "to get one.")


def run_ratings(args):
    """Does a rating edge show up on the scoreboard, without guaranteeing it?

    Each arm biases exactly one side of the first-innings contest, so the
    first-innings column reads directly as "this batting against that
    bowling". ``A win%`` is the side-A win rate, which is the number that
    answers the brief's "can a weaker team still win" question: it must move
    with the ratings and must never reach 0% or 100%.
    """
    _set_env(True)
    print("rating sensitivity (realism engine) — %d matches per arm, pitch %s"
          % (args.n, args.pitch))
    print("%-26s %9s %7s %8s %7s %7s"
          % ("arm", "mean 1st", "wkts", "50s/inn", "A win%", "upset%"))
    arms = (("equal sides", 0, 0),
            ("A batting +10", 10, 0),
            ("A batting -10", -10, 0),
            ("B bowling +10", 0, 10),
            ("B bowling -10", 0, -10))
    for label, bat_bias, bowl_bias in arms:
        S = _collect(args.pitch, args.n, args.seed, args.overs,
                     bat_bias, bowl_bias)
        m = metrics(S)
        decided = S["upsets"] + S["stronger_won"]
        a_wins = 100.0 * (S["matches"] - S["chases_won"]) / max(1, S["matches"])
        upset = ("%7.1f" % (100.0 * S["upsets"] / decided)) if decided else "      -"
        print("%-26s %9.1f %7.2f %8.3f %7.1f %s"
              % (label, m["mean_first_innings"], m["wickets_per_innings"],
                 m["fifties_per_innings"], a_wins, upset))
    print("\n'equal sides' is the symmetry check: tools.pitch_calibration.make_xi")
    print("builds both XIs from the same rating tables, so A win% there should")
    print("sit near 50 and upset% is undefined (no side is stronger).")
    return 0


def run_shadow(args):
    from engine.realism import shadow
    shadow.reset()
    _set_env(False, shadow=True)
    S = _collect(args.pitch, args.n, args.seed, args.overs)
    print(shadow.format_report())
    print("\n(live outcomes were produced by the OLD engine: %d innings, "
          "%d legal balls)" % (S["innings"], S["legal"]))
    return 0


def run_calibrate_curve(args):
    """Measure the rating curve's ball-weighted mean multiplier.

    The curve must redistribute probability between match-ups without moving
    the overall level: its mean multiplier over the deliveries that actually
    get bowled should be 1.0 for every outcome. Because exp() is convex and a
    real XI contains #9-#11 batters facing good bowlers, the raw curve's mean
    is above 1.0 for wickets and dots. This measures it so the divisor can be
    set from data rather than guessed.

    The curve is wrapped here rather than instrumented in the engine, so
    production code carries no calibration hook.
    """
    from engine.realism import config as rcfg, rating_curve
    rcfg.clear_cache()
    _set_env(True)
    current = dict((rcfg.get_config()["rating_curve"].get("normaliser") or {}))

    acc = collections.defaultdict(collections.Counter)
    n_balls = collections.Counter()
    original = rating_curve.multipliers

    def wrapped(bat, bowl, cfg=None, phase=None):
        out = original(bat, bowl, cfg, phase)
        key = phase or "default"
        n_balls[key] += 1
        for o in OUTCOMES:
            acc[key][o] += out[o]
        return out

    rating_curve.multipliers = wrapped
    try:
        _collect(args.pitch, args.n, args.seed, args.overs)
    finally:
        rating_curve.multipliers = original

    if not sum(n_balls.values()):
        print("no deliveries went through the rating curve — is realism on?")
        return 1
    recommended = {}
    for phase in sorted(n_balls):
        n = n_balls[phase]
        now = rating_curve._normaliser_for(
            rcfg.get_config()["rating_curve"], phase)
        print("\n%s — %d deliveries" % (phase, n))
        print("%-10s %12s %12s %12s"
              % ("outcome", "mean mult", "norm now", "norm next"))
        row = {}
        for o in OUTCOMES:
            mean = acc[phase][o] / n
            cur = float(now.get(o, 1.0))
            # `mean` is measured WITH `cur` already applied, so the divisor
            # that drives the mean to 1.0 is cur * mean.
            row[o] = round(max(0.25, min(4.0, cur * mean)), 4)
            print("%-10s %12.4f %12.4f %12.4f" % (o, mean, cur, row[o]))
        recommended[phase] = row
    print("\nPut this in config/realism.json under rating_curve.normaliser,")
    print("then re-run until every mean multiplier is ~1.0000:\n")
    print(json.dumps({"rating_curve": {"normaliser": recommended}}, indent=1))
    return 0


def run_calibrate_gain(args):
    """Measure the downstream gain the realism config needs, per phase.

    The realism distribution enters a pipeline that still applies the
    momentum engine, the pressure engine, the approach system and the
    fielding mechanics. Those layers do not average to 1.0, and they do not
    bite equally in every phase, so the realised output drifts from the
    baseline phase by phase. This measures the drift and prints the
    correction that cancels it, for ``integration.downstream_gain`` in
    config/realism.json.
    """
    from engine.realism import baselines, config as rcfg, rating_curve
    rcfg.clear_cache()
    _set_env(True)
    cfg = rcfg.get_config()
    table = (cfg.get("integration") or {}).get("downstream_gain") or {}

    doc = baselines.load(format_name="T20", reload=True)
    if not doc:
        print("no T20 reference; nothing to calibrate")
        return 1
    comp = baselines.competition_key(doc)

    def target_for(node):
        base = baselines.smoothed(doc, node)
        legal = 1.0 - 0.65 * base["Extras"]
        out = {o: base[o] / legal * 100.0 for o in OUTCOMES}
        out["Extras"] = base["Extras"] * 0.65 / legal * 100.0
        return out

    S = _collect(args.pitch, args.n, args.seed, args.overs)
    print("\ndownstream gain calibration — %d innings, %d legal balls"
          % (S["innings"], S["legal"]))
    recommended = {}
    for phase in ("powerplay", "middle", "death"):
        legal = S["phase_balls"][phase]
        if not legal:
            continue
        want = target_for("T20/%s/%s" % (comp, phase))
        now = rating_curve.phase_map(table, phase)
        print("\n%s — %d legal balls" % (phase, legal))
        print("%-10s %10s %10s %9s %10s %10s"
              % ("outcome", "baseline%", "realised%", "ratio", "gain now",
                 "gain next"))
        row = {}
        for o in OUTCOMES:
            mark = {v: k for k, v in MARK_TO_OUTCOME.items()}.get(o)
            if o == "Extras":
                got = 100.0 * (S["phase_extras"][phase]) / legal
            else:
                got = 100.0 * S["phase_marks"][(phase, mark)] / legal
            w = want[o]
            ratio = (got / w) if w > 0 else 1.0
            cur = float(now.get(o, 1.0))
            # Damped: the outcomes are coupled through normalisation, so a
            # full correction overshoots. 0.75 converges in 2-3 passes.
            nxt = cur / (ratio ** 0.75) if ratio > 0 else cur
            row[o] = round(max(0.25, min(4.0, nxt)), 4)
            print("%-10s %10.2f %10.2f %9.3f %10.4f %10.4f"
                  % (o, w, got, ratio, cur, row[o]))
        recommended[phase] = row
    recommended["default"] = recommended.get("middle", {})
    print("\nPut this in config/realism.json under integration.downstream_gain,")
    print("then re-run until every ratio is ~1.00. 'Three' is ~0.5% of balls,")
    print("so its ratio is dominated by sampling noise — do not chase it.\n")
    print(json.dumps({"integration": {"downstream_gain": recommended}}, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=60, help="matches per arm")
    ap.add_argument("--pitch", default="Even")
    ap.add_argument("--overs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--json", default="")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--ratings", action="store_true")
    g.add_argument("--shadow", action="store_true")
    g.add_argument("--calibrate-gain", action="store_true")
    g.add_argument("--calibrate-curve", action="store_true")
    args = ap.parse_args(argv)
    if args.ratings:
        return run_ratings(args)
    if args.shadow:
        return run_shadow(args)
    if args.calibrate_curve:
        return run_calibrate_curve(args)
    if args.calibrate_gain:
        return run_calibrate_gain(args)
    return run_compare(args)


if __name__ == "__main__":
    sys.exit(main())
