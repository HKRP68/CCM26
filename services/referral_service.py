"""Referral / invite system.

Flow:
  1. Inviter shares their link: t.me/<bot>?start=ref<inviter_telegram_id>
  2. Invitee clicks → /start ref<id> runs → record_referral() creates a
     pending Referral row (not counted yet).
  3. Invitee runs /debut → debut_handler calls complete_referral() →
     `completed_at` is set, leaderboard counts it, per-invite reward is paid.
  4. Admin can run "Declare winners" on a finished competition → top
     ranked inviters get top1/top2/top3 prizes.

Anti-abuse:
  - Self-referral blocked (inviter == invitee).
  - One referrer per invitee (UNIQUE on invitee_user_id).
  - Invitee must actually complete /debut to count (filters drive-by clicks).
  - Admin can mark referrals invalid → excluded from leaderboard.
"""

import logging
from datetime import datetime
from sqlalchemy import func

logger = logging.getLogger(__name__)


def get_active_competition(session):
    """Return the currently-active competition, or None.

    Active = is_active=True AND start_date <= now <= end_date.
    """
    from models import ReferralCompetition
    now = datetime.utcnow()
    return (session.query(ReferralCompetition)
            .filter(ReferralCompetition.is_active == True,
                    ReferralCompetition.start_date <= now,
                    ReferralCompetition.end_date >= now)
            .order_by(ReferralCompetition.start_date.desc())
            .first())


def record_referral(session, inviter_user_id, invitee_user_id):
    """Create a Referral row when an invitee starts the bot via a deep link.

    Idempotent: if the invitee already has a referral record (any inviter),
    this is a no-op. Returns the Referral row (existing or new) or None on
    bad input.
    """
    from models import Referral, User

    if not inviter_user_id or not invitee_user_id:
        return None
    if inviter_user_id == invitee_user_id:
        # Self-referral
        return None

    # Confirm both users exist
    inviter = session.query(User).get(inviter_user_id)
    invitee = session.query(User).get(invitee_user_id)
    if not inviter or not invitee:
        return None

    # If invitee already has a referrer, return that
    existing = (session.query(Referral)
                .filter(Referral.invitee_user_id == invitee_user_id).first())
    if existing:
        return existing

    comp = get_active_competition(session)
    ref = Referral(
        inviter_user_id=inviter_user_id,
        invitee_user_id=invitee_user_id,
        competition_id=comp.id if comp else None,
        created_at=datetime.utcnow(),
    )
    session.add(ref)
    try:
        session.flush()
    except Exception:
        session.rollback()
        # Race: another process added a referral between our query + insert.
        # Fetch the existing one and return.
        existing = (session.query(Referral)
                    .filter(Referral.invitee_user_id == invitee_user_id).first())
        return existing
    return ref


def complete_referral(session, invitee_user_id):
    """Called from /debut: mark referral as completed + pay reward.

    Returns dict {ok, paid_coins, paid_gems, inviter_user_id} if a referral
    was completed, else None.
    """
    from models import Referral, User, ReferralCompetition

    ref = (session.query(Referral)
           .filter(Referral.invitee_user_id == invitee_user_id,
                   Referral.completed_at.is_(None)).first())
    if not ref:
        return None

    ref.completed_at = datetime.utcnow()

    paid_coins = 0
    paid_gems = 0

    # Pay per-invite reward if competition has it configured
    if ref.competition_id:
        comp = session.query(ReferralCompetition).get(ref.competition_id)
        if comp:
            paid_coins = int(comp.prize_per_invite or 0)
            paid_gems = int(comp.prize_per_invite_gems or 0)
    if paid_coins > 0 or paid_gems > 0:
        inviter = session.query(User).get(ref.inviter_user_id)
        if inviter:
            inviter.total_coins = (inviter.total_coins or 0) + paid_coins
            inviter.total_gems = (inviter.total_gems or 0) + paid_gems
            ref.reward_paid_at = datetime.utcnow()
            try:
                from services.activity_service import log_activity
                log_activity(
                    session, inviter.id, "referral_reward",
                    f"Invite reward — invitee #{invitee_user_id}",
                    coins_change=paid_coins, gems_change=paid_gems,
                )
            except Exception:
                pass

    return {
        "ok": True,
        "paid_coins": paid_coins,
        "paid_gems": paid_gems,
        "inviter_user_id": ref.inviter_user_id,
        "competition_id": ref.competition_id,
    }


