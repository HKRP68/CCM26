"""Quest service — tracks user progress on quest events, handles claims.

Event-driven design:
  - Whenever the user does something (claims a player, wins a match, etc.),
    the relevant handler calls track_event(user_id, event_key, count).
  - This service finds all active quests matching that event_key for the
    current period (today / this month) and increments user progress.
  - When progress reaches target_count, quest is marked completed (auto).
  - User must explicitly press "Claim" to receive the reward.

Which matches count
-------------------
:func:`match_counts_for_quests` is the single gate every finalize goes through,
and it turns quest tracking off for

  * spectator and bot-vs-bot matches, which nobody is playing,
  * unranked practice, which already pays no coins, gems, W/L or streak, and
  * matches the fair-match gate voided because the two Team Overalls are
    ``player_stats_service.STATS_FAIRNESS_OVR_GAP`` or more apart.

Ranked matches against the AI (``/wpmbot`` and ``/vsbot``, the two that pay
coins, gems and a Win/Loss record) DO count, but only for the first
:data:`BOT_QUEST_MATCH_DAILY_CAP` of them each day — see
:func:`consume_bot_quest_allowance`. A player with nobody online should still be
able to finish "play 3 matches today" against the bot; what the cap stops is a
whole daily card being farmed off an opponent that is always available and never
refuses. The allowance is spent per user per UTC day, in the finalize, by every
bot match that reaches the tracker — whether or not the player happens to hold a
quest it would have moved.

``/lpbot`` and ``/ciplbot`` remain outside all of this: they are explicitly
unranked practice (``stats_disabled``/``unranked``, set by
``handlers.cipl_play.mark_bot_match``) and already pay nothing at all, so they
stay refused by the gate below along with every other voided match.

Cumulative keys count over the *quest's own period* — a ``wickets_taken``
quest is "wickets today" as a daily, "wickets this week" as a weekly and
"wickets this month" as a monthly. The period comes from the quest, never from
the event.

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

Per-match event_keys (added for the v2 quest list):
  'sixes_hit'             — fired with N sixes at match end (cumulative quests)
  'sixes_in_match'        — fired with max(N) once if N >= target (single-match quests)
  'boundaries_hit'        — fired with N (4s+6s) at match end
  'boundaries_in_match'   — fired with N once if N >= target
  'fours_hit'             — fired with N fours at match end (cumulative)
  'wickets_in_match'      — fired with N once at match end (single-match)
  'runs_in_innings'       — fired with N once at match end (single-match)
  'hattrick'              — fired once if at least one bowler took a hat-trick
  'three_fer'             — once per bowler who took 3+ wickets in the match
  'five_fer'              — once per bowler who took 5+ wickets in the match
  'dot_balls'             — fired with N dot balls bowled (cumulative)
  'powerplay_runs'        — team runs in your first 6 overs (single-innings MAX)
  'death_over_runs'       — team runs in your last 5 overs (single-innings MAX)
  'defended_total'        — fired once when you won batting first
  'super_over_won'        — fired when you win a Super Over

Bot-match event_keys (fire only when the match was against the AI, and only
while the player still has bot-match quest allowance left for the day):
  'vsbot_played'  — finished a match against the AI
  'vsbot_won'     — …and won it

Pitch event_keys — one per surface in services.match_constants.PITCH_TYPES,
fired once at match end for the pitch the match was played on, so a quest can
ask for "3 matches on a Green pitch today". The key is
``pitch_<surface lowercased>``; see PITCH_EVENT_KEYS.
  'pitch_dry', 'pitch_dusty', 'pitch_hard', 'pitch_even', 'pitch_flat',
  'pitch_green', 'pitch_bouncy'

Career Player event_keys (fired only for the user's own /cmucareer card, and
only ever consumed by quests flagged career_only). Every one is derived from
the same per-player match stats the scorecards read, so nothing here depends on
the engine recording anything new:

  Appearance
  'career_match_played'   — the career player featured in a completed match
  'career_match_won'      — …and that match was won
  'career_potm'           — career player was Player of the Match

  Batting (cumulative)
  'career_runs_scored'    — runs the career player made
  'career_chase_runs'     — runs made batting second
  'career_boundary_runs'  — runs that came in fours and sixes (4x4 + 6x6)
  'career_quickfire_runs' — runs from innings of 10+ balls at a strike rate
                            of 150 or better; slower innings contribute none
  'career_fours_hit'      — fours hit
  'career_sixes_hit'      — sixes hit
  'career_balls_faced'    — legal balls faced

  Batting (once per qualifying innings)
  'career_fifty'          — passed 50
  'career_hundred'        — passed 100
  'career_score_25_plus'  — reached 25, the "did the job" threshold
  'career_score_75_plus'  — reached 75
  'career_not_out'        — faced a ball and finished unbeaten

  Bowling (cumulative)
  'career_wickets_taken'  — wickets taken
  'career_dot_balls'      — dot balls bowled
  'career_maiden_overs'   — maiden overs bowled
  'career_balls_bowled'   — legal balls bowled

  Bowling (once per qualifying match/spell)
  'career_three_fer'      — 3+ wickets in a match
  'career_five_fer'       — 5+ wickets in a match
  'career_hattrick'       — took a hat-trick
  'career_economy_spell'  — 4+ overs at an economy under 6

  All-round
  'career_allrounder_match' — 25+ runs AND 2+ wickets in the same match

Manual event_key:
  'manual' — quest is admin-only progressed (e.g. yorker counts, super overs).
             Admin can bump UserQuestProgress.progress directly via the website."""

