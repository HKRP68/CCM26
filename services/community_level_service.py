"""Community levels + one-time milestone rewards.

Levels are computed from lifetime CXP (see
:func:`services.community_xp_service.level_for_xp`); this module handles the
*reward* side: granting each level's milestone at most once per manager.

Cosmetic rewards (badge/title) are always granted on reaching a level.
Monetary rewards (coins/gems/packs) are only granted when an administrator has
``enabled`` that level's reward — disabled by default per spec. Idempotency is
guaranteed by the unique ``(user_id, level)`` row in
:class:`models.CommunityMilestoneClaim`.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def milestone_rewards(session):
    from models import CommunityMilestoneReward
    return {r.level: r for r in session.query(CommunityMilestoneReward).all()}


def claimed_levels(session, user_id):
    from models import CommunityMilestoneClaim
    return {lvl for (lvl,) in session.query(CommunityMilestoneClaim.level)
            .filter(CommunityMilestoneClaim.user_id == user_id).all()}


def grant_due_milestones(session, user_id, *, now=None):
    """Grant every unclaimed milestone up to the manager's current level.

    Returns a list of human-readable reward descriptions granted this call.
    Caller commits.
    """
    from datetime import datetime
    from models import CommunityMilestoneClaim, User
    from services import community_xp_service as X

    now = now or datetime.utcnow()
    prof = X.get_profile(session, user_id)
    if prof is None:
        return []
    current = prof.level or 1
    rewards = milestone_rewards(session)
    already = claimed_levels(session, user_id)
    granted = []

    user = session.get(User, user_id)

    for level in sorted(rewards.keys()):
        if level > current or level in already:
            continue
        r = rewards[level]
        parts = []
        # Cosmetic — always.
        if r.badge:
            parts.append(f"🏅 {r.badge} badge")
        if r.title:
            parts.append(f"🎖️ “{r.title}” title")
        # Monetary — only if enabled by an admin.
        if r.enabled and user is not None:
            if r.coins:
                user.total_coins = (user.total_coins or 0) + int(r.coins)
                parts.append(f"{int(r.coins):,} coins")
            if r.gems:
                user.total_gems = (user.total_gems or 0) + int(r.gems)
                parts.append(f"{int(r.gems):,} gems")
            if r.pack_json:
                try:
                    pack = json.loads(r.pack_json)
                    parts.append(f"player pack ({pack.get('label', 'pack')})")
                except Exception:
                    pass
            if r.cosmetic:
                parts.append(r.cosmetic)
        detail = ", ".join(parts) if parts else f"Level {level} milestone"
        session.add(CommunityMilestoneClaim(
            user_id=user_id, level=level, claimed_at=now, detail=detail[:200]))
        try:
            session.flush()
        except Exception:
            session.rollback()
            continue
        granted.append({"level": level, "detail": detail})
    return granted


def milestone_overview(session, user_id):
    """Rows for /gcrewards: each level's reward + claimed state."""
    from services import community_xp_service as X
    prof = X.get_profile(session, user_id)
    current = (prof.level if prof else 1) or 1
    rewards = milestone_rewards(session)
    claimed = claimed_levels(session, user_id)
    out = []
    for level in sorted(rewards.keys()):
        r = rewards[level]
        bits = []
        if r.badge:
            bits.append(f"{r.badge} badge")
        if r.title:
            bits.append(f"“{r.title}” title")
        if r.enabled:
            if r.coins:
                bits.append(f"{int(r.coins):,} coins")
            if r.gems:
                bits.append(f"{int(r.gems):,} gems")
            if r.cosmetic:
                bits.append(r.cosmetic)
        out.append({
            "level": level,
            "title": X.level_title(session, level),
            "reward": ", ".join(bits) if bits else "—",
            "claimed": level in claimed,
            "reached": level <= current,
        })
    return out