def get_user_stats(session, user_id, competition_id=None):
    """Return invite stats for a user.

    If `competition_id` is given, scoped to that competition. Otherwise,
    counts use the currently-active competition; lifetime totals are also
    included.

    Returns: {
      pending: count of invitees who clicked but didn't debut,
      completed: count of completed (counts toward leaderboard),
      lifetime_completed: total completed across all competitions,
      rank: rank in active/specified competition (or None),
      total_competitors: total users with at least 1 completed invite,
    }
    """
    from models import Referral

    target_comp_id = competition_id
    if target_comp_id is None:
        active = get_active_competition(session)
        target_comp_id = active.id if active else None

    base_q = session.query(Referral).filter(
        Referral.inviter_user_id == user_id,
        Referral.is_valid == True,
    )
    lifetime_completed = base_q.filter(Referral.completed_at.isnot(None)).count()

    pending = 0
    completed = 0
    rank = None
    total_competitors = 0

    if target_comp_id is not None:
        comp_q = base_q.filter(Referral.competition_id == target_comp_id)
        pending = comp_q.filter(Referral.completed_at.is_(None)).count()
        completed = comp_q.filter(Referral.completed_at.isnot(None)).count()

        if completed > 0:
            # Rank: count of users with strictly more completed invites + 1
            sub = (session.query(Referral.inviter_user_id,
                                 func.count(Referral.id).label("c"))
                   .filter(Referral.competition_id == target_comp_id,
                           Referral.is_valid == True,
                           Referral.completed_at.isnot(None))
                   .group_by(Referral.inviter_user_id).subquery())
            higher = (session.query(func.count())
                      .select_from(sub)
                      .filter(sub.c.c > completed).scalar() or 0)
            rank = int(higher) + 1
            total_competitors = (session.query(func.count())
                                 .select_from(sub).scalar() or 0)

    return {
        "pending": pending,
        "completed": completed,
        "lifetime_completed": lifetime_completed,
        "rank": rank,
        "total_competitors": total_competitors,
        "competition_id": target_comp_id,
    }


def get_leaderboard(session, competition_id, limit=50):
    """Return top inviters for a competition.

    Returns list of dicts: [{user_id, telegram_id, username, first_name,
    completed_invites, pending_invites}, ...] sorted by completed desc.
    """
    from models import Referral, User

    # Aggregate
    rows = (session.query(
                Referral.inviter_user_id,
                func.count(Referral.id).label("completed"),
            )
            .filter(Referral.competition_id == competition_id,
                    Referral.is_valid == True,
                    Referral.completed_at.isnot(None))
            .group_by(Referral.inviter_user_id)
            .order_by(func.count(Referral.id).desc())
            .limit(limit).all())

    if not rows:
        return []

    user_ids = [r[0] for r in rows]
    users = {u.id: u for u in
             session.query(User).filter(User.id.in_(user_ids)).all()}

    # Pending counts per user
    pending_rows = (session.query(
                        Referral.inviter_user_id,
                        func.count(Referral.id))
                    .filter(Referral.competition_id == competition_id,
                            Referral.inviter_user_id.in_(user_ids),
                            Referral.is_valid == True,
                            Referral.completed_at.is_(None))
                    .group_by(Referral.inviter_user_id).all())
    pending_map = {uid: cnt for uid, cnt in pending_rows}

    result = []
    for rank, (uid, completed) in enumerate(rows, start=1):
        u = users.get(uid)
        if not u:
            continue
        result.append({
            "rank": rank,
            "user_id": uid,
            "telegram_id": u.telegram_id,
            "username": u.username or "",
            "first_name": u.first_name or "",
            "team_name": u.team_name or "",
            "completed_invites": int(completed),
            "pending_invites": int(pending_map.get(uid, 0)),
        })
    return result


def get_user_invitee_list(session, user_id, competition_id=None, limit=100):
    """Return list of users invited by this user (for the admin's per-user
    detail view).
    """
    from models import Referral, User

    q = (session.query(Referral, User)
         .join(User, User.id == Referral.invitee_user_id)
         .filter(Referral.inviter_user_id == user_id))
    if competition_id is not None:
        q = q.filter(Referral.competition_id == competition_id)
    q = q.order_by(Referral.created_at.desc()).limit(limit)

    return [{
        "referral_id": r.id,
        "invitee_user_id": u.id,
        "telegram_id": u.telegram_id,
        "username": u.username or "",
        "first_name": u.first_name or "",
        "created_at": r.created_at,
        "completed_at": r.completed_at,
        "is_valid": r.is_valid,
        "reward_paid_at": r.reward_paid_at,
    } for r, u in q.all()]


