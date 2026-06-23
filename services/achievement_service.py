"""Achievement system — permanent unlockable badges.

Architecture:
  - Catalog of achievements is defined in CATALOG below (hardcoded)
  - Each achievement has a `check_fn` that takes (session, user) and returns True/False
  - When a user does something significant, the calling handler calls
    check_and_award(session, user_id) — this evaluates ALL achievements and
    unlocks any newly-met ones, awarding the bonus
  - Already-unlocked achievements are stored in user_achievements table

Categories:
  COLLECTION — roster size, rare players, etc.
  MATCH      — wins, streaks, big scores
  BATTING    — runs, fifties, hundreds
  BOWLING    — wickets, hat-tricks
  SOCIAL     — trades, gifts (future)
  ECONOMY    — coins, gems
  TRAITS     — applied/upgraded traits
  ENGAGEMENT — daily streaks, days active
  SPECIAL    — uncommon achievements
"""

import logging
from datetime import datetime
from sqlalchemy.exc import IntegrityError

from models import (
    User, UserAchievement, UserRoster, Player, PlayerTrait,
    Trade, PlayerGameStats, Match, UserStats,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# CATALOG — every achievement available
# Each entry: key, emoji, name, description, category, reward_coins,
#             reward_gems, check_fn(session, user) -> bool
# ════════════════════════════════════════════════════════════════════

def _has_player_ge(session, user, rating):
    """User has at least one player with rating ≥ X."""
    return (session.query(UserRoster).join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.rating >= rating)
            .first() is not None)


def _has_n_players_ge(session, user, n, rating):
    """User has at least N players with rating ≥ X."""
    return (session.query(UserRoster).join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.rating >= rating)
            .count() >= n)


def _has_legend(session, user):
    """User has at least one legendary card (95+)."""
    return _has_player_ge(session, user, 95)


def _max_individual_score(session, user):
    """Highest individual run score by this user across all matches."""
    r = (session.query(PlayerGameStats.highest_score)
         .filter(PlayerGameStats.user_id == user.id,
                 PlayerGameStats.highest_score.isnot(None))
         .order_by(PlayerGameStats.highest_score.desc()).first())
    return r[0] if r else 0


def _max_individual_wickets(session, user):
    """Most wickets by this user in a single innings (best 5fer count)."""
    # Use five_fers > 0 indicator; PlayerGameStats stores cumulative
    # so we use: any 5fer ever?
    r = (session.query(PlayerGameStats.five_fers)
         .filter(PlayerGameStats.user_id == user.id,
                 PlayerGameStats.five_fers > 0).first())
    return 5 if r else 0


def _total_runs(session, user):
    from sqlalchemy import func
    r = (session.query(func.sum(PlayerGameStats.runs))
         .filter(PlayerGameStats.user_id == user.id).scalar())
    return int(r or 0)


def _total_wickets(session, user):
    from sqlalchemy import func
    r = (session.query(func.sum(PlayerGameStats.wickets_taken))
         .filter(PlayerGameStats.user_id == user.id).scalar())
    return int(r or 0)


def _trait_count(session, user):
    """How many active player-trait bindings this user has."""
    return (session.query(PlayerTrait).join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
            .filter(UserRoster.user_id == user.id).count())


def _max_trait_level(session, user):
    """Highest level any of this user's traits is at."""
    r = (session.query(PlayerTrait.level).join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
         .filter(UserRoster.user_id == user.id)
         .order_by(PlayerTrait.level.desc()).first())
    return r[0] if r else 0


def _trade_count(session, user):
    return (session.query(Trade)
            .filter((Trade.initiator_id == user.id) | (Trade.receiver_id == user.id),
                    Trade.status == "completed").count())


# Each achievement entry:
#   "key": stable identifier (don't change after release)
#   "emoji": single badge character
#   "name": display name
#   "desc": player-facing description
#   "category": grouping (display only)
#   "coins": coin reward on unlock
#   "gems": gem reward on unlock
#   "check": function (session, user) -> bool