import logging
import os
import random
from datetime import datetime, timedelta
from sqlalchemy import func
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

# Career weekly quests are drawn on top of the ordinary weekly ones, not out of
# the same slots. Sharing them meant a career owner could go a whole week
# without a single career quest — which also stalls the streak, since a week
# with no career quests assigned is a week that cannot be judged.
CAREER_QUESTS_PER_USER = 5

# …and the five are drawn one bucket at a time rather than five at random, so
# nobody spends a week on five bowling quests. Buckets are read off the event
# key, so a new quest lands in the right one with no extra admin field to set.
CAREER_BAT_EVENTS = frozenset({
    "career_runs_scored", "career_chase_runs", "career_boundary_runs",
    "career_quickfire_runs", "career_fours_hit", "career_sixes_hit",
    "career_balls_faced", "career_fifty", "career_hundred",
    "career_score_25_plus", "career_score_75_plus", "career_not_out",
})
CAREER_BOWL_EVENTS = frozenset({
    "career_wickets_taken", "career_dot_balls", "career_maiden_overs",
    "career_balls_bowled", "career_three_fer", "career_five_fer",
    "career_hattrick", "career_economy_spell",
})
CAREER_ALL_EVENTS = frozenset({
    "career_match_played", "career_match_won", "career_potm",
    "career_allrounder_match",
})

# (bucket, how many of the five come from it). Two batting, two bowling and one
# all-round mirrors what a Career Player actually is — always an All-rounder.
CAREER_QUEST_MIX = (
    (CAREER_BAT_EVENTS, 2),
    (CAREER_BOWL_EVENTS, 2),
    (CAREER_ALL_EVENTS, 1),
)


# How many AI matches may feed a user's quests each UTC day. Enough to finish a
# "play 3 matches today" card when nobody is online to play; not enough to farm
# a whole daily/monthly board off an opponent that never says no. 0 turns AI
# quest progress off entirely. A malformed env value falls back to the default
# rather than taking the whole import down with it.
def _bot_quest_cap(default=3):
    raw = os.getenv("BOT_QUEST_MATCH_DAILY_CAP")
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("BOT_QUEST_MATCH_DAILY_CAP=%r is not an integer — "
                       "using the default of %d", raw, default)
        return default


BOT_QUEST_MATCH_DAILY_CAP = _bot_quest_cap()

# Surfaces a match can be played on, and the quest event each one fires. Sourced
# from services.match_constants.PITCH_TYPES so adding a pitch there adds its
# quest key here, with no second list to keep in step.
try:
    from services.match_constants import PITCH_TYPES as _PITCH_TYPES
except Exception:  # pragma: no cover - import cycle / partial deploy safety
    _PITCH_TYPES = ["Dry", "Dusty", "Hard", "Even", "Flat", "Green", "Bouncy"]


def pitch_event_key(pitch_type) -> str | None:
    """'Green' → 'pitch_green'. None for a blank/unknown surface.

    Normalised on the way in (case and surrounding space), because the pitch on
    a match state comes from a mix of admin input, a captain's pick and
    ``random_match_settings``.
    """
    name = str(pitch_type or "").strip().lower()
    if not name:
        return None
    return "pitch_" + name.replace(" ", "_")


PITCH_EVENT_KEYS = {p: pitch_event_key(p) for p in _PITCH_TYPES}


def is_bot_match(state, *, is_vsbot=False) -> bool:
    """True when one side of this match was the AI."""
    s = state or {}
    return bool(is_vsbot or s.get("is_vsbot") or s.get("is_bot_match"))


def match_counts_for_quests(state, *, is_vsbot=False):
    """True when a finished match may feed match-end quest progress.

    One gate for every finalize — the in-chat one (``handlers.match``), the
    Mini App one (``services.match_webapp_service``), Challenge League and
    LetsPlay (``handlers.cipl_play``) and the Super Over decider
    (``handlers.super_over``) — so a quest counts the same wherever the match
    was played, and a mode added later has one place to opt in.

    A match is refused when:

    * ``is_spectator`` / ``is_bot_vs_bot`` — nobody played it.
    * ``stats_disabled`` — the fair-match gate voided the match because the two
      Team Overalls were too far apart (see
      ``player_stats_service.STATS_FAIRNESS_OVR_GAP``, currently 10). Such a
      match already earns no career stats, coins, gems, W/L or streak; letting
      it still tick quests along would leave exactly the stat-farming hole the
      gate exists to close. Unranked practice sets this flag too, which is the
      same answer for the same reason.

    Matches against the AI pass this gate — ``/wpmbot`` and friends complete
    quests. Their *daily* limit is enforced separately, per user, by
    :func:`consume_bot_quest_allowance`, because it needs a user row and a
    write; this function stays a pure predicate that any caller can ask.

    ``state`` may be ``{}`` or ``None`` — an unknown match counts, so a caller
    that cannot supply state does not silently lose every quest.
    """
    s = state or {}
    if s.get("is_spectator") or s.get("is_bot_vs_bot"):
        return False
    if s.get("stats_disabled") or s.get("unranked"):
        return False
    return True


