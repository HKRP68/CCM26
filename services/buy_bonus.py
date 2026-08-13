"""The Elite Signing Bonus — a limited-time offer on high-rated signings.

While the offer is running, buying a card rated at or above its threshold pays
a slice of the coins spent back as gems: at the default 96+ and 0.1%, a 97 OVR
at 4,450,000 🪙 hands back 4,450 💎.

It is an *offer*, not a permanent rule. An admin opens and closes it on the
economy page, sets the rating it starts at and the rate it pays, and can give
it a window that opens and closes it on its own. Everything about "is it live
right now, and on what terms" is answered here, by :func:`current_offer`, so
the bot, the Mini App, the receipts and the tutorial can never disagree about
whether the offer is on.

Every buy path routes through :func:`award_buy_gem_bonus` (bot ``/buypl``, the
bot player market, and both Mini App buy endpoints) so the rebate, its audit
row and its wording stay identical between them.
"""

import logging
from datetime import datetime

from config import (
    GEM_BONUS_BPS, GEM_BONUS_MIN_RATING, get_buy_gem_bonus,
)

logger = logging.getLogger(__name__)


class Offer:
    """The Elite Signing Bonus as it stands right now.

    ``active`` already accounts for the admin's switch *and* the window, so
    callers never re-derive it. When it is False the other fields still carry
    the configured terms, which is what the admin page needs in order to show
    a closed or not-yet-started offer.
    """

    __slots__ = ("active", "min_rating", "bps", "starts_at", "ends_at", "now")

    def __init__(self, *, active, min_rating, bps, starts_at=None,
                 ends_at=None, now=None):
        self.active = bool(active)
        self.min_rating = int(min_rating)
        self.bps = int(bps)
        self.starts_at = starts_at
        self.ends_at = ends_at
        self.now = now or datetime.utcnow()

    @property
    def percent(self):
        """The rate as a percentage, for display (10 bps → 0.1)."""
        return self.bps / 100

    @property
    def seconds_left(self):
        """Seconds until the offer auto-closes, or None if it has no end."""
        if not self.ends_at:
            return None
        return max(0, int((self.ends_at - self.now).total_seconds()))

    @property
    def scheduled(self):
        """True when the offer is configured and waiting for its start time."""
        return (not self.active and self.starts_at is not None
                and self.starts_at > self.now)

    def gems_for(self, rating, price_paid=None):
        """Gems this offer pays for that buy — 0 while it isn't running."""
        if not self.active:
            return 0
        return get_buy_gem_bonus(rating, price_paid,
                                 min_rating=self.min_rating, bps=self.bps)


def _fallback_offer(now=None):
    """What applies when the config row can't be read: the baked-in rate."""
    return Offer(active=True, min_rating=GEM_BONUS_MIN_RATING,
                 bps=GEM_BONUS_BPS, now=now)


def current_offer(session=None, now=None):
    """Read the Elite Signing Bonus as it stands right now.

    Read fresh rather than from ``config_service``'s process-local cache: the
    admin website and the bot commonly run as separate processes, and opening
    or closing an offer has to take effect on the next buy without a restart.
    Pass the session you already hold and it costs one single-row query.

    Fails open to the baked-in rate — a database hiccup shouldn't silently stop
    paying a bonus players have been promised.
    """
    now = now or datetime.utcnow()
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True
    try:
        from models import GameConfig
        row = session.query(GameConfig).first()
        if row is None:
            return _fallback_offer(now)

        min_rating = row.gem_bonus_min_rating
        bps = row.gem_bonus_bps
        starts_at = row.gem_bonus_starts_at
        ends_at = row.gem_bonus_ends_at

        active = bool(row.gem_bonus_enabled)
        if active and starts_at is not None and now < starts_at:
            active = False          # scheduled, not open yet
        if active and ends_at is not None and now >= ends_at:
            active = False          # the offer has run out

        return Offer(
            active=active,
            min_rating=(GEM_BONUS_MIN_RATING if min_rating is None
                        else min_rating),
            bps=GEM_BONUS_BPS if bps is None else bps,
            starts_at=starts_at, ends_at=ends_at, now=now,
        )
    except Exception:
        logger.exception("gem bonus offer read failed; using the baked rate")
        return _fallback_offer(now)
    finally:
        if own:
            session.close()


def gem_bonus_for(rating, price_paid=None, session=None):
    """Gems the live offer would pay for this buy (0 when it isn't running)."""
    return current_offer(session).gems_for(rating, price_paid)


def award_buy_gem_bonus(session, user, player, price_paid, *, source="buy",
                        offer=None):
    """Credit the buyer's gem rebate for this purchase; return the gems paid.

    Returns 0 (and touches nothing) when the offer is closed or the card isn't
    elite enough. Call it before ``session.commit()`` — the credit and its
    ActivityLog row ride on the caller's transaction, so a rolled-back buy
    never leaves gems behind. Pass ``offer`` if you already read one.
    """
    offer = offer or current_offer(session)
    gems = offer.gems_for(getattr(player, "rating", 0), price_paid)
    if gems <= 0:
        return 0

    user.total_gems = (user.total_gems or 0) + gems
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "buy_gem_bonus",
                     f"Elite signing bonus for {player.name} "
                     f"({player.rating} OVR) — +{gems:,} gems [{source}]",
                     gems_change=gems,
                     player_name=player.name, player_rating=player.rating)
    except Exception:
        logger.exception("buy gem bonus activity log failed (non-fatal)")
    return gems


# ── Wording ─────────────────────────────────────────────────────────

def format_time_left(seconds):
    """A short, human countdown: "2d 4h", "3h 20m", "45m", "under a minute"."""
    if seconds is None:
        return ""
    if seconds < 60:
        return "under a minute"
    minutes, hours = (seconds // 60) % 60, seconds // 3600
    if hours >= 24:
        days, hours = hours // 24, hours % 24
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _ends_in(offer):
    """" · ends in 3h 20m" for a closing offer, "" for an open-ended one."""
    left = offer.seconds_left if offer else None
    return f" · ends in {format_time_left(left)}" if left else ""


def bonus_line(gems, *, prefix="\n", offer=None) -> str:
    """One HTML line for a purchase message, or "" when no bonus was paid."""
    if gems <= 0:
        return ""
    return (f"{prefix}💎 Elite Signing Bonus: <b>+{gems:,}</b> gems"
            f"{_ends_in(offer)}")


def teaser_line(rating, price=None, *, prefix="\n", offer=None,
                session=None) -> str:
    """The bonus a buyer *would* earn, for a card they haven't bought yet.

    Empty while the offer is closed, so a card stops advertising it the moment
    an admin shuts it off. Carries the countdown when the offer has an end
    time — that urgency is the point of a limited-time offer.
    """
    offer = offer or current_offer(session)
    gems = offer.gems_for(rating, price)
    if gems <= 0:
        return ""
    return (f"{prefix}💎 Elite Signing Bonus: <b>+{gems:,}</b> gems on purchase"
            f"{_ends_in(offer)}")