CATALOG = [
    # ── COLLECTION ──────────────────────────────────────────────────
    {"key": "first_player", "emoji": "🃏", "name": "First Pick",
     "desc": "Add your first player to the roster",
     "category": "Collection", "coins": 0, "gems": 1,
     "check": lambda s, u: u.roster_count >= 1},

    {"key": "roster_10", "emoji": "📋", "name": "Squad Ready",
     "desc": "Have 10 players on your roster",
     "category": "Collection", "coins": 1500, "gems": 0,
     "check": lambda s, u: u.roster_count >= 10},

    {"key": "roster_25", "emoji": "👥", "name": "Full Squad",
     "desc": "Fill your roster to 25 players",
     "category": "Collection", "coins": 7500, "gems": 1,
     "check": lambda s, u: u.roster_count >= 25},

    {"key": "first_legend", "emoji": "🌟", "name": "Legend Acquired",
     "desc": "Own a player rated 95+",
     "category": "Collection", "coins": 0, "gems": 3,
     "check": _has_legend},

    {"key": "five_legends", "emoji": "💫", "name": "Hall of Fame",
     "desc": "Own 5 different 95+ rated players",
     "category": "Collection", "coins": 0, "gems": 15,
     "check": lambda s, u: _has_n_players_ge(s, u, 5, 95)},

    {"key": "elite_squad", "emoji": "⭐", "name": "Elite Squad",
     "desc": "Have 11 players rated 85+",
     "category": "Collection", "coins": 15000, "gems": 0,
     "check": lambda s, u: _has_n_players_ge(s, u, 11, 85)},

    # ── MATCH RESULTS ───────────────────────────────────────────────
    {"key": "first_match", "emoji": "🎯", "name": "Debut Match",
     "desc": "Play your first match",
     "category": "Match", "coins": 300, "gems": 0,
     "check": lambda s, u: (u.matches_played or 0) >= 1},

    {"key": "first_win", "emoji": "🏆", "name": "First Victory",
     "desc": "Win your first match",
     "category": "Match", "coins": 1500, "gems": 1,
     "check": lambda s, u: (u.matches_won or 0) >= 1},

    {"key": "ten_wins", "emoji": "🎖️", "name": "Ten Wins",
     "desc": "Win 10 matches",
     "category": "Match", "coins": 7500, "gems": 1,
     "check": lambda s, u: (u.matches_won or 0) >= 10},

    {"key": "fifty_wins", "emoji": "👑", "name": "Half Century of Wins",
     "desc": "Win 50 matches",
     "category": "Match", "coins": 30000, "gems": 7,
     "check": lambda s, u: (u.matches_won or 0) >= 50},

    {"key": "hundred_wins", "emoji": "🏅", "name": "Century Maker",
     "desc": "Win 100 matches",
     "category": "Match", "coins": 75000, "gems": 30,
     "check": lambda s, u: (u.matches_won or 0) >= 100},

    {"key": "ten_played", "emoji": "🎮", "name": "Regular Player",
     "desc": "Play 10 matches",
     "category": "Match", "coins": 3000, "gems": 0,
     "check": lambda s, u: (u.matches_played or 0) >= 10},

    {"key": "fifty_played", "emoji": "🎟️", "name": "Veteran",
     "desc": "Play 50 matches",
     "category": "Match", "coins": 9000, "gems": 3,
     "check": lambda s, u: (u.matches_played or 0) >= 50},

    # ── STREAKS ─────────────────────────────────────────────────────
    {"key": "streak_3", "emoji": "🔥", "name": "Hot Streak",
     "desc": "Win 3 matches in a row",
     "category": "Streak", "coins": 1500, "gems": 1,
     "check": lambda s, u: (u.best_streak or 0) >= 3},

    {"key": "streak_5", "emoji": "🌋", "name": "On Fire",
     "desc": "Win 5 matches in a row",
     "category": "Streak", "coins": 4500, "gems": 1,
     "check": lambda s, u: (u.best_streak or 0) >= 5},

    {"key": "streak_10", "emoji": "🚀", "name": "Unstoppable",
     "desc": "Win 10 matches in a row",
     "category": "Streak", "coins": 15000, "gems": 7,
     "check": lambda s, u: (u.best_streak or 0) >= 10},

    # ── BATTING ─────────────────────────────────────────────────────
    {"key": "first_fifty", "emoji": "5️⃣0️⃣", "name": "Half Century",
     "desc": "Score 50+ in a single match",
     "category": "Batting", "coins": 600, "gems": 0,
     "check": lambda s, u: _max_individual_score(s, u) >= 50},

    {"key": "first_hundred", "emoji": "💯", "name": "Centurion",
     "desc": "Score 100+ in a single match",
     "category": "Batting", "coins": 3000, "gems": 1,
     "check": lambda s, u: _max_individual_score(s, u) >= 100},

    {"key": "double_century", "emoji": "2️⃣0️⃣0️⃣", "name": "Double Century",
     "desc": "Score 200+ in a single match",
     "category": "Batting", "coins": 15000, "gems": 7,
     "check": lambda s, u: _max_individual_score(s, u) >= 200},

    {"key": "runs_1k", "emoji": "🏏", "name": "1000 Run Club",
     "desc": "Total 1,000 career runs",
     "category": "Batting", "coins": 1500, "gems": 0,
     "check": lambda s, u: _total_runs(s, u) >= 1000},

    {"key": "runs_5k", "emoji": "📈", "name": "5K Striker",
     "desc": "Total 5,000 career runs",
     "category": "Batting", "coins": 7500, "gems": 3,
     "check": lambda s, u: _total_runs(s, u) >= 5000},

    {"key": "runs_10k", "emoji": "🎢", "name": "10K Run Machine",
     "desc": "Total 10,000 career runs",
     "category": "Batting", "coins": 30000, "gems": 15,
     "check": lambda s, u: _total_runs(s, u) >= 10000},

    # ── BOWLING ─────────────────────────────────────────────────────
    {"key": "five_for", "emoji": "🎳", "name": "Five-Wicket Haul",
     "desc": "Take 5 wickets in a match",
     "category": "Bowling", "coins": 3000, "gems": 1,
     "check": lambda s, u: _max_individual_wickets(s, u) >= 5},

    {"key": "wickets_50", "emoji": "💀", "name": "Wicket Hunter",
     "desc": "Take 50 career wickets",
     "category": "Bowling", "coins": 1500, "gems": 0,
     "check": lambda s, u: _total_wickets(s, u) >= 50},

    {"key": "wickets_250", "emoji": "🩸", "name": "Wicket Machine",
     "desc": "Take 250 career wickets",
     "category": "Bowling", "coins": 7500, "gems": 3,
     "check": lambda s, u: _total_wickets(s, u) >= 250},

    {"key": "wickets_1k", "emoji": "⚰️", "name": "Wicket Legend",
     "desc": "Take 1,000 career wickets",
     "category": "Bowling", "coins": 30000, "gems": 15,
     "check": lambda s, u: _total_wickets(s, u) >= 1000},

    # ── ECONOMY ─────────────────────────────────────────────────────
    {"key": "coins_100k", "emoji": "💰", "name": "Wealthy",
     "desc": "Have 100,000 coins at once",
     "category": "Economy", "coins": 0, "gems": 1,
     "check": lambda s, u: (u.total_coins or 0) >= 100000},

    {"key": "coins_1m", "emoji": "💎", "name": "Millionaire",
     "desc": "Have 1,000,000 coins at once",
     "category": "Economy", "coins": 0, "gems": 7,
     "check": lambda s, u: (u.total_coins or 0) >= 1000000},

    {"key": "gems_100", "emoji": "💍", "name": "Gem Collector",
     "desc": "Have 100 gems at once",
     "category": "Economy", "coins": 1500, "gems": 0,
     "check": lambda s, u: (u.total_gems or 0) >= 100},

    {"key": "gems_500", "emoji": "💠", "name": "Gem Hoarder",
     "desc": "Have 500 gems at once",
     "category": "Economy", "coins": 7500, "gems": 0,
     "check": lambda s, u: (u.total_gems or 0) >= 500},

    # ── TRAITS ──────────────────────────────────────────────────────
    {"key": "first_trait", "emoji": "✨", "name": "Trait Trained",
     "desc": "Apply your first trait",
     "category": "Traits", "coins": 300, "gems": 1,
     "check": lambda s, u: _trait_count(s, u) >= 1},

    {"key": "five_traits", "emoji": "🔮", "name": "Trait Tactician",
     "desc": "Have 5 active player-trait bindings",
     "category": "Traits", "coins": 1500, "gems": 1,
     "check": lambda s, u: _trait_count(s, u) >= 5},

    {"key": "trait_l5", "emoji": "🌠", "name": "Maxed Out",
     "desc": "Get any trait to Level 5",
     "category": "Traits", "coins": 3000, "gems": 3,
     "check": lambda s, u: _max_trait_level(s, u) >= 5},

    # ── ENGAGEMENT ──────────────────────────────────────────────────
    {"key": "active_7", "emoji": "📅", "name": "Week-Long Player",
     "desc": "Be active 7 different days",
     "category": "Engagement", "coins": 1500, "gems": 1,
     "check": lambda s, u: (u.active_days or 0) >= 7},

    {"key": "active_30", "emoji": "🗓️", "name": "Monthly Devotee",
     "desc": "Be active 30 different days",
     "category": "Engagement", "coins": 7500, "gems": 3,
     "check": lambda s, u: (u.active_days or 0) >= 30},

    {"key": "first_trade", "emoji": "🤝", "name": "Trader",
     "desc": "Complete your first trade",
     "category": "Social", "coins": 600, "gems": 1,
     "check": lambda s, u: _trade_count(s, u) >= 1},

    {"key": "ten_trades", "emoji": "🔄", "name": "Deal Maker",
     "desc": "Complete 10 trades",
     "category": "Social", "coins": 4500, "gems": 1,
     "check": lambda s, u: _trade_count(s, u) >= 10},
]

