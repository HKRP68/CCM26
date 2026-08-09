"""Changing a Career Player's name and country after it has been created.

A career card is created once and kept for good, so the identity on it — the
name and the flag — is the one thing owners ask to change. This module sells
that change and, when the owner types a name themselves, holds it for review.

Three rules shape everything here:

* **The first change is free, the rest are a ladder.** Change 1 costs nothing,
  change 2 costs :data:`DEFAULT_PRICE_2`, change 3 :data:`DEFAULT_PRICE_3`, and
  every change after that is the one before it plus
  :data:`DEFAULT_PRICE_STEP` — 300 → 500 → 750 → 1000 … All four numbers are
  website-tunable, and the paid rungs are only sold at all while the website has
  ``career_paid_changes_open`` switched on. The free one is always available.

* **One change buys both halves.** Name and country submitted together cost a
  single rung, so nobody pays twice to fix an identity they picked in one go.

* **A typed name is reviewed; a rolled one is not.** Names rolled out of the
  admin-curated ``career_name_pool`` are already vetted, so they land instantly
  — exactly as the /cmucareer wizard hands them out. A name the owner typed goes
  to the website as a :class:`~models.CareerChangeRequest`, and **nothing on the
  card changes until an admin approves it**: the old name *and* the old country
  keep running, so a card is never half-changed while it waits.

Gems are taken at submission, not at approval. That is what keeps the review
queue honest — a speculative name costs the same as a real one — and every
refund path (reject, cancel, an owner whose card is deleted underneath them)
puts the gems *and* the ladder rung back, so a refused name never costs anybody
a rung of the price ladder.
"""

import logging
import re
from datetime import datetime

from models import CareerChangeRequest, Player, User
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# How many changes are free. The first one always is, whatever the website has
# done with the paid rungs.
FREE_CHANGES = 1

DEFAULT_PRICE_2 = 300
DEFAULT_PRICE_3 = 500
DEFAULT_PRICE_STEP = 250

# Where a new name came from, which is what decides whether it is reviewed.
SOURCE_POOL = "pool"
SOURCE_CUSTOM = "custom"

STATUS_PENDING = "pending"      # typed name waiting for the website
STATUS_APPLIED = "applied"      # went on the card immediately
STATUS_APPROVED = "approved"    # reviewed, then went on the card
STATUS_REJECTED = "rejected"    # refused; gems and rung returned
STATUS_CANCELLED = "cancelled"  # owner withdrew it; gems and rung returned

OPEN_STATUSES = (STATUS_PENDING,)
DONE_STATUSES = (STATUS_APPLIED, STATUS_APPROVED, STATUS_REJECTED,
                 STATUS_CANCELLED)


# ── Settings ────────────────────────────────────────────────────────────────

def _int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def settings(session=None, cfg=None):
    """The website's change settings, normalised and always complete."""
    if cfg is None:
        try:
            from services.config_service import get_config
            cfg = get_config(session)
        except Exception:
            logger.exception("career change settings read failed")
            cfg = {}
    return {
        "price_2": max(0, _int(cfg.get("career_change_price_2"), DEFAULT_PRICE_2)),
        "price_3": max(0, _int(cfg.get("career_change_price_3"), DEFAULT_PRICE_3)),
        "step": max(0, _int(cfg.get("career_change_price_step"), DEFAULT_PRICE_STEP)),
        "paid_open": bool(cfg.get("career_paid_changes_open")),
        "custom_open": bool(cfg.get("career_custom_names_open", True)),
        "needs_approval": bool(cfg.get("career_custom_names_need_approval", True)),
        "blocklist": cfg.get("career_name_blocklist") or "",
    }


def change_cost(index, conf=None):
    """Gems for the ``index``-th change of a card (1-based).

    ``index`` 1 is free; 2 and 3 have their own prices; every rung after 3 adds
    ``step`` to the one before it, so the sequence continues 750, 1000, 1250 …
    """
    index = _int(index, 1)
    if index <= FREE_CHANGES:
        return 0
    conf = conf if isinstance(conf, dict) and "price_2" in conf else settings()
    if index == 2:
        return conf["price_2"]
    return max(0, conf["price_3"] + conf["step"] * (index - 3))


