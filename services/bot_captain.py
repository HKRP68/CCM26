"""The AI captain for /lpbot and /ciplbot — the bot's three per-over decisions.

Every over of a Challenge League "Approach" match asks the two captains for
three things: the bowling side picks a bowler, then a Bowling Approach, then the
batting side picks a Batting Approach. When one of those captains is the bot,
these functions supply the answer.

Everything is a pure function of the live ``cipl_approach`` state dict — no
Telegram, no DB — so the tactics can be unit-tested directly. Eligibility,
quotas and the no-back-to-back rule are NOT re-implemented here: they come from
``services.cipl_match.eligible_bowlers``, which the human picker uses too, so
the bot plays under exactly the same constraints as its opponent.

**How a decision is actually made.** Five layers, in order:

  1. **The game solver** (:mod:`services.bot_tactics`) is the brain. It rebuilds
     the ball-outcome odds for the upcoming over, runs all 25 approach match-ups
     through the *engine's own* modifiers, scores each one on win probability
     (chasing) or runs-minus-the-real-price-of-wickets (batting first), and
     solves the resulting 5×5 zero-sum game. What comes back is a Nash mixed
     strategy: the mix no human counter-strategy can beat.
  2. **The situational read** below — phase, who is on strike, the chase maths,
     wickets in hand — is kept as a *prior* on top of the solver. It carries the
     cricketing sense the crude model can't see, but it no longer decides alone.
  3. **The opponent model.** ``observe_over`` records what the human picked and
     what it cost them. Once there is enough evidence, the bot computes the
     best response to their habits and blends it in — capped, so reading you
     never makes the bot itself readable.
  4. **A persona per match.** Each match the bot draws a captaincy style
     (:data:`PERSONAS`) that biases how aggressive it is and how often it
     bluffs, so two matches from the same scoreline still unfold differently.
  5. **The difficulty the player chose** (:data:`DIFFICULTIES`) scales all of
     the above — how far the solver leads, how wide the mix stays, how hard the
     read is pressed. It never changes the rules the bot plays under, only how
     well it plays them.

That ordering is deliberate. An equilibrium mix is unpredictable *because* it is
optimal, not in spite of it — so the bot stopped being a table a player can
memorise without becoming a random one.
"""

import logging
import math
import random

from engine.approach_modifiers import (
    BATTING_APPROACHES, BATTING_KEYS, BOWLING_APPROACHES, BOWLING_KEYS,
    DEFAULT_BATTING, DEFAULT_BOWLING, normalize_batting, normalize_bowling,
)
from services import bot_tactics, cipl_match
from services.bot_tactics import (            # noqa: F401  (re-exported)
    DEATH_UNITS, POWERPLAY_UNITS, phase,
)

logger = logging.getLogger(__name__)


# Batting approaches ordered least → most aggressive. The batting logic picks a
# (fractional) position on this ladder and spreads its weights around it, which
# keeps "one notch more/less aggressive" honest instead of scattering magic
# strings around.
BAT_LADDER = ["defensive", "rotate", "balanced", "aggressive", "ultra"]
BOWL_PLANS = ["defensive", "balanced", "mixed", "aggressive", "variation"]

# These two lists ARE the sampling space: a key the engine has that is missing
# here would silently never be picked, and a key here that the engine dropped
# would make ``BAT_LADDER.index()`` raise mid-over. Fail at import instead.
assert set(BAT_LADDER) == BATTING_KEYS, (
    f"BAT_LADDER is out of step with engine.approach_modifiers: "
    f"{set(BAT_LADDER) ^ BATTING_KEYS}")
assert set(BOWL_PLANS) == BOWLING_KEYS, (
    f"BOWL_PLANS is out of step with engine.approach_modifiers: "
    f"{set(BOWL_PLANS) ^ BOWLING_KEYS}")

# How much the situational prior counts once the game solver has spoken. Below
# 1.0 it shades the solver's mix rather than overriding it — the solver knows
# what the approaches *do*, the prior knows what a captain would *feel*.
PRIOR_EXPONENT = 0.55

# Hardest the bot will ever lean into its read of the opponent. Going all-in on
# a best response wins more against a predictable human and turns the bot into a
# pure function of their last few picks, which is exactly the trap we came from.
MAX_EXPLOIT = 0.55

# Overs of evidence at which the opponent model is taken half seriously.
EXPLOIT_PRIOR = 4.0

# Bounded nudge (± points) the tactical model may apply to the bowler ranking,
# small enough that traits, quotas and the death hold-back still lead.
MATCHUP_SWING = 8.0

# Trait bonuses when choosing who bowls, by phase.
_POWERPLAY_TRAITS = {"bowl_powerplay"}
_DEATH_TRAITS = {"bowl_death", "bowl_yorker"}
_WICKET_TRAITS = {"bowl_wicket_hunter", "bowl_strike"}

# A par over in a T20 innings — the yardstick for "is this plan working?".
PAR_RUNS_PER_OVER = 8.0


# ════════════════════════════════════════════════════════════════════
# Captaincy personas — the macro variety between one match and the next
# ════════════════════════════════════════════════════════════════════