def consume_bot_quest_allowance(session, user) -> bool:
    """Spend one of ``user``'s daily AI-match quest slots. True if they had one.

    Called once per user per finished bot match, immediately before that match's
    quest events fire. Returns False once the day's allowance is gone, which is
    the caller's signal to skip tracking entirely — so the 4th bot match of the
    day still pays coins and gems, it just stops moving quest bars.

    The counter lives on the User row (``bot_quest_matches_today`` plus the UTC
    date it belongs to) and resets lazily on the first bot match of a new day,
    the same pattern Quick Match's daily limit uses. Caller commits.
    """
    if user is None:
        return False
    if BOT_QUEST_MATCH_DAILY_CAP <= 0:
        return False
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if getattr(user, "bot_quest_matches_date", None) != today:
        user.bot_quest_matches_date = today
        user.bot_quest_matches_today = 0
    used = user.bot_quest_matches_today or 0
    if used >= BOT_QUEST_MATCH_DAILY_CAP:
        return False
    user.bot_quest_matches_today = used + 1
    return True


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


# How far back a returning user's unjudged weeks are caught up. Weeks with no
# assigned career quests cost one cheap query each and change nothing, so this
# only has to be generous enough to cover a realistic absence.
_MAX_CATCHUP_WEEKS = 60


def _unjudged_weeks(last_judged, now):
    """Week keys still to be judged, oldest first.

    A week is judged lazily, on the owner's first quest read after it closes.
    Somebody who does not open /mq for a month leaves three weeks unjudged in
    between, and one of those may be a week they were assigned career quests and
    failed them — so the marker cannot simply jump to the week that just closed
    or the streak would survive a week it should not have.

    With no marker at all there is no history to catch up on (the marker is set
    the first time a week is judged), so only the week that just closed is
    returned.
    """
    keys = []
    cursor = now - timedelta(days=7)
    limit = _MAX_CATCHUP_WEEKS if last_judged else 1
    for _ in range(limit):
        key = weekly_period_key(cursor)
        if last_judged and key <= last_judged:
            break
        keys.append(key)
        cursor -= timedelta(days=7)
    keys.reverse()
    return keys


def _judge_career_week(session, user, period, weeks, bonus_gems):
    """Judge one closed week against the user's career streak, or ``None``.

    A week counts only if the user was assigned at least one career quest and
    completed *every* one of them. Miss one and the streak resets to zero. Every
    ``weeks`` consecutive cleared weeks pays a ``bonus_gems`` jackpot.

    Deactivated quests are ignored. A quest retired mid-week — by the seeder
    after a rebalance, or by an admin — vanishes from ``get_user_quests`` and
    stops taking progress in ``track_event``, both of which filter on
    ``is_active``. Judging it here too would fail the owner on a quest they
    could no longer see, let alone finish.
    """
    rows = (session.query(UserQuestProgress, Quest)
            .join(Quest, Quest.id == UserQuestProgress.quest_id)
            .filter(UserQuestProgress.user_id == user.id,
                    UserQuestProgress.assigned == True,
                    UserQuestProgress.period_key == period,
                    Quest.quest_type == "weekly",
                    Quest.is_active == True,
                    Quest.career_only == True)
            .all())
    if not rows:
        # Nothing to judge — a user who had no career quests that week (no
        # career player yet, say) neither builds nor loses a streak.
        return None

    if not all(uqp.completed for uqp, _ in rows):
        had = user.career_weekly_streak or 0
        user.career_weekly_streak = 0
        return {"cleared": False, "streak": 0, "lost": had, "bonus": 0,
                "weeks": weeks}

    streak = (user.career_weekly_streak or 0) + 1
    user.career_weekly_streak = streak
    user.career_weekly_best_streak = max(user.career_weekly_best_streak or 0,
                                         streak)

    bonus = 0
    if weeks > 0 and streak % weeks == 0 and bonus_gems > 0:
        bonus = bonus_gems
        # Credit in SQL rather than read-modify-write on the ORM object, so a
        # concurrent gem change elsewhere in the app cannot swallow the payout.
        session.query(User).filter(User.id == user.id).update(
            {User.total_gems: func.coalesce(User.total_gems, 0) + bonus},
            synchronize_session=False)
        session.expire(user, ["total_gems"])
        try:
            from services.activity_service import log_activity
            log_activity(session, user.id, "career_streak",
                         f"{streak}-week Career quest streak — {bonus} gem jackpot",
                         gems_change=bonus)
        except Exception:
            logger.exception("career streak activity log failed")

    return {"cleared": True, "streak": streak, "lost": 0, "bonus": bonus,
            "weeks": weeks}


