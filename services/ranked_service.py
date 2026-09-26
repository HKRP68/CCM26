"""Ranked ladder — a 1v1 skill rating that rewards beating strong opponents.

Every completed player-vs-player match moves both captains' rating with an Elo
update: beating a higher-rated side pays more than beating a lower one, and a
tie moves the two ratings towards each other. The global leaderboard counts
wins; this counts *who* you beat.

Rules:
  • Only human-vs-human matches that pay rewards count. A match against the AI,
    a /letsplay mismatch flagged for anti stat-farming (``count_result=False``)
    and anything a caller does not hand in are never rated.
  • Anti-boosting: the same pair can only move each other's rating
    ``PAIR_DAILY_CAP`` times per UTC day. Further matches still count for the
    rivalry and for rewards, just not for the ladder.
  • New players are placed faster: K is higher for the first ``PLACEMENT_GAMES``.
  • The ladder runs on the monthly season (``services.season_service``). When a
    season ends, the ladder is archived and paid by division, and every rating
    is soft-reset halfway back to the start rating on its next touch.

Everything here takes a session and never commits — callers own the
transaction, the same contract as ``services.match_rewards``.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

START_RATING = 1000
MIN_RATING = 100
PLACEMENT_GAMES = 10
K_PLACEMENT = 40
K_STANDARD = 24
PAIR_DAILY_CAP = 3
# Custom-length friendlies (/letsplay 5, /cipl 6) shorter than this are not
# rated — a two-over slog is a coin flip, not a measure of skill.
MIN_RANKED_OVERS = 5
# Played games needed in a season for the end-of-season division reward.
MIN_GAMES_FOR_REWARD = 5

# (floor, name, emoji, season reward coins, season reward gems) — high → low.
DIVISIONS = (
    (1450, "Legend", "👑", 20000, 10),
    (1350, "Diamond", "💎", 10000, 5),
    (1250, "Platinum", "🔷", 5000, 2),
    (1150, "Gold", "🥇", 2500, 0),
    (1050, "Silver", "🥈", 1000, 0),
    (0, "Bronze", "🥉", 0, 0),
)


# ── pure maths ───────────────────────────────────────────────────────

def division_for(rating):
    """``(name, emoji)`` of the division ``rating`` sits in."""
    for floor, name, emoji, _c, _g in DIVISIONS:
        if rating >= floor:
            return name, emoji
    return DIVISIONS[-1][1], DIVISIONS[-1][2]


def division_reward(rating):
    """``(coins, gems)`` paid at season end to a player finishing on ``rating``."""
    for floor, _n, _e, coins, gems in DIVISIONS:
        if rating >= floor:
            return coins, gems
    return 0, 0


def next_division(rating):
    """``(name, emoji, points_needed)`` for the next division up, or None at the top."""
    above = None
    for floor, name, emoji, _c, _g in DIVISIONS:
        if rating >= floor:
            break
        above = (floor, name, emoji)
    if above is None:
        return None
    return above[1], above[2], above[0] - rating


def expected_score(rating, opponent):
    """Elo expected score of ``rating`` against ``opponent`` (0..1)."""
    return 1.0 / (1.0 + 10 ** ((opponent - rating) / 400.0))


def k_factor(played):
    return K_PLACEMENT if (played or 0) < PLACEMENT_GAMES else K_STANDARD


def rating_delta(rating, opponent, score, played):
    """Points ``rating`` gains (or loses, negative) for ``score`` (1 / 0.5 / 0)."""
    raw = k_factor(played) * (score - expected_score(rating, opponent))
    delta = int(round(raw))
    # A win is never worth nothing, and a loss always costs something — an
    # upset-proof 0 reads as a bug on the result card.
    if score == 1 and delta < 1:
        delta = 1
    elif score == 0 and delta > -1:
        delta = -1
    return delta


def soft_reset(rating):
    """Where a rating starts the next season: halfway back to the start."""
    return START_RATING + int((rating - START_RATING) / 2)


# ── rows ─────────────────────────────────────────────────────────────

def _live_key(session):
    from services.season_service import ensure_current_season
    return ensure_current_season(session).season_key


def _roll_season(row, live_key, session=None):
    """Soft-reset ``row`` into the live season if it belongs to an older one.

    The old season is archived and paid first (idempotent). The monthly
    rollover normally did that already; this is the retry for a rollover whose
    ranked payout failed — resetting the row first would lose it for good. If
    the payout still fails, the row is left untouched in its old season (a
    later read retries); callers that write check ``season_key`` themselves.
    """
    if row.season_key == live_key:
        return row
    if session is not None and not _ensure_finalized(session, row.season_key):
        return row
    row.rating = max(MIN_RATING, soft_reset(row.rating or START_RATING))
    row.peak_rating = row.rating
    row.played = row.wins = row.losses = row.draws = 0
    row.season_key = live_key
    return row


def _ensure_finalized(session, season_key):
    """Archive + pay ``season_key`` if that never happened. Never raises.

    Returns True once the season is archived (now or earlier), False when the
    payout failed and must be retried before any row of it is reset.
    """
    from models import RankedSeasonResult
    try:
        if (session.query(RankedSeasonResult.id)
                .filter(RankedSeasonResult.season_key == season_key).first()):
            return True
        with session.begin_nested():
            finalize_season(session, season_key)
        return True
    except Exception:
        logger.exception("ranked: finalize retry for %s failed", season_key)
        return False


def get_rating(session, user_id, create=False, live_key=None):
    """The user's ladder row for the live season (soft-reset if stale).

    Returns None when the user has no row and ``create`` is False.
    """
    from models import RankedRating
    live_key = live_key or _live_key(session)
    row = (session.query(RankedRating)
           .filter(RankedRating.user_id == user_id).first())
    if row is None:
        if not create:
            return None
        row = RankedRating(user_id=user_id, season_key=live_key,
                           rating=START_RATING, peak_rating=START_RATING,
                           career_peak=START_RATING, played=0, wins=0,
                           losses=0, draws=0)
        session.add(row)
        session.flush()
        return row
    return _roll_season(row, live_key, session)


def _pair_games_today(session, a_id, b_id, exclude_match_id=None, now=None):
    """Rated games already played between this pair since UTC midnight."""
    from models import Match, MatchPostResult
    from sqlalchemy import and_, or_
    now = now or datetime.utcnow()
    midnight = datetime(now.year, now.month, now.day)
    q = (session.query(Match.id)
         .join(MatchPostResult, MatchPostResult.match_id == Match.id)
         .filter(MatchPostResult.rated.is_(True),
                 Match.completed_at >= midnight,
                 Match.completed_at < midnight + timedelta(days=1),
                 or_(and_(Match.user1_id == a_id, Match.user2_id == b_id),
                     and_(Match.user1_id == b_id, Match.user2_id == a_id))))
    if exclude_match_id is not None:
        q = q.filter(Match.id != exclude_match_id)
    return q.count()


def apply_result(session, user1_id, user2_id, winner_id=None, tie=False,
                 match_id=None, now=None):
    """Rate one finished match. Returns a result dict for the chat card.

    ``{"rated": bool, "reason": str|None, "players": {uid: {...}}}`` where each
    player entry holds ``before``, ``after``, ``delta``, ``division`` and
    ``promoted`` / ``relegated`` flags. Never commits.
    """
    out = {"rated": False, "reason": None, "players": {}}
    if not user1_id or not user2_id or user1_id == user2_id:
        out["reason"] = "not a two-player match"
        return out
    if not tie and winner_id not in (user1_id, user2_id):
        out["reason"] = "no winner"
        return out

    if _pair_games_today(session, user1_id, user2_id, exclude_match_id=match_id,
                         now=now) >= PAIR_DAILY_CAP:
        out["reason"] = (f"daily cap — this pair has already played "
                         f"{PAIR_DAILY_CAP} rated matches today")
        return out

    live_key = _live_key(session)
    r1 = get_rating(session, user1_id, create=True, live_key=live_key)
    r2 = get_rating(session, user2_id, create=True, live_key=live_key)
    if r1.season_key != live_key or r2.season_key != live_key:
        # A previous season's payout is still failing: rating this match would
        # write the live result into last season's unpaid row.
        out["reason"] = "last season's ranked payout is pending"
        return out
    before1, before2 = r1.rating, r2.rating
    if tie:
        s1 = s2 = 0.5
    else:
        s1 = 1.0 if winner_id == user1_id else 0.0
        s2 = 1.0 - s1
    d1 = rating_delta(before1, before2, s1, r1.played)
    d2 = rating_delta(before2, before1, s2, r2.played)

    for row, delta, score, before, uid in ((r1, d1, s1, before1, user1_id),
                                           (r2, d2, s2, before2, user2_id)):
        old_div, _ = division_for(before)
        row.rating = max(MIN_RATING, before + delta)
        row.played = (row.played or 0) + 1
        if score == 1:
            row.wins = (row.wins or 0) + 1
        elif score == 0:
            row.losses = (row.losses or 0) + 1
        else:
            row.draws = (row.draws or 0) + 1
        row.peak_rating = max(row.peak_rating or 0, row.rating)
        row.career_peak = max(row.career_peak or 0, row.rating)
        new_div, new_emoji = division_for(row.rating)
        order = [d[1] for d in DIVISIONS]
        out["players"][uid] = {
            "before": before,
            "after": row.rating,
            "delta": row.rating - before,
            "division": new_div,
            "emoji": new_emoji,
            "promoted": order.index(new_div) < order.index(old_div),
            "relegated": order.index(new_div) > order.index(old_div),
        }
    out["rated"] = True
    return out


def leaderboard(session, limit=10):
    """Top of the live season ladder: ``[(rank, RankedRating, User)]``."""
    from models import RankedRating, User
    live_key = _live_key(session)
    rows = (session.query(RankedRating, User)
            .join(User, User.id == RankedRating.user_id)
            .filter(RankedRating.season_key == live_key,
                    RankedRating.played > 0,
                    User.is_banned.is_(False))
            .order_by(RankedRating.rating.desc(), RankedRating.wins.desc(),
                      RankedRating.id.asc())
            .limit(limit).all())
    return [(i + 1, r, u) for i, (r, u) in enumerate(rows)]


def rank_of(session, row):
    """1-based ladder position of ``row`` in the live season (None if unplayed)."""
    from models import RankedRating
    if row is None or not row.played:
        return None
    higher = (session.query(RankedRating)
              .filter(RankedRating.season_key == row.season_key,
                      RankedRating.played > 0,
                      RankedRating.rating > row.rating)
              .count())
    return higher + 1


def finalize_season(session, season_key):
    """Archive and pay the ladder for a season being closed. Idempotent.

    Called from ``season_service._finalize_season``. Rows whose ``season_key``
    is still the closing season are exactly the players who played in it — any
    row touched since has already been rolled into the new season.
    """
    from models import RankedRating, RankedSeasonResult
    if (session.query(RankedSeasonResult)
            .filter(RankedSeasonResult.season_key == season_key).first()):
        return 0
    rows = (session.query(RankedRating)
            .filter(RankedRating.season_key == season_key,
                    RankedRating.played > 0)
            .order_by(RankedRating.rating.desc(), RankedRating.wins.desc(),
                      RankedRating.id.asc())
            .all())
    paid = 0
    for idx, r in enumerate(rows):
        division, _ = division_for(r.rating)
        coins = gems = 0
        if (r.played or 0) >= MIN_GAMES_FOR_REWARD:
            coins, gems = division_reward(r.rating)
        session.add(RankedSeasonResult(
            season_key=season_key, user_id=r.user_id, rank=idx + 1,
            rating=r.rating, division=division, played=r.played or 0,
            wins=r.wins or 0, prize_coins=coins, prize_gems=gems))
        if coins or gems:
            from models import User
            u = session.get(User, r.user_id)
            if u is not None:
                u.total_coins = (u.total_coins or 0) + coins
                u.total_gems = (u.total_gems or 0) + gems
                paid += 1
                try:
                    from services.activity_service import log_activity
                    log_activity(session, u.id, "ranked_season_reward",
                                 f"Ranked {season_key}: {division} — "
                                 f"+{coins} coins, +{gems} gems",
                                 coins_change=coins, gems_change=gems)
                except Exception:
                    pass
    session.flush()
    logger.info("Ranked season %s finalized: %s players, %s paid",
                season_key, len(rows), paid)
    return len(rows)