CATALOG_BY_KEY = {a["key"]: a for a in CATALOG}


# ════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════

def get_user_unlocked_keys(session, user_id):
    """Returns set of achievement keys the user has unlocked."""
    rows = (session.query(UserAchievement.achievement_key)
            .filter(UserAchievement.user_id == user_id).all())
    return {r[0] for r in rows}


def get_user_achievements_with_progress(session, user_id):
    """Returns list of dicts: each catalog entry + 'unlocked' + 'unlocked_at'.
    Used by /achievements UI."""
    unlocked_rows = (session.query(UserAchievement)
                     .filter(UserAchievement.user_id == user_id).all())
    by_key = {r.achievement_key: r for r in unlocked_rows}
    out = []
    for a in CATALOG:
        u = by_key.get(a["key"])
        out.append({
            **a,
            "unlocked": u is not None,
            "unlocked_at": u.unlocked_at if u else None,
        })
    return out


def get_top_unlocked(session, user_id, limit=3):
    """Most recently-unlocked achievements (for /myprofile display)."""
    rows = (session.query(UserAchievement)
            .filter(UserAchievement.user_id == user_id)
            .order_by(UserAchievement.unlocked_at.desc())
            .limit(limit).all())
    out = []
    for r in rows:
        a = CATALOG_BY_KEY.get(r.achievement_key)
        if a:
            out.append({
                "key": a["key"],
                "emoji": a["emoji"],
                "name": a["name"],
                "unlocked_at": r.unlocked_at,
            })
    return out