# ``aggression`` shifts every decision along the attack/defend axis, ``bluff``
# is the chance of throwing a completely off-script plan (the curveball that
# stops a human from ever settling into a read), ``noise`` is how much the
# sampled weights jitter, and ``temperature`` is how loosely the bowler choice
# follows its own ranking.
PERSONAS = {
    "aggressor": {"label": "🔥 Aggressor", "aggression": 0.55, "bluff": 0.12,
                  "noise": 0.30, "temperature": 7.0},
    "tactician": {"label": "🧠 Tactician", "aggression": 0.05, "bluff": 0.16,
                  "noise": 0.22, "temperature": 6.0},
    "grinder": {"label": "🧱 Grinder", "aggression": -0.45, "bluff": 0.10,
                "noise": 0.25, "temperature": 6.5},
    "maverick": {"label": "🎲 Maverick", "aggression": 0.20, "bluff": 0.30,
                 "noise": 0.50, "temperature": 11.0},
}
DEFAULT_PERSONA = "tactician"


# ════════════════════════════════════════════════════════════════════
# Difficulty — how close to optimal the AI captain actually plays
# ════════════════════════════════════════════════════════════════════

# Persona is *flavour* (two Hard bots still feel different); difficulty is
# *strength*. Every setting plays the same game with the same information — a
# weaker bot is not given worse cards or fed dumber rules, it simply trusts the
# solver less, reads you less, and misjudges an over more often. That keeps Easy
# beatable without making it behave like nonsense.
#
#   solver      how hard the equilibrium mix outweighs the situational prior
#   explore     how far the mix is pulled back toward "anything is possible"
#   exploit     ceiling on leaning into the read of your habits
#   temperature looseness of the bowler ranking (higher = sloppier)
#   matchup     how much the bowler match-up model counts
#   blunder     chance per decision of simply picking wrong
#
# ``explore`` matters more than it looks. A Nash mix is often close to pure —
# when one plan really is the answer to this batter in this phase, the solver
# says so with 99% of its weight, and a bot that obeyed it would be back to
# bowling the same thing over after over. Mixing a slice of uniform back in
# costs very little (near the equilibrium the alternatives are worth almost the
# same) and buys back the unpredictability that makes the bot worth playing.
DIFFICULTIES = {
    "easy": {"label": "🟢 Easy", "solver": 0.30, "explore": 0.50, "exploit": 0.10,
             "temperature": 2.2, "matchup": 0.25, "blunder": 0.22},
    "normal": {"label": "🟡 Normal", "solver": 0.80, "explore": 0.32, "exploit": 0.35,
               "temperature": 1.0, "matchup": 0.70, "blunder": 0.07},
    "hard": {"label": "🔴 Hard", "solver": 1.30, "explore": 0.18, "exploit": 0.55,
             "temperature": 0.55, "matchup": 1.00, "blunder": 0.0},
}
DEFAULT_DIFFICULTY = "normal"
DIFFICULTY_ORDER = ("easy", "normal", "hard")


def normalize_difficulty(key):
    """Coerce anything to a real difficulty key, defaulting to Normal."""
    key = (key or "").strip().lower()
    return key if key in DIFFICULTIES else DEFAULT_DIFFICULTY


def assign_difficulty(state, key=None):
    """Fix this match's difficulty on ``state`` and return the key used."""
    key = normalize_difficulty(key)
    if isinstance(state, dict):
        state["bot_difficulty"] = key
    return key


def _difficulty(state):
    """This match's difficulty settings — Normal for a state that has none."""
    return DIFFICULTIES[normalize_difficulty((state or {}).get("bot_difficulty"))]


def difficulty_label(state):
    """Human-readable difficulty for the match cards (e.g. "🔴 Hard")."""
    return _difficulty(state)["label"]


def assign_persona(state, key=None):
    """Draw (or force) this match's captaincy style and remember it on ``state``.

    Called once when a practice match is created so the style is stable for the
    whole match — and shown to the player — instead of flickering over by over.
    """
    if key not in PERSONAS:
        key = random.choice(list(PERSONAS))
    state["bot_persona"] = key
    return key


def _persona(state):
    """This match's persona dict, drawing one lazily for older/None states."""
    key = (state or {}).get("bot_persona")
    if key not in PERSONAS:
        key = assign_persona(state) if isinstance(state, dict) else DEFAULT_PERSONA
    return PERSONAS[key]


def persona_label(state):
    """Human-readable captaincy style for the match cards (e.g. "🧠 Tactician")."""
    return _persona(state)["label"]


# ════════════════════════════════════════════════════════════════════
# Match-reading helpers
# ════════════════════════════════════════════════════════════════════

# The phase/units/striker/rating readers all live in ``bot_tactics`` so the
# solver and the captaincy layer can never drift apart on what "the death" or
# "wickets in hand" means. These aliases keep the call sites here readable.
_total_units = bot_tactics.total_units
_current_unit = bot_tactics.current_unit
_units_left = bot_tactics.units_left
_death_units_left = bot_tactics.death_units_left
_striker = bot_tactics.striker
_striker_form = bot_tactics.striker_form
_wickets_lost = bot_tactics.wickets_lost
_wickets_in_hand = bot_tactics.wickets_in_hand
_rating = bot_tactics.rating


