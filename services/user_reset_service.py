"""Admin "Reset user" — wipe a player's progress so they can /debut again.

The ``User`` row itself is kept: dozens of tables point at ``users.id`` (match
history shared with opponents, referrals, reports, memberships…), and several
of those foreign keys have no ON DELETE rule, so deleting the row would either
fail or tear holes in other people's records. Instead the account is emptied and
flagged with ``needs_debut``:

* the bot's middleware answers every command except the doorway ones
  (/debut, /start, help, membership) with "send /debut to start again";
* the Mini App treats the account as not debuted;
* ``/debut`` re-runs the full first-time setup on the existing row (starter XI,
  debut coins/gems, onboarding journey) and clears the flag.

What is wiped is the player's *progress*: squad, career card, traits, packs,
quests, achievements, stats, balances, market slots, open trades, ranked rating
and activity log. What is kept is identity and anything paid for or shared:
Telegram id and names, team name/logo/colour, membership, ban state, referral
code, club, and match history other players also appear in.
"""

import logging

logger = logging.getLogger(__name__)


# Numeric ``User`` counters that start at zero on a fresh account.
_ZERO_FIELDS = (
    "total_coins", "total_gems", "roster_count", "quest_points",
    "matches_played", "matches_won", "matches_lost", "win_streak",
    "best_streak", "season_points", "season_wins", "active_days",
    "quick_matches_played", "quick_matches_won", "quick_matches_lost",
    "quick_matches_today", "bot_quest_matches_today", "pack_pity_counter",
    "comeback_tier", "career_weekly_streak", "career_weekly_best_streak",
)
# ``User`` fields that are NULL on a fresh account.
_NULL_FIELDS = (
    "captain_roster_id", "season_key", "last_match_date",
    "batting_order_set_at", "quick_matches_today_date",
    "bot_quest_matches_date", "onboarding_started_at", "onboarding_steps",
    "onboarding_done_at", "comeback_sent_at", "comeback_claim_tier",
    "career_weekly_last_period",
)


def _delete(session, model, *criteria):
    return (session.query(model).filter(*criteria)
            .delete(synchronize_session=False))


def reset_user(session, user):
    """Empty ``user``'s progress and flag the account for a fresh /debut.

    Deletes run children-first (traits and trades before the roster rows they
    point at) so no foreign key is violated on Postgres, where one failed
    statement would abort the whole transaction. Caller commits.

    Returns ``{"before": {...}, "deleted": {table: rows}}`` for the audit log.
    """
    from sqlalchemy import or_
    from models import (
        ActivityLog, PendingUndo, Player, PlayerFormHistory, PlayerMarket,
        PlayerTrait, RankedRating, RosterOverflowClaim, Trade, TraitDaily,
        TraitInventory, TraitMarket, UnopenedPack, UserAchievement,
        UserQuestProgress, UserRoster, UserStats,
    )

    uid = user.id
    before = {
        "coins": user.total_coins or 0,
        "gems": user.total_gems or 0,
        "qp": user.quest_points or 0,
        "roster": user.roster_count or 0,
        "matches": user.matches_played or 0,
    }
    deleted = {}

    # The career card first: its own helper clears every pointer at the card
    # (roster slot, traits, pending name change) before deleting the Player row.
    career = (session.query(Player)
              .filter(Player.career_owner_user_id == uid).first())
    if career is not None:
        from services.career_service import delete_career_player
        result = delete_career_player(session, career, refund_gems=False)
        deleted["career_player"] = 1 if result.get("ok") else 0

    roster_ids = [rid for (rid,) in session.query(UserRoster.id)
                  .filter(UserRoster.user_id == uid).all()]

    trade_match = [Trade.initiator_id == uid, Trade.receiver_id == uid]
    trait_match = [PlayerTrait.user_id == uid]
    if roster_ids:
        trade_match += [Trade.initiator_roster_id.in_(roster_ids),
                        Trade.receiver_roster_id.in_(roster_ids)]
        trait_match.append(PlayerTrait.roster_id.in_(roster_ids))
    deleted["trades"] = _delete(session, Trade, or_(*trade_match))
    deleted["player_traits"] = _delete(session, PlayerTrait, or_(*trait_match))

    for model in (TraitInventory, TraitMarket, TraitDaily, PlayerMarket,
                  UnopenedPack, PendingUndo, RosterOverflowClaim,
                  UserQuestProgress, UserAchievement, PlayerFormHistory,
                  RankedRating, ActivityLog, UserStats, UserRoster):
        deleted[model.__tablename__] = _delete(session, model, model.user_id == uid)

    for field in _ZERO_FIELDS:
        if hasattr(user, field):
            setattr(user, field, 0)
    for field in _NULL_FIELDS:
        if hasattr(user, field):
            setattr(user, field, None)
    user.needs_debut = True
    session.flush()
    # The roster relationship may still hold the rows deleted in bulk above.
    session.expire(user)
    return {"before": before, "deleted": deleted}


# ── Bot gate ────────────────────────────────────────────────────────

RESET_NOTICE = ("🔄 <b>Your account has been reset.</b>\n\n"
                "Send /debut to start again with a fresh squad.")
RESET_ALERT = "Your account has been reset — send /debut to start again."


def should_block_update(update, bot_username=None) -> bool:
    """True when a reset account's update must be turned away to /debut.

    Commands and button taps are blocked unless they are one of the doorway
    ones Rookie mode also leaves open (/debut, /start, help, membership…).
    Plain chatter passes: it isn't the player using a feature, and blocking it
    would swallow ordinary group messages.
    """
    from services.command_token import command_name_from_update
    from services.rookie_gate import is_free_callback, is_free_command
    query = getattr(update, "callback_query", None)
    if query is not None:
        return not is_free_callback(getattr(query, "data", "") or "")
    name = command_name_from_update(update, bot_username)
    return bool(name) and not is_free_command(name)
