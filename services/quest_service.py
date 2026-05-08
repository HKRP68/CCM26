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
from datetime import datetime
from sqlalchemy.exc import IntegrityError

from models import Quest, UserQuestProgress, User

logger = logging.getLogger(__name__)

# Constants
DAILY_DEFAULT_REWARD_POINTS = 5
MONTHLY_DEFAULT_REWARD_POINTS = 10


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
            if not uqp:
                uqp = UserQuestProgress(
                    user_id=user_id, quest_id=q.id, period_key=period,
                    progress=0, completed=False, claimed=False,
                    last_updated=now,
                )
                session.add(uqp)
                session.flush()

            if uqp.claimed:
                continue

            # Apply mode
            if mode == "max":
                # Set progress to max(current, count), capped at target
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
    """Return all quests for the type with the user's progress for the current period.

    Returns: list of dicts with keys:
      quest, progress (0..target), target, completed, claimed, percent
    """
    now = datetime.utcnow()
    period = period_key_for(quest_type, now)

    quests = (session.query(Quest)
              .filter(Quest.quest_type == quest_type,
                      Quest.is_active == True)
              .order_by(Quest.sort_order, Quest.id).all())

    out = []
    for q in quests:
        uqp = (session.query(UserQuestProgress)
               .filter(UserQuestProgress.user_id == user_id,
                       UserQuestProgress.quest_id == q.id,
                       UserQuestProgress.period_key == period).first())
        progress = uqp.progress if uqp else 0
        completed = uqp.completed if uqp else False
        claimed = uqp.claimed if uqp else False
        percent = min(100, int(progress * 100 / q.target_count)) if q.target_count else 0
        out.append({
            "quest": q,
            "progress": progress,
            "target": q.target_count,
            "completed": completed,
            "claimed": claimed,
            "percent": percent,
            "uqp_id": uqp.id if uqp else None,
        })
    return out


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