def _traits_of(player):
    """The set of trait effect keys on a player, ignoring malformed entries."""
    return {str((t or {}).get("effect_key") or "")
            for t in (player.get("traits") or []) if isinstance(t, dict)}


def _economy(state, rid):
    """``(overs_bowled, runs_per_over)`` for a bowler in the current innings."""
    stats = (state.get("bowl_stats") or {}).get(str(rid), {})
    balls = int(stats.get("balls", 0) or 0)
    if balls <= 0:
        return 0.0, None
    runs = int(stats.get("runs", 0) or 0)
    return balls / 6.0, runs / balls * 6.0


def _bot_is_batting(state):
    """True when the AI captain owns the batting side of ``state``."""
    bot_uid = (state or {}).get("bot_user_id")
    return bot_uid is not None and state.get("bat_team_id") == bot_uid


# ════════════════════════════════════════════════════════════════════
# Opponent memory — what the human does, and what it costs them
# ════════════════════════════════════════════════════════════════════

# The plan that hurts each batting approach most, used when the human has shown
# they will keep picking the same thing.
_BAT_COUNTER = {
    "ultra": "variation",       # cut the six off and the risk stays
    "aggressive": "mixed",      # boundaries down, wicket chance up
    "balanced": "aggressive",   # nothing to fear — hunt the wicket
    "rotate": "aggressive",
    "defensive": "aggressive",
}

# ...and the reply to a bowling plan the human keeps repeating.
_BOWL_COUNTER = {
    "defensive": "rotate",      # boundaries are shut off — milk the singles
    "balanced": "aggressive",
    "mixed": "rotate",
    "aggressive": "rotate",     # their wicket multiplier only bites a slogger
    "variation": "rotate",
}

_MEMORY_SHAPE = ("opp_bat", "opp_bowl", "own_bat", "own_bowl")
_RECENT_KEEP = 6


def _memory(state, create=True):
    """The bot's per-match read of its opponent, created on first use.

    Plain nested dicts/lists of primitives so it survives the JSON round-trip
    every ``save_state`` does, and tolerant of a partial dict restored from a
    match that started before this existed.

    ``create=False`` returns a throwaway empty memory instead of writing one onto
    ``state`` — used by the read paths so a state that is not a bot match never
    picks up a ``bot_memory`` blob it would then persist.
    """
    mem = state.get("bot_memory")
    if not isinstance(mem, dict):
        mem = {}
        if create:
            state["bot_memory"] = mem
    for key in _MEMORY_SHAPE:
        if not isinstance(mem.get(key), dict):
            mem[key] = {}
    for key in ("recent_opp_bat", "recent_opp_bowl"):
        if not isinstance(mem.get(key), list):
            mem[key] = []
    return mem


def _record(bucket, key, runs, wickets):
    """Fold one over's runs and wickets into a memory bucket for ``key``."""
    slot = bucket.get(key)
    if not isinstance(slot, dict):
        slot = {"n": 0, "runs": 0, "wkts": 0}
        bucket[key] = slot
    slot["n"] = int(slot.get("n", 0)) + 1
    slot["runs"] = int(slot.get("runs", 0)) + runs
    slot["wkts"] = int(slot.get("wkts", 0)) + wickets


def observe_over(state, summary):
    """Feed a completed over back into the bot's read of the match.

    Safe to call for every over of every match (it no-ops when the bot isn't
    playing), and never raises — a memory failure must not cost anybody an over.
    """
    try:
        if not state or not state.get("is_bot_match") or not summary:
            return
        mem = _memory(state)
        bat_app = normalize_batting(summary.get("batting_approach"))
        bowl_app = normalize_bowling(summary.get("bowling_approach"))
        runs = int(summary.get("over_runs") or 0)
        wickets = int(summary.get("over_wickets") or 0)

        if _bot_is_batting(state):
            _record(mem["own_bat"], bat_app, runs, wickets)
            _record(mem["opp_bowl"], bowl_app, runs, wickets)
            mem["recent_opp_bowl"] = (mem["recent_opp_bowl"] + [bowl_app])[-_RECENT_KEEP:]
            mem["last_own_bat"] = bat_app
        else:
            _record(mem["opp_bat"], bat_app, runs, wickets)
            _record(mem["own_bowl"], bowl_app, runs, wickets)
            mem["recent_opp_bat"] = (mem["recent_opp_bat"] + [bat_app])[-_RECENT_KEEP:]
            mem["last_own_bowl"] = bowl_app
    except Exception:
        logger.exception("bot captain: recording the over failed (match %s)",
                         (state or {}).get("match_id"))


def _share(bucket, keys):
    """Fraction of recorded overs that used one of ``keys`` (0.0 when empty)."""
    total = sum(int((s or {}).get("n", 0)) for s in bucket.values())
    if total <= 0:
        return 0.0, 0
    hits = sum(int((bucket.get(k) or {}).get("n", 0)) for k in keys)
    return hits / total, total


def _repeated_pick(recent, times=3):
    """The approach the opponent has picked ``times`` overs running, if any."""
    if len(recent) < times:
        return None
    tail = recent[-times:]
    return tail[0] if len(set(tail)) == 1 else None


