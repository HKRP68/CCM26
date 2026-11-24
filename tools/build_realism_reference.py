"""Build the realism engine's reference data. Offline, run by hand.

    # 1) From real Cricsheet ball-by-ball data (preferred, needs the download)
    python -m tools.build_realism_reference cricsheet \
        --zip ~/cricsheet/ipl_json.zip --zip ~/cricsheet/t20s_json.zip \
        --format T20 --out data/simulation_reference/v2

    # 2) From the real IPL aggregates already in this repository
    python -m tools.build_realism_reference anchors \
        --format T20 --out data/simulation_reference/v1

    # 3) What is in a reference, and where it came from
    python -m tools.build_realism_reference coverage --version v1

Nothing here runs at bot startup or during a match (§4, §11 of the brief).
The bot only ever *reads* the small JSON files this writes.

--------------------------------------------------------------------------
TWO BUILD MODES, AND WHY BOTH EXIST
--------------------------------------------------------------------------

``cricsheet`` is the real thing: it walks Cricsheet match JSON and *counts*
deliveries per context, so every node is ``count_kind: "observed"`` with a
true sample size. Use it whenever the archive is available.

``anchors`` is the fallback for an environment with no network access to
cricsheet.org. It does NOT invent ball-by-ball data and does not pretend to.
It builds the distribution from two clearly separated ingredients:

  * **Measured level**, from ``static/ipl16/ipl_master_calibrated.csv`` —
    real IPL player-season aggregates already committed to this repository,
    carrying Cricsheet registry ids. Summed across players these give three
    genuinely measured, player-independent anchors per era: runs off the bat
    per ball faced, dismissals per ball faced, and extras (wides + no-balls)
    per legal ball. These are measurements, not estimates.

  * **Shape prior**, from published aggregate T20 outcome shares — the same
    reference figures this repository already carries in
    ``tools/realism_audit.REAL_BALLS`` — for the split of scoring between
    dots, ones, twos, threes, fours and sixes, and for the relative phase
    profile. These are a documented prior, NOT a measurement from this data,
    and every node built this way is marked ``count_kind: "pseudo"`` so no
    reader can mistake one for the other.

The shape is then *fitted* to the measured level by exponential tilting
(maximum-entropy): among all distributions with the prior's shape, pick the
one whose expected runs match the measured anchor exactly. So the numbers the
engine plays with reproduce real IPL scoring and dismissal rates by
construction, while being explicit about which part is measured and which
part is prior.

--------------------------------------------------------------------------
LICENSING
--------------------------------------------------------------------------
Cricsheet data is published under the Open Data Commons Open Database
Licence (ODbL) 1.0. The derived aggregate reference files this tool writes
are a Derivative Database and must carry the same notice — the manifest
records it, and ``docs/realism_engine_v1.md`` states the redistribution
terms in full. Code ideas borrowed from Cricket-Intelligence-Lab (MIT) are
attributed in the manifest and the docs.
"""

import argparse
import collections
import csv
import datetime
import json
import math
import os
import sys
import zipfile

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")
RUN_OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six")
RUN_VALUE = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6}

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IPL_AGGREGATES = os.path.join(REPO, "static", "ipl16", "ipl_master_calibrated.csv")

SCHEMA_VERSION = 1
MODEL_VERSION = "realism-v1"

# --------------------------------------------------------------------------
# Published aggregate priors (documented, NOT measured here)
# --------------------------------------------------------------------------
# Shares of legal balls in modern men's professional T20. Identical to the
# reference block already in tools/realism_audit.REAL_BALLS, kept in one
# place so the audit tool and the model cannot drift apart.
SHAPE_PRIOR_T20 = {"Dot": 35.5, "Single": 33.0, "Double": 6.5, "Three": 0.5,
                   "Four": 11.5, "Six": 5.5}
SHAPE_PRIOR_ODI = {"Dot": 40.0, "Single": 38.5, "Double": 8.5, "Three": 0.8,
                   "Four": 9.5, "Six": 2.7}

