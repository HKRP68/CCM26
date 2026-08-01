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

Career Player event_keys (fired only for the user's own /cmucareer card, and
only ever consumed by quests flagged career_only):
  'career_match_played'   — the career player featured in a completed match
  'career_runs_scored'    — runs the career player made (cumulative)
  'career_wickets_taken'  — wickets the career player took (cumulative)
  'career_fifty'          — career player passed 50 in a match
  'career_hundred'        — career player passed 100 in a match
  'career_sixes_hit'      — sixes the career player hit (cumulative)
  'career_potm'           — career player was Player of the Match

Manual event_key:
  'manual' — quest is admin-only progressed (e.g. yorker counts, super overs).
             Admin can bump UserQuestProgress.progress directly via the website."""

import logging
import random
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError

from models import Quest, UserQuestProgress, User

logger = logging.getLogger(__name__)

# Constants
DAILY_DEFAULT_REWARD_POINTS = 5
WEEKLY_DEFAULT_REWARD_POINTS = 8
MONTHLY_DEFAULT_REWARD_POINTS = 10

# Every quest cadence the system understands.
QUEST_TYPES = ("daily", "weekly", "monthly")

# How many quests are randomly assigned per period per user.
# These are the only quests that count for tracking + appear in /mq.
DAILY_QUESTS_PER_USER = 3
WEEKLY_QUESTS_PER_USER = 3
MONTHLY_QUESTS_PER_USER = 5

QUESTS_PER_USER = {
    "daily": DAILY_QUESTS_PER_USER,
    "weekly": WEEKLY_QUESTS_PER_USER,
    "monthly": MONTHLY_QUESTS_PER_USER,
}


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


def _user_has_career(session, user_id):
    """True when the user owns a Career Player (/cmucareer)."""
    try:
        from services.career_service import get_career_player
        return get_career_player(session, user_id) is not None
    except Exception:
        logger.exception("career player lookup failed")
        return False


def _evaluate_career_streak(session, user, current_period, now=None):
    """Judge the week that just closed and update the user's career streak.

    A week counts only if the user was assigned at least one career quest and
    completed *every* one of them. Miss one and the streak resets to zero.
    Every ``career_streak_weeks`` consecutive weeks pays a gem jackpot.

    Idempotent: ``career_weekly_last_period`` records the last week already
    judged, so repeated calls in the same week change nothing.
    """
    now = now or datetime.utcnow()
    previous_period = weekly_period_key(now - timedelta(days=7))
    if (user.career_weekly_last_period or "") >= previous_period:
        return None
    user.career_weekly_last_period = previous_period

    rows = (session.query(UserQuestProgress, Quest)
            .join(Quest, Quest.id == UserQuestProgress.quest_id)
            .filter(UserQuestProgress.user_id == user.id,
                    UserQuestProgress.assigned == True,
                    UserQuestProgress.period_key == previous_period,
                    Quest.quest_type == "weekly",
                    Quest.career_only == True)
            .all())
    if not rows:
        # Nothing to judge — a user who had no career quests that week (no
        # career player yet, say) neither builds nor loses a streak.
        return None

    cleared = all(uqp.completed for uqp, _ in rows)
    if not cleared:
        had = user.career_weekly_streak or 0
        user.career_weekly_streak = 0
        return {"cleared": False, "streak": 0, "lost": had, "bonus": 0}

    streak = (user.career_weekly_streak or 0) + 1
    user.career_weekly_streak = streak
    user.career_weekly_best_streak = max(user.career_weekly_best_streak or 0, streak)

    try:
        from services.config_service import get_config
        cfg = get_config(session)
        weeks = int(cfg.get("career_streak_weeks") or 4)
        bonus_gems = int(cfg.get("career_streak_bonus_gems") or 100)
    except Exception:
        logger.exception("career streak config unavailable; using defaults")
        weeks, bonus_gems = 4, 100

    bonus = 0
    if weeks > 0 and streak % weeks == 0 and bonus_gems > 0:
        bonus = bonus_gems
        user.total_gems = (user.total_gems or 0) + bonus
        try:
            from services.activity_service import log_activity
            log_activity(session, user.id, "career_streak",
                         f"{streak}-week Career quest streak — {bonus} gem jackpot",
                         gems_change=bonus)
        except Exception:
            logger.exception("career streak activity log failed")

    return {"cleared": True, "streak": streak, "lost": 0, "bonus": bonus,
            "weeks": weeks}


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
        max_count = QUESTS_PER_USER.get(quest_type, MONTHLY_QUESTS_PER_USER)

    now = datetime.utcnow()
    current_period = period_key_for(quest_type, now)

    user = session.query(User).get(user_id)
    if not user:
        return {"assigned": [], "auto_claimed": []}

    # Career quests only make sense for users who have a Career Player.
    has_career = _user_has_career(session, user_id)

    # Have we already assigned for the current period?
    already_assigned = (session.query(UserQuestProgress)
                        .join(Quest, Quest.id == UserQuestProgress.quest_id)
                        .filter(UserQuestProgress.user_id == user_id,
                                UserQuestProgress.period_key == current_period,
                                UserQuestProgress.assigned == True,
                                Quest.quest_type == quest_type)
                        .count())
    if already_assigned > 0:
        # User already has their random set for this period. BUT a pinned quest
        # (always_assign) might have been added after their assignment — make
        # sure those are present too, so newly-pinned quests appear immediately.
        pinned_now = (session.query(Quest)
                      .filter(Quest.quest_type == quest_type,
                              Quest.is_active == True,
                              Quest.always_assign == True)
                      .all())
        pinned_now = [q for q in pinned_now
                      if has_career or not getattr(q, "career_only", False)]
        newly_pinned = []
        for q in pinned_now:
            exists = (session.query(UserQuestProgress)
                      .filter(UserQuestProgress.user_id == user_id,
                              UserQuestProgress.quest_id == q.id,
                              UserQuestProgress.period_key == current_period,
                              UserQuestProgress.assigned == True)
                      .first())
            if not exists:
                uqp = UserQuestProgress(
                    user_id=user_id, quest_id=q.id, period_key=current_period,
                    progress=0, completed=False, claimed=False,
                    assigned=True, last_updated=now,
                )
                session.add(uqp)
                newly_pinned.append(q)
        if newly_pinned:
            session.flush()
        return {"assigned": newly_pinned, "auto_claimed": []}

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

    # ── Career weekly streak ──
    # The week that just closed is now final, so judge it before handing out a
    # new set. Doing it here means no cron, and career_weekly_last_period keeps
    # it to exactly once per user per week.
    streak = None
    if quest_type == "weekly":
        streak = _evaluate_career_streak(session, user, current_period, now)

    # ── Pick the new random set ──
    pool = (session.query(Quest)
            .filter(Quest.quest_type == quest_type,
                    Quest.is_active == True)
            .all())
    pool = [q for q in pool
            if has_career or not getattr(q, "career_only", False)]
    if not pool:
        return {"assigned": [], "auto_claimed": auto_claimed,
                "career_streak": streak}

    # Pinned quests (always_assign=True) are ALWAYS included for every user.
    # They don't consume a random slot — they're added on top.
    pinned = [q for q in pool if getattr(q, "always_assign", False)]
    random_pool = [q for q in pool if not getattr(q, "always_assign", False)]

    chosen = list(pinned)  # start with all pinned
    if random_pool:
        chosen += random.sample(random_pool, min(max_count, len(random_pool)))

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
    return {"assigned": assigned_quests, "auto_claimed": auto_claimed,
            "career_streak": streak}


def weekly_period_key(now=None):
    """ISO week key ('YYYY-Wnn') for the current week in UTC.

    ISO weeks start on Monday, so weekly quests roll over at Monday 00:00 UTC.
    Like every other period key this is evaluated lazily on read — there is no
    cron; a user's first quest read in a new week rolls them over.
    """
    return (now or datetime.utcnow()).strftime("%G-W%V")


def daily_period_key(now=None):
    """YYYY-MM-DD for the current day in UTC."""
    return (now or datetime.utcnow()).strftime("%Y-%m-%d")


def monthly_period_key(now=None):
    """YYYY-MM for the current month in UTC."""
    return (now or datetime.utcnow()).strftime("%Y-%m")


def period_key_for(quest_type, now=None):
    if quest_type == "daily":
        return daily_period_key(now)
    if quest_type == "weekly":
        return weekly_period_key(now)
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
    coins = quest.reward_coins or 0
    # Apply active event coin multiplier
    try:
        from services.event_service import apply_coin_multiplier
        coins, _m = apply_coin_multiplier(session, coins)
    except Exception:
        pass
    if coins:
        user.total_coins = (user.total_coins or 0) + coins
    if quest.reward_gems:
        user.total_gems = (user.total_gems or 0) + quest.reward_gems
    uqp.claimed = True
    uqp.claimed_at = datetime.utcnow()

    # Quests contribute to the monthly season
    try:
        from services.season_service import safe_add_season_points
        safe_add_season_points(session, user, points=10)
    except Exception:
        pass

    reward = {
        "points": quest.reward_points or 0,
        "coins": coins,
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


def track_user_match_quests(session, state, user, is_winner, is_vsbot, winner_uid):
    """Fire every per-user match-end quest event for ``user`` from a completed
    match ``state``.

    Shared by the in-chat finalize (``handlers.match``) and the Mini App finalize
    (``services.match_webapp_service.finalize_webapp_match``) so /wpm and /wpmbot
    track exactly the same quests as /vsbot and PvP. ``state`` uses the common
    shape (``inn1_*`` snapshots plus the live 2nd-innings stats). The bot user
    (telegram_id == -1) is a no-op. Each event is best-effort via ``safe_track``;
    callers still wrap this in their own try/except.
    """
    from models import UserRoster

    if not user or getattr(user, "telegram_id", None) == -1:
        return

    uid = user.id
    safe_track(session, uid, "match_played", 1)
    if is_vsbot:
        safe_track(session, uid, "vsbot_played", 1)
    if is_winner:
        safe_track(session, uid, "match_won", 1)
        if is_vsbot:
            safe_track(session, uid, "vsbot_won", 1)

    # Aggregates across all of this user's players (both innings)
    runs_total = wkts_total = fifties = hundreds = 0
    sixes_total = fours_total = 0
    hattricks_total = 0
    maidens_total = 0
    # Single-match maxes
    max_runs_in_innings = 0
    max_wickets_in_match = 0
    max_sixes_in_innings = 0
    max_boundaries_in_innings = 0
    # Per-match accumulators (best single match, not summed across innings)
    user_match_runs = 0
    user_match_wkts = 0
    user_match_not_outs = 0
    cleanest_econ = None
    had_clean_spell = False
    # Career Player totals, accumulated alongside the squad-wide ones in the
    # same pass — the loop already resolves each XI slot to a UserRoster row,
    # so recognising the career card costs one extra id lookup, not a query.
    career_player_id = None
    try:
        from services.career_service import get_career_player
        career = get_career_player(session, uid)
        career_player_id = career.id if career else None
    except Exception:
        logger.exception("career player lookup failed for quest tracking")
    career_runs = career_wickets = career_sixes = 0
    career_fifties = career_hundreds = 0
    career_played = False

    for xi_key, stats_key, is_bat in [
        ("inn1_bat_xi", "inn1_bat_stats", True),
        ("inn1_bowl_xi", "inn1_bowl_stats", False),
        ("bat_xi", "bat_stats", True),
        ("bowl_xi", "bowl_stats", False),
    ]:
        xi = state.get(xi_key, [])
        stats = state.get(stats_key, {}) or {}
        for p in xi:
            rid = p.get("roster_id")
            if rid is None or rid <= 0:
                continue
            # Live state round-trips through JSON, so stat keys may be strings
            # while roster_id in the XI stays an int — look up both.
            pst = stats.get(rid)
            if pst is None:
                pst = stats.get(str(rid))
            if not pst:
                continue
            ur = session.query(UserRoster).get(rid)
            if not ur or ur.user_id != uid:
                continue
            is_career_slot = (career_player_id is not None
                              and ur.player_id == career_player_id)
            if is_career_slot:
                career_played = True
                if is_bat:
                    runs_here = pst.get("runs", 0)
                    career_runs += runs_here
                    career_sixes += pst.get("sixes", 0)
                    if runs_here >= 100:
                        career_hundreds += 1
                    elif runs_here >= 50:
                        career_fifties += 1
                else:
                    career_wickets += pst.get("wickets", 0)
            if is_bat:
                r = pst.get("runs", 0)
                balls = pst.get("balls", 0)
                runs_total += r
                user_match_runs += r
                max_runs_in_innings = max(max_runs_in_innings, r)
                if r >= 100:
                    hundreds += 1
                elif r >= 50:
                    fifties += 1
                if balls > 0 and not pst.get("out"):
                    user_match_not_outs += 1
                sixes_p = pst.get("sixes", 0)
                fours_p = pst.get("fours", 0)
                sixes_total += sixes_p
                fours_total += fours_p
                max_sixes_in_innings = max(max_sixes_in_innings, sixes_p)
                max_boundaries_in_innings = max(
                    max_boundaries_in_innings, sixes_p + fours_p)
            else:
                w = pst.get("wickets", 0)
                wkts_total += w
                user_match_wkts += w
                max_wickets_in_match = max(max_wickets_in_match, w)
                if pst.get("hattrick"):
                    hattricks_total += 1
                maidens_total += pst.get("maidens", 0)
                balls_b = pst.get("balls", 0)
                runs_b = pst.get("runs", 0)
                if balls_b >= 24:  # 4+ overs
                    econ = (runs_b * 6.0) / balls_b if balls_b else 999
                    if cleanest_econ is None or econ < cleanest_econ:
                        cleanest_econ = econ
                    if pst.get("maidens", 0) >= 1 and w >= 3:
                        had_clean_spell = True

    # Cumulative events
    if runs_total > 0:
        safe_track(session, uid, "runs_scored", runs_total)
    if wkts_total > 0:
        safe_track(session, uid, "wickets_taken", wkts_total)
    if sixes_total > 0:
        safe_track(session, uid, "sixes_hit", sixes_total)
    if (sixes_total + fours_total) > 0:
        safe_track(session, uid, "boundaries_hit", sixes_total + fours_total)
    for _ in range(fifties):
        safe_track(session, uid, "fifty", 1)
    for _ in range(hundreds):
        safe_track(session, uid, "hundred", 1)
    for _ in range(hattricks_total):
        safe_track(session, uid, "hattrick", 1)
    if maidens_total > 0:
        safe_track(session, uid, "maiden_over", maidens_total)
    for _ in range(user_match_not_outs):
        safe_track(session, uid, "not_out_innings", 1)

    # Career Player events — only fired when the user's own career card
    # actually featured, and only ever consumed by career_only quests.
    if career_played:
        safe_track(session, uid, "career_match_played", 1)
        if career_runs > 0:
            safe_track(session, uid, "career_runs_scored", career_runs)
        if career_wickets > 0:
            safe_track(session, uid, "career_wickets_taken", career_wickets)
        if career_sixes > 0:
            safe_track(session, uid, "career_sixes_hit", career_sixes)
        for _ in range(career_fifties):
            safe_track(session, uid, "career_fifty", 1)
        for _ in range(career_hundreds):
            safe_track(session, uid, "career_hundred", 1)

    # Single-match max events
    if max_runs_in_innings > 0:
        safe_track(session, uid, "runs_in_innings", max_runs_in_innings, mode="max")
    if max_wickets_in_match > 0:
        safe_track(session, uid, "wickets_in_match", max_wickets_in_match, mode="max")
    if max_sixes_in_innings > 0:
        safe_track(session, uid, "sixes_in_match", max_sixes_in_innings, mode="max")
    if max_boundaries_in_innings > 0:
        safe_track(session, uid, "boundaries_in_match", max_boundaries_in_innings,
                   mode="max")

    # Allrounder match: 30+ runs AND 2+ wickets in same game
    if user_match_runs >= 30 and user_match_wkts >= 2:
        safe_track(session, uid, "allrounder_match", 1)

    # Economy tier triggers (cumulative count of "clean spells")
    if cleanest_econ is not None:
        if cleanest_econ < 4.5:
            safe_track(session, uid, "economy_under_4_5", 1)
        if cleanest_econ < 5.0:
            safe_track(session, uid, "economy_under_5", 1)
        if cleanest_econ < 6.0:
            safe_track(session, uid, "economy_under_6", 1)
        if cleanest_econ < 7.0:
            safe_track(session, uid, "economy_under_7", 1)

    if had_clean_spell:
        safe_track(session, uid, "clean_spell", 1)

    # Chase win: user won AND batted second ("bat_xi" is the 2nd-innings lineup).
    if is_winner and state.get("bat_team_id") == uid:
        safe_track(session, uid, "chase_won", 1)