def price_ladder(conf=None, rungs=6):
    """``[(index, cost)]`` for the first ``rungs`` changes — website display."""
    conf = conf if isinstance(conf, dict) and "price_2" in conf else settings()
    return [(i, change_cost(i, conf)) for i in range(1, max(1, rungs) + 1)]


# ── Reading a card's position on the ladder ─────────────────────────────────

def changes_used(player):
    return max(0, _int(getattr(player, "career_changes_used", 0), 0))


def free_grants(player):
    return max(0, _int(getattr(player, "career_free_changes", 0), 0))


def pending_request(session, user_id):
    """The owner's un-reviewed request, or ``None``. One at a time, by design."""
    if not user_id:
        return None
    return (session.query(CareerChangeRequest)
            .filter(CareerChangeRequest.user_id == user_id,
                    CareerChangeRequest.status == STATUS_PENDING)
            .order_by(CareerChangeRequest.id.desc())
            .first())


def quote(session, user, player, conf=None):
    """What the owner's next change would cost, and whether they can make it.

    One dict with everything the bot caption, the Mini App panel and the website
    all need, so none of them has to re-derive the price ladder.
    """
    conf = conf or settings(session)
    used = changes_used(player)
    grants = free_grants(player)
    index = used + 1
    cost = 0 if grants else change_cost(index, conf)
    balance = _int(getattr(user, "total_gems", 0), 0)
    pending = pending_request(session, getattr(user, "id", None))

    if pending is not None:
        blocked = "pending"
    elif cost and not conf["paid_open"]:
        blocked = "closed"
    elif cost > balance:
        blocked = "gems"
    else:
        blocked = None

    return {
        "changes_used": used,
        "next_index": index,
        "cost": cost,
        "is_free": cost == 0,
        "free_grants": grants,
        "balance": balance,
        "paid_open": conf["paid_open"],
        "custom_open": conf["custom_open"],
        "needs_approval": conf["needs_approval"],
        "can_change": blocked is None,
        "blocked": blocked,
        "message": _blocked_message(blocked, cost, balance, conf),
        "next_costs": [(i, change_cost(i, conf)) for i in range(index, index + 3)],
        "pending": request_summary(pending) if pending else None,
    }


def _blocked_message(blocked, cost, balance, conf):
    if blocked is None:
        return None
    if blocked == "pending":
        return ("You already have a name waiting to be reviewed. Cancel it or "
                "wait for the decision before changing anything else.")
    if blocked == "closed":
        return (f"Your free change is used up. Paid changes ({cost:,} 💎 for "
                f"the next one) aren't open right now — watch for the "
                f"announcement.")
    return (f"This change costs {cost:,} 💎 and you have {balance:,} 💎.")


# ── Names ───────────────────────────────────────────────────────────────────

_ALNUM = re.compile(r"[^a-z0-9]+")


def _squash(value):
    """Lower-case a name down to its letters and digits.

    ``"S. l  u-r"`` and ``"slur"`` squash to the same thing, so the blocklist
    can't be walked around with punctuation and spacing.
    """
    return _ALNUM.sub("", (value or "").lower())


def blocked_words(conf=None):
    """The website's blocklist as a list of squashed terms."""
    conf = conf if isinstance(conf, dict) and "blocklist" in conf else settings()
    raw = re.split(r"[,\n\r]+", conf.get("blocklist") or "")
    return [w for w in (_squash(part) for part in raw) if w]


def name_is_blocked(name, conf=None):
    """The blocklist term this name contains, or ``None``."""
    squashed = _squash(name)
    if not squashed:
        return None
    return next((word for word in blocked_words(conf) if word in squashed), None)