# Per-phase shape priors and relative level profile.
#
# A single exponential tilt cannot express the powerplay, which is BOTH
# high-dot and high-boundary: the new ball and the field restrictions make it
# the most polarised phase of the innings, while the middle overs trade
# boundaries for strike rotation against spin. One tilt parameter can only
# slide mass along the run axis, so each phase gets its own published shape
# prior and the tilt is then used only to pin that shape's *level* to the
# measured anchor. Shares are of legal balls, from published aggregate
# phase splits for modern men's professional T20; the level multipliers are
# renormalised by `_fit_phases` so the ball-weighted innings average
# reproduces the measured anchor exactly.
PHASE_SHAPE = {
    "T20": {
        "powerplay": {"Dot": 36.0, "Single": 28.5, "Double": 5.5, "Three": 0.4,
                      "Four": 14.5, "Six": 6.5},
        "middle":    {"Dot": 31.5, "Single": 38.0, "Double": 7.5, "Three": 0.6,
                      "Four": 9.5, "Six": 4.5},
        "death":     {"Dot": 27.5, "Single": 28.0, "Double": 7.0, "Three": 0.5,
                      "Four": 15.0, "Six": 13.0},
    },
    "ODI": {
        "powerplay": {"Dot": 44.0, "Single": 33.0, "Double": 6.5, "Three": 0.6,
                      "Four": 12.0, "Six": 2.0},
        "middle":    {"Dot": 37.5, "Single": 42.5, "Double": 9.5, "Three": 0.9,
                      "Four": 7.5, "Six": 1.8},
        "death":     {"Dot": 33.0, "Single": 34.0, "Double": 9.0, "Three": 0.7,
                      "Four": 13.0, "Six": 6.5},
    },
}

PHASE_PRIOR = {
    "T20": {
        # (share of innings balls, runs multiplier, wicket multiplier)
        "powerplay": (0.30, 0.99, 0.88),
        "middle":    (0.50, 0.90, 0.95),
        "death":     (0.20, 1.32, 1.45),
    },
    "ODI": {
        "powerplay": (0.20, 1.00, 1.05),
        "middle":    (0.60, 0.88, 0.85),
        "death":     (0.20, 1.40, 1.45),
    },
}

# Second-innings relative profile: NEUTRAL, deliberately.
#
# The measured anchors pool both innings and cannot be split, so any
# first-vs-second-innings asymmetry here would be an assertion, not a
# measurement — and it turns out to be an expensive one. A prior of
# (0.985, 1.03) — chasing sides scoring slightly slower and losing slightly
# more wickets, which sounds unobjectionable — cost chases about ten
# percentage points of success, because a 186 target sits on the steep part
# of the distribution where a four-run shift in the mean moves the win
# probability a long way. Measured, it dropped chase success to 36% against a
# real figure near 50%.
#
# So the innings level stays neutral and the only thing that distinguishes a
# chase is the scoreboard it can see, via conditions.chase_multipliers. The
# hierarchy LEVEL is kept so that a reference built from real ball-by-ball
# data can populate it with observed inn1/inn2 counts, which would be a
# measurement and would belong here.
INNINGS_PRIOR = {
    "inn1": (1.00, 1.00),
    "inn2": (1.00, 1.00),
}

# Wickets-in-hand profile. A side three down plays differently from a side
# none down; a side seven down is batting with its bowlers.
WICKETS_PRIOR = {
    "w0_2":   (1.02, 0.97),
    "w3_5":   (1.00, 1.00),
    "w6_plus": (0.92, 1.12),
}

# How an Extras event splits, matching engine/ball_outcome.py's default
# extras_split, and the runs each type concedes on average. Needed to convert
# a measured "extras runs per legal ball" into a per-delivery Extras
# probability, because wides and no-balls do not consume a legal ball.
EXTRAS_SPLIT = {"Wide": 0.40, "No Ball": 0.25, "Leg Bye": 0.20, "Byes": 0.15}
# Runs each extra type actually costs IN THIS ENGINE, which is not the same as
# what it costs in cricket. engine/ball_outcome.calculate_outcome sets
# result["runs"] = 1 for a Wide and for a No Ball, and every caller then adds
# the one-run penalty on top ("total_runs += 1 + runs" in both
# services/cipl_match.py and services/sim_match.py), so a wide concedes TWO.
# That is a long-standing CCM26 convention and changing it would move every
# legacy scorecard, so the reference is fitted to the engine as it is. Getting
# this wrong is not subtle: assuming one run per wide doubled the extras rate
# and put ~6 runs an innings on every total.
EXTRAS_RUNS = {"Wide": 2.0, "No Ball": 2.0, "Leg Bye": 1.2, "Byes": 1.15}
_ILLEGAL_SHARE = EXTRAS_SPLIT["Wide"] + EXTRAS_SPLIT["No Ball"]
_BOWLER_CHARGED_RUNS = (EXTRAS_SPLIT["Wide"] * EXTRAS_RUNS["Wide"]
                        + EXTRAS_SPLIT["No Ball"] * EXTRAS_RUNS["No Ball"])

