"""Quest service — tracks user progress on quest events, handles claims.

Event-driven design:
  - Whenever the user does something (claims a player, wins a match, etc.),
    the relevant handler calls track_event(user_id, event_key, count).
  - This service finds all active quests matching that event_key for the
    current period (today / this month) and increments user progress.
  - When progress reaches target_count, quest is marked completed (auto).
  - User must explicitly press "Claim" to receive the reward.

Standard event_keys:
  'claim'          — fired by /claim on a successful retain
  'gspin'          — fired by /gspin (any spin)
  'daily'          — fired by /daily (any reward)
  'match_played'   — fired at match end (regardless of result)
  'match_won'      — fired at match end (winner only)
  'runs_scored'    — fired with N (runs in last match) at match end
  'wickets_taken'  — fired with N (wickets in last match) at match end
  'fifty'          — fired when a player scores 50+ in a match
  'hundred'        — fired when a player scores 100+ in a match
  'trait_apply'    — fired when /traitapply succeeds
  'trait_buy'      — fired when /traitbuy succeeds
  'market_buy'     — fired when /playermarket buy succeeds
  'vsbot_played'   — fired when /vsbot match completes
  'vsbot_won'      — fired when user wins a /vsbot match

Per-match event_keys (added for the v2 quest list):
  'sixes_hit'             — fired with N sixes at match end (cumulative quests)
  'sixes_in_match'        — fired with max(N) once if N >= target (single-match quests)
  'boundaries_hit'        — fired with N (4s+6s) at match end
  'boundaries_in_match'   — fired with N once if N >= target
  'wickets_in_match'      — fired with N once at match end (single-match)
  'runs_in_innings'       — fired with N once at match end (single-match)
  'hattrick'              — fired once if at least one bowler took a hat-trick

Manual event_key:
  'manual' — quest is admin-only progressed (e.g. yorker counts, super overs).
             Admin can bump UserQuestProgress.progress directly via the website."""

import logging
import random
from datetime import datetime
from sqlalchemy.exc import IntegrityError

from models import Quest, UserQuestProgress, User

logger = logging.getLogger(__name__)

# Constants
DAILY_DEFAULT_REWARD_POINTS = 5
MONTHLY_DEFAULT_REWARD_POINTS = 10


# How many quests are randomly assigned per period per user.
# These are the only quests that count for tracking + appear in /mq.
DAILY_QUESTS_PER_USER = 3
MONTHLY_QUESTS_PER_USER = 5


def _claim_silently(session, user, uqp, q):
    """Auto-claim a completed-but-unclaimed quest. Used at period rollover.
    Returns dict with reward info applied, or None if nothing to claim."""
    if not uqp.completed or uqp.claimed:
        return None
    user.quest_points = (user.quest_points or 0) + (q.reward_points or 0)
    user.total_coins = (user.total_coins or 0) + (q.reward_coins or 0)
    user.total_gems = (user.total_gems or 0) + (q.reward_gems or 0)
    uqp.claimed = True
    uqp.claimed_at = datetime.utcnow()
    return {
        "name": q.name,
        "points": q.reward_points or 0,
        "coins": q.reward_coins or 0,
        "gems": q.reward_gems or 0,
    }