def validate_name(session, player, name, *, source, conf=None):
    """Check a requested name. Returns ``(clean_name, None)`` or ``(None, msg)``.

    Everything :func:`services.career_service.validate_career_name` enforces —
    the character set, the length, and never colliding with another player —
    plus the website's blocklist for a typed name. A pool name skips the
    blocklist: it came from the admin's own curated list.
    """
    from services.career_service import validate_career_name

    clean, error = validate_career_name(session, name, player=player)
    if error:
        return None, error
    if source == SOURCE_CUSTOM:
        word = name_is_blocked(clean, conf)
        if word:
            logger.info("career custom name refused by the blocklist")
            return None, ("That name isn't allowed. Please pick another one.")
    return clean, None


def name_is_from_pool(session, country, name):
    """True when ``name`` really is a first name + surname from this pool.

    The caller *claims* where a name came from, and that claim decides whether
    the name is reviewed — so it is checked rather than believed. Without this,
    anything could be submitted as ``source="pool"`` and skip the queue entirely.

    Pool entries may themselves contain spaces ("de Villiers", "Jean Paul"), so
    every way of splitting the name into two halves is tried rather than
    assuming the first space is the divide.
    """
    from sqlalchemy import func

    from models import CareerNamePool
    from services.career_name_service import FIRST, SURNAME

    parts = (name or "").split()
    if len(parts) < 2:
        return False
    halves = [(" ".join(parts[:i]), " ".join(parts[i:]))
              for i in range(1, len(parts))]
    wanted = {half.lower() for pair in halves for half in pair}

    rows = (session.query(CareerNamePool.name_kind, CareerNamePool.value)
            .filter(CareerNamePool.is_active.is_(True),
                    CareerNamePool.country == country,
                    func.lower(CareerNamePool.value).in_(sorted(wanted)))
            .all())
    firsts = {v.lower() for kind, v in rows if kind == FIRST}
    surnames = {v.lower() for kind, v in rows if kind == SURNAME}
    return any(first.lower() in firsts and surname.lower() in surnames
               for first, surname in halves)


def roll_pool_name(session, country, attempts=6):
    """A fresh unused name from ``country``'s pool, or ``None``.

    The same generator the /cmucareer wizard uses, with the letters picked for
    the owner — a rename is not the moment to make somebody walk two more
    button steps.
    """
    from services.career_name_service import generate_name, random_letters

    for _ in range(max(1, attempts)):
        initial, surname_letter = random_letters(session, country)
        if not initial:
            return None
        name = generate_name(session, country, initial, surname_letter)
        if name:
            return name
    return None


def available_countries(session):
    """The countries a career player may move to — the wizard's own list."""
    from services.career_name_service import available_countries as _countries
    try:
        return _countries(session)
    except Exception:
        logger.exception("career country list failed")
        return []


# ── Submitting ──────────────────────────────────────────────────────────────

def _invalidate_card(player):
    """Drop cached renders so the new name/flag shows on the very next card.

    Only the in-memory caches and the stored file_id on this ORM object, exactly
    as :mod:`services.career_service` does — a second session issuing its own
    UPDATE would block on the row the caller still has open.
    """
    player.gen_card_file_id = None
    try:
        from services.card_generator import drop_cached_cards
        drop_cached_cards(player.id)
    except Exception:
        logger.exception("career card cache invalidation failed")


def _apply(session, player, *, name=None, country=None):
    """Put an approved change on the card. Returns ``(ok, message)``."""
    was = (player.name, player.country)
    if country:
        player.country = country
    if name:
        player.name = name
    _invalidate_card(player)
    try:
        # SAVEPOINT so losing the race against uq_players_name_version undoes
        # this one write and leaves the caller's session usable for the reply.
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # The savepoint took the INSERT/UPDATE back but not the attributes on
        # the object, and a stale name left sitting there would be flushed
        # again by the caller's own commit — putting the same IntegrityError in
        # the way of a reply that has nothing to do with it.
        logger.info("career change for player %s hit a unique index", player.id)
        player.name, player.country = was
        return False, f"'{name}' was just taken by another player."
    return True, None