def check_and_award(session, user_id):
    """Evaluate all achievements for a user. Unlock any newly-met ones.

    Returns: list of newly-unlocked entries (full catalog dicts).
    Calling code should commit the session.
    """
    user = session.query(User).get(user_id)
    if not user:
        return []
    if user.telegram_id == -1:  # bot user — skip
        return []

    unlocked_keys = get_user_unlocked_keys(session, user_id)
    newly = []

    for a in CATALOG:
        if a["key"] in unlocked_keys:
            continue
        try:
            ok = a["check"](session, user)
        except Exception:
            logger.exception(f"Achievement check failed: {a['key']}")
            ok = False
        if ok:
            try:
                ua = UserAchievement(
                    user_id=user_id, achievement_key=a["key"],
                    unlocked_at=datetime.utcnow(),
                )
                session.add(ua)
                session.flush()
                # Award rewards
                if a.get("coins"):
                    user.total_coins = (user.total_coins or 0) + a["coins"]
                if a.get("gems"):
                    user.total_gems = (user.total_gems or 0) + a["gems"]
                newly.append(a)
            except IntegrityError:
                # Race condition — already unlocked from another path
                session.rollback()
            except Exception:
                logger.exception(f"Award failed for {a['key']}")
                session.rollback()
    return newly


def safe_check_and_award(session, user_id):
    """Wrapper that swallows all exceptions — never breaks the calling handler."""
    try:
        return check_and_award(session, user_id)
    except Exception:
        logger.exception(f"safe_check_and_award failed for user {user_id}")
        return []