def ensure_quests_assigned(session, user_id, quest_type, *, max_count=None):
    """Make sure the user has a randomly-assigned set of quests for the current period.

    On every call:
      1. If the period hasn't rolled, do nothing (quests already assigned).
      2. If the period rolled (or no assignment exists for this period yet):
         a. Auto-claim any completed-unclaimed quests from the user's PREVIOUS
            period assignments (so users don't lose rewards by forgetting).
         b. Randomly pick `max_count` active quests of the given type.
         c. Insert UserQuestProgress rows with assigned=True.
         d. Any older period assignments stay in the DB for history but
            won't show up in /mq (we filter by current period).

    Returns: dict {assigned: list of Quest, auto_claimed: list of reward dicts}
    """
    if max_count is None:
        max_count = (DAILY_QUESTS_PER_USER if quest_type == "daily"
                     else MONTHLY_QUESTS_PER_USER)

    now = datetime.utcnow()
    current_period = period_key_for(quest_type, now)

    user = session.query(User).get(user_id)
    if not user:
        return {"assigned": [], "auto_claimed": []}

    # Have we already assigned for the current period?
    already_assigned = (session.query(UserQuestProgress)
                        .join(Quest, Quest.id == UserQuestProgress.quest_id)
                        .filter(UserQuestProgress.user_id == user_id,
                                UserQuestProgress.period_key == current_period,
                                UserQuestProgress.assigned == True,
                                Quest.quest_type == quest_type)
                        .count())
    if already_assigned > 0:
        return {"assigned": [], "auto_claimed": []}

    # ── Auto-claim from PREVIOUS period (if any unclaimed completed quests) ──
    auto_claimed = []
    previous_uqps = (session.query(UserQuestProgress, Quest)
                     .join(Quest, Quest.id == UserQuestProgress.quest_id)
                     .filter(UserQuestProgress.user_id == user_id,
                             UserQuestProgress.assigned == True,
                             UserQuestProgress.completed == True,
                             UserQuestProgress.claimed == False,
                             UserQuestProgress.period_key != current_period,
                             Quest.quest_type == quest_type)
                     .all())
    for uqp, q in previous_uqps:
        rewards = _claim_silently(session, user, uqp, q)
        if rewards:
            auto_claimed.append(rewards)

    # ── Pick the new random set ──
    pool = (session.query(Quest)
            .filter(Quest.quest_type == quest_type,
                    Quest.is_active == True)
            .all())
    if not pool:
        return {"assigned": [], "auto_claimed": auto_claimed}

    chosen = random.sample(pool, min(max_count, len(pool)))

    assigned_quests = []
    for q in chosen:
        # Reuse existing row for this period if somehow exists (idempotent)
        uqp = (session.query(UserQuestProgress)
               .filter(UserQuestProgress.user_id == user_id,
                       UserQuestProgress.quest_id == q.id,
                       UserQuestProgress.period_key == current_period)
               .first())
        if uqp:
            uqp.assigned = True
        else:
            uqp = UserQuestProgress(
                user_id=user_id, quest_id=q.id, period_key=current_period,
                progress=0, completed=False, claimed=False,
                assigned=True, last_updated=now,
            )
            session.add(uqp)
        assigned_quests.append(q)

    session.flush()
    return {"assigned": assigned_quests, "auto_claimed": auto_claimed}


def daily_period_key(now=None):
    """YYYY-MM-DD for the current day in UTC."""
    return (now or datetime.utcnow()).strftime("%Y-%m-%d")


def monthly_period_key(now=None):
    """YYYY-MM for the current month in UTC."""
    return (now or datetime.utcnow()).strftime("%Y-%m")


def period_key_for(quest_type, now=None):
    if quest_type == "daily":
        return daily_period_key(now)
    return monthly_period_key(now)


# ════════════════════════════════════════════════════════════════════
# EVENT TRACKING
# ════════════════════════════════════════════════════════════════════

def track_event(session, user_id, event_key, count=1, mode="add"):
    """Increment (or set-to-max) progress on every active quest matching this event.

    Args:
      session: open SQLAlchemy session
      user_id: User.id
      event_key: standard event identifier (see module docstring)
      count: how much to increment (default 1)
      mode: 'add' (default) increments by count;
            'max' sets progress = max(current, count) — used for "X+ in a single
                  match" quests where we only care about the BEST single attempt,
                  not the sum across many.

    Returns: list of newly-completed Quest IDs (those that just hit target).
    """
    now = datetime.utcnow()
    newly_completed = []
    try:
        quests = (session.query(Quest)
                  .filter(Quest.event_key == event_key,
                          Quest.is_active == True).all())
        for q in quests:
            period = period_key_for(q.quest_type, now)
            uqp = (session.query(UserQuestProgress)
                   .filter(UserQuestProgress.user_id == user_id,
                           UserQuestProgress.quest_id == q.id,
                           UserQuestProgress.period_key == period).first())
            # Only assigned quests count toward progress. If no row exists for
            # this user/quest/period, the quest is NOT in their selection
            # — skip without creating a stub row (otherwise we'd track every
            # quest in the catalog).
            if not uqp or not uqp.assigned:
                continue

            if uqp.claimed:
                continue

            # Apply mode
            if mode == "max":
                new_progress = max(uqp.progress, count)
            else:
                new_progress = uqp.progress + count
            uqp.progress = min(new_progress, q.target_count)
            uqp.last_updated = now

            if not uqp.completed and uqp.progress >= q.target_count:
                uqp.completed = True
                uqp.completed_at = now
                newly_completed.append(q.id)

        session.flush()
    except IntegrityError:
        session.rollback()
        logger.exception(f"track_event integrity error for user {user_id}")
    except Exception:
        logger.exception(f"track_event failed for user {user_id} event {event_key}")
    return newly_completed