def _evaluate_career_streak(session, user, current_period, now=None):
    """Judge every closed-but-unjudged week and update the career streak.

    ``career_weekly_last_period`` records the most recent week already judged.
    It is claimed with a conditional UPDATE rather than an ORM assignment: two
    quest reads racing each other — the bot's /mq and the Mini App, say — would
    otherwise both read the old marker, both pass, and both pay the jackpot. The
    ``WHERE`` makes exactly one of them win, and the loser returns ``None``.
    """
    now = now or datetime.utcnow()
    previous_period = weekly_period_key(now - timedelta(days=7))
    last_judged = user.career_weekly_last_period or ""
    if last_judged >= previous_period:
        return None

    claimed = (session.query(User)
               .filter(User.id == user.id,
                       func.coalesce(User.career_weekly_last_period, "")
                       < previous_period)
               .update({User.career_weekly_last_period: previous_period},
                       synchronize_session=False))
    if not claimed:
        # Another session judged these weeks between our read and this UPDATE.
        session.expire(user, ["career_weekly_last_period"])
        return None
    session.expire(user, ["career_weekly_last_period"])

    try:
        from services.config_service import get_config
        cfg = get_config(session)
        weeks = int(cfg.get("career_streak_weeks") or 4)
        bonus_gems = int(cfg.get("career_streak_bonus_gems") or 100)
    except Exception:
        logger.exception("career streak config unavailable; using defaults")
        weeks, bonus_gems = 4, 100

    # Oldest first, so a failed week resets the streak before a later cleared
    # week starts building it again.
    result = None
    for period in _unjudged_weeks(last_judged, now):
        judged = _judge_career_week(session, user, period, weeks, bonus_gems)
        if judged is None:
            continue
        if result is not None:
            # Only the newest week is reported, but a jackpot from a skipped
            # week was still paid and must not vanish from the message.
            judged["bonus"] += result.get("bonus", 0)
            judged["lost"] = max(judged.get("lost", 0), result.get("lost", 0))
        result = judged
    return result


def _quests_assigned_in(session, user_id, period):
    """Quest ids the user was assigned in one past period."""
    rows = (session.query(UserQuestProgress.quest_id)
            .filter(UserQuestProgress.user_id == user_id,
                    UserQuestProgress.period_key == period,
                    UserQuestProgress.assigned == True)
            .all())
    return {row[0] for row in rows}


def _draw(candidates, count, seen):
    """Take ``count`` at random, preferring ones not already ``seen``.

    ``seen`` carries both last week's set and whatever this draw has already
    taken, so the buckets cannot hand out the same quest twice.
    """
    fresh = [q for q in candidates if q.id not in seen]
    stale = [q for q in candidates if q.id in seen]
    random.shuffle(fresh)
    random.shuffle(stale)
    picked = (fresh + stale)[:count]
    seen.update(q.id for q in picked)
    return picked


def _by_effort(quests):
    """Split a bucket into its lighter and heavier halves.

    ``reward_gems`` is the difficulty grade: the seeder prices a quest at the
    configured base times its tier, so what a quest pays *is* how much work it
    is. Target count breaks ties between quests on the same tier.
    """
    ordered = sorted(quests, key=lambda q: (q.reward_gems or 0,
                                            q.target_count or 0, q.id))
    mid = len(ordered) // 2
    return ordered[:mid], ordered[mid:]


def _draw_spread(candidates, count, seen):
    """Draw ``count`` from one bucket, spread across the difficulty range.

    A purely random draw regularly produced a week of five marathon quests —
    "win 8 matches", "take 5 in an innings", "bowl 60 overs" — which is not a
    hard week so much as a written-off one, and because the streak needs every
    quest cleared it also wrote off the jackpot. Taking half of each multi-slot
    bucket from the lighter end guarantees every week contains something
    achievable alongside the stretch goals.
    """
    lighter, heavier = _by_effort(candidates)
    if count < 2 or not lighter or not heavier:
        return _draw(candidates, count, seen)
    from_lighter = count // 2
    picked = _draw(lighter, from_lighter, seen)
    picked += _draw(heavier, count - len(picked), seen)
    # A half that ran dry (everything in it was already taken) must not cost a
    # slot — top up from whatever is left in the bucket as a whole.
    if len(picked) < count:
        rest = [q for q in candidates if q.id not in {p.id for p in picked}]
        picked += _draw(rest, count - len(picked), seen)
    return picked