def declare_winners(session, competition_id, declared_by="admin"):
    """Distribute top-N prizes for a finished competition.

    Returns dict {ok, winners: [{rank, user_id, telegram_id, prize}], message}
    """
    from models import ReferralCompetition, User
    comp = session.query(ReferralCompetition).get(competition_id)
    if not comp:
        return {"ok": False, "message": "Competition not found."}
    if comp.winners_announced_at is not None:
        return {"ok": False, "message": "Winners already declared."}

    lb = get_leaderboard(session, competition_id, limit=3)
    if not lb:
        return {"ok": False, "message": "No completed invites — no winners to declare."}

    prizes = [comp.prize_top1, comp.prize_top2, comp.prize_top3]
    winners = []
    for entry in lb:
        rank = entry["rank"]
        if rank > 3: break
        prize = prizes[rank - 1] or 0
        if prize <= 0:
            continue
        u = session.query(User).get(entry["user_id"])
        if not u: continue
        u.total_coins = (u.total_coins or 0) + prize
        winners.append({
            "rank": rank,
            "user_id": u.id,
            "telegram_id": u.telegram_id,
            "username": u.username or "",
            "first_name": u.first_name or "",
            "prize_coins": prize,
        })
        if rank == 1: comp.winner_top1_user_id = u.id
        elif rank == 2: comp.winner_top2_user_id = u.id
        elif rank == 3: comp.winner_top3_user_id = u.id

        try:
            from services.activity_service import log_activity
            log_activity(session, u.id, "competition_prize",
                         f"Competition '{comp.name}' rank #{rank} prize",
                         coins_change=prize)
        except Exception:
            pass

    comp.winners_announced_at = datetime.utcnow()
    return {"ok": True, "winners": winners,
            "message": f"Declared {len(winners)} winner(s)."}


def get_invite_link(bot_username, telegram_id):
    """Build the user's referral link.

    Format: t.me/<bot_username>?start=ref<telegram_id>
    `telegram_id` is used (not internal user.id) because that's what the
    invitee's /start handler can resolve back to the inviter without an
    extra DB join.
    """
    if not bot_username:
        return None
    return f"https://t.me/{bot_username.lstrip('@')}?start=ref{telegram_id}"


# ── Code-based redemption (fallback when start payload didn't fire) ────

# Avoid ambiguous chars (0/O, 1/I) so codes are easy to type
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6


def _generate_code():
    import secrets
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def ensure_referral_code(session, user):
    """Return the user's referral code, generating + persisting on first use.

    Caller must commit (we flush but don't commit).
    """
    if user.referral_code:
        return user.referral_code
    from models import User
    # Try up to 8 times on rare collisions
    for _ in range(8):
        candidate = _generate_code()
        existing = session.query(User).filter(
            User.referral_code == candidate).first()
        if not existing:
            user.referral_code = candidate
            try:
                session.flush()
            except Exception:
                session.rollback()
                continue
            return candidate
    return None


def find_inviter_by_code(session, code):
    """Look up the user who owns this code. Case-insensitive. Returns User|None."""
    if not code:
        return None
    code = code.strip().upper()
    if not code:
        return None
    from models import User
    return session.query(User).filter(User.referral_code == code).first()