def _lock_wallet(session, user):
    """Re-read the owner ``FOR UPDATE`` so two taps can't spend the same gems."""
    try:
        return (session.query(User).filter(User.id == user.id)
                .with_for_update().first()) or user
    except Exception:
        # SQLite has no row locks and raises on FOR UPDATE.
        return session.query(User).filter(User.id == user.id).first() or user


def submit_change(session, user, *, name=None, country=None, source=None,
                  player=None):
    """Buy a name and/or country change. Caller commits.

    Returns ``{"ok": True, "request": …, "applied": bool, …}``. When ``applied``
    is False the card is untouched and the request is waiting on the website —
    the old name and old country both keep running until it is approved.
    """
    from services.career_service import get_career_player

    player = player or get_career_player(session, user.id)
    if player is None:
        return {"ok": False, "error": "no_career",
                "message": "You don't have a Career Player yet. Use /cmucareer."}

    conf = settings(session)
    name = (name or "").strip()
    country = (country or "").strip()
    if not name and not country:
        return {"ok": False, "error": "nothing",
                "message": "Pick a new name, a new country, or both."}

    if pending_request(session, user.id) is not None:
        return {"ok": False, "error": "pending",
                "message": _blocked_message("pending", 0, 0, conf)}

    # ── The country half ──
    if country:
        if country == player.country:
            country = ""
        else:
            allowed = available_countries(session)
            if allowed and country not in allowed:
                return {"ok": False, "error": "bad_country",
                        "message": "That country isn't available. Pick one from "
                                   "the list."}

    # ── The name half ──
    if source not in (None, SOURCE_POOL, SOURCE_CUSTOM):
        return {"ok": False, "error": "bad_source",
                "message": "Unknown name source."}
    if name:
        if source is None:
            source = SOURCE_CUSTOM
        # "It came from the pool" is a claim the caller makes, and it decides
        # whether the name is reviewed — so verify it against the pool the name
        # would have been rolled from. Anything else is a typed name, whatever
        # the caller said, and goes through the custom rules below.
        if source == SOURCE_POOL and not name_is_from_pool(
                session, country or player.country, name):
            source = SOURCE_CUSTOM
        if source == SOURCE_CUSTOM and not conf["custom_open"]:
            return {"ok": False, "error": "custom_closed",
                    "message": ("Custom names are closed right now — roll a name "
                                "from your country's pool instead.")}
        clean, error = validate_name(session, player, name, source=source,
                                     conf=conf)
        if error:
            return {"ok": False, "error": "bad_name", "message": error}
        name = clean
        if name == player.name:
            name = ""
            source = None
    else:
        source = None

    if not name and not country:
        return {"ok": False, "error": "unchanged",
                "message": "That's already your name and country."}

    # ── Price ──
    grants = free_grants(player)
    index = changes_used(player) + 1
    cost = 0 if grants else change_cost(index, conf)
    if cost and not conf["paid_open"]:
        return {"ok": False, "error": "closed", "cost": cost,
                "message": _blocked_message("closed", cost, 0, conf)}

    wallet = _lock_wallet(session, user)
    balance = _int(wallet.total_gems, 0)
    if cost > balance:
        return {"ok": False, "error": "insufficient_gems", "cost": cost,
                "balance": balance,
                "message": _blocked_message("gems", cost, balance, conf)}

    # ── Charge, then either apply or queue ──
    if cost:
        wallet.total_gems = balance - cost
        if wallet is not user:
            user.total_gems = wallet.total_gems
    if grants:
        player.career_free_changes = grants - 1
    else:
        player.career_changes_used = index

    needs_review = bool(name and source == SOURCE_CUSTOM and conf["needs_approval"])
    request = CareerChangeRequest(
        user_id=user.id, player_id=player.id,
        old_name=player.name, new_name=name or None,
        old_country=player.country, new_country=country or None,
        name_source=source,
        change_index=0 if grants else index,
        gems_charged=cost, used_free_grant=bool(grants),
        status=STATUS_PENDING if needs_review else STATUS_APPLIED,
        created_at=datetime.utcnow(),
        decided_at=None if needs_review else datetime.utcnow(),
    )
    session.add(request)

    if not needs_review:
        ok, message = _apply(session, player, name=name or None,
                             country=country or None)
        if not ok:
            # Nothing was charged in the database's eyes yet — the caller
            # rolls back — so report the clash and let them pick again.
            return {"ok": False, "error": "name_taken", "message": message}

    session.flush()
    _log(session, user.id, request, player)
    return {"ok": True, "request": request, "applied": not needs_review,
            "cost": cost, "used_free_grant": bool(grants),
            "balance": _int(wallet.total_gems, 0),
            "name": name or None, "country": country or None,
            "message": _submitted_message(request)}