def _pick_career_weeklies(session, user_id, career_pool, now, *, slots=None,
                          exclude=()):
    """Choose this week's Career Player quests: five, balanced, and new.

    ``slots`` overrides how many to draw, for the callers that have already
    filled part of the five — a pinned career quest, or a top-up part-way
    through a week. ``exclude`` holds quest ids the owner already has, so a
    top-up cannot deal the same quest twice.

    Three things shape the pick, in order:

    * **Balance.** Two batting, two bowling and one all-round, per
      :data:`CAREER_QUEST_MIX`. Five uniform draws from one catalogue regularly
      produced a week of nothing but bowling, which is a poor week for a card
      that is always an All-rounder.
    * **Range.** Each two-slot bucket takes one from its lighter half and one
      from its heavier half, so a week is never five marathons — see
      :func:`_draw_spread`.
    * **Freshness.** Quests the user was assigned last week go to the back of
      the queue, so the same five rarely land twice running.
    * **Never short.** A bucket with too few quests (or an admin who has
      deactivated most of a category) tops up from whatever is left, so the
      user still gets five whenever the catalogue holds five.
    """
    want = CAREER_QUESTS_PER_USER if slots is None else max(0, int(slots))
    if not want:
        return []

    exclude = set(exclude)
    career_pool = [q for q in career_pool if q.id not in exclude]
    last_week = _quests_assigned_in(
        session, user_id, weekly_period_key(now - timedelta(days=7)))

    seen = set(last_week)
    chosen = []
    # Scale the bucket mix to the slots actually going out, so a partly-filled
    # week still comes out as balanced as the room allows.
    for events, count in CAREER_QUEST_MIX:
        if len(chosen) >= want:
            break
        count = min(count, want - len(chosen))
        bucket = [q for q in career_pool if q.event_key in events]
        chosen += _draw_spread(bucket, count, seen)

    # Top up from anything not already taken — a short bucket must not cost the
    # user a slot, and a quest whose event key predates the buckets (or was
    # typed in by hand on the website) is still eligible here.
    taken = {q.id for q in chosen}
    if len(chosen) < want:
        rest = [q for q in career_pool if q.id not in taken]
        chosen += _draw(rest, want - len(chosen), set(last_week) | taken)
    return chosen[:want]


def _top_up_career_weeklies(session, user_id, quest_type, current_period, now,
                            has_career, *, already_pinned=()):
    """Deal any career quests a mid-week career owner is still short of.

    The weekly deal happens once, on the owner's first quest read of the week.
    Create a Career Player *after* that read and the week's five never arrive —
    so this fills the gap the moment they next open their quests, rather than
    leaving the new card with nothing to do until Monday.

    Returns the quests newly assigned (empty for everyone already up to date).
    """
    if quest_type != "weekly" or not has_career:
        return []

    held = (session.query(Quest)
            .join(UserQuestProgress, Quest.id == UserQuestProgress.quest_id)
            .filter(UserQuestProgress.user_id == user_id,
                    UserQuestProgress.period_key == current_period,
                    UserQuestProgress.assigned == True,
                    Quest.quest_type == "weekly",
                    Quest.career_only == True)
            .all())
    held_ids = {q.id for q in held} | {q.id for q in already_pinned
                                       if getattr(q, "career_only", False)}
    missing = CAREER_QUESTS_PER_USER - len(held_ids)
    if missing <= 0:
        return []

    pool = (session.query(Quest)
            .filter(Quest.quest_type == "weekly",
                    Quest.is_active == True,
                    Quest.career_only == True,
                    Quest.always_assign == False)
            .all())
    dealt = _pick_career_weeklies(session, user_id, pool, now, slots=missing,
                                  exclude=held_ids)
    for quest in dealt:
        session.add(UserQuestProgress(
            user_id=user_id, quest_id=quest.id, period_key=current_period,
            progress=0, completed=False, claimed=False, assigned=True,
            last_updated=now))
    if dealt:
        logger.info("topped up %s career weekly quest(s) for user %s",
                    len(dealt), user_id)
    return dealt


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

        # Somebody who opened their quests on Monday and created a Career
        # Player on Tuesday had no career quests at all until the next Monday,
        # because the week's deal happened before they had a card. Top them up
        # now instead — the streak treats a week with no career quests as
        # unjudged, so this costs them nothing retroactively.
        newly_pinned += _top_up_career_weeklies(
            session, user_id, quest_type, current_period, now, has_career,
            already_pinned=newly_pinned)

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

    # A quest on the 'manual' event key has no trigger behind it: nothing in the
    # game ever calls track_event for it, so its bar sits at 0/N until an admin
    # types a number in by hand. Dealing those at random is most of why players
    # report quests "not counting" — a third of somebody's day could be spent on
    # a quest that cannot move. They stay in the catalogue (an admin can still
    # pin one and drive it manually), but they are no longer dealt by the draw.
    trackable = [q for q in random_pool if (q.event_key or "") != "manual"]
    if trackable:
        random_pool = trackable
    else:
        logger.warning(
            "no auto-tracked %s quests available — falling back to the manual "
            "ones so users are not left with an empty quest list", quest_type)

    # Career weekly quests get their own five slots rather than competing with
    # the ordinary weekly ones, so a Career Player owner always has a full
    # career card to work through — and always has a week that can be judged.
    career_pool = []
    career_slots = 0
    if quest_type == "weekly" and has_career:
        career_pool = [q for q in random_pool
                       if getattr(q, "career_only", False)]
        random_pool = [q for q in random_pool
                       if not getattr(q, "career_only", False)]
        # A career quest an admin has pinned already occupies one of the five.
        # The streak judge counts every assigned career_only row, so letting a
        # pinned one arrive *on top* would quietly make the week a six-quest
        # week for those owners — harder than the deal advertises, and harder
        # than it is for everybody else.
        career_slots = CAREER_QUESTS_PER_USER - sum(
            1 for q in pinned if getattr(q, "career_only", False))

    chosen = list(pinned)  # start with all pinned
    if random_pool:
        chosen += random.sample(random_pool, min(max_count, len(random_pool)))
    if career_pool and career_slots > 0:
        chosen += _pick_career_weeklies(session, user_id, career_pool, now,
                                        slots=career_slots)

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
# ADMIN — force a fresh deal for everybody
# ════════════════════════════════════════════════════════════════════