def _note_read(state, text):
    """Leave a one-line note that the bot has spotted a pattern.

    ``handlers.cipl_play._render_over_summary`` shows and clears it, so the
    player can see the AI reacting to them instead of just feeling it.
    """
    state["bot_read_note"] = text


def take_read_note(state):
    """Pop the pending "the bot has read you" line, if there is one."""
    if not isinstance(state, dict):
        return None
    return state.pop("bot_read_note", None)


# ════════════════════════════════════════════════════════════════════
# Weighted sampling
# ════════════════════════════════════════════════════════════════════

def _tilt(weights, key, factor):
    """Scale one weight, chosen at runtime (e.g. a counter looked up by name)."""
    if key in weights:
        weights[key] *= factor


def _tilts(weights, **factors):
    """Scale several named weights at once: ``_tilts(w, aggressive=1.9, ...)``.

    The keyword form keeps each situation's adjustment readable as one table
    instead of a column of near-identical single-key calls.
    """
    for key, factor in factors.items():
        if key in weights:
            weights[key] *= factor


def _sample(weights, noise=0.0):
    """Pick one key at random, in proportion to its weight.

    ``noise`` jitters each weight multiplicatively (log-normal) so even a
    lopsided situation occasionally produces the second- or third-best plan —
    which is what stops a human from ever being sure what is coming.
    """
    keys = [k for k, w in weights.items() if w is not None]
    if not keys:
        return None
    vals = []
    for key in keys:
        weight = max(0.01, float(weights[key]))
        if noise > 0:
            weight *= math.exp(random.gauss(0.0, noise))
        vals.append(weight)
    return random.choices(keys, weights=vals, k=1)[0]


def _apply_learning(weights, record, *, bowling):
    """Nudge weights toward whatever has actually been working this match.

    Bowling wants cheap overs and wickets; batting wants runs without losing the
    top order. The tilt is bounded and confidence-scaled, so one lucky over never
    hijacks the plan but a plan that keeps going for 15 quietly falls away.
    """
    for key, slot in (record or {}).items():
        if key not in weights or not isinstance(slot, dict):
            continue
        n = int(slot.get("n", 0) or 0)
        if n <= 0:
            continue
        rpo = float(slot.get("runs", 0)) / n
        wpo = float(slot.get("wkts", 0)) / n
        if bowling:
            edge = (PAR_RUNS_PER_OVER - rpo) * 0.06 + wpo * 0.35
        else:
            edge = (rpo - PAR_RUNS_PER_OVER) * 0.06 - wpo * 0.30
        edge = max(-0.8, min(0.8, edge)) * min(1.0, n / 4.0)
        weights[key] *= math.exp(edge)


# ════════════════════════════════════════════════════════════════════
# The game-solver layer
# ════════════════════════════════════════════════════════════════════

def _predict_opponent(state, *, bot_batting):
    """``({approach: probability}, exploit_weight)`` for the human's next pick.

    Counts every approach they have used this match, weights the last three
    overs double (people drift, and the recent drift is the exploitable part),
    and smooths so a plan they have not shown yet is never treated as
    impossible. ``exploit_weight`` scales with the evidence and is capped at
    :data:`MAX_EXPLOIT`: with two overs on the board the bot barely leans on the
    read, with a dozen it leans hard — but never all the way.
    """
    mem = _memory(state, create=False)
    ceiling = min(MAX_EXPLOIT, _difficulty(state)["exploit"])
    if ceiling <= 0:
        return None, 0.0
    if bot_batting:
        bucket, recent, keys = mem["opp_bowl"], mem["recent_opp_bowl"], BOWL_PLANS
    else:
        bucket, recent, keys = mem["opp_bat"], mem["recent_opp_bat"], BAT_LADDER

    counts = {k: float(int((bucket.get(k) or {}).get("n", 0) or 0)) for k in keys}
    for key in recent[-3:]:
        if key in counts:
            counts[key] += 1.0

    observed = sum(counts.values())
    if observed <= 0:
        return None, 0.0

    smoothing = 0.5
    denominator = observed + smoothing * len(keys)
    predicted = {k: (counts[k] + smoothing) / denominator for k in keys}
    weight = ceiling * (observed / (observed + EXPLOIT_PRIOR))
    return predicted, weight


def _explore(state, mix, keys):
    """Pull a mix back toward uniform by the difficulty's exploration share."""
    epsilon = max(0.0, min(0.9, float(_difficulty(state)["explore"])))
    if epsilon <= 0:
        return mix
    share = epsilon / len(keys)
    return {key: (1.0 - epsilon) * mix.get(key, 0.0) + share for key in keys}


