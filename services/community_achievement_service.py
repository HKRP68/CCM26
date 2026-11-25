"""Community Arena achievements.

Kept deliberately separate from the core achievement framework (community
eligibility is its own thing), but modelled the same way: an unlock is one row
in :class:`models.CommunityAchievement` with a unique ``(user_id, code)`` guard,
so re-checking is idempotent and cheap. Checks are called from the XP/game
flows right after the underlying counter advances.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# code → (display name, requirement description)
CATALOG = {
    "first_words":      ("First Words", "Send your first eligible chat message"),
    "chatterbox":       ("Chatterbox", "1,000 eligible chat messages"),
    "match_addict":     ("Match Addict", "100 completed group matches"),
    "card_collector":   ("Card Collector", "100 successful group claims"),
    "dedicated_member": ("Dedicated Member", "30-day check-in streak"),
    "quick_fingers":    ("Quick Fingers", "First Fastest Fingers win"),
    "lightning_fast":   ("Lightning Fast", "Win a game within 3 seconds"),
    "mystery_master":   ("Mystery Master", "Win 25 Mystery Card games"),
    "rating_expert":    ("Rating Expert", "Win 25 Guess the Rating games"),
    "cricket_genius":   ("Cricket Genius", "Win 100 community games"),
    "all_round_genius": ("All-Round Genius", "Win every type of community game"),
    "community_legend": ("Community Legend", "Reach 25,000 lifetime CXP"),
    "cmu_goat":         ("CMU GOAT", "Reach 100,000 lifetime CXP"),
}

ALL_GAME_TYPES = ("mystery", "oddoneout", "wordle", "fastestfingers", "rating", "cricketer")


def unlock(session, user_id: int, code: str) -> bool:
    """Grant an achievement once. Returns True if newly unlocked.

    Caller commits. A unique-constraint race collapses to 'already had it'.
    """
    if code not in CATALOG:
        return False
    from models import CommunityAchievement
    exists = (session.query(CommunityAchievement.id)
              .filter(CommunityAchievement.user_id == user_id,
                      CommunityAchievement.code == code).first())
    if exists:
        return False
    session.add(CommunityAchievement(user_id=user_id, code=code))
    try:
        session.flush()
    except Exception:
        session.rollback()
        return False
    return True


def unlocked_codes(session, user_id: int) -> set:
    from models import CommunityAchievement
    return {c for (c,) in session.query(CommunityAchievement.code)
            .filter(CommunityAchievement.user_id == user_id).all()}


def _profile(session, user_id):
    from services import community_xp_service as X
    return X.get_profile(session, user_id)


def check_chat_achievements(session, user_id: int) -> list:
    prof = _profile(session, user_id)
    if prof is None:
        return []
    got = []
    if (prof.chat_messages or 0) >= 1 and unlock(session, user_id, "first_words"):
        got.append("first_words")
    if (prof.chat_messages or 0) >= 1000 and unlock(session, user_id, "chatterbox"):
        got.append("chatterbox")
    return got


def check_match_achievements(session, user_id: int) -> list:
    prof = _profile(session, user_id)
    if prof is None:
        return []
    got = []
    total = (prof.pvp_matches or 0) + (prof.ai_matches or 0)
    if total >= 100 and unlock(session, user_id, "match_addict"):
        got.append("match_addict")
    return got


def check_claim_achievements(session, user_id: int) -> list:
    prof = _profile(session, user_id)
    if prof is None:
        return []
    got = []
    if (prof.claims or 0) >= 100 and unlock(session, user_id, "card_collector"):
        got.append("card_collector")
    return got


def check_checkin_achievements(session, user_id: int) -> list:
    prof = _profile(session, user_id)
    if prof is None:
        return []
    got = []
    if (prof.current_streak or 0) >= 30 and unlock(session, user_id, "dedicated_member"):
        got.append("dedicated_member")
    return got


def check_xp_achievements(session, user_id: int) -> list:
    prof = _profile(session, user_id)
    if prof is None:
        return []
    got = []
    xp = prof.lifetime_xp or 0
    if xp >= 25000 and unlock(session, user_id, "community_legend"):
        got.append("community_legend")
    if xp >= 100000 and unlock(session, user_id, "cmu_goat"):
        got.append("cmu_goat")
    return got


def _game_stats(session, user_id):
    from models import CommunityGameStat
    return {s.game_type: s for s in session.query(CommunityGameStat)
            .filter(CommunityGameStat.user_id == user_id).all()}


def check_game_win_achievements(session, user_id: int, game_type: str,
                                response_ms=None) -> list:
    got = []
    stats = _game_stats(session, user_id)
    total_wins = sum(s.wins for s in stats.values())

    if game_type == "fastestfingers" and unlock(session, user_id, "quick_fingers"):
        got.append("quick_fingers")
    if response_ms is not None and response_ms <= 3000 and unlock(session, user_id, "lightning_fast"):
        got.append("lightning_fast")
    if stats.get("mystery") and stats["mystery"].wins >= 25 and unlock(session, user_id, "mystery_master"):
        got.append("mystery_master")
    if stats.get("rating") and stats["rating"].wins >= 25 and unlock(session, user_id, "rating_expert"):
        got.append("rating_expert")
    if total_wins >= 100 and unlock(session, user_id, "cricket_genius"):
        got.append("cricket_genius")
    if (all((stats.get(t) and stats[t].wins >= 1) for t in ALL_GAME_TYPES)
            and unlock(session, user_id, "all_round_genius")):
        got.append("all_round_genius")
    return got


def names(codes) -> list:
    return [CATALOG[c][0] for c in codes if c in CATALOG]