def get_user_quests(session, user_id, quest_type):
    """Return the user's currently-assigned quests for the period.

    On call, ensures the user has a fresh random selection if their period
    rolled, and silently auto-claims any completed-but-unclaimed from the
    previous period.

    Returns: list of dicts with keys:
      quest, progress (0..target), target, completed, claimed, percent
    """
    now = datetime.utcnow()
    period = period_key_for(quest_type, now)

    # Auto-assign + auto-claim previous period (best-effort, not fatal)
    try:
        ensure_quests_assigned(session, user_id, quest_type)
    except Exception:
        logger.exception("ensure_quests_assigned failed")

    # Fetch only the user's assigned quests for the current period
    assigned_rows = (session.query(UserQuestProgress, Quest)
                     .join(Quest, Quest.id == UserQuestProgress.quest_id)
                     .filter(UserQuestProgress.user_id == user_id,
                             UserQuestProgress.period_key == period,
                             UserQuestProgress.assigned == True,
                             Quest.quest_type == quest_type,
                             Quest.is_active == True)
                     .order_by(Quest.sort_order, Quest.id).all())

    out = []
    for uqp, q in assigned_rows:
        progress = uqp.progress or 0
        percent = min(100, int(progress * 100 / q.target_count)) if q.target_count else 0
        out.append({
            "quest": q,
            "progress": progress,
            "target": q.target_count,
            "completed": uqp.completed,
            "claimed": uqp.claimed,
            "percent": percent,
            "uqp_id": uqp.id,
        })
    return out


def consume_pending_auto_claims(session, user_id, quest_type):
    """Return any auto-claims that happened on the most recent assignment
    rollover. Returns list of dicts and clears the marker.

    Note: this is a separate function so handlers can show the user "you
    auto-earned X coins from yesterday" notifications. It runs the rollover
    if it hasn't happened yet.
    """
    result = ensure_quests_assigned(session, user_id, quest_type)
    return result.get("auto_claimed", [])


def claim_quest_reward(session, user_id, quest_id):
    """User claims the reward for a completed quest.
    Returns (success, message, reward_dict).
    """
    user = session.query(User).get(user_id)
    if not user:
        return False, "User not found.", None

    quest = session.query(Quest).get(quest_id)
    if not quest:
        return False, "Quest not found.", None

    period = period_key_for(quest.quest_type)
    uqp = (session.query(UserQuestProgress)
           .filter(UserQuestProgress.user_id == user_id,
                   UserQuestProgress.quest_id == quest_id,
                   UserQuestProgress.period_key == period).first())
    if not uqp:
        return False, "No progress on this quest.", None

    if not uqp.completed:
        return False, f"Not yet complete: {uqp.progress}/{quest.target_count}.", None

    if uqp.claimed:
        return False, "Already claimed.", None

    # Award rewards
    user.quest_points = (user.quest_points or 0) + (quest.reward_points or 0)
    if quest.reward_coins:
        user.total_coins = (user.total_coins or 0) + quest.reward_coins
    if quest.reward_gems:
        user.total_gems = (user.total_gems or 0) + quest.reward_gems
    uqp.claimed = True
    uqp.claimed_at = datetime.utcnow()

    reward = {
        "points": quest.reward_points or 0,
        "coins": quest.reward_coins or 0,
        "gems": quest.reward_gems or 0,
    }
    return True, "Reward claimed!", reward


def claim_all_completed(session, user_id, quest_type):
    """Claim rewards for ALL completed but unclaimed quests of given type.
    Returns (count_claimed, total_reward_dict)."""
    items = get_user_quests(session, user_id, quest_type)
    total = {"points": 0, "coins": 0, "gems": 0}
    count = 0
    for it in items:
        if it["completed"] and not it["claimed"]:
            ok, _, reward = claim_quest_reward(session, user_id, it["quest"].id)
            if ok and reward:
                count += 1
                for k in total:
                    total[k] += reward[k]
    return count, total


# ════════════════════════════════════════════════════════════════════
# Standard event helpers (call these from the relevant handlers)
# ════════════════════════════════════════════════════════════════════

def safe_track(session, user_id, event_key, count=1, mode="add"):
    """Wrapper that swallows any exception — quest tracking should never break
    the calling handler if a quest service bug happens."""
    try:
        return track_event(session, user_id, event_key, count, mode=mode)
    except Exception:
        logger.exception(f"safe_track failed: user={user_id} event={event_key}")
        return []