def _solver_mix(state, *, bot_batting):
    """The solver's recommended mix over the bot's five approaches, or ``None``.

    Nash equilibrium for the over, blended toward the best response to whatever
    the opponent model has picked up. Never raises: a modelling failure drops
    the bot back to the situational prior below rather than costing anyone an
    over.
    """
    try:
        bat_mix, bowl_mix, matrix = bot_tactics.tactical_mixes(state)
        if not bot_tactics.matrix_is_decisive(matrix):
            # Every approach leads to the same place — a chase already gone, or
            # a total already out of reach. There is nothing for the solver to
            # say, and letting it speak anyway would replace the situational
            # read with a coin toss.
            return None
        if bot_batting:
            keys, vector = bot_tactics.BAT_KEYS, bat_mix
        else:
            keys, vector = bot_tactics.BOWL_KEYS, bowl_mix
        if not vector:
            return None
        nash = {key: vector[idx] for idx, key in enumerate(keys)}

        predicted, weight = _predict_opponent(state, bot_batting=bot_batting)
        if predicted and weight > 0:
            counter = bot_tactics.exploit_mix(matrix, predicted,
                                              batting=bot_batting, keys=keys)
            nash = {key: (1.0 - weight) * nash[key] + weight * counter[key]
                    for key in keys}
        return _explore(state, nash, keys)
    except Exception:
        logger.exception("bot captain: the game solver failed (match %s) — "
                         "falling back to the situational read",
                         (state or {}).get("match_id"))
        return None


# How hard the bot avoids repeating the plan it used last over. This is applied
# to the *final* distribution, after the solver has had its say: an equilibrium
# mix is unexploitable in a one-shot over but an opponent watching a sequence of
# them still reads a repeat, so the damping has to survive the blend.
REPEAT_DAMPING = 0.6


def _damp_repeat(state, weights, *, bot_batting):
    """Push down whatever the bot picked last over, whoever suggested it."""
    mem = _memory(state, create=False)
    last = mem.get("last_own_bat" if bot_batting else "last_own_bowl")
    if last in weights:
        weights[last] *= REPEAT_DAMPING
    return weights


def _blend_with_solver(state, prior, *, bot_batting):
    """Fold the solver's mix into the situational weights.

    A geometric blend, so the two layers argue in the same (multiplicative)
    currency the rest of this module uses: the solver sets the shape of the
    distribution and the prior — raised to :data:`PRIOR_EXPONENT` — shades it.
    Neither can veto the other, which is the point: the solver is right about
    the odds, the prior is right about the cricket.
    """
    solved = _solver_mix(state, bot_batting=bot_batting)
    if not solved:
        return prior

    trust = _difficulty(state)["solver"]
    total = sum(max(0.0, v) for v in prior.values()) or 1.0
    blended = {}
    for key, weight in prior.items():
        share = max(1e-6, weight / total)
        blended[key] = ((share ** PRIOR_EXPONENT)
                        * (max(1e-6, solved.get(key, 0.0)) ** trust))
    # Normalise before handing the mix on. The product of two sub-1 numbers
    # raised to powers lands anywhere on the number line depending on the
    # difficulty, and ``_sample``'s ``max(0.01, ...)`` guard would then quietly
    # become the thing setting the mix — compressing a 200:1 solver preference
    # into 20:1 on Hard. The guard should catch bad input, not do the tuning.
    scale = sum(blended.values()) or 1.0
    return {key: value / scale for key, value in blended.items()}


# ════════════════════════════════════════════════════════════════════
# Bowler selection
# ════════════════════════════════════════════════════════════════════

def _bowler_score(state, player, ph, hold_back):
    """Rank a candidate bowler for the upcoming over.

    Rating is the backbone; phase-matched traits add a real but not overwhelming
    bump; a part-timer is a last resort; and in the middle overs the frontline
    quicks are pushed down so their overs survive until the death. On top of
    that the bot captains to what is happening in front of it — the best bowler
    is thrown at a set batter, a wicket-taker greets a new one, and whoever is
    being carted this innings gets taken out of the attack.
    """
    bowl_rating = _rating(player, "bowl_rating", default=0.0)
    score = bowl_rating
    traits = _traits_of(player)

    if ph == "powerplay" and traits & _POWERPLAY_TRAITS:
        score += 12
    if ph == "death" and traits & _DEATH_TRAITS:
        score += 15
    if ph == "death" and traits & _POWERPLAY_TRAITS:
        score -= 4  # a new-ball specialist is not the answer at the death

    if cipl_match.is_part_time_bowler(player):
        score -= 25

    if player.get("roster_id") in hold_back:
        score -= 30

    # ── Matchup: who is on strike right now ──
    balls_faced, sr = _striker_form(state)
    if balls_faced >= 12 and sr >= 130:
        # A set batter needs the best bowler in the side, not a holding over.
        score += (bowl_rating - 70) * 0.15
    elif balls_faced < 4:
        # Fresh batter: back a wicket-taker to make the new-batter over count.
        if traits & _WICKET_TRAITS:
            score += 10

    # ── This innings' evidence: economy so far ──
    overs_done, rpo = _economy(state, player.get("roster_id"))
    if rpo is not None and overs_done >= 1.0:
        if rpo >= 11:
            score -= 9
        elif rpo >= 9.5:
            score -= 4
        elif rpo <= 6:
            score += 6
        wickets = int((state.get("bowl_stats") or {})
                      .get(str(player.get("roster_id")), {}).get("wickets", 0) or 0)
        score += min(10, wickets * 5)   # a bowler on a roll keeps the ball

    return score