def _submitted_message(request):
    bits = []
    if request.new_name:
        bits.append(f"name → {request.new_name}")
    if request.new_country:
        bits.append(f"country → {request.new_country}")
    what = " and ".join(bits)
    if request.status == STATUS_PENDING:
        return (f"Sent for review: {what}. Your card keeps its current name "
                f"and country until it's approved.")
    return f"Done: {what}."


def _log(session, user_id, request, player, extra=None, gems_change=None):
    """One activity row per step of a request's life.

    ``gems_change`` is stated by the caller rather than derived, so the refund
    rows read ``+300`` instead of silently logging a movement of zero against a
    balance that really did change.
    """
    try:
        from services.activity_service import log_activity
        if gems_change is None:
            gems_change = -_int(request.gems_charged, 0)
        log_activity(session, user_id, "career_change",
                     _submitted_message(request) if extra is None else extra,
                     gems_change=gems_change,
                     player_name=player.name if player else None,
                     player_rating=getattr(player, "rating", None))
    except Exception:
        logger.exception("career change activity log failed")


# ── Deciding ────────────────────────────────────────────────────────────────

def _refund(session, request, player):
    """Give back the gems and the ladder rung a refused request consumed."""
    owner = session.query(User).get(request.user_id) if request.user_id else None
    refunded = max(0, _int(request.gems_charged, 0))
    if owner is not None and refunded:
        owner.total_gems = _int(owner.total_gems, 0) + refunded
    if player is not None:
        if request.used_free_grant:
            player.career_free_changes = free_grants(player) + 1
        elif request.change_index:
            # Put the rung back only if it is still the last one used, so a
            # later change made in between can't be un-charged by this refund.
            if changes_used(player) == request.change_index:
                player.career_changes_used = request.change_index - 1
    return owner, refunded


def approve_request(session, request, *, reviewer=None):
    """Approve a pending request and put it on the card. Caller commits."""
    if request is None or request.status != STATUS_PENDING:
        return {"ok": False, "error": "not_pending",
                "message": "That request has already been decided."}

    player = session.query(Player).get(request.player_id)
    if player is None or not player.is_career:
        # The card went away while the name sat in the queue (an admin delete,
        # say). There is nothing to rename, so refund rather than strand them.
        owner, refunded = _refund(session, request, None)
        request.status = STATUS_REJECTED
        request.review_note = "Career Player no longer exists"
        request.reviewed_by = reviewer
        request.decided_at = datetime.utcnow()
        session.flush()
        _log(session, request.user_id, request, None, gems_change=refunded,
             extra=f"Name change '{request.new_name}' closed — the Career "
                   f"Player no longer exists")
        return {"ok": False, "error": "no_player", "refunded": refunded,
                "message": ("That Career Player no longer exists — the request "
                            "was refunded and closed.")}

    if request.new_name:
        # Re-checked because the queue is not instant: the name may have been
        # taken by another card since it was submitted. The blocklist is
        # deliberately *not* re-run (source is left unset) — an admin pressing
        # Approve on a flagged name is overruling it on purpose.
        clean, error = validate_name(session, player, request.new_name,
                                     source=None)
        if error:
            # Stays pending: the admin decides whether to reject it or wait.
            return {"ok": False, "error": "bad_name", "message": error}
        request.new_name = clean

    ok, message = _apply(session, player, name=request.new_name,
                         country=request.new_country)
    if not ok:
        return {"ok": False, "error": "name_taken", "message": message}

    request.status = STATUS_APPROVED
    request.reviewed_by = reviewer
    request.decided_at = datetime.utcnow()
    session.flush()
    _log(session, request.user_id, request, player, gems_change=0,
         extra=f"Name change approved: {request.old_name} → {player.name}")
    return {"ok": True, "player": player, "request": request,
            "name": player.name, "country": player.country,
            "message": f"Approved — {request.old_name} is now {player.name}."}