# Era buckets for the IPL aggregates. Chosen on the data's own structure
# (scoring steps up at 2014 and again at 2022) rather than on round numbers,
# and overridable with --eras.
DEFAULT_ERAS = {
    "ipl_2008_2013": (2008, 2013),
    "ipl_2014_2021": (2014, 2021),
    "ipl_modern": (2022, 2026),
}
DEFAULT_COMPETITION = "ipl_modern"


def _phase_for_fraction(frac):
    """Phase bucket for a delivery, from engine.realism.phases when importable.

    The builder runs offline and may be invoked without the package on the
    path; the literal fallback is the SAME 0.30 / 0.80 split the central
    module defines, so a reference built either way files deliveries under the
    identical nodes the live engine looks up.
    """
    try:
        from engine.realism import phases
        return phases.phase_for_fraction(frac)
    except Exception:
        return ("powerplay" if frac < 0.30
                else ("middle" if frac < 0.80 else "death"))


# ==========================================================================
# Mode 1: Cricsheet ball-by-ball ETL
# ==========================================================================
# The traversal below follows the shape of Cricsheet's JSON as
# HalcyonVector/Cricket-Intelligence-Lab's `cil_etl.parse` / `ingest_zip` do
# (MIT licensed): iterate `innings[] -> overs[] -> deliveries[]`, read
# `runs.batter` / `runs.extras` / `extras.{wides,noballs,byes,legbyes}` and
# `wickets[]`, and read competition and date from `info`. The code is written
# fresh for CCM26's eight-outcome vocabulary; the debt is to the data layout
# and the idea of counting into a context key, which is why that repository
# is credited in the manifest and the docs.

def _delivery_outcome(d):
    """Map one Cricsheet delivery onto one CCM26 outcome name."""
    extras = d.get("extras") or {}
    if extras:
        # Wides and no-balls are not legal balls; byes and leg byes are. Both
        # are the engine's single "Extras" outcome, which then picks a type.
        return "Extras"
    wickets = d.get("wickets") or []
    if wickets:
        # A run out off a scoring shot is still a wicket for the ball model;
        # the engine credits the completed run itself.
        return "Wicket"
    runs = int(((d.get("runs") or {}).get("batter")) or 0)
    if runs == 5:
        runs = 4          # overthrow oddity; fold into the nearest outcome
    if runs >= 6:
        return "Six"
    return {0: "Dot", 1: "Single", 2: "Double", 3: "Three", 4: "Four"}[min(runs, 4)]


def _match_competition(info, era_map):
    """Competition/era key for a match, or None to skip it."""
    comp = str(info.get("event", {}).get("name") or info.get("match_type") or "")
    dates = info.get("dates") or []
    year = None
    if dates:
        try:
            year = int(str(dates[0])[:4])
        except Exception:
            year = None
    league = "ipl" if "indian premier league" in comp.lower() else None
    if league is None:
        mt = str(info.get("match_type") or "").upper()
        if mt in ("T20", "IT20"):
            league = "intl_t20"
        elif mt in ("ODI", "ODM"):
            league = "intl_odi"
        else:
            league = "other"
    if league == "ipl" and year is not None:
        for key, (lo, hi) in era_map.items():
            if lo <= year <= hi:
                return key
    if year is not None and league in ("intl_t20", "intl_odi"):
        return "%s_%s" % (league, "modern" if year >= 2018 else "legacy")
    return league


def _iter_matches(paths):
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.endswith(".json") and name != "README.json":
                    with open(os.path.join(path, name), "r", encoding="utf-8") as fh:
                        yield name, json.load(fh)
            continue
        with zipfile.ZipFile(path) as zf:
            for name in sorted(zf.namelist()):
                if not name.endswith(".json") or name.endswith("README.json"):
                    continue
                with zf.open(name) as fh:
                    try:
                        yield name, json.loads(fh.read().decode("utf-8"))
                    except Exception:
                        continue


