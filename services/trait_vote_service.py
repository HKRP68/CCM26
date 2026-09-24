"""Trait Vote — the two captains decide whether traits play in this match.

``services.trait_engine`` moves the per-ball probabilities and
``services.trait_rating_service`` puts the ⚡ Trait Boost on the card. Both of
them answer "what do these traits do?". This module answers the question that
comes first, and only in a head-to-head between two human squads: **are traits
in this match at all?**

THE RULE
────────
Both captains answer Yes or No before the toss, and the two answers decide::

    Yes + Yes → traits ON      (what every match did before this existed)
    No  + No  → traits OFF
    split     → ``config.TRAIT_VOTE_SPLIT_RULE``, "underdog" by default:
                the weaker XI's answer stands

The split rule is the whole design. A coin flip would settle it too, but a
captain who loses a coin flip has been told nothing — whereas "the weaker side
decides" is a rule the stronger captain can read off the card in front of them
before they vote, and it points the game where it should point: the side with
less to gain from traits is the side that says whether they are used. Strength
is measured WITH the Trait Boost (``team_effective_overall``), because that boost
is precisely the advantage being argued about; two exactly equal XIs have no
underdog to defer to, so they fall back to ``config.TRAIT_VOTE_DEFAULT``.

Votes are collected in secret and revealed together. Shown as they arrive, the
second captain does not vote on the match — they vote on the answer already on
the screen.

WHAT "OFF" MEANS
────────────────
Everything a trait is worth, for both sides:

* the ball engine sees no traits (no activations, no commentary lines);
* the ⚡ Trait Boost is not added to any card, so Team Overall is the printed
  rating;
* nothing else changes — no equipping, selling, levelling or ownership is
  touched, and the traits are back the next match.

It is deliberately symmetric. Turning the engine off while leaving the boost on
the card would hand the trait-heavy squad a free rating lead in a match that was
supposed to be without traits.

WHAT THIS MODULE IS NOT
───────────────────────
It holds no state and talks to no database. The flows own their own lobbies
(``handlers.match`` for /wpm, ``handlers.letsplay`` for /letsplay) and call in
here to normalise a tap, resolve two answers, and render the result — so both
flows resolve a split the same way, and the rule can be tested without a
Telegram update or a session.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

import config

logger = logging.getLogger(__name__)

#: The two answers, as they travel in callback data.
VOTE_YES = "yes"
VOTE_NO = "no"

#: Split-rule keys accepted by ``config.TRAIT_VOTE_SPLIT_RULE``.
SPLIT_UNDERDOG = "underdog"
SPLIT_ON = "traits_on"
SPLIT_OFF = "traits_off"
SPLIT_RANDOM = "random"

#: Emoji the vote is labelled with everywhere it is shown.
VOTE_EMOJI = "⚡"


class VoteOutcome(NamedTuple):
    """How a finished vote was settled.

    ``enabled``   do traits play in this match?
    ``outcome``   "both_yes" | "both_no" | "split"
    ``decided_by``for a split: "host", "guest" (whose answer stood), or the
                  split-rule key when no side's answer decided it ("traits_on",
                  "traits_off", "random", "default" for an exact rating tie).
                  ``None`` when both captains agreed.
    """

    enabled: bool
    outcome: str
    decided_by: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# Settings, read defensively
# ──────────────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Is the vote offered at all? False makes every match play with traits."""
    return bool(getattr(config, "TRAIT_VOTE_ENABLED", True))


def vote_timeout() -> int:
    """Seconds a captain has to answer before the vote settles without them."""
    try:
        return max(5, int(getattr(config, "TRAIT_VOTE_TIMEOUT", 45)))
    except (TypeError, ValueError):
        return 45


def default_vote() -> str:
    """The vote a captain who never answered is counted as."""
    return normalize_vote(getattr(config, "TRAIT_VOTE_DEFAULT", VOTE_YES)) or VOTE_YES


def split_rule() -> str:
    """How a one-Yes-one-No vote is settled. Unknown keys read as "underdog"."""
    rule = str(getattr(config, "TRAIT_VOTE_SPLIT_RULE", SPLIT_UNDERDOG) or "").strip().lower()
    return rule if rule in (SPLIT_UNDERDOG, SPLIT_ON, SPLIT_OFF, SPLIT_RANDOM) else SPLIT_UNDERDOG