def _hold_back_for_death(state, candidates):
    """Roster ids to keep in reserve during the middle overs.

    A captain who burns their two best bowlers by over 14 has nothing left when
    it matters. If the death phase still needs covering, the top two bowlers'
    remaining overs are earmarked for it — unless they have enough spare overs
    to bowl now AND at the death.
    """
    if phase(state) != "middle":
        return set()
    death_left = _death_units_left(state)
    if death_left <= 0:
        return set()
    best = sorted(candidates, key=lambda p: p.get("bowl_rating") or 0, reverse=True)[:2]
    hold = set()
    for p in best:
        if cipl_match.overs_left(state, p) <= death_left:
            hold.add(p.get("roster_id"))
    # Never hold back everyone — somebody has to bowl this over.
    if len(hold) >= len(candidates):
        return set()
    return hold


def _matchup_bonuses(state, candidates):
    """A bounded ranking nudge per candidate from the tactical model.

    Each candidate's over is solved as its own 5×5 game and scored on what it
    would be worth to the *batting* side; the cheapest over earns the biggest
    bonus. That is how the bot notices that the spinner rated 80 is a better
    answer to this batter, in this phase, on this pitch, than the quick rated
    86 — a comparison ``bowl_rating`` on its own cannot make. Rescaled into
    ±:data:`MATCHUP_SWING` points so it informs the ranking without overruling
    the trait, quota and death-reserve logic around it.
    """
    if len(candidates) < 2:
        return [0.0] * len(candidates)
    try:
        values = [bot_tactics.bowler_edge(state, p) for p in candidates]
    except Exception:
        logger.exception("bot captain: bowler match-up model failed (match %s)",
                         (state or {}).get("match_id"))
        return [0.0] * len(candidates)
    low, high = min(values), max(values)
    span = high - low
    if span <= 1e-9:
        return [0.0] * len(candidates)
    swing = MATCHUP_SWING * _difficulty(state)["matchup"]
    # Lower value = the over is worth less to the batting side = better bowler.
    return [swing * (2.0 * (high - v) / span - 1.0) for v in values]


def pick_bowler(state):
    """Choose the bot's bowler for the upcoming over.

    Only ever returns a player ``cipl_match.eligible_bowlers`` already approved,
    so the quota, the no-back-to-back rule and the part-timer tiers are honoured
    exactly as they are for a human captain. The choice is a softmax over the
    whole eligible list rather than a fixed top-three lottery: the right bowler
    still bowls most of the time, but the attack is never a fixed rotation the
    opponent can plan an innings around. Returns ``None`` only when the XI
    somehow has nobody to bowl.
    """
    candidates = cipl_match.eligible_bowlers(state)
    if not candidates:
        logger.warning("bot captain: no eligible bowlers for match %s",
                       state.get("match_id"))
        return None
    if len(candidates) == 1:
        return candidates[0]

    persona = _persona(state)
    ph = phase(state)
    hold_back = _hold_back_for_death(state, candidates)
    matchup = _matchup_bonuses(state, candidates)
    scored = [(p, _bowler_score(state, p, ph, hold_back) + bonus)
              for p, bonus in zip(candidates, matchup)]
    best = max(score for _p, score in scored)
    # Floor only guards against a zero/negative temperature blowing up the
    # softmax below. It used to be 1.0, which would silently cancel Hard's 0.55
    # multiplier for any persona rated under ~1.8 — none are today, but the
    # difficulty knob should not quietly stop working if one is ever added.
    temperature = max(0.15, float(persona["temperature"])
                      * float(_difficulty(state)["temperature"]))

    keys = list(range(len(scored)))
    weights = [math.exp(max(-40.0, (score - best) / temperature))
               for _player, score in scored]
    chosen = random.choices(keys, weights=weights, k=1)[0]
    return scored[chosen][0]


# ════════════════════════════════════════════════════════════════════
# Bowling approach
# ════════════════════════════════════════════════════════════════════

def _bowling_weights(state):
    """Raw per-plan weights from the match situation alone."""
    ph = phase(state)
    balls_faced, sr = _striker_form(state)
    chase = cipl_match.chase(state)
    wickets_lost = _wickets_lost(state)

    w = {"defensive": 1.0, "balanced": 1.3, "mixed": 1.2,
         "aggressive": 1.2, "variation": 1.3}

    # ── Phase ──
    if ph == "powerplay":
        _tilts(w, aggressive=1.9, balanced=1.2, variation=0.9, defensive=0.5)
    elif ph == "death":
        _tilts(w, mixed=2.0, variation=1.6, defensive=1.2, balanced=0.7,
               aggressive=0.8)
    else:
        _tilts(w, balanced=1.3, variation=1.3, mixed=1.1)

    # ── Who is on strike ──
    if balls_faced < 6:
        # A brand-new batter is at their most vulnerable — go after them.
        _tilts(w, aggressive=2.2, mixed=1.15, defensive=0.6)
    elif balls_faced >= 8 and sr >= 150:
        # Someone flying: change of pace beats more of the same.
        _tilts(w, variation=2.1, mixed=1.8, aggressive=0.6)
    elif balls_faced >= 10 and sr < 95:
        # Bogged down — squeeze harder and the wicket usually follows.
        _tilts(w, aggressive=1.5, defensive=0.75)

    # ── Wickets: into the tail, bowl them out ──
    if wickets_lost >= 7:
        _tilts(w, aggressive=1.7, mixed=1.2, defensive=0.6)
    elif wickets_lost >= 5:
        _tilt(w, "aggressive", 1.2)

    # ── The chase maths, when defending ──
    if chase and chase["balls_remaining"] > 0:
        rrr = chase["rrr"]
        if rrr >= 13:
            # They have to swing at everything: deny the boundary, take the risk.
            _tilts(w, defensive=2.6, mixed=1.5, aggressive=0.5)
        elif rrr >= 10:
            _tilts(w, mixed=1.5, variation=1.4)
        elif rrr <= 6:
            # Cruising — only wickets change this game.
            _tilts(w, aggressive=1.8, variation=1.3, defensive=0.6)
        if chase["runs_required"] <= 12 and chase["balls_remaining"] <= 12:
            # A tight finish is won by dots, not by heroics.
            _tilts(w, defensive=1.8, mixed=1.5, aggressive=0.7)
    return w