def build_from_cricsheet(paths, format_name, era_map, min_year=None,
                         max_year=None, holdout_from=None):
    """Count deliveries into the context hierarchy. Returns (counts, stats)."""
    want = {"T20": ("T20", "IT20"), "ODI": ("ODI", "ODM")}.get(format_name,
                                                               (format_name,))
    counts = collections.defaultdict(lambda: collections.Counter())
    stats = collections.Counter()
    holdout = collections.defaultdict(lambda: collections.Counter())

    for _name, doc in _iter_matches(paths):
        info = doc.get("info") or {}
        if str(info.get("match_type") or "").upper() not in want:
            continue
        if (info.get("gender") or "male") != "male":
            continue
        dates = info.get("dates") or []
        year = None
        if dates:
            try:
                year = int(str(dates[0])[:4])
            except Exception:
                pass
        if min_year and (year or 0) < min_year:
            continue
        if max_year and (year or 9999) > max_year:
            continue
        comp = _match_competition(info, era_map)
        if not comp:
            continue
        is_holdout = bool(holdout_from and year and year >= holdout_from)
        sink = holdout if is_holdout else counts
        stats["holdout_matches" if is_holdout else "matches"] += 1

        overs_per_innings = int(info.get("overs") or (20 if format_name == "T20" else 50))
        innings_balls = overs_per_innings * 6
        target = None

        for inn_no, inn in enumerate(doc.get("innings") or [], start=1):
            legal = 0
            runs_so_far = 0
            wkts = 0
            inn_key = "inn1" if inn_no == 1 else "inn2"
            for over in inn.get("overs") or []:
                for d in over.get("deliveries") or []:
                    outcome = _delivery_outcome(d)
                    frac = min(0.999, legal / float(max(1, innings_balls)))
                    phase = _phase_for_fraction(frac)
                    wb = "w0_2" if wkts <= 2 else ("w3_5" if wkts <= 5 else "w6_plus")
                    path = [format_name, comp, phase, inn_key, wb]
                    if inn_no == 2 and target:
                        need = max(0, target - runs_so_far)
                        left = max(1, innings_balls - legal)
                        rrr = need * 6.0 / left
                        band = ("rrr_lt6" if rrr < 6 else "rrr_6_9" if rrr < 9
                                else "rrr_9_12" if rrr < 12 else "rrr_12_plus")
                        path.append(band)
                    key = ""
                    for part in path:
                        key = part if not key else "%s/%s" % (key, part)
                        sink[key][outcome] += 1
                    sink["root"][outcome] += 1
                    stats["deliveries" if not is_holdout else "holdout_deliveries"] += 1

                    r = d.get("runs") or {}
                    runs_so_far += int(r.get("total") or 0)
                    if (d.get("wickets") or []):
                        wkts += 1
                    ex = d.get("extras") or {}
                    if not (ex.get("wides") or ex.get("noballs")):
                        legal += 1
            if inn_no == 1:
                target = runs_so_far + 1
    return counts, holdout, stats