async def check_and_notify(context, chat_id, session, user_id):
    """Run achievement check; if any newly unlocked, send a notification message.

    Use after committing the relevant state change (so checks see fresh data).
    """
    newly = safe_check_and_award(session, user_id)
    if not newly:
        return []
    try:
        # Commit the awarded rewards
        session.commit()
    except Exception:
        session.rollback()
        return []

    # Send notification
    try:
        if len(newly) == 1:
            a = newly[0]
            bits = []
            if a.get("coins"): bits.append(f"+{a['coins']:,} 🪙")
            if a.get("gems"):  bits.append(f"+{a['gems']} 💎")
            reward_str = "  ·  ".join(bits)
            text = (
                f"🏆 <b>ACHIEVEMENT UNLOCKED!</b>\n\n"
                f"{a['emoji']} <b>{a['name']}</b>\n"
                f"<i>{a['desc']}</i>"
                + (f"\n\n🎁 {reward_str}" if reward_str else "")
            )
        else:
            lines = [f"🏆 <b>{len(newly)} ACHIEVEMENTS UNLOCKED!</b>\n"]
            total_coins = sum(a.get("coins", 0) for a in newly)
            total_gems = sum(a.get("gems", 0) for a in newly)
            for a in newly:
                lines.append(f"  {a['emoji']} <b>{a['name']}</b> — <i>{a['desc']}</i>")
            bits = []
            if total_coins: bits.append(f"+{total_coins:,} 🪙")
            if total_gems:  bits.append(f"+{total_gems} 💎")
            if bits:
                lines.append(f"\n🎁 Total: {'  ·  '.join(bits)}")
            text = "\n".join(lines)

        await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("Achievement notify failed")
    return newly


def get_top_unlocked(session, user_id, limit=3):
    """Return the user's most prestigious unlocked achievements for showcase.

    Ranking: gem-rewards desc, then coin-rewards desc, then most recently
    unlocked first. Gems are the rarer reward so they're a decent rarity proxy.

    Returns list of dicts: {key, emoji, name, desc, gems, coins, unlocked_at}.
    """
    from models import UserAchievement
    rows = (session.query(UserAchievement)
            .filter(UserAchievement.user_id == user_id)
            .all())
    if not rows:
        return []

    key_to_unlock = {r.achievement_key: r.unlocked_at for r in rows}
    catalog_by_key = {c["key"]: c for c in CATALOG}

    unlocked = []
    for key, unlocked_at in key_to_unlock.items():
        c = catalog_by_key.get(key)
        if not c:
            continue
        unlocked.append({
            "key": key,
            "emoji": c.get("emoji", "🏆"),
            "name": c.get("name", key),
            "desc": c.get("desc", ""),
            "gems": c.get("gems", 0),
            "coins": c.get("coins", 0),
            "unlocked_at": unlocked_at,
        })

    # Sort by rarity proxy: gems desc, then coins desc, then most recent
    unlocked.sort(
        key=lambda a: (a["gems"], a["coins"],
                       a["unlocked_at"].timestamp() if a["unlocked_at"] else 0),
        reverse=True,
    )
    return unlocked[:limit]