def redeem_code(session, code, invitee_user_id):
    """Apply a referral code post-debut. Returns outcome dict.

    Used when the invitee missed /start ref<id>. They can run /redeem <code>
    or enter it in the Mini App invite tab.

    Eligibility (NEW): only "new users" can redeem. A user is eligible if:
      - They debuted within the last 24 hours, AND
      - They have not played any matches yet (matches_played == 0)
    This prevents established players from boosting friends' leaderboards
    long after joining.

    Rules:
      - Code must match a real user (case-insensitive)
      - Invitee cannot redeem own code
      - Invitee can only redeem ONE code, ever (one-shot)
      - Eligibility check above
      - If invitee already has a pending referral (from /start ref<id>),
        we complete THAT one instead of creating a new one (preserves
        original inviter attribution)

    Returns {ok, error, message, paid_coins, paid_gems, inviter_user_id}
    """
    from models import Referral, User
    from datetime import timedelta

    inviter = find_inviter_by_code(session, code)
    if not inviter:
        return {"ok": False, "error": "invalid_code",
                "message": "Invalid code. Check spelling and try again."}
    if inviter.id == invitee_user_id:
        return {"ok": False, "error": "self_referral",
                "message": "You can't use your own code."}

    invitee = session.query(User).get(invitee_user_id)
    if not invitee:
        return {"ok": False, "error": "invitee_missing",
                "message": "User not found."}

    # ── Eligibility: only NEW users can redeem ──
    # Two conditions, both must hold:
    #   1. Account < 24h old
    #   2. No matches played yet
    # The exception: if there's already a PENDING referral (from /start ref<id>),
    # we still let the user complete it via code — they were always meant to
    # be credited; this is just the fallback for the link not firing properly.
    existing = (session.query(Referral)
                .filter(Referral.invitee_user_id == invitee_user_id).first())
    has_pending_existing = (existing is not None
                            and existing.completed_at is None)

    if not has_pending_existing:
        # Fresh redemption — apply new-user gate
        debut_age = None
        if invitee.created_at:
            debut_age = datetime.utcnow() - invitee.created_at
        if debut_age is None or debut_age > timedelta(hours=24):
            return {
                "ok": False, "error": "not_new_user",
                "message": ("Referral codes can only be used by new users "
                            "(within 24 hours of /debut)."),
            }
        if (invitee.matches_played or 0) > 0:
            return {
                "ok": False, "error": "not_new_user",
                "message": ("Referral codes can only be used by new users "
                            "(before playing your first match)."),
            }

    if existing:
        if existing.completed_at is not None:
            return {"ok": False, "error": "already_redeemed",
                    "message": "You've already used a referral code."}
        # Pending: complete the existing one (don't overwrite inviter)
        cr = complete_referral(session, invitee_user_id)
        if cr:
            return {
                "ok": True, "error": None,
                "message": "Code redeemed (using earlier link).",
                "paid_coins": cr["paid_coins"],
                "paid_gems": cr["paid_gems"],
                "inviter_user_id": cr["inviter_user_id"],
            }
        return {"ok": False, "error": "complete_failed",
                "message": "Could not complete referral. Try again later."}

    # Fresh — create + complete in one go
    ref = record_referral(session, inviter.id, invitee_user_id)
    if not ref:
        return {"ok": False, "error": "record_failed",
                "message": "Couldn't record referral."}
    cr = complete_referral(session, invitee_user_id)
    if not cr:
        return {"ok": False, "error": "complete_failed",
                "message": "Couldn't complete referral."}
    return {
        "ok": True, "error": None,
        "message": "Code redeemed!",
        "paid_coins": cr["paid_coins"],
        "paid_gems": cr["paid_gems"],
        "inviter_user_id": cr["inviter_user_id"],
    }


def is_eligible_to_redeem(session, user):
    """Return (eligible: bool, reason: str|None).

    Used by /api/webapp/invite to hide the redeem input for ineligible users.
    """
    from models import Referral
    from datetime import timedelta

    # Already redeemed → not eligible
    existing = (session.query(Referral)
                .filter(Referral.invitee_user_id == user.id).first())
    if existing and existing.completed_at is not None:
        return False, "already_redeemed"
    # Pending referral from link → eligible (always — they should complete it)
    if existing and existing.completed_at is None:
        return True, None
    # Fresh eligibility check
    if not user.created_at:
        return False, "no_debut_date"
    if datetime.utcnow() - user.created_at > timedelta(hours=24):
        return False, "too_old"
    if (user.matches_played or 0) > 0:
        return False, "match_played"
    return True, None



# ── Branding helper ───────────────────────────────────────────────────

def get_branding(session):
    """Return branding dict pulled from GameConfig.

    Returns:
      {channel_username, channel_label, channel_link,
       group_username, group_label, group_link,
       tagline, has_any}
    """
    from models import GameConfig
    cfg = session.query(GameConfig).first()
    if not cfg:
        return {
            "channel_username": "", "channel_label": "", "channel_link": "",
            "group_username": "", "group_label": "", "group_link": "",
            "tagline": "", "has_any": False,
        }
    cu = (getattr(cfg, 'branding_channel_username', '') or "").strip().lstrip("@")
    gu = (getattr(cfg, 'branding_group_username', '') or "").strip().lstrip("@")
    return {
        "channel_username": cu,
        "channel_label": (getattr(cfg, 'branding_channel_label', '') or "").strip(),
        "channel_link": f"https://t.me/{cu}" if cu else "",
        "group_username": gu,
        "group_label": (getattr(cfg, 'branding_group_label', '') or "").strip(),
        "group_link": f"https://t.me/{gu}" if gu else "",
        "tagline": (getattr(cfg, 'branding_tagline', '') or "").strip(),
        "has_any": bool(cu or gu),
    }


def format_branding_html(session, prefix="\n\n"):
    """Format branding as an HTML snippet for bot messages. Empty string if
    nothing configured."""
    b = get_branding(session)
    if not b["has_any"]:
        return ""
    lines = []
    if b["tagline"]:
        lines.append(f"<i>{b['tagline']}</i>")
    if b["channel_username"]:
        label = b["channel_label"] or "official channel"
        lines.append(f"📢 <a href='{b['channel_link']}'>@{b['channel_username']}</a> — {label}")
    if b["group_username"]:
        label = b["group_label"] or "official group"
        lines.append(f"💬 <a href='{b['group_link']}'>@{b['group_username']}</a> — {label}")
    return prefix + "\n".join(lines)