# ==========================================================================
# Mode 2: measured anchors from the in-repo IPL aggregates
# ==========================================================================
def measure_ipl_anchors(path=IPL_AGGREGATES, era_map=None):
    """Real, player-independent anchors per era from committed IPL data.

    Sums every player-season row, so the result is an aggregate of the whole
    competition and carries no individual player information into the model.

    Returns ``{era_key: {...}}`` with:
        runs_per_ball_faced   measured, runs off the bat / balls faced
        wickets_per_ball      measured, batting dismissals / balls faced
        extras_runs_per_legal measured, (runs conceded - runs off bat) / legal
        balls, dismissals, legal_balls, seasons
    """
    era_map = era_map or DEFAULT_ERAS
    with open(path, "r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    acc = collections.defaultdict(lambda: collections.Counter())
    seasons = collections.defaultdict(set)

    def num(row, key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    for row in rows:
        try:
            season = int(row.get("Season") or 0)
        except ValueError:
            continue
        for key, (lo, hi) in era_map.items():
            if lo <= season <= hi:
                seasons[key].add(season)
                a = acc[key]
                a["runs"] += num(row, "Batting_Runs")
                a["balls"] += num(row, "Batting_Balls_Faced")
                a["dismissals"] += num(row, "Batting_Dismissals")
                a["bowl_balls"] += num(row, "Bowling_Balls")
                a["bowl_runs"] += num(row, "Bowling_Runs_Conceded")
                break

    out = {}
    for key, a in acc.items():
        if a["balls"] <= 0 or a["bowl_balls"] <= 0:
            continue
        out[key] = {
            "runs_per_ball_faced": a["runs"] / a["balls"],
            "wickets_per_ball": a["dismissals"] / a["balls"],
            "extras_runs_per_legal": max(0.0, (a["bowl_runs"] - a["runs"])
                                         / a["bowl_balls"]),
            "balls_faced": int(a["balls"]),
            "dismissals": int(a["dismissals"]),
            "legal_balls": int(a["bowl_balls"]),
            "seasons": sorted(seasons[key]),
            "source": "static/ipl16/ipl_master_calibrated.csv",
            "measured": True,
        }
    return out


def _tilt_runs(prior, mass, mean_runs, tol=1e-12, iters=200):
    """Exponential tilt of a run-outcome prior to hit a target mean.

    Among all distributions over {0,1,2,3,4,6} that keep the prior's shape,
    ``p(r) ∝ prior(r) * exp(theta * r)`` is the maximum-entropy choice with a
    given mean (an exponential family / Gibbs tilt). ``theta`` is solved by
    bisection, which is safe because the mean is strictly increasing in it.

    *mass* is how much total probability the run outcomes get; the returned
    mean is per-delivery, i.e. ``sum(r * p(r))``.
    """
    base = {k: float(prior[k]) for k in RUN_OUTCOMES}
    s = sum(base.values())
    base = {k: v / s for k, v in base.items()}

    def mean_at(theta):
        w = {k: base[k] * math.exp(theta * RUN_VALUE[k]) for k in RUN_OUTCOMES}
        t = sum(w.values())
        return sum(RUN_VALUE[k] * w[k] / t for k in RUN_OUTCOMES), w, t

    target = mean_runs / mass if mass > 0 else 0.0
    lo, hi = -6.0, 6.0
    if mean_at(lo)[0] > target:
        theta = lo
    elif mean_at(hi)[0] < target:
        theta = hi
    else:
        for _ in range(iters):
            mid = (lo + hi) / 2.0
            if mean_at(mid)[0] < target:
                lo = mid
            else:
                hi = mid
            if hi - lo < tol:
                break
        theta = (lo + hi) / 2.0
    _m, w, t = mean_at(theta)
    return {k: mass * w[k] / t for k in RUN_OUTCOMES}, theta


def distribution_from_anchors(anchor, shape_prior, runs_mult=1.0,
                              wicket_mult=1.0):
    """Turn measured anchors into a per-delivery outcome distribution.

    The anchors are per *legal ball* / per *ball faced*; the engine's
    distribution is per *delivery bowled*, and wides and no-balls are not
    legal balls. Let ``E`` be the per-delivery Extras probability. Then the
    legal fraction of deliveries is ``L = 1 - illegal_share * E``, and the
    bowler-charged extras runs per legal ball are
    ``charged * E / L``, which inverts to a closed form for ``E``.

    ``runs_mult`` / ``wicket_mult`` apply a relative phase, innings or
    wickets profile to the measured level.
    """
    charged = _BOWLER_CHARGED_RUNS
    e_target = max(0.0, float(anchor["extras_runs_per_legal"]))
    # charged * E = e_target * L = e_target * (1 - illegal * E)
    denom = charged + e_target * _ILLEGAL_SHARE
    extras = (e_target / denom) if denom > 0 else 0.0
    extras = max(0.0, min(0.20, extras))
    legal = 1.0 - _ILLEGAL_SHARE * extras

    wicket = float(anchor["wickets_per_ball"]) * wicket_mult * legal
    wicket = max(1e-4, min(0.20, wicket))

    mass = max(1e-6, 1.0 - extras - wicket)
    mean_runs = float(anchor["runs_per_ball_faced"]) * runs_mult * legal
    runs, theta = _tilt_runs(shape_prior, mass, mean_runs)

    dist = dict(runs)
    dist["Wicket"] = wicket
    dist["Extras"] = extras
    total = sum(dist.values())
    dist = {k: v / total for k, v in dist.items()}
    return dist, {"theta": theta, "legal_fraction": legal}


def _fit_phases(prior_table):
    """Renormalise a phase profile so the ball-weighted mean is exactly 1.0.

    Without this the phase multipliers would quietly shift the whole innings
    level away from the measured anchor, which would make the anchor a
    decoration rather than a constraint.
    """
    shares = {k: v[0] for k, v in prior_table.items()}
    s = sum(shares.values())
    shares = {k: v / s for k, v in shares.items()}
    for idx in (1, 2):
        mean = sum(shares[k] * prior_table[k][idx] for k in prior_table)
        if mean <= 0:
            continue
        prior_table = {k: tuple(v[j] / mean if j == idx else v[j]
                                for j in range(3))
                       for k, v in prior_table.items()}
    return prior_table, shares


def build_from_anchors(format_name, era_map=None, pseudo_weight=4000.0):
    """Build the full node tree from measured anchors + documented priors."""
    era_map = era_map or DEFAULT_ERAS
    shape = SHAPE_PRIOR_T20 if format_name == "T20" else SHAPE_PRIOR_ODI
    phase_shape = PHASE_SHAPE.get(format_name, PHASE_SHAPE["T20"])
    phase_prior, phase_shares = _fit_phases(
        PHASE_PRIOR.get(format_name, PHASE_PRIOR["T20"]))

    if format_name == "T20":
        anchors = measure_ipl_anchors(era_map=era_map)
        if not anchors:
            raise SystemExit("no IPL anchors could be measured — is "
                             "static/ipl16/ipl_master_calibrated.csv present?")
    else:
        # No measured in-repo ODI/List-A source exists. The ODI reference is
        # therefore prior-only, and says so loudly in its provenance. It is
        # not enabled by default (see engine/realism/config.enabled_formats).
        anchors = {"odi_prior": {
            "runs_per_ball_faced": 0.97,
            "wickets_per_ball": 0.0300,
            "extras_runs_per_legal": 0.0600,
            "measured": False,
            "source": "published aggregate ODI reference figures (prior only)",
            "seasons": [],
        }}

    nodes = {}

    def put(key, parent, dist, weight, kind="pseudo", meta=None):
        nodes[key] = {
            "parent": parent,
            "counts": {o: round(max(0.0, dist[o]) * weight, 6) for o in OUTCOMES},
            "count_kind": kind,
        }
        if meta:
            nodes[key]["meta"] = meta

    # Root and format level: the pooled distribution across the eras we
    # actually have, weighted by how many balls each era contributed.
    pooled = collections.Counter()
    era_dists = {}
    total_balls = sum(a.get("balls_faced", 1) or 1 for a in anchors.values())
    for key, a in anchors.items():
        dist, info = distribution_from_anchors(a, shape)
        era_dists[key] = (dist, a, info)
        w = (a.get("balls_faced", 1) or 1) / float(total_balls)
        for o in OUTCOMES:
            pooled[o] += dist[o] * w

    put("root", None, pooled, pseudo_weight * 4)
    put(format_name, "root", pooled, pseudo_weight * 4)

    for comp, (dist, anchor, _info) in sorted(era_dists.items()):
        comp_key = "%s/%s" % (format_name, comp)
        w_comp = pseudo_weight * 2
        put(comp_key, format_name, dist, w_comp, meta={
            "anchor_source": anchor.get("source"),
            "anchor_measured": bool(anchor.get("measured")),
            "seasons": anchor.get("seasons"),
            "measured_balls_faced": anchor.get("balls_faced"),
        })
        for phase, (share, rmult, wmult) in phase_prior.items():
            pshape = phase_shape[phase]
            p_dist, _ = distribution_from_anchors(anchor, pshape, rmult, wmult)
            p_key = "%s/%s" % (comp_key, phase)
            put(p_key, comp_key, p_dist, pseudo_weight * share * 2)
            for inn, (irm, iwm) in INNINGS_PRIOR.items():
                i_dist, _ = distribution_from_anchors(
                    anchor, pshape, rmult * irm, wmult * iwm)
                i_key = "%s/%s" % (p_key, inn)
                put(i_key, p_key, i_dist, pseudo_weight * share)
                for wb, (wrm, wwm) in WICKETS_PRIOR.items():
                    w_dist, _ = distribution_from_anchors(
                        anchor, pshape, rmult * irm * wrm, wmult * iwm * wwm)
                    put("%s/%s" % (i_key, wb), i_key,
                        w_dist, pseudo_weight * share * 0.33)

    default_comp = (DEFAULT_COMPETITION if DEFAULT_COMPETITION in era_dists
                    else sorted(era_dists)[-1])
    return nodes, {k: v[1] for k, v in era_dists.items()}, default_comp


# ==========================================================================
# Assembly
# ==========================================================================
def _counts_to_nodes(counts, format_name, min_n=200):
    """Turn observed Counters into reference nodes, dropping thin leaves.

    A node with fewer than *min_n* deliveries behind it is left out entirely:
    the lookup then stops at its parent, which is exactly what the smoothing
    hierarchy is for. Keeping it would add noise, not information.
    """
    nodes = {}
    keys = sorted(counts, key=lambda k: (k.count("/"), k))
    for key in keys:
        c = counts[key]
        total = sum(c.get(o, 0) for o in OUTCOMES)
        if key != "root" and total < min_n:
            continue
        parent = None
        if key != "root":
            parent = key.rsplit("/", 1)[0] if "/" in key else "root"
            while parent and parent != "root" and parent not in nodes:
                parent = parent.rsplit("/", 1)[0] if "/" in parent else "root"
            parent = parent or "root"
        nodes[key] = {
            "parent": parent,
            "counts": {o: int(c.get(o, 0)) for o in OUTCOMES},
            "count_kind": "observed",
        }
        if any(nodes[key]["counts"][o] == 0 for o in OUTCOMES):
            # A zero cell is legitimate in sparse data; smoothing fills it.
            # Record it so coverage reporting can show where that happened.
            nodes[key]["meta"] = {"zero_cells": [o for o in OUTCOMES
                                                 if nodes[key]["counts"][o] == 0]}
    return nodes


def write_reference(out_dir, format_name, nodes, default_competition,
                    anchors, provenance, holdout=None):
    os.makedirs(out_dir, exist_ok=True)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "format": format_name,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .replace(microsecond=0).isoformat(),
        "default_competition": default_competition,
        "outcomes": list(OUTCOMES),
        "nodes": nodes,
        "anchors": anchors,
        "provenance": provenance,
    }
    if holdout:
        doc["holdout"] = holdout
    path = os.path.join(out_dir, "%s_baselines.json" % format_name.lower())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=False)
        fh.write("\n")
    return path


