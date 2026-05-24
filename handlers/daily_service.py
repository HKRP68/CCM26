"""Service-level daily-claim logic.

Replicates handlers/daily.py's daily_claim_callback() so both the bot's
inline button AND the Mini App's /api/webapp/daily endpoint can use the
same code path. Both write to UserStats.last_daily — shared cooldown.

The Mini App can't show the "squad full → release/replace" picker
(it's a Telegram inline keyboard, not webapp UI). For overflow players,
this function just skips them and reports the count.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def check_daily_ready(session, user):
    """Returns (ready: bool, remaining_seconds: int).

    Reads UserStats.last_daily. The Mini App uses this for the cooldown
    timer; the bot does its own check too.
    """
    from models import UserStats
    from config import DAILY_COOLDOWN
    from services.command_config_service import get_cooldown

    stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
    effective_cd = get_cooldown(session, "daily", DAILY_COOLDOWN)
    if not stats or not stats.last_daily:
        return True, 0
    elapsed = (datetime.utcnow() - stats.last_daily).total_seconds()
    if elapsed >= effective_cd:
        return True, 0
    return False, int(effective_cd - elapsed)


def claim_daily(session, user, source_label="bot"):
    """Apply the daily reward. Caller must commit() afterwards.

    Returns a dict describing what happened:
      {
        "ok": bool,
        "coins": int,            # coins awarded
        "gems": int,              # gems awarded
        "streak": int,            # new streak count
        "milestone": bool,        # hit a streak milestone?
        "players_added": [{name, rating}, ...],
        "players_skipped": [{name, rating}, ...],  # roster was full
        "lines": [str, ...],      # human-friendly summary lines
      }

    If cooldown isn't ready, returns {"ok": False, "error": "cooldown",
    "remaining": int_seconds}.
    """
    from models import UserStats, UserRoster
    from config import DAILY_COOLDOWN, DAILY_COINS, STREAK_MILESTONE, MAX_ROSTER
    from services.command_config_service import (
        get_cooldown, get_coin_amount, get_player_count, get_reward,
    )
    from services.config_service import get_config
    from services.streak_service import update_streak
    from services.player_service import (
        get_random_player_by_rarity, get_random_player_by_rating_range,
    )

    stats = session.query(UserStats).filter(UserStats.user_id == user.id).first()
    if not stats:
        stats = UserStats(user_id=user.id)
        session.add(stats); session.flush()

    # Cooldown gate (defense in depth — caller should have checked)
    effective_cd = get_cooldown(session, "daily", DAILY_COOLDOWN)
    if stats.last_daily:
        elapsed = (datetime.utcnow() - stats.last_daily).total_seconds()
        if elapsed < effective_cd:
            return {"ok": False, "error": "cooldown",
                    "remaining": int(effective_cd - elapsed)}

    # Streak update
    streak_count, milestone = update_streak(stats)

    # Coins + gems (CommandReward overrides GameConfig)
    cfg = get_config(session)
    coins_award = get_coin_amount(session, "daily", cfg["daily_coins"])
    cr = get_reward(session, "daily")
    gems_award = (cr.gem_amount if cr and cr.gem_amount > 0
                  else cfg["daily_gems"])

    # Streak bonus
    if streak_count > 1:
        coins_award += cfg["daily_streak_bonus_coins"] * (streak_count - 1)
        gems_award += cfg["daily_streak_bonus_gems"] * (streak_count - 1)

    user.total_coins = (user.total_coins or 0) + coins_award
    if gems_award:
        user.total_gems = (user.total_gems or 0) + gems_award

    # Players (admin-tunable count)
    player_count = get_player_count(session, "daily", 2)
    players = []
    for _ in range(player_count):
        p = get_random_player_by_rarity(session)
        if p:
            players.append(p)

    # Milestone bonus
    milestone_player = None
    if milestone:
        milestone_player = get_random_player_by_rating_range(session, 81, 85)

    # Roster placement — auto-add if space, otherwise skip (Mini App can't
    # show release/replace picker)
    added = []
    skipped = []
    all_players = players + ([milestone_player] if milestone_player else [])
    for p in all_players:
        if not p:
            continue
        if (user.roster_count or 0) < MAX_ROSTER:
            entry = UserRoster(
                user_id=user.id, player_id=p.id,
                order_position=(user.roster_count or 0) + 1,
                acquired_date=datetime.utcnow(),
            )
            session.add(entry)
            user.roster_count = (user.roster_count or 0) + 1
            added.append({"name": p.name, "rating": p.rating, "id": p.id})
        else:
            skipped.append({"name": p.name, "rating": p.rating, "id": p.id})

    stats.last_daily = datetime.utcnow()

    # Activity log + quest tracking
    try:
        from services.activity_service import log_activity
        pnames = ", ".join(p.name for p in all_players if p)
        log_activity(
            session, user.id, "daily",
            f"[{source_label}] Daily: +{coins_award}c"
            + (f" +{gems_award}g" if gems_award else "")
            + f", players: {pnames}, streak {streak_count}",
            coins_change=coins_award, gems_change=gems_award,
        )
    except Exception:
        logger.exception("daily log_activity failed")
    try:
        from services.quest_service import safe_track
        safe_track(session, user.id, "daily", 1)
    except Exception:
        pass

    # Friendly lines
    remaining_days = (STREAK_MILESTONE - streak_count
                      if streak_count > 0 else STREAK_MILESTONE)
    lines = [f"+{coins_award:,} coins"]
    if gems_award:
        lines.append(f"+{gems_award} gems")
    lines.append(f"Streak: {streak_count}/{STREAK_MILESTONE}")
    if milestone and milestone_player:
        lines.append(f"🎉 MILESTONE: {milestone_player.name} ({milestone_player.rating} OVR)")
    for p in added:
        lines.append(f"✅ {p['name']} ({p['rating']}) added")
    for p in skipped:
        lines.append(f"⚠️ {p['name']} ({p['rating']}) — squad full")

    return {
        "ok": True,
        "coins": coins_award,
        "gems": gems_award,
        "streak": streak_count,
        "streak_max": STREAK_MILESTONE,
        "streak_remaining": remaining_days,
        "milestone": bool(milestone),
        "milestone_player": (
            {"name": milestone_player.name, "rating": milestone_player.rating}
            if milestone_player else None
        ),
        "players_added": added,
        "players_skipped": skipped,
        "lines": lines,
    }