QUEST_TYPES = ("daily", "weekly", "monthly")


def reassign_all_users(session, quest_type, *, now=None):
    """Clear this period's ``quest_type`` quests for EVERY user so they re-deal.

    The website calls this after editing the catalogue: without it, a user who
    already opened their quests today keeps the old set until the period rolls,
    because :func:`ensure_quests_assigned` deliberately does nothing once an
    assignment exists. Wiping the current period's rows puts every user back in
    the "not dealt yet" state, and their next quest read draws a fresh set from
    the live catalogue.

    Progress is intentionally lost — that is what a reassign is. Rewards are
    not: any quest already completed but not yet claimed is auto-claimed and
    credited first, exactly as a period rollover would, so nobody is punished
    for not having tapped Claim before the admin pressed the button.

    Returns ``{"cleared": int, "auto_claimed": int, "points": int,
    "coins": int, "gems": int, "period": str}``.
    """
    now = now or datetime.utcnow()
    period = period_key_for(quest_type, now)

    quest_ids = [qid for (qid,) in
                 session.query(Quest.id).filter(Quest.quest_type == quest_type)]
    if not quest_ids:
        return {"cleared": 0, "auto_claimed": 0, "points": 0, "coins": 0,
                "gems": 0, "period": period}

    # Pay out what users already earned this period before it disappears.
    totals = {"points": 0, "coins": 0, "gems": 0}
    pending = (session.query(UserQuestProgress, Quest, User)
               .join(Quest, Quest.id == UserQuestProgress.quest_id)
               .join(User, User.id == UserQuestProgress.user_id)
               .filter(UserQuestProgress.period_key == period,
                       # Only a quest the user was actually dealt can pay out.
                       # ensure_quests_assigned's rollover claim filters the
                       # same way; a stale unassigned row is deleted below
                       # rather than credited.
                       UserQuestProgress.assigned.is_(True),
                       UserQuestProgress.completed.is_(True),
                       UserQuestProgress.claimed.is_(False),
                       UserQuestProgress.quest_id.in_(quest_ids))
               .all())
    auto_claimed = 0
    for uqp, quest, user in pending:
        rewards = _claim_silently(session, user, uqp, quest)
        if rewards:
            auto_claimed += 1
            totals["points"] += rewards["points"]
            totals["coins"] += rewards["coins"]
            totals["gems"] += rewards["gems"]
    session.flush()

    # Drop every row for the period, assigned or not, so the next read starts
    # from nothing and a re-picked quest can't inherit stale progress.
    cleared = (session.query(UserQuestProgress)
               .filter(UserQuestProgress.period_key == period,
                       UserQuestProgress.quest_id.in_(quest_ids))
               .delete(synchronize_session=False))

    return {"cleared": int(cleared or 0), "auto_claimed": auto_claimed,
            "period": period, **totals}


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

    The ``claimed`` flag is flipped by a conditional UPDATE *before* any
    currency moves, not by assigning to the ORM object afterwards. Reading
    ``uqp.claimed``, paying out, and then setting the flag left the entire
    payout sitting between the check and the write, so two taps that overlapped
    both read ``claimed == False`` and both paid. Now the UPDATE only matches a
    row that is still unclaimed: exactly one caller gets rowcount 1 and pays
    out, and the other is told it's already claimed.
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

    # Take the claim before paying it. Whoever flips the flag owns the reward;
    # anyone racing them matches no row here and leaves empty-handed.
    claimed_at = datetime.utcnow()
    won = (session.query(UserQuestProgress)
           .filter(UserQuestProgress.user_id == user_id,
                   UserQuestProgress.quest_id == quest_id,
                   UserQuestProgress.period_key == period,
                   UserQuestProgress.claimed == False)   # noqa: E712 (SQL, not Python)
           .update({UserQuestProgress.claimed: True,
                    UserQuestProgress.claimed_at: claimed_at},
                   synchronize_session=False))
    if not won:
        return False, "Already claimed.", None
    # Bring the in-session row in step with the UPDATE above.
    uqp.claimed = True
    uqp.claimed_at = claimed_at

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

    Shared by every finalize — in-chat (``handlers.match``), Mini App
    (``services.match_webapp_service.finalize_webapp_match``), LetsPlay and
    Challenge League (``handlers.cipl_play``) and the Super Over decider
    (``handlers.super_over``) — so /lp, /wpm, /cipl and tour matches all track
    the same quests off the same numbers. ``state`` uses the common shape
    (``inn1_*`` snapshots plus the live 2nd-innings stats). The bot user
    (telegram_id == -1) is a no-op. Each event is best-effort via ``safe_track``;
    callers still wrap this in their own try/except.

    Nothing fires at all unless :func:`match_counts_for_quests` says the match
    qualifies, so a spectator match or a Team-Overall mismatch leaves every
    quest exactly where it was. A match against the AI qualifies, but spends one
    of the day's :data:`BOT_QUEST_MATCH_DAILY_CAP` bot-match slots to do it, and
    fires nothing once those are gone.

    Player of the Match is worked out by the caller, not here, so a caller that
    wants ``career_potm`` to fire puts ``potm_player_id`` and
    ``potm_owner_user_id`` on ``state`` before calling. Both finalizes do.
    """
    from models import UserRoster

    if not user or getattr(user, "telegram_id", None) == -1:
        return
    if not match_counts_for_quests(state, is_vsbot=is_vsbot):
        return

    bot_match = is_bot_match(state, is_vsbot=is_vsbot)
    if bot_match and not consume_bot_quest_allowance(session, user):
        # Daily AI-match allowance spent — the match still paid out, it just
        # stops here rather than moving any quest bar.
        return

    uid = user.id
    safe_track(session, uid, "match_played", 1)
    if is_winner:
        safe_track(session, uid, "match_won", 1)
    if bot_match:
        # Bot-specific quests ("beat the AI twice today") — these only ever
        # fire from an AI match, so they are never reachable in a PvP game.
        safe_track(session, uid, "vsbot_played", 1)
        if is_winner:
            safe_track(session, uid, "vsbot_won", 1)

    # Pitch quests — "play 3 matches on a Green pitch today". One event per
    # match, keyed on the surface it was played on.
    pitch_key = pitch_event_key(state.get("pitch_type"))
    if pitch_key:
        safe_track(session, uid, pitch_key, 1)

    # Aggregates across all of this user's players (both innings)
    runs_total = wkts_total = fifties = hundreds = 0
    sixes_total = fours_total = 0
    hattricks_total = 0
    maidens_total = 0
    dots_total = 0
    three_fers = five_fers = 0
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
    career_runs = career_wickets = career_sixes = career_fours = 0
    career_fifties = career_hundreds = 0
    career_balls_faced = career_boundary_runs = career_chase_runs = 0
    career_quickfire_runs = career_not_outs = 0
    career_scores_25 = career_scores_75 = 0
    career_dots = career_maidens = career_balls_bowled = 0
    career_hattricks = career_clean_spells = 0
    career_played = False

    for xi_key, stats_key, is_bat in [
        ("inn1_bat_xi", "inn1_bat_stats", True),
        ("inn1_bowl_xi", "inn1_bowl_stats", False),
        ("bat_xi", "bat_stats", True),
        ("bowl_xi", "bowl_stats", False),
    ]:
        # "bat_xi"/"bowl_xi" are the second-innings line-ups, so batting there
        # is batting in a chase — the only innings split the state gives us.
        is_chase = is_bat and xi_key == "bat_xi"
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
                    balls_here = pst.get("balls", 0)
                    fours_here = pst.get("fours", 0)
                    sixes_here = pst.get("sixes", 0)
                    career_runs += runs_here
                    career_sixes += sixes_here
                    career_fours += fours_here
                    career_balls_faced += balls_here
                    career_boundary_runs += fours_here * 4 + sixes_here * 6
                    if is_chase:
                        career_chase_runs += runs_here
                    if runs_here >= 100:
                        career_hundreds += 1
                    elif runs_here >= 50:
                        career_fifties += 1
                    if runs_here >= 75:
                        career_scores_75 += 1
                    if runs_here >= 25:
                        career_scores_25 += 1
                    if balls_here > 0 and not pst.get("out"):
                        career_not_outs += 1
                    # Only the innings that were actually played at pace count
                    # toward a strike-rate quest — a 10-ball cameo says nothing.
                    if balls_here >= 10 and runs_here * 100 >= balls_here * 150:
                        career_quickfire_runs += runs_here
                else:
                    wkts_here = pst.get("wickets", 0)
                    balls_bowled_here = pst.get("balls", 0)
                    career_wickets += wkts_here
                    career_dots += pst.get("dots", 0)
                    career_maidens += pst.get("maidens", 0)
                    career_balls_bowled += balls_bowled_here
                    if pst.get("hattrick"):
                        career_hattricks += 1
                    # A "spell" needs enough overs for the economy to mean
                    # something, matching the squad-wide 4-over threshold. Under
                    # six an over is exactly "fewer runs than balls".
                    if (balls_bowled_here >= 24
                            and pst.get("runs", 0) < balls_bowled_here):
                        career_clean_spells += 1
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
                if w >= 5:
                    five_fers += 1
                if w >= 3:
                    three_fers += 1
                maidens_total += pst.get("maidens", 0)
                dots_total += pst.get("dots", 0)
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
    if fours_total > 0:
        safe_track(session, uid, "fours_hit", fours_total)
    if (sixes_total + fours_total) > 0:
        safe_track(session, uid, "boundaries_hit", sixes_total + fours_total)
    if dots_total > 0:
        safe_track(session, uid, "dot_balls", dots_total)
    for _ in range(fifties):
        safe_track(session, uid, "fifty", 1)
    for _ in range(hundreds):
        safe_track(session, uid, "hundred", 1)
    for _ in range(hattricks_total):
        safe_track(session, uid, "hattrick", 1)
    for _ in range(three_fers):
        safe_track(session, uid, "three_fer", 1)
    for _ in range(five_fers):
        safe_track(session, uid, "five_fer", 1)
    if maidens_total > 0:
        safe_track(session, uid, "maiden_over", maidens_total)
    for _ in range(user_match_not_outs):
        safe_track(session, uid, "not_out_innings", 1)

    # Career Player events — only fired when the user's own career card
    # actually featured, and only ever consumed by career_only quests.
    if career_played:
        safe_track(session, uid, "career_match_played", 1)
        if is_winner:
            safe_track(session, uid, "career_match_won", 1)

        # Cumulative totals — one call each, skipped when the figure is zero so
        # a quiet match does not churn through every quest in the catalogue.
        for event_key, amount in (
            ("career_runs_scored", career_runs),
            ("career_wickets_taken", career_wickets),
            ("career_sixes_hit", career_sixes),
            ("career_fours_hit", career_fours),
            ("career_boundary_runs", career_boundary_runs),
            ("career_balls_faced", career_balls_faced),
            ("career_chase_runs", career_chase_runs),
            ("career_quickfire_runs", career_quickfire_runs),
            ("career_dot_balls", career_dots),
            ("career_maiden_overs", career_maidens),
            ("career_balls_bowled", career_balls_bowled),
        ):
            if amount > 0:
                safe_track(session, uid, event_key, amount)

        # Counted occurrences — each innings that qualified fires once, so a
        # "three times this week" quest counts weeks, not matches.
        for event_key, times in (
            ("career_fifty", career_fifties),
            ("career_hundred", career_hundreds),
            ("career_score_25_plus", career_scores_25),
            ("career_score_75_plus", career_scores_75),
            ("career_not_out", career_not_outs),
            ("career_hattrick", career_hattricks),
            ("career_economy_spell", career_clean_spells),
        ):
            for _ in range(times):
                safe_track(session, uid, event_key, 1)

        # Once-per-match milestones, judged on the match total.
        if career_wickets >= 3:
            safe_track(session, uid, "career_three_fer", 1)
        if career_wickets >= 5:
            safe_track(session, uid, "career_five_fer", 1)
        if career_runs >= 25 and career_wickets >= 2:
            safe_track(session, uid, "career_allrounder_match", 1)

        # Player of the Match is decided by the finalize that calls us, which
        # stashes the winner on the state before doing so. Without it a
        # 'career_potm' quest could never progress, and because the weekly
        # streak needs *every* assigned career quest cleared, one such quest
        # would put the streak and its jackpot permanently out of reach.
        if (state.get("potm_player_id") == career_player_id
                and state.get("potm_owner_user_id") == uid):
            safe_track(session, uid, "career_potm", 1)

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
    # …and the mirror image: won after setting a total and defending it.
    if is_winner and state.get("inn1_bat_team_id") == uid:
        safe_track(session, uid, "defended_total", 1)

    # Phase scoring, off the per-over run list the Manhattan chart already
    # keeps. "Score 45+ in the powerplay" quests were previously untrackable
    # and had to be seeded as manual, which meant they never progressed.
    pp_runs, death_runs = _phase_runs(state, uid)
    if pp_runs > 0:
        safe_track(session, uid, "powerplay_runs", pp_runs, mode="max")
    if death_runs > 0:
        safe_track(session, uid, "death_over_runs", death_runs, mode="max")


# How many overs count as the powerplay and as the death, for the phase quests.
POWERPLAY_OVERS = 6
DEATH_OVERS = 5


def _phase_runs(state, user_id):
    """(powerplay runs, death-overs runs) from the innings ``user_id`` batted.

    Reads ``over_runs`` — the per-over run list every ball loop appends to for
    the Manhattan chart — so no engine change is needed to score a phase. The
    second innings keeps its list in ``over_runs`` and the first innings' copy
    is snapshotted to ``inn1_over_runs`` at the break.

    Returns ``(0, 0)`` when the user did not bat or the list is missing, and
    the death window is skipped for innings too short to have one (a 6-over
    game is all powerplay; counting its last 5 overs as "death" too would hand
    out both quests for the same runs).
    """
    s = state or {}
    if s.get("inn1_bat_team_id") == user_id:
        overs = s.get("inn1_over_runs") or []
    elif s.get("bat_team_id") == user_id:
        overs = s.get("over_runs") or []
    else:
        return 0, 0

    runs = [r for r in overs if isinstance(r, (int, float))]
    if not runs:
        return 0, 0
    powerplay = int(sum(runs[:POWERPLAY_OVERS]))
    death = (int(sum(runs[-DEATH_OVERS:]))
             if len(runs) > POWERPLAY_OVERS + DEATH_OVERS else 0)
    return powerplay, death