def write_manifest(out_dir, entries, extra=None):
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .replace(microsecond=0).isoformat(),
        "builder": "tools/build_realism_reference.py",
        "formats": entries,
        "licensing": {
            "cricsheet_data": "Open Data Commons Open Database Licence (ODbL) 1.0",
            "cricsheet_url": "https://cricsheet.org/",
            "notice": (
                "Reference files derived from Cricsheet ball-by-ball data are a "
                "Derivative Database under ODbL 1.0 and must be redistributed "
                "under the same licence, with attribution to Cricsheet and a "
                "copy of the licence. Files built in 'anchors' mode derive from "
                "static/ipl16/ipl_master_calibrated.csv, itself keyed to the "
                "Cricsheet player registry, and carry the same obligation."),
            "code_attribution": (
                "Data-layout and context-counting approach informed by "
                "HalcyonVector/Cricket-Intelligence-Lab (MIT licence); no code "
                "copied verbatim."),
        },
    }
    if extra:
        manifest.update(extra)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    return path


# ==========================================================================
# CLI
# ==========================================================================
def _parse_eras(spec):
    if not spec:
        return dict(DEFAULT_ERAS)
    out = {}
    for part in spec.split(","):
        name, _, rng = part.partition("=")
        lo, _, hi = rng.partition("-")
        out[name.strip()] = (int(lo), int(hi or lo))
    return out


