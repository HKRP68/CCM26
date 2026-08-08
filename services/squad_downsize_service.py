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


def find_over_cap_user_ids(session):
    """User ids over the roster cap, the squad trait cap, or both.

    Two GROUP BY queries rather than a per-user loop: on a large user table the
    over-cap accounts are a small minority, and walking every row to ask each
    one costs a query apiece.

    The caps are read from config rather than taken as arguments on purpose.
    The trimming underneath (``trim_roster_to_cap``, ``trim_squad_traits_to_cap``)
    reads them from config too, so a caller passing custom caps here would get a
    candidate list that disagrees with what actually happens to those accounts.
    """
    over_roster = (session.query(UserRoster.user_id)
                   .group_by(UserRoster.user_id)
                   .having(func.count(UserRoster.id) > MAX_ROSTER))

    # Same non-career rule as trait_service._squad_traits_query — the Career
    # Player's traits are outside the budget, so they must not push a user onto
    # this list. NULL is_career predates the career feature and counts as "not".
    over_traits = (session.query(PlayerTrait.user_id)
                   .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                   .join(Player, UserRoster.player_id == Player.id)
                   .filter(or_(Player.is_career.is_(False),
                               Player.is_career.is_(None)))
                   .group_by(PlayerTrait.user_id)
                   .having(func.count(PlayerTrait.id) > TRAIT_MAX_PER_SQUAD))

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


def _preview_walk(session, user_ids, on_user_done=None):
    """Downsize each account, note the numbers, then undo it.

    One account per transaction, rolled back immediately. Two things follow,
    and both matter:

    * **A bad account can't abort the run.** A mid-flush error leaves the
      session needing a rollback, which is exactly what happens next anyway, so
      the walk records the failure and moves on — matching ``run_downsize``,
      which isolates per user with its own commit. Without this a dry run could
      die on an account the real run would simply skip.
    * **No long-held locks.** Doing every account in one transaction and
      rolling back at the end would hold write locks on ``user_roster``,
      ``player_traits`` and ``users`` for the whole scan, blocking live
      gameplay writes on a large database.

    SAVEPOINTs would be the textbook tool here, but pysqlite's transaction
    handling makes them unreliable — verified: a released savepoint survived
    the outer rollback — and the test suite runs on SQLite. A plain rollback
    per account behaves identically on both backends.
    """
    totals = _empty_totals()
    for user_id in user_ids:
        try:
            user = session.get(User, user_id)
            result = downsize_user(session, user) if user else None
        except Exception:
            totals["failed"] += 1
            logger.exception("squad downsize preview failed for user %s",
                             user_id)
            result = None
        finally:
            # Unconditional: a preview that leaked a partial write would be far
            # worse than one that failed.
            session.rollback()
            session.expunge_all()

        if not result or not (result["cards"] or result["traits"]):
            continue
        totals["users"] += 1
        for key in ("cards", "coins", "traits", "gems"):
            totals[key] += result[key]
        if on_user_done:
            on_user_done(user_id, result)
    return totals


def preview_downsize(session):
    """Exactly what an apply would do, without keeping any of it.

    Runs the real downsizing and rolls it back per account, so the numbers are
    the true outcome rather than an estimate. This matters because the two
    passes interact: releasing cards frees their traits into inventory, so
    counting over-cap traits up front would overstate how many get refunded.

    ``failed`` is populated the same way ``run_downsize`` populates it, so a
    dry run reports the accounts a real run would skip instead of dying on the
    first one.

    NOTE: like ``run_downsize``, this expunges the session as it goes, which
    detaches **every** object in it — including any the caller loaded
    beforehand. Re-query anything you still need afterwards.
    """
    try:
        return _preview_walk(session, find_over_cap_user_ids(session))
    finally:
        # Belt and braces: _preview_walk already rolls back after every
        # account, but a preview must never leave a write behind.
        session.rollback()


def run_downsize(session, on_user_done=None, log=None):
    """Apply the downsizing for real, committing per user.

    Per-user commits inside per-user try/except: one bad row skips that account
    instead of aborting the sweep, and the accounts already fixed stay fixed.
    Re-running is a no-op, so a skipped user can be retried by running again.

    ``on_user_done(user_id, result)`` is called after each account commits. It
    is handed the id rather than the User instance on purpose — see the expunge
    note below — and its failures are logged, never counted as a failed user.

    NOTE: this expunges the session between users to bound memory on a large
    user table, which detaches **every** object in it — including any the
    caller loaded beforehand. Re-query anything you still need afterwards.
    Both callers pass a session they own for the duration, so this costs them
    nothing.
    """
    totals = _empty_totals()
    for user_id in find_over_cap_user_ids(session):
        result = None
        try:
            user = session.get(User, user_id)
            if not user:
                continue
            result = downsize_user(session, user)
            if not (result["cards"] or result["traits"]):
                session.rollback()
                result = None
                continue
            # Read anything reporting needs BEFORE the commit: commit expires
            # the instance, so touching it afterwards is a fresh query at best
            # and a DetachedInstanceError after the expunge below.
            tg_id = getattr(user, "telegram_id", "?")
            session.commit()
        except Exception:
            session.rollback()
            result = None
            totals["failed"] += 1
            logger.exception("squad downsize failed for user %s — skipped",
                             user_id)
            if log:
                log.exception("  user %s FAILED — skipped", user_id)
            continue
        finally:
            # commit() expires objects but leaves them in the identity map.
            # Without this the one long-lived Session accumulates every User,
            # UserRoster, Player and PlayerTrait touched by the whole run.
            session.expunge_all()

        # Past here the work is COMMITTED. Nothing below may mark it failed or
        # roll anything back — this account is done, and reporting is only
        # reporting. Counting a committed user as "skipped" would tell the
        # admin to retry work that already happened.
        totals["users"] += 1
        for key in ("cards", "coins", "traits", "gems"):
            totals[key] += result[key]
        try:
            if on_user_done:
                on_user_done(user_id, result)
            if log:
                log.info("  user %s (tg %s): -%s cards (+%s coins), "
                         "-%s traits (+%s gems)",
                         user_id, tg_id,
                         result["cards"], f"{result['coins']:,}",
                         result["traits"], f"{result['gems']:,}")
        except Exception:
            logger.exception("post-commit reporting failed for user %s "
                             "(the downsizing itself succeeded)", user_id)
    return totals
