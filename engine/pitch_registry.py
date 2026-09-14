"""The eight surfaces, and what each one *is* — one source of truth.

Before this module the bot held at least six independent opinions about which
pitches exist. ``services/match_constants.PITCH_TYPES`` offered seven,
``services/probability_engine.PITCH_MODS`` knew a different seven (no Bouncy —
so a Bouncy pitch picked in a manual match was silently played as *Flat*, the
most batting-friendly surface in the table), ``config/ground_conditions.yaml``
configured eight, a stale second copy under ``data/`` configured five with
different numbers, ``engine/game_state_engine`` judged par against five, and
``services/pitch_report`` told the toss-winner to bat on a road while
``engine/format_config`` told them to bowl.

Everything descriptive about a surface now lives here, and the numeric model
stays where it already was:

    engine/pitch_registry  →  which surfaces exist, and their cricket identity
    config/ground_conditions.yaml
                           →  the ball-by-ball model (scoring matrix, who takes
                              the wickets, par band, chase grid)
    engine/pitch_state     →  how the surface changes between innings
    engine/approach_modifiers
                           →  which captaincy calls the surface rewards

The three numeric layers are keyed by the names here, and
``tests/test_pitch_registry.py`` fails if any of them gains, loses or disagrees
about a surface.

Public API:
    PITCHES / SELECTABLE / DEFAULT
    profile(name) -> PitchProfile
    normalise(name) -> str
    is_known(name) -> bool
    par_band(name) -> (low, high) | None
    toss_call(name, day_night=None) -> "bat" | "bowl"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class PitchProfile:
    """One surface's cricket identity.

    ``order`` runs 0 (rankest bowling surface) to 7 (deadest road) and is the
    canonical difficulty ranking every other table is checked against.
    """

    name: str
    order: int
    family: str          # seam | turn | bounce | true | road
    favours: str         # Pace | Spin | Balanced | Batting
    archetype: str       # the real-world surface this is modelled on
    blurb: str           # one line, for cards and pickers
    character: str       # a sentence of actual cricket, for help screens
    effectiveness: Tuple[int, int, int]   # (pacers, spinners, batters), 0-5
    grass: Tuple[str, ...]                # weighted grass-cover draw
    toss: str                             # day match: "bat" | "bowl"
    toss_dn: str                          # day/night: dew moves most of these
    toss_reason: str                      # why, in the captain's words
    selectable: bool = True               # offered when a host picks a pitch


# Ordered bowler-friendly → batting-friendly. That order is load-bearing: the
# par bands, the run factors and the guard tests all read it.
_PROFILES = (
    PitchProfile(
        name="Dusty", order=0, family="turn", favours="Spin",
        archetype="Chepauk / Kanpur — a raging, bare turner",
        blurb="Raging turner — spinners rule, nobody gets away",
        character=(
            "Square turn from the first over and rough outside both off stumps "
            "by the tenth. A single is always on and a boundary almost never "
            "is, so the middle overs strangle; the quicks bowl cutters into the "
            "footmarks and hope."),
        effectiveness=(1, 5, 1),
        grass=("No Grass", "No Grass", "Little"),
        toss="bat", toss_dn="bowl",
        toss_reason="the footmarks only deepen — post a total and squeeze",
    ),
    PitchProfile(
        name="Green", order=1, family="seam", favours="Pace",
        archetype="Wellington / Headingley — a grassed-up seaming top",
        blurb="Green top — seam and swing; nick off or cash in",
        character=(
            "Lateral movement off the seam and in the air while the grass and "
            "the shine last. You cannot work it around — the runs come in "
            "fours off the edge and through the covers, or they do not come at "
            "all. Boom or bust, and the boom fades as the grass burns off."),
        effectiveness=(5, 1, 2),
        grass=("Heavy", "Heavy", "Medium"),
        toss="bowl", toss_dn="bowl",
        toss_reason="use the grass and the new ball before they go",
    ),
    PitchProfile(
        name="Dry", order=2, family="turn", favours="Spin",
        archetype="Abu Dhabi / Kandy — slow, low, gripping",
        blurb="Slow turner — grip through the middle, cracks late",
        character=(
            "The ball does not come on, so the big shot is a mug's game and the "
            "nudged single is not. Spin grips from the middle overs, and once "
            "the cracks open the last four overs become a lottery of variable "
            "bounce."),
        effectiveness=(2, 4, 2),
        grass=("Little", "No Grass", "Little"),
        toss="bat", toss_dn="bowl",
        toss_reason="the cracks widen — bat before the surface breaks up",
    ),
    PitchProfile(
        name="Bouncy", order=3, family="bounce", favours="Pace",
        archetype="Perth / the Wanderers — steep, true bounce",
        blurb="Bouncy deck — express pace bites, timing carries",
        character=(
            "Steep bounce off a hard surface. Express pace is worth more here "
            "than anywhere, the pull and the hook are dangerous shots played "
            "and dangerous shots left, and a mistimed one still clears the "
            "rope. The vicious bounce settles as the match wears on."),
        effectiveness=(4, 1, 3),
        grass=("Medium", "Medium", "Heavy"),
        toss="bowl", toss_dn="bowl",
        toss_reason="the bounce is at its steepest with the new ball",
    ),
    PitchProfile(
        name="Even", order=4, family="true", favours="Balanced",
        archetype="a neutral international T20 square",
        blurb="Neutral track — bat and ball share the honours",
        character=(
            "The standard surface: enough for the quicks with the new ball, "
            "enough grip for the spinners through the middle, and enough "
            "bounce to hit through the line. Nobody is shut out and nobody is "
            "handed anything."),
        effectiveness=(3, 3, 3),
        grass=("Little", "Medium", "Little"),
        toss="bat", toss_dn="bowl",
        toss_reason="it grips a little as it wears — runs on the board",
    ),
    PitchProfile(
        name="Hard", order=5, family="true", favours="Batting",
        archetype="a modern drop-in with real carry",
        blurb="Hard and true — good carry, a batting edge",
        character=(
            "True bounce and genuine carry. The ball comes onto the bat, so "
            "the drive and the pull both pay and blocking wastes the surface; "
            "pace is still rewarded for hitting the deck, but spin has to earn "
            "every wicket."),
        effectiveness=(3, 2, 4),
        grass=("Medium", "Little", "Medium"),
        toss="bat", toss_dn="bowl",
        toss_reason="pace carry drops in the second innings — bat first",
    ),
    PitchProfile(
        name="Flat", order=6, family="road", favours="Batting",
        archetype="Wankhede / Chinnaswamy — a genuine road",
        blurb="Road — even bounce, no movement, a run-fest",
        character=(
            "Even bounce, no lateral movement and a fast outfield. Every "
            "scoring option is open and the bowler's only currency is change "
            "of pace. It barely wears, so the side batting second gets the "
            "same surface and a target to aim at."),
        effectiveness=(2, 2, 5),
        grass=("Little", "Little", "No Grass"),
        toss="bowl", toss_dn="bowl",
        toss_reason="it does not deteriorate — take the target and chase",
    ),
    PitchProfile(
        name="Dead", order=7, family="road", favours="Batting",
        archetype="Sharjah in its shirtfront years",
        blurb="Shirtfront — nothing for anyone, a batting festival",
        character=(
            "Lifeless. No seam, no carry, no turn and no pace to work with; "
            "boundary hitting is the default scoring shot rather than the "
            "reward for one. Wrist spin is the last thing that still buys a "
            "wicket, and it buys few."),
        effectiveness=(1, 1, 5),
        grass=("No Grass", "Little", "No Grass"),
        toss="bowl", toss_dn="bowl",
        toss_reason="nothing changes — chase with the number in front of you",
        selectable=False,
    ),
)

_BY_NAME = {p.name: p for p in _PROFILES}

#: Every surface the engine models, bowler-friendly → batting-friendly.
PITCHES = tuple(p.name for p in _PROFILES)

#: The surfaces a host may pick for a match. ``Dead`` is fully configured but
#: not offered: a guaranteed 250 is a novelty, not a contest.
SELECTABLE = tuple(p.name for p in _PROFILES if p.selectable)

#: What an unknown or missing pitch name resolves to. ``Even`` is the neutral
#: surface, so a lost pitch type degrades to "no pitch effect" rather than to
#: whichever surface happened to be first in a dict.
DEFAULT = "Even"


def is_known(name) -> bool:
    """True if *name* is one of the engine's surfaces."""
    return str(name or "") in _BY_NAME