def cmd_cricsheet(args):
    era_map = _parse_eras(args.eras)
    counts, holdout, stats = build_from_cricsheet(
        args.zip, args.format, era_map, args.min_year, args.max_year,
        args.holdout_from)
    if not counts:
        raise SystemExit("no deliveries parsed — check --zip paths and --format")
    nodes = _counts_to_nodes(counts, args.format, args.min_node)
    comps = sorted(k.split("/", 1)[1] for k in nodes
                   if k.count("/") == 1 and k.startswith(args.format + "/"))
    default = args.default_competition or (comps[-1] if comps else "")
    holdout_summary = None
    if holdout:
        hc = holdout.get("root", {})
        tot = sum(hc.get(o, 0) for o in OUTCOMES) or 1
        holdout_summary = {
            "from_year": args.holdout_from,
            "deliveries": tot,
            "shares": {o: hc.get(o, 0) / tot for o in OUTCOMES},
            "note": ("Held out of the fitted nodes above and reported here so "
                     "tools/compare_realism.py can score the model on data it "
                     "never saw."),
        }
    path = write_reference(
        args.out, args.format, nodes, default, {}, {
            "mode": "cricsheet",
            "inputs": [os.path.basename(p) for p in args.zip],
            "matches": stats.get("matches", 0),
            "deliveries": stats.get("deliveries", 0),
            "min_node_deliveries": args.min_node,
            "eras": {k: list(v) for k, v in era_map.items()},
            "all_nodes_observed": True,
            "data_licence": "ODbL 1.0 (Cricsheet)",
        }, holdout=holdout_summary)
    print("wrote %s  (%d nodes, %d deliveries, %d matches)"
          % (path, len(nodes), stats.get("deliveries", 0), stats.get("matches", 0)))
    write_manifest(args.out, {args.format: {
        "file": os.path.basename(path), "mode": "cricsheet",
        "nodes": len(nodes), "deliveries": stats.get("deliveries", 0),
        "default_competition": default, "competitions": comps}})
    return 0