def _read_the_batter(state, w):
    """Counter the way this human has actually been batting."""
    mem = _memory(state, create=False)
    attack_share, samples = _share(mem["opp_bat"], ("aggressive", "ultra"))
    block_share, _ = _share(mem["opp_bat"], ("defensive", "rotate"))
    if samples >= 3:
        if attack_share >= 0.5:
            # A swinger: take the boundary away and let the risk do the work.
            _tilts(w, variation=1.5, mixed=1.4, aggressive=1.15, defensive=0.8)
        if block_share >= 0.5:
            # A blocker gives up nothing but their wicket — so go get it.
            _tilts(w, aggressive=1.6, balanced=1.1, defensive=0.6)
    repeated = _repeated_pick(mem["recent_opp_bat"])
    if repeated:
        _tilt(w, _BAT_COUNTER.get(repeated, "balanced"), 1.9)
        _note_read(state, f"The bot has spotted three straight "
                          f"{batting_label(repeated)} overs — it's planning for it.")
    _apply_learning(w, mem["own_bowl"], bowling=True)


def pick_bowling_approach(state):
    """Choose the bot's Bowling Approach for the over.

    The solver decides the shape: it prices all 25 approach match-ups for this
    exact over and returns the equilibrium mix, pulled toward the best response
    to whatever the human has been doing. The situational read below — phase,
    the batter on strike, wickets in hand, the chase maths — shades that mix,
    the persona tilts it along the attack/defend axis, and the final draw is
    sampled so the plan is never a foregone conclusion. Occasionally the bot
    bluffs with something completely off-script.
    """
    persona = _persona(state)
    take_read_note(state)          # last over's note, if the chat never used it
    if random.random() < persona["bluff"] + _difficulty(state)["blunder"]:
        return random.choice(BOWL_PLANS)

    w = _bowling_weights(state)
    _read_the_batter(state, w)
    w = _damp_repeat(state, _blend_with_solver(state, w, bot_batting=False),
                     bot_batting=False)

    aggression = persona["aggression"]
    _tilts(w, aggressive=math.exp(aggression),
           mixed=math.exp(aggression * 0.35),
           defensive=math.exp(-aggression))

    return _sample(w, noise=persona["noise"]) or DEFAULT_BOWLING


# ════════════════════════════════════════════════════════════════════
# Batting approach
# ════════════════════════════════════════════════════════════════════

def _base_bat_index(state):
    """Where on the aggression ladder this situation sits, as a float.

    A fraction rather than a rung, because the weights below are spread around
    it — "2.6" leans balanced with a real chance of aggressive, which is how a
    captain's intent actually reads.
    """
    ph = phase(state)
    chase = cipl_match.chase(state)

    if chase and chase["balls_remaining"] > 0:
        # Straight off the required rate: ~4.5 rpo is a stroll, 14+ is a slog.
        idx = 0.9 + max(0.0, chase["rrr"] - 4.0) / 2.3
        return max(0.5, min(4.4, idx))

    # First innings — build, then accelerate, with the scoreboard as a check.
    idx = {"powerplay": 3.0, "middle": 2.2, "death": 4.0}[ph]
    balls = cipl_match.balls_bowled(state)
    if balls >= 24:
        run_rate = int(state.get("total_runs") or 0) / (balls / 6.0)
        if run_rate < PAR_RUNS_PER_OVER - 2:
            idx += 0.5          # behind par — somebody has to take a risk
        elif run_rate > PAR_RUNS_PER_OVER + 2:
            idx -= 0.3          # well ahead — no need to force it
    return idx


def _bat_situation_index(state):
    """The base intent, adjusted for wickets, the striker and the bowler."""
    idx = _base_bat_index(state)
    chase = cipl_match.chase(state)
    desperate = bool(chase and chase["balls_remaining"] > 0 and chase["rrr"] >= 12)
    wickets_lost = _wickets_lost(state)

    # Wickets in hand buy risk; wickets lost demand care.
    if wickets_lost >= 8:
        idx -= 1.8
    elif wickets_lost >= 6:
        idx -= 0.9
    elif wickets_lost <= 2 and _units_left(state) <= 8:
        idx += 0.4          # plenty in the shed and not long left — cash in

    if phase(state) == "death":
        idx += 0.8

    # Ability + form check — mirrors the rating-aware shot logic in bot_ai.
    striker = _striker(state)
    bat_rating = _rating(striker, "bat_rating", "rating", default=50.0)
    balls_faced, sr = _striker_form(state)
    if not desperate:
        if bat_rating < 45:
            idx -= 0.9
        elif bat_rating >= 85:
            idx += 0.5
        if balls_faced <= 3:
            idx -= 0.5      # let a new batter have a look first
        elif balls_faced >= 15 and sr >= 130:
            idx += 0.5      # set and seeing it well — press the advantage

    # Who is bowling: attack the weak link, respect the spearhead.
    bowler = state.get("current_bowler") or {}
    if bowler:
        bowl_rating = _rating(bowler, "bowl_rating", default=60.0)
        if cipl_match.is_part_time_bowler(bowler) or bowl_rating < 55:
            idx += 0.7
        elif bowl_rating >= 85:
            idx -= 0.3

    return idx, desperate