def normalize_vote(value: Any) -> Optional[str]:
    """A tap, a stored answer or a config string → ``"yes"``/``"no"``/``None``.

    Anything unrecognised reads as "not voted" rather than raising: this sits on
    the callback path of a lobby two people are waiting in.
    """
    if value is True:
        return VOTE_YES
    if value is False:
        return VOTE_NO
    text = str(value or "").strip().lower()
    if text in (VOTE_YES, "y", "1", "true", "on"):
        return VOTE_YES
    if text in (VOTE_NO, "n", "0", "false", "off"):
        return VOTE_NO
    return None


# ──────────────────────────────────────────────────────────────────────
# XI helpers
# ──────────────────────────────────────────────────────────────────────

def has_any_traits(xi: Optional[Sequence[Dict[str, Any]]]) -> bool:
    """True when at least one card in this XI is carrying a trait.

    A vote between two untraited squads decides nothing, so the flows skip the
    prompt and go straight to the toss — the shortest match setup is the one
    that does not ask a question with only one possible answer.
    """
    for player in xi or ():
        if isinstance(player, dict) and player.get("traits"):
            return True
    return False


def team_strength(xi: Optional[Sequence[Dict[str, Any]]]) -> float:
    """Team Overall WITH the Trait Boost — what "weaker XI" means here.

    Falls back to the printed average if the boost cannot be computed, because a
    match setup must never fail on a rating display.
    """
    try:
        from services.trait_rating_service import team_effective_overall
        return float(team_effective_overall(list(xi or [])))
    except Exception:
        logger.exception("trait vote: effective overall failed; using base")
        ratings = [float(p.get("rating") or 0) for p in (xi or ())
                   if isinstance(p, dict)]
        return round(sum(ratings) / len(ratings), 1) if ratings else 0.0