def cmd_anchors(args):
    era_map = _parse_eras(args.eras)
    entries = {}
    for fmt in args.format:
        nodes, anchors, default = build_from_anchors(fmt, era_map,
                                                     args.pseudo_weight)
        measured = any(a.get("measured") for a in anchors.values())
        path = write_reference(args.out, fmt, nodes, default, anchors, {
            "mode": "anchors",
            "level_source": ("static/ipl16/ipl_master_calibrated.csv "
                             "(real IPL player-season aggregates, summed to "
                             "competition level; Cricsheet registry ids)"
                             if measured else
                             "published aggregate reference figures only"),
            "level_measured": measured,
            "shape_source": ("published aggregate T20/ODI outcome shares; same "
                             "figures as tools/realism_audit.REAL_BALLS"),
            "shape_measured": False,
            "fitting": ("maximum-entropy exponential tilt of the shape prior "
                        "onto the measured runs-per-ball anchor; wicket and "
                        "extras rates taken directly from the anchors"),
            "all_nodes_observed": False,
            "pseudo_weight": args.pseudo_weight,
            "eras": {k: list(v) for k, v in era_map.items()},
            "limitation": ("No ball-by-ball deliveries were counted. Phase, "
                           "innings and wickets structure is a documented "
                           "prior, not a measurement. Rebuild with the "
                           "'cricsheet' mode for observed counts."),
            "data_licence": "ODbL 1.0 (Cricsheet-derived aggregates)",
        })
        comps = sorted(k.split("/", 1)[1] for k in nodes
                       if k.count("/") == 1 and k.startswith(fmt + "/"))
        print("wrote %s  (%d nodes, default=%s, level_measured=%s)"
              % (path, len(nodes), default, measured))
        for key, a in sorted(anchors.items()):
            print("    %-16s rpb=%.4f (rpo %.2f)  wkt/ball=%.4f  extras_rpo=%.3f  %s"
                  % (key, a["runs_per_ball_faced"], a["runs_per_ball_faced"] * 6,
                     a["wickets_per_ball"], a["extras_runs_per_legal"] * 6,
                     "MEASURED" if a.get("measured") else "prior"))
        entries[fmt] = {"file": os.path.basename(path), "mode": "anchors",
                        "nodes": len(nodes), "default_competition": default,
                        "competitions": comps, "level_measured": measured}
    write_manifest(args.out, entries)
    print("wrote %s" % os.path.join(args.out, "manifest.json"))
    return 0


def cmd_coverage(args):
    sys.path.insert(0, REPO)
    from engine.realism import baselines
    any_found = False
    for fmt in ("T20", "ODI"):
        doc = baselines.load(version=args.version, format_name=fmt, reload=True)
        if not doc:
            print("%-5s no usable reference in %s" % (fmt, args.version))
            continue
        any_found = True
        cov = baselines.coverage(doc)
        print("%-5s nodes=%-4d observed=%-4d pseudo=%-4d observed_deliveries=%d"
              % (fmt, cov["nodes"], cov["observed_nodes"], cov["pseudo_nodes"],
                 int(cov["observed_deliveries"])))
        print("      default=%s  competitions=%s"
              % (cov["default_competition"], ", ".join(cov["competitions"])))
        prov = cov["provenance"]
        print("      mode=%s  level_measured=%s  shape_measured=%s"
              % (prov.get("mode"), prov.get("level_measured"),
                 prov.get("shape_measured")))
        if prov.get("limitation"):
            print("      limitation: %s" % prov["limitation"])
        comp = baselines.competition_key(doc)
        root = baselines.smoothed(doc, "%s/%s" % (fmt, comp))
        print("      %s baseline: %s" % (comp, "  ".join(
            "%s=%.4f" % (o, root[o]) for o in baselines.OUTCOMES)))
    return 0 if any_found else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cricsheet", help="count real Cricsheet deliveries")
    c.add_argument("--zip", action="append", required=True,
                   help="Cricsheet *_json.zip (repeatable) or a directory")
    c.add_argument("--format", default="T20", choices=["T20", "ODI"])
    c.add_argument("--out", default=os.path.join(REPO, "data",
                                                 "simulation_reference", "v2"))
    c.add_argument("--eras", default="", help="name=lo-hi,name=lo-hi")
    c.add_argument("--min-year", type=int, default=None)
    c.add_argument("--max-year", type=int, default=None)
    c.add_argument("--holdout-from", type=int, default=None,
                   help="keep matches from this year out of the fit")
    c.add_argument("--min-node", type=int, default=200,
                   help="drop nodes with fewer deliveries than this")
    c.add_argument("--default-competition", default="")
    c.set_defaults(func=cmd_cricsheet)

    a = sub.add_parser("anchors", help="build from in-repo measured aggregates")
    a.add_argument("--format", action="append", default=None,
                   choices=["T20", "ODI"])
    a.add_argument("--out", default=os.path.join(REPO, "data",
                                                 "simulation_reference", "v1"))
    a.add_argument("--eras", default="")
    a.add_argument("--pseudo-weight", type=float, default=4000.0)
    a.set_defaults(func=cmd_anchors)

    v = sub.add_parser("coverage", help="describe an existing reference")
    v.add_argument("--version", default="v1")
    v.set_defaults(func=cmd_coverage)

    args = p.parse_args(argv)
    if args.cmd == "anchors" and not args.format:
        args.format = ["T20", "ODI"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
