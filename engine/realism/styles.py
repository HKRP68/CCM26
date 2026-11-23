"""Style normalisation — read-only, card-preserving.

CCM26 player cards carry free-text styles ("Right arm medium fast\\n",
"left arm off break", "Right Arm leg Break"). The engine already normalises
these once, in :func:`services.cipl_match.approach_bowling_type`, into the
canonical set ``Fast`` / ``Fast-medium`` / ``Medium-fast`` / ``Off spin`` /
``Leg spin`` / ``Finger spin`` / ``Wrist spin``, and handedness into
``Right`` / ``Left``.

This module collapses that canonical set one step further, into the only two
distinctions real aggregate data actually supports at this sample size:

    pace  vs  spin                 (bowling family)
    same-handed vs opposite-handed (the ball turning/angling across or in)

Nothing here reads, writes or derives a player attribute. It is a pure
function of the two strings already on the card, and the card is never
touched.

Why so coarse: Cricsheet records the bowler and the batter, not the ball's
line, length, seam position or turn. Published aggregates support a pace/spin
split and a broad handedness-matchup effect. They do not support "left-arm
orthodox to a right-hander on a dry pitch in the 14th over" as an *empirical*
figure, so this module deliberately refuses to invent that resolution — see
``docs/realism_engine_v1.md``.
"""

PACE = "pace"
SPIN = "spin"

# Canonical engine bowling_type values (engine/ball_outcome.py) plus the raw
# card spellings, so this works whether it is handed an adapted dict or a
# straight card.
_SPIN_TOKENS = ("spin", "break", "googly", "orthodox", "chinaman", "slow")
_PACE_TOKENS = ("fast", "medium", "seam", "pace", "swing")


def bowling_family(bowling_type) -> str:
    """``"pace"`` or ``"spin"`` for any CCM26 bowling style string.

    Unknown or empty styles fall back to ``"pace"``, which is the majority
    class on the roster and the same default
    :func:`services.cipl_match.approach_bowling_type` uses.
    """
    s = str(bowling_type or "").strip().lower()
    if not s:
        return PACE
    # Pace tokens win: "medium fast" contains no spin token, but "slow medium"
    # does contain both and is a pace bowler.
    if any(t in s for t in _PACE_TOKENS):
        return PACE
    if any(t in s for t in _SPIN_TOKENS):
        return SPIN
    return PACE


def hand(value) -> str:
    """``"Left"`` or ``"Right"`` from a handedness or style string."""
    s = str(value or "").strip().lower()
    if s.startswith("left") or " left" in s or s == "l":
        return "Left"
    return "Right"


def matchup_key(batting_hand, bowling_hand, bowling_type) -> str:
    """The style-matchup bucket for this delivery.

    Four buckets, each a genuine cricket distinction that aggregate data can
    speak to at least weakly:

        ``pace_same``   right-arm pace to a right-hander (and the mirror)
        ``pace_cross``  left-arm pace to a right-hander (and the mirror)
        ``spin_into``   the ball turning *into* the bat
        ``spin_away``   the ball turning *away* from the bat

    ``spin_away`` is the classic advantage (off-spin to a left-hander,
    leg-spin to a right-hander); ``spin_into`` is the easier match-up.
    """
    fam = bowling_family(bowling_type)
    bh = hand(batting_hand)
    if fam == PACE:
        return "pace_same" if hand(bowling_hand) == bh else "pace_cross"

    s = str(bowling_type or "").strip().lower()
    # Off-break / finger spin turns away from a left-hander, into a
    # right-hander. Leg-break / wrist spin is the reverse.
    finger = ("off" in s) or ("finger" in s) or ("orthodox" in s)
    wrist = ("leg" in s) or ("wrist" in s) or ("googly" in s) or ("chinaman" in s)
    if finger and not wrist:
        return "spin_away" if bh == "Left" else "spin_into"
    if wrist:
        return "spin_away" if bh == "Right" else "spin_into"
    return "spin_into"
