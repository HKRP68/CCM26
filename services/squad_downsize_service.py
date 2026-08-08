"""Bringing over-cap squads back under MAX_ROSTER and TRAIT_MAX_PER_SQUAD.

When the squad cap was cut (25 → 19) and the squad-wide trait budget (18) was
introduced, every existing account was suddenly over one or both. Enforcement
only guards *new* acquisitions, so nothing shrinks a squad that was already too
big — such a captain is simply stuck, unable to buy or claim, with no
indication why.

This module is the single implementation of the fix. Both entry points use it:

  • ``migrate_squad_and_trait_limits.py`` — the CLI, for a deploy-time sweep.
  • The **Apply squad limits** button on the admin Maintenance page.

What it does to one account, in this order:

  1. Release the lowest-rated cards until the roster is at MAX_ROSTER, refunded
     at FULL BUY PRICE. A forced release is not a sale, so the captain does not
     eat the buy/sell spread on a card they never chose to part with.
  2. Refund the lowest-value equipped traits until the non-career squad is at
     TRAIT_MAX_PER_SQUAD, at full gem buy value.

The order matters: releasing a card un-equips its traits back into inventory,
which usually brings the squad under the trait cap on its own. Running the
trait pass first would destroy traits the roster pass was about to hand back
intact.

The Career Player is never released and its own traits are never touched — it
sits outside the squad trait budget by design.
"""

import logging

from sqlalchemy import func, or_

from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
from models import Player, PlayerTrait, User, UserRoster
from services.roster_service import get_roster_count, trim_roster_to_cap
from services.trait_service import count_squad_traits, trim_squad_traits_to_cap

logger = logging.getLogger(__name__)


def _empty_totals():
    return {"users": 0, "cards": 0, "coins": 0, "traits": 0, "gems": 0,
            "failed": 0}


def find_over_cap_user_ids(session, roster_cap=MAX_ROSTER,
                           trait_cap=TRAIT_MAX_PER_SQUAD):
    """User ids over the roster cap, the squad trait cap, or both.

    Two GROUP BY queries rather than a per-user loop: on a large user table the
    over-cap accounts are a small minority, and walking every row to ask each
    one costs a query apiece.
    """
    over_roster = (session.query(UserRoster.user_id)
                   .group_by(UserRoster.user_id)
                   .having(func.count(UserRoster.id) > roster_cap))

    # Same non-career rule as trait_service._squad_traits_query — the Career
    # Player's traits are outside the budget, so they must not push a user onto
    # this list. NULL is_career predates the career feature and counts as "not".
    over_traits = (session.query(PlayerTrait.user_id)
                   .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                   .join(Player, UserRoster.player_id == Player.id)
                   .filter(or_(Player.is_career.is_(False),
                               Player.is_career.is_(None)))
                   .group_by(PlayerTrait.user_id)
                   .having(func.count(PlayerTrait.id) > trait_cap))

    ids = {row[0] for row in over_roster.all()}
    ids |= {row[0] for row in over_traits.all()}
    return sorted(ids)


def is_over_cap(session, user):
    """True if this one account is over either cap."""
    return (get_roster_count(session, user.id) > MAX_ROSTER
            or count_squad_traits(session, user.id) > TRAIT_MAX_PER_SQUAD)


def downsize_user(session, user):
    """Bring one account under both caps. Does NOT commit — the caller decides.

    Returning without committing is what lets :func:`preview_downsize` run the
    real thing and roll it back, so the preview reports exactly what an apply
    would do rather than an estimate.
    """
    summary = {"cards": 0, "coins": 0, "traits": 0, "gems": 0}
    if not is_over_cap(session, user):
        return summary

    roster_result = trim_roster_to_cap(session, user)
    summary["cards"] = roster_result["count"]
    summary["coins"] = roster_result["coins"]

    trait_result = trim_squad_traits_to_cap(session, user)
    summary["traits"] = trait_result["count"]
    summary["gems"] = trait_result["gems"]
    return summary


def _walk(session, user_ids, on_user_done=None):
    """Run downsize_user over ``user_ids``, accumulating totals.

    Never commits or rolls back — that is the caller's call, which is the only
    difference between a preview and an apply.
    """
    totals = _empty_totals()
    for user_id in user_ids:
        user = session.query(User).get(user_id)
        if not user:
            continue
        result = downsize_user(session, user)
        if not (result["cards"] or result["traits"]):
            continue
        totals["users"] += 1
        for key in ("cards", "coins", "traits", "gems"):
            totals[key] += result[key]
        if on_user_done:
            on_user_done(user, result)
    return totals


def preview_downsize(session):
    """Exactly what an apply would do, without keeping any of it.

    Runs the real downsizing and rolls it back, so the numbers are the true
    outcome rather than an estimate. This matters because the two passes
    interact: releasing cards frees their traits into inventory, so counting
    over-cap traits up front would overstate how many get refunded.
    """
    try:
        totals = _walk(session, find_over_cap_user_ids(session))
    finally:
        # Roll back unconditionally. A preview that leaked a partial write
        # would be far worse than one that failed.
        session.rollback()
    return totals


def run_downsize(session, on_user_done=None, log=None):
    """Apply the downsizing for real, committing per user.

    Per-user commits inside per-user try/except: one bad row skips that account
    instead of aborting the sweep, and the accounts already fixed stay fixed.
    Re-running is a no-op, so a skipped user can be retried by running again.

    NOTE: this expunges the session between users to bound memory on a large
    user table, which detaches **every** object in it — including any the
    caller loaded beforehand. Re-query anything you still need afterwards.
    Both callers pass a session they own for the duration, so this costs them
    nothing.
    """
    totals = _empty_totals()
    for user_id in find_over_cap_user_ids(session):
        try:
            user = session.query(User).get(user_id)
            if not user:
                continue
            result = downsize_user(session, user)
            if not (result["cards"] or result["traits"]):
                session.rollback()
                continue

            session.commit()
            totals["users"] += 1
            for key in ("cards", "coins", "traits", "gems"):
                totals[key] += result[key]
            if on_user_done:
                on_user_done(user, result)
            if log:
                log.info("  user %s (tg %s): -%s cards (+%s coins), "
                         "-%s traits (+%s gems)",
                         user.id, getattr(user, "telegram_id", "?"),
                         result["cards"], f"{result['coins']:,}",
                         result["traits"], f"{result['gems']:,}")
        except Exception:
            session.rollback()
            totals["failed"] += 1
            logger.exception("squad downsize failed for user %s — skipped",
                             user_id)
            if log:
                log.exception("  user %s FAILED — skipped", user_id)
        finally:
            # commit() expires objects but leaves them in the identity map.
            # Without this the one long-lived Session accumulates every User,
            # UserRoster, Player and PlayerTrait touched by the whole run.
            session.expunge_all()
    return totals