def _read_the_bowler(state, w, desperate):
    """Counter the way this human has actually been bowling."""
    mem = _memory(state, create=False)
    attack_share, samples = _share(mem["opp_bowl"], ("aggressive",))
    defend_share, _ = _share(mem["opp_bowl"], ("defensive", "mixed"))
    wickets_in_hand = _wickets_in_hand(state)
    if samples >= 3:
        if defend_share >= 0.5:
            # They are shutting the boundary down — take the singles on offer.
            _tilts(w, rotate=1.6, balanced=1.2, ultra=0.75)
        if attack_share >= 0.5 and not desperate:
            if wickets_in_hand >= 7:
                # Their wicket-hunting plan leaks boundaries — make it hurt.
                _tilts(w, aggressive=1.4, ultra=1.2)
            else:
                # Too thin to trade wickets with them.
                _tilts(w, rotate=1.5, balanced=1.2, ultra=0.7)
    repeated = _repeated_pick(mem["recent_opp_bowl"])
    if repeated and not desperate:
        _tilt(w, _BOWL_COUNTER.get(repeated, "balanced"), 1.7)
        _note_read(state, f"The bot has read three straight "
                          f"{bowling_label(repeated)} overs — it's countering.")
    _apply_learning(w, mem["own_bat"], bowling=False)


def pick_batting_approach(state):
    """Choose the bot's Batting Approach for the over.

    The intent starts as a *spread* over the aggression ladder — the phase or
    the required rate, adjusted for wickets in hand, who is on strike and who is
    bowling, so a number eleven does not slog like a top-order batter unless the
    chase leaves no choice. The solver then reprices that spread against what
    each approach really does to this over's odds: chasing, every rung is scored
    on the win probability it leaves behind, which is what makes the bot's death
    overs add up instead of just looking busy.
    """
    persona = _persona(state)
    take_read_note(state)          # last over's note, if the chat never used it
    idx, desperate = _bat_situation_index(state)
    idx += persona["aggression"] * 0.8

    if (random.random() < persona["bluff"] + _difficulty(state)["blunder"]
            and not desperate):
        # Total curveball: a block in the powerplay, a slog out of nowhere. It
        # still goes through the hard rules below — a bluff is a tactic, not a
        # licence to throw the innings away.
        choice = random.choice(BAT_LADDER)
    else:
        # Spread the intent over neighbouring rungs — wider for a loose persona.
        spread = 0.55 + persona["noise"]
        w = {key: math.exp(-((pos - idx) ** 2) / (2 * spread * spread))
             for pos, key in enumerate(BAT_LADDER)}

        _read_the_bowler(state, w, desperate)
        w = _damp_repeat(state, _blend_with_solver(state, w, bot_batting=True),
                         bot_batting=True)

        choice = _sample(w, noise=persona["noise"] * 0.5) or DEFAULT_BATTING

    # ── Hard rules the dice never override ──
    if desperate:
        # A desperate chase overrides caution: you cannot block your way to a win.
        if BAT_LADDER.index(choice) < BAT_LADDER.index("aggressive"):
            choice = random.choice(("aggressive", "ultra"))
    elif _wickets_in_hand(state) <= 1 and _units_left(state) >= 3:
        # Last pair with overs to survive — bat, don't gift it away.
        if BAT_LADDER.index(choice) > BAT_LADDER.index("balanced"):
            choice = random.choice(("rotate", "balanced"))
    return choice


# ════════════════════════════════════════════════════════════════════
# Labels for the chat messages
# ════════════════════════════════════════════════════════════════════

_BAT_LABELS = {key: (emoji, label) for key, emoji, label in BATTING_APPROACHES}
_BOWL_LABELS = {key: (emoji, label) for key, emoji, label in BOWLING_APPROACHES}


def batting_label(key):
    """A Batting Approach as it reads in chat, e.g. ``"💥 Ultra Attack"``."""
    emoji, label = _BAT_LABELS.get(key, _BAT_LABELS[DEFAULT_BATTING])
    return f"{emoji} {label}"


def bowling_label(key):
    """A Bowling Approach as it reads in chat, e.g. ``"🌀 Variation"``."""
    emoji, label = _BOWL_LABELS.get(key, _BOWL_LABELS[DEFAULT_BOWLING])
    return f"{emoji} {label}"


def elect_toss_decision():
    """Bat or bowl after winning the toss — a straight 50/50, as specified."""
    return random.choice(("bat", "bowl"))