def normalise(name) -> str:
    """*name* if the engine knows it, else :data:`DEFAULT`.

    Every pitch lookup in the codebase should go through this rather than
    inventing its own fallback — that is how a Bouncy pitch came to be played
    as a road.
    """
    return name if is_known(name) else DEFAULT


def profile(name) -> PitchProfile:
    """The :class:`PitchProfile` for *name* (falling back to :data:`DEFAULT`)."""
    return _BY_NAME[normalise(name)]


def order(name) -> int:
    """Difficulty rank: 0 is the rankest bowling surface, 7 the deadest road."""
    return profile(name).order


def favours(name) -> str:
    """``"Pace"`` | ``"Spin"`` | ``"Balanced"`` | ``"Batting"``."""
    return profile(name).favours


def blurb(name) -> str:
    """One-line description, for pickers and cards."""
    return profile(name).blurb


def effectiveness(name) -> Tuple[int, int, int]:
    """Base ``(pacers, spinners, batters)`` ratings out of 5, before weather."""
    return profile(name).effectiveness


def grass_draw(name) -> Tuple[str, ...]:
    """Weighted grass-cover options to pick from for this surface."""
    return profile(name).grass


def toss_call(name, day_night=None) -> str:
    """``"bat"`` or ``"bowl"`` — what the toss-winner should do.

    Pass ``day_night="Night"`` for a match under lights: dew in the second
    innings makes bowling first correct on every surface, which is why the
    day-night column is not simply a copy of the day one.
    """
    p = profile(name)
    return p.toss_dn if str(day_night or "").lower().startswith("n") else p.toss


def toss_reason(name, day_night=None) -> str:
    """The one-line case for :func:`toss_call`."""
    if str(day_night or "").lower().startswith("n"):
        return "dew under lights — the ball skids on, chasing is easier"
    return profile(name).toss_reason


def par_band(name) -> Optional[Tuple[int, int]]:
    """``(par_low, par_high)`` from the ground-conditions config, or ``None``.

    Imported lazily: this module must stay importable with no config on disk.
    """
    try:
        from engine import ground_config
    except Exception:       # pragma: no cover - engine always ships ground_config
        return None
    dyn = ground_config.get_scoring_dynamics(normalise(name)) or {}
    lo, hi = dyn.get("par_low"), dyn.get("par_high")
    return (int(lo), int(hi)) if lo and hi else None


def par(name) -> Optional[float]:
    """The midpoint of :func:`par_band`, or ``None`` when unconfigured."""
    band = par_band(name)
    return (band[0] + band[1]) / 2.0 if band else None
