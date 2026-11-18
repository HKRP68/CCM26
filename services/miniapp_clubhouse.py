"""Account-scoped views for the Mini App clubhouse."""

from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from models import ActivityLog, Giveaway, GiveawayEntry, Match, MatchScorecard, MatchState, MiniappGift
from services.match_outcome import derive_end_reason, end_reason_label, match_type_label

PAGE_SIZE = 15


def utc_string(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _team_name(user):
    return (user.team_name or user.first_name or user.username or "Opponent") if user else "Opponent"


def _page(query, model, before):
    if before:
        query = query.filter(model.id < before)
    rows = query.order_by(model.id.desc()).limit(PAGE_SIZE + 1).all()
    next_before = rows[PAGE_SIZE - 1][0].id if len(rows) > PAGE_SIZE else None
    return rows[:PAGE_SIZE], next_before


def matches(session, user, before):
    participant = or_(Match.user1_id == user.id, Match.user2_id == user.id)
    base = session.query(Match).options(joinedload(Match.user1), joinedload(Match.user2)).filter(participant)

    def describe(match):
        opponent = match.user2 if match.user1_id == user.id else match.user1
        return {"id": match.id, "opponent": _team_name(opponent),
                "mode": match_type_label(match)[0], "overs": match.overs}

    now = datetime.utcnow()
    state_exists = session.query(MatchState.match_id).filter(MatchState.match_id == Match.id).exists()
    active = (base.add_columns(state_exists.label("has_state"))
              .filter(Match.status.in_(("pending", "accepted", "toss", "selecting", "playing", "active", "in_progress", "paused")),
                      or_(Match.status.in_(("playing", "active", "in_progress", "paused")),
                          Match.expires_at.is_(None), Match.expires_at > now))
              .order_by(Match.id.desc()).limit(8).all()) if not before else []
    live = [dict(describe(m), status=m.status, can_open=bool(has_state and m.status in ("toss", "selecting", "playing", "active", "in_progress")))
            for m, has_state in active]

    has_card = session.query(MatchScorecard.match_id).filter(MatchScorecard.match_id == Match.id).exists()
    latest = base.filter(Match.status == "completed").add_columns(has_card.label("has_card"))
    rows, next_before = _page(latest, Match, before)
    recent = []
    for match, card in rows:
        result = (("Won" if match.winner_id == user.id else "Lost") if match.winner_id else
                  ("Tied" if match.margin_type in ("tie", "tied") else end_reason_label(derive_end_reason(match)[0])))
        margin = (f"{match.margin_value} {match.margin_type}"
                  if match.margin_type in ("runs", "wickets") and match.margin_value is not None else None)
        scores = [{"runs": runs, "wickets": wickets} for runs, wickets in (
            (match.inn1_runs, match.inn1_wickets), (match.inn2_runs, match.inn2_wickets)
        ) if runs is not None]
        recent.append(dict(describe(match), result=result, margin=margin,
                           date=utc_string(match.completed_at or match.created_at),
                           scores=scores, has_scorecard=bool(card)))
    return {"ok": True, "live": live, "matches": recent, "next_before": next_before}


def quick_results(session, user_id, before):
    rows, next_before = _page(session.query(ActivityLog, ActivityLog.id).filter(
        ActivityLog.user_id == user_id, ActivityLog.action == "quickmatch"), ActivityLog, before)
    return {"ok": True, "next_before": next_before, "matches": [
        {"summary": (log.detail or "").removeprefix("[Quick Match] ").capitalize(),
         "date": utc_string(log.created_at), "coins": log.coins_change or 0}
        for log, _ in rows]}


_WALLET_LABELS = {
    "market_buy": "Market signing", "buy_market": "Market signing", "buy": "Player signing",
    "release": "Player sale", "release_multi": "Squad sale", "gspin": "Lucky Card Pick",
    "daily": "Daily reward", "quickmatch": "Quick Match reward", "buy_pack": "Pack purchase",
    "miniapp_gift": "Clubhouse gift", "giveaway_coins": "Giveaway prize",
    "giveaway_gems": "Giveaway prize", "giveaway_player_coins": "Giveaway prize",
}


def wallet(session, user, before, currency):
    query = session.query(ActivityLog, ActivityLog.id).filter(ActivityLog.user_id == user.id)
    coins = ActivityLog.coins_change != 0
    gems = ActivityLog.gems_change != 0
    query = query.filter(coins if currency == "coins" else gems if currency == "gems" else or_(coins, gems))
    rows, next_before = _page(query, ActivityLog, before)
    transactions = [{
        "label": _WALLET_LABELS.get(log.action, log.action.replace("_", " ").capitalize()),
        "player": log.player_name,
        "date": utc_string(log.created_at),
        "coins": log.coins_change or 0, "gems": log.gems_change or 0,
        "coins_after": log.coins_balance_after, "gems_after": log.gems_balance_after,
    } for log, _ in rows]
    return {"ok": True, "transactions": transactions, "next_before": next_before,
            "balance": {"coins": user.total_coins or 0, "gems": user.total_gems or 0,
                        "quest_points": user.quest_points or 0}}


def gift_payload(gift):
    item = None
    if gift.player:
        item = f"{gift.player.name} · {gift.player.rating} OVR"
    elif gift.pack:
        item = gift.pack.name
    return {"id": gift.id, "title": gift.title, "currency": gift.currency,
            "amount": gift.amount, "item": item, "player_id": gift.player_id,
            "created_at": utc_string(gift.created_at), "claimed_at": utc_string(gift.claimed_at)}


def rewards(session, user_id):
    from services.giveaway_service import group_join_url, prize_label
    from models import GameConfig

    gifts = session.query(MiniappGift).options(joinedload(MiniappGift.player), joinedload(MiniappGift.pack)).filter_by(user_id=user_id)
    available = gifts.filter(MiniappGift.claimed_at.is_(None))
    inbox = available.order_by(MiniappGift.id.desc()).limit(30).all()
    inbox += gifts.filter(MiniappGift.claimed_at.isnot(None)).order_by(MiniappGift.id.desc()).limit(10).all()
    now = datetime.utcnow()
    query = session.query(Giveaway, GiveawayEntry).outerjoin(
        GiveawayEntry, (GiveawayEntry.giveaway_id == Giveaway.id) & (GiveawayEntry.user_id == user_id)
    ).options(joinedload(Giveaway.prize_player))
    active = query.filter(Giveaway.status == "running", Giveaway.start_time <= now, Giveaway.end_time > now)
    history = query.filter(Giveaway.status.in_(("ended", "cancelled")), GiveawayEntry.id.isnot(None))
    rows = active.order_by(Giveaway.end_time).limit(20).all() + history.order_by(Giveaway.id.desc()).limit(8).all()
    ids = [g.id for g, _ in rows]
    counts = dict(session.query(GiveawayEntry.giveaway_id, func.count(GiveawayEntry.id))
                  .filter(GiveawayEntry.giveaway_id.in_(ids)).group_by(GiveawayEntry.giveaway_id).all())
    return {"ok": True, "gifts": [gift_payload(g) for g in inbox], "unclaimed": available.count(),
            "group_link": group_join_url(session.query(GameConfig).first()),
            "giveaways": [{
                "id": g.id, "title": g.title, "status": g.status, "ends_at": utc_string(g.end_time),
                "prize": prize_label(g), "player_id": g.prize_player_id,
                "entries": counts.get(g.id, 0), "joined": entry is not None,
                "won": bool(entry and entry.is_winner), "requires_match": g.require_min_activity,
            } for g, entry in rows]}


def profile(session, user):
    """Read the player's actual cricket record and current-season rank."""
    from models import Player, PlayerGameStats, RankedRating, UserAchievement
    from services.achievement_service import CATALOG, get_top_unlocked
    from services.season_service import current_season_key
    from services.ranked_service import rank_of

    stats = (session.query(func.sum(PlayerGameStats.runs), func.sum(PlayerGameStats.wickets_taken),
                           func.sum(PlayerGameStats.hundreds), func.sum(PlayerGameStats.five_fers))
             .filter(PlayerGameStats.user_id == user.id).one())
    best_batter = (session.query(Player.name, PlayerGameStats.highest_score,
                                 PlayerGameStats.highest_score_not_out)
                   .join(Player, Player.id == PlayerGameStats.player_id)
                   .filter(PlayerGameStats.user_id == user.id, PlayerGameStats.highest_score > 0)
                   .order_by(PlayerGameStats.highest_score.desc(), PlayerGameStats.player_id).first())
    best_bowler = (session.query(Player.name, PlayerGameStats.best_bowl_wickets,
                                 PlayerGameStats.best_bowl_runs)
                   .join(Player, Player.id == PlayerGameStats.player_id)
                   .filter(PlayerGameStats.user_id == user.id, PlayerGameStats.best_bowl_wickets > 0)
                   .order_by(PlayerGameStats.best_bowl_wickets.desc(),
                             PlayerGameStats.best_bowl_runs, PlayerGameStats.player_id).first())
    ranked = session.query(RankedRating).filter_by(
        user_id=user.id, season_key=current_season_key(),).filter(RankedRating.played > 0).first()
    played, wins = user.matches_played or 0, user.matches_won or 0
    return {
        "ok": True, "name": user.first_name or user.username or "Player", "username": user.username,
        "team": user.team_name, "joined_at": utc_string(user.created_at),
        "played": played, "wins": wins, "losses": user.matches_lost or 0,
        "win_rate": round(wins * 100 / played, 1) if played else None,
        "best_streak": user.best_streak or 0,
        "quick_played": user.quick_matches_played or 0, "quick_wins": user.quick_matches_won or 0,
        "runs": stats[0] or 0, "wickets": stats[1] or 0,
        "hundreds": stats[2] or 0, "five_fers": stats[3] or 0,
        "best_batter": list(best_batter) if best_batter else None,
        "best_bowler": list(best_bowler) if best_bowler else None,
        "ranked": {"rating": ranked.rating, "rank": rank_of(session, ranked)} if ranked else None,
        "achievements": {
            "total": len(CATALOG),
            "unlocked": session.query(UserAchievement).filter(
                UserAchievement.user_id == user.id,
                UserAchievement.achievement_key.in_([entry["key"] for entry in CATALOG])
            ).count(),
            "recent": [{"name": a["name"], "emoji": a["emoji"]}
                       for a in get_top_unlocked(session, user.id)],
        },
    }