def reject_request(session, request, *, reviewer=None, note=None):
    """Refuse a pending request, refund it in full. Caller commits."""
    if request is None or request.status != STATUS_PENDING:
        return {"ok": False, "error": "not_pending",
                "message": "That request has already been decided."}

    player = session.query(Player).get(request.player_id)
    owner, refunded = _refund(session, request, player)
    request.status = STATUS_REJECTED
    request.review_note = (note or "").strip()[:300] or None
    request.reviewed_by = reviewer
    request.decided_at = datetime.utcnow()
    session.flush()
    _log(session, request.user_id, request, player, gems_change=refunded,
         extra=f"Name change refused: '{request.new_name}'"
               + (f" — {request.review_note}" if request.review_note else ""))
    return {"ok": True, "request": request, "owner": owner,
            "refunded": refunded,
            "message": f"Rejected. {refunded:,} 💎 refunded."}


def cancel_request(session, request, *, by_owner=True):
    """Withdraw a pending request and refund it in full. Caller commits."""
    if request is None or request.status != STATUS_PENDING:
        return {"ok": False, "error": "not_pending",
                "message": "There is nothing waiting to be reviewed."}

    player = session.query(Player).get(request.player_id)
    owner, refunded = _refund(session, request, player)
    request.status = STATUS_CANCELLED
    request.review_note = "Withdrawn by the owner" if by_owner else "Cancelled by an admin"
    request.decided_at = datetime.utcnow()
    session.flush()
    _log(session, request.user_id, request, player, gems_change=refunded,
         extra=f"Name change withdrawn: '{request.new_name}'")
    return {"ok": True, "request": request, "owner": owner, "refunded": refunded,
            "message": (f"Withdrawn — {refunded:,} 💎 back in your balance."
                        if refunded else
                        "Withdrawn. Your free change is still yours.")}


def grant_free_change(session, player, count=1):
    """Give a card an extra free change. Caller commits.

    The admin's way of saying "try again on us" after refusing a name, and of
    handing out a change without opening the paid ones for everybody. A granted
    change never advances the price ladder.
    """
    count = max(1, _int(count, 1))
    player.career_free_changes = free_grants(player) + count
    return player.career_free_changes


# ── Presentation ────────────────────────────────────────────────────────────

def request_summary(request):
    """A plain dict of one request — bot captions, Mini App and website alike."""
    if request is None:
        return None
    return {
        "id": request.id,
        "status": request.status,
        "old_name": request.old_name,
        "new_name": request.new_name,
        "old_country": request.old_country,
        "new_country": request.new_country,
        "name_source": request.name_source,
        "gems_charged": _int(request.gems_charged, 0),
        "change_index": _int(request.change_index, 0),
        "used_free_grant": bool(request.used_free_grant),
        "review_note": request.review_note,
        "reviewed_by": request.reviewed_by,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
    }


def list_requests(session, *, status=None, limit=100, offset=0):
    """Requests newest-first, optionally filtered to one status."""
    query = session.query(CareerChangeRequest)
    if status:
        query = query.filter(CareerChangeRequest.status == status)
    return (query.order_by(CareerChangeRequest.created_at.desc(),
                           CareerChangeRequest.id.desc())
            .offset(max(0, _int(offset, 0)))
            .limit(max(1, min(500, _int(limit, 100)))).all())


def pending_count(session):
    """How many requests are waiting — the badge on the website's nav."""
    try:
        return (session.query(CareerChangeRequest)
                .filter(CareerChangeRequest.status == STATUS_PENDING).count())
    except Exception:
        logger.exception("career pending request count failed")
        return 0