def strip_traits(xi: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Copies of these XI dicts with their traits removed.

    Used by the flows that carry traits inline on the engine dicts (/letsplay).
    The copies are shallow but the ``traits`` key is replaced rather than
    mutated, so the caller's own list — the one the XI card was rendered from —
    is left alone.
    """
    out: List[Dict[str, Any]] = []
    for player in xi or ():
        if not isinstance(player, dict):
            out.append(player)
            continue
        clean = dict(player)
        clean["traits"] = []
        # The per-card boost annotations (services.trait_rating_service.
        # annotate_xi) would otherwise keep quoting a boost this match is not
        # applying.
        clean.pop("trait_bonus", None)
        clean.pop("effective_rating", None)
        out.append(clean)
    return out


def traits_enabled(state: Optional[Dict[str, Any]]) -> bool:
    """Do traits play in this match state?

    Absent means yes: every match state written before the vote existed, and
    every mode that never asks, plays with traits.
    """
    if not isinstance(state, dict):
        return True
    return state.get("traits_enabled") is not False


# ──────────────────────────────────────────────────────────────────────
# The rule
# ──────────────────────────────────────────────────────────────────────

def resolve(host_vote: Any, guest_vote: Any,
            host_strength: Optional[float] = None,
            guest_strength: Optional[float] = None,
            rng: Optional[random.Random] = None) -> VoteOutcome:
    """Settle a vote. Missing answers count as ``default_vote()``."""
    host = normalize_vote(host_vote) or default_vote()
    guest = normalize_vote(guest_vote) or default_vote()

    if host == guest:
        return VoteOutcome(enabled=(host == VOTE_YES),
                           outcome="both_yes" if host == VOTE_YES else "both_no")

    rule = split_rule()
    if rule == SPLIT_ON:
        return VoteOutcome(True, "split", SPLIT_ON)
    if rule == SPLIT_OFF:
        return VoteOutcome(False, "split", SPLIT_OFF)
    if rule == SPLIT_RANDOM:
        picker = rng or random
        return VoteOutcome(picker.random() < 0.5, "split", SPLIT_RANDOM)

    # "underdog" — the weaker XI's answer stands.
    try:
        host_val = float(host_strength if host_strength is not None else 0.0)
        guest_val = float(guest_strength if guest_strength is not None else 0.0)
    except (TypeError, ValueError):
        host_val = guest_val = 0.0
    if host_val < guest_val:
        return VoteOutcome(host == VOTE_YES, "split", "host")
    if guest_val < host_val:
        return VoteOutcome(guest == VOTE_YES, "split", "guest")
    # Dead level: no underdog to defer to, so the default answers it.
    return VoteOutcome(default_vote() == VOTE_YES, "split", "default")


# ──────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────

def _fmt(value: float) -> str:
    return f"{float(value or 0):.1f}".rstrip("0").rstrip(".")


def prompt_text(host_label: str, guest_label: str,
                host_strength: float, guest_strength: float,
                host_voted: bool = False, guest_voted: bool = False,
                timeout: Optional[int] = None) -> str:
    """The question both captains answer, with the live "who has voted" line.

    The *answers* stay hidden until both are in — only the fact that a captain
    has answered is shown — so the second vote is cast on the match rather than
    on the first captain's answer.
    """
    seconds = vote_timeout() if timeout is None else timeout
    split = {
        SPLIT_UNDERDOG: "the lower-rated XI's answer stands",
        SPLIT_ON: "traits are ON",
        SPLIT_OFF: "traits are OFF",
        SPLIT_RANDOM: "it is decided by a coin flip",
    }[split_rule()]
    default_label = "YES" if default_vote() == VOTE_YES else "NO"
    return (
        f"{VOTE_EMOJI} <b>TRAITS — YES OR NO?</b> {VOTE_EMOJI}\n"
        "═════════════════════════════\n"
        f"• {host_label} — <b>{_fmt(host_strength)}</b> ovr\n"
        f"• {guest_label} — <b>{_fmt(guest_strength)}</b> ovr\n"
        "<i>(Team Overall shown with the ⚡ Trait Boost included)</i>\n\n"
        "<b>Both captains choose.</b> Answers stay hidden until both are in.\n"
        "• Yes + Yes → traits are <b>ON</b>\n"
        "• No + No → traits are <b>OFF</b>\n"
        f"• One of each → {split}\n\n"
        f"🗳 {host_label}: {'✅ locked in' if host_voted else '⏳ waiting'}\n"
        f"🗳 {guest_label}: {'✅ locked in' if guest_voted else '⏳ waiting'}\n"
        f"<i>No answer within {seconds}s counts as {default_label}.</i>"
    )


def result_text(result: VoteOutcome, host_label: str, guest_label: str,
                host_vote: Any = None, guest_vote: Any = None) -> str:
    """One block saying what was decided and why — votes now revealed."""
    host = normalize_vote(host_vote)
    guest = normalize_vote(guest_vote)

    def _answer(vote: Optional[str]) -> str:
        if vote == VOTE_YES:
            return "Yes"
        if vote == VOTE_NO:
            return "No"
        return f"no answer ({'Yes' if default_vote() == VOTE_YES else 'No'})"

    headline = (f"{VOTE_EMOJI} <b>TRAITS: ON</b>" if result.enabled
                else "🚫 <b>TRAITS: OFF</b>")
    if result.outcome == "both_yes":
        why = "Both captains said yes."
    elif result.outcome == "both_no":
        why = "Both captains said no — this one is cards only."
    elif result.decided_by == "host":
        why = f"Split vote — {host_label} field the lower-rated XI, so their call stands."
    elif result.decided_by == "guest":
        why = f"Split vote — {guest_label} field the lower-rated XI, so their call stands."
    elif result.decided_by == SPLIT_RANDOM:
        why = "Split vote — settled by a coin flip."
    elif result.decided_by == "default":
        why = "Split vote — the two XIs are rated dead level, so the default stands."
    else:
        why = "Split vote — settled by the house rule."

    detail = "" if result.outcome in ("both_yes", "both_no") else (
        f"\n<i>{host_label}: {_answer(host)} · {guest_label}: {_answer(guest)}</i>")
    tail = ("" if result.enabled else
            "\n<i>No trait effects and no ⚡ Trait Boost for either side. "
            "Nothing is unequipped — traits are back next match.</i>")
    return f"{headline} — {why}{detail}{tail}"


def status_line(enabled: bool) -> str:
    """One-line badge for a match card, so nobody has to remember the vote."""
    return (f"{VOTE_EMOJI} <b>Traits:</b> ON" if enabled
            else "🚫 <b>Traits:</b> OFF (both XIs play on card ratings)")


# ══════════════════════════════════════════════════════════════════════
# The same two cards as rich blocks
# ══════════════════════════════════════════════════════════════════════
#
# The vote is the one moment in a Lets Play setup where both captains are
# reading the same card and weighing it against each other's XI — two team
# ratings, three outcome rules and two vote states, all of which the HTML has
# to draw as a column of bullets and hope lines up. They are tables.
#
# Both renderings stay live (``docs/rich-text-messages.md``), so these mirror
# the text above exactly: same ratings, same rules, same "who has answered",
# same default. A builder that raises answers None and the HTML goes out — two
# captains are waiting on this to tap something.


def prompt_blocks(host_label: str, guest_label: str,
                  host_strength: float, guest_strength: float,
                  host_voted: bool = False, guest_voted: bool = False,
                  timeout: Optional[int] = None):
    """The traits question as blocks — the twin of :func:`prompt_text`."""
    try:
        from services import rich_message as R
        seconds = vote_timeout() if timeout is None else timeout
        split = {
            SPLIT_UNDERDOG: "the lower-rated XI's answer stands",
            SPLIT_ON: "traits are ON",
            SPLIT_OFF: "traits are OFF",
            SPLIT_RANDOM: "it is decided by a coin flip",
        }[split_rule()]
        default_label = "YES" if default_vote() == VOTE_YES else "NO"

        def tick(voted):
            return (["✅ ", R.bold("locked in")] if voted
                    else ["⏳ ", R.italic("waiting")])

        return [
            R.heading(f"{VOTE_EMOJI} Traits — yes or no?", size=2),
            R.table([
                [R.cell(R.bold(host_label)),
                 R.cell(R.bold(_fmt(host_strength)), align="right"),
                 R.cell("ovr"), R.cell(tick(host_voted), align="right")],
                [R.cell(R.bold(guest_label)),
                 R.cell(R.bold(_fmt(guest_strength)), align="right"),
                 R.cell("ovr"), R.cell(tick(guest_voted), align="right")],
            ], bordered=True, compact=True),
            R.paragraph(R.italic(
                "Team Overall shown with the ⚡ Trait Boost included.")),
            R.paragraph([R.bold("Both captains choose."),
                         " Answers stay hidden until both are in."]),
            R.table([
                [R.cell(R.bold("Yes + Yes")), R.cell(["traits are ", R.bold("ON")])],
                [R.cell(R.bold("No + No")), R.cell(["traits are ", R.bold("OFF")])],
                [R.cell(R.bold("One of each")), R.cell(split)],
            ], bordered=True, striped=True, compact=True),
            R.footer(R.italic(
                f"No answer within {seconds}s counts as {default_label}.")),
        ]
    except Exception:
        logger.exception("trait vote prompt blocks failed to build")
        return None


def result_blocks(result: VoteOutcome, host_label: str, guest_label: str,
                  host_vote: Any = None, guest_vote: Any = None):
    """The decision as blocks — the twin of :func:`result_text`.

    The verdict is a ``pullquote`` because it is the one thing on the card that
    changes how the match is played, and the reasoning belongs under it rather
    than wrapped around it.
    """
    try:
        from services import rich_message as R
        host = normalize_vote(host_vote)
        guest = normalize_vote(guest_vote)

        def answer(vote):
            if vote == VOTE_YES:
                return "Yes"
            if vote == VOTE_NO:
                return "No"
            return f"no answer ({'Yes' if default_vote() == VOTE_YES else 'No'})"

        if result.outcome == "both_yes":
            why = "Both captains said yes."
        elif result.outcome == "both_no":
            why = "Both captains said no — this one is cards only."
        elif result.decided_by == "host":
            why = (f"Split vote — {host_label} field the lower-rated XI, so "
                   f"their call stands.")
        elif result.decided_by == "guest":
            why = (f"Split vote — {guest_label} field the lower-rated XI, so "
                   f"their call stands.")
        elif result.decided_by == SPLIT_RANDOM:
            why = "Split vote — settled by a coin flip."
        elif result.decided_by == "default":
            why = ("Split vote — the two XIs are rated dead level, so the "
                   "default stands.")
        else:
            why = "Split vote — settled by the house rule."

        blocks = [R.pullquote(
            R.bold(f"{VOTE_EMOJI} TRAITS: ON" if result.enabled
                   else "🚫 TRAITS: OFF"), caption=why)]
        if result.outcome not in ("both_yes", "both_no"):
            blocks.append(R.table([
                [R.cell(R.bold(host_label)), R.cell(answer(host), align="right")],
                [R.cell(R.bold(guest_label)), R.cell(answer(guest), align="right")],
            ], bordered=True, compact=True))
        if not result.enabled:
            blocks.append(R.footer(R.italic(
                "No trait effects and no ⚡ Trait Boost for either side. "
                "Nothing is unequipped — traits are back next match.")))
        return blocks
    except Exception:
        logger.exception("trait vote result blocks failed to build")
        return None
