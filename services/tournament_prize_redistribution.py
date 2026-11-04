"""Explicitly reopen a locked team split and replace all its prize shares."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime


def _team(session, tour, team_id):
    from models import TournamentTeam
    team = (session.query(TournamentTeam)
            .filter_by(id=int(team_id), tournament_id=tour.id)
            .populate_existing().one_or_none())
    if team is None:
        raise ValueError("Team not found in this tournament.")
    return team


def reopen_split(session, tour, team_id, actor=None):
    """Open the admin editor without changing recipients or paying any money."""
    from services.tournament_prize_splits import _lock_tournament, _started
    with session.begin_nested():
        tour = _lock_tournament(session, tour)
        team = _team(session, tour, team_id)
        if not (team.prize_split_locked_at or _started(session, tour)):
            raise ValueError("Prize percentages are not locked yet.")
        team.prize_split_reopened_at = team.prize_split_reopened_at or datetime.utcnow()
        session.flush()
        return team


def _request(team_id, shares, reason, expected_snapshot):
    from services.tournament_prize_corrections import _normalise_shares
    normalised = _normalise_shares(shares) if shares else None
    return {
        "team_id": int(team_id),
        "shares": sorted(normalised, key=lambda row: row["telegram_id"]) if normalised else None,
        "reason": reason,
        "expected_snapshot": expected_snapshot,
    }


def _recipients(team, request):
    from services.tournament_prize_splits import _manager_ids
    managers = _manager_ids(team)
    if not managers:
        raise ValueError("Assign an owner or co-owner before applying redistribution.")
    custom = request["shares"]
    if custom is None:
        return [{"telegram_id": tg, "weight": 1} for tg in managers]
    by_id = {row["telegram_id"]: row["weight"] for row in custom}
    if set(by_id) != set(managers):
        raise ValueError("Give a percentage for every current owner and co-owner, and no one else.")
    return [{"telegram_id": tg, "weight": by_id[tg]} for tg in managers]


def _awards(session, tour, team):
    from models import TournamentHonour, TournamentPrizePayout
    from sqlalchemy import and_, or_
    honours = (session.query(TournamentHonour)
               .filter(TournamentHonour.tournament_id == tour.id,
                       or_(TournamentHonour.tournament_team_id == team.id,
                           and_(TournamentHonour.tournament_team_id.is_(None),
                                TournamentHonour.team_name == team.name)))
               .populate_existing().order_by(TournamentHonour.id).all())
    result = []
    for honour in honours:
        if not (honour.team_name or honour.player_name):
            continue
        has_rows = session.query(TournamentPrizePayout.id).filter_by(
            honour_id=honour.id, revision=honour.prize_revision or 0).first() is not None
        if (has_rows or honour.prize_snapshot_coins is not None
                or honour.prize_coins or honour.prize_gems):
            result.append(honour)
    return result


def _recoverability(session, honours):
    """Recover the entire old pool before replacement credits can subsidise it."""
    from models import TournamentPrizePayout, User
    from services.tournament_payouts import _lock_payout
    if not honours:
        return
    current = {honour.id: honour.prize_revision or 0 for honour in honours}
    rows = (session.query(TournamentPrizePayout)
            .filter(TournamentPrizePayout.honour_id.in_(current))
            .populate_existing().all())
    needs = defaultdict(lambda: [0, 0])
    identities = {}
    for original in sorted(rows, key=lambda row: (row.recipient_tg_id, row.id)):
        if original.revision != current[original.honour_id]:
            continue
        row = _lock_payout(session, original)
        if row.status != "paid" or not (row.coins or row.gems):
            continue
        if not row.user_id:
            raise ValueError("A previous paid recipient account is unavailable; no balances changed.")
        needs[row.user_id][0] += row.coins
        needs[row.user_id][1] += row.gems
        identities[row.user_id] = row.recipient_tg_id
    for user_id in sorted(needs):
        user = (session.query(User).filter_by(id=user_id)
                .populate_existing().with_for_update().one_or_none())
        if user is None or user.telegram_id != identities[user_id]:
            raise ValueError("A previous paid recipient account is unavailable; no balances changed.")
        coins, gems = needs[user_id]
        if (user.total_coins or 0) < coins or (user.total_gems or 0) < gems:
            raise ValueError(
                f"Recipient {user.telegram_id} has insufficient coins or gems to reverse "
                "the team's original prizes. No balances changed.")


def redistribute_team(session, tour, team_id, shares, reason, request_key,
                      expected_snapshot, actor=None):
    """Replace a team's prize pool atomically, then lock the corrected split."""
    from models import TournamentPrizeCorrection, TournamentPrizeRedistribution
    from services.tournament_prize_corrections import correct_award
    from services.tournament_prize_splits import _lock_tournament, split_amount
    from services.tournament_payouts import _from_remaining, plan_honours
    reason = str(reason or "").strip()
    request_key = str(request_key or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("A correction reason of at most 500 characters is required.")
    if not request_key or len(request_key) > 64:
        raise ValueError("A valid redistribution request key is required.")
    expected_snapshot = str(expected_snapshot or "")
    request = _request(team_id, shares, reason, expected_snapshot)
    with session.begin_nested():
        tour = _lock_tournament(session, tour)
        team = _team(session, tour, team_id)
        previous = session.query(TournamentPrizeRedistribution).filter_by(
            request_key=request_key).first()
        if previous:
            saved = json.loads(previous.new_split_json)
            if (previous.tournament_id != tour.id or previous.team_id != team.id
                    or saved.get("request") != request):
                raise ValueError("This redistribution request key was already used for another change.")
            keys = saved.get("correction_keys", [])
            return (session.query(TournamentPrizeCorrection)
                    .filter(TournamentPrizeCorrection.request_key.in_(keys))
                    .order_by(TournamentPrizeCorrection.id).all()) if keys else []
        if not team.prize_split_reopened_at:
            raise ValueError("Reopen the locked percentages before applying redistribution.")
        if expected_snapshot != (team.prize_split_snapshot_json or ""):
            raise ValueError("The locked split changed. Refresh the page and review the redistribution.")
        recipients = _recipients(team, request)
        honours = _awards(session, tour, team)
        # Backfill paid legacy receipts without issuing any additional credit.
        plan_honours(session, tour, honours)
        _recoverability(session, honours)
        amounts = [(honour,
                    int(honour.prize_snapshot_coins if honour.prize_snapshot_coins is not None
                        else honour.prize_coins or 0),
                    int(honour.prize_snapshot_gems if honour.prize_snapshot_gems is not None
                        else honour.prize_gems or 0)) for honour in honours]
        keys = [hashlib.sha256(f"team-prize-redistribution:{request_key}:{honour.id}".encode()).hexdigest()
                for honour, _, _ in amounts]
        batch = TournamentPrizeRedistribution(
            tournament_id=tour.id, team_id=team.id, request_key=request_key,
            reason=reason, corrected_by=str(actor)[:120] if actor is not None else None,
            old_split_json=team.prize_split_snapshot_json or "[]",
            new_split_json=json.dumps({"request": request, "recipients": recipients,
                                       "correction_keys": keys}, sort_keys=True))
        session.add(batch)
        session.flush()
        coins_left = split_amount(sum(coins for _, coins, _ in amounts), recipients)
        gems_left = split_amount(sum(gems for _, _, gems in amounts), recipients)
        # Percentages are supplied for validation; internal allocations preserve
        # equal thirds and cumulative rounding more exactly than display bps.
        display_bps = split_amount(10000, recipients)
        percentages = {recipient["telegram_id"]: f"{bps / 100:.2f}"
                       for recipient, bps in zip(recipients, display_bps)}
        corrected = []
        for (honour, coins, gems), key in zip(amounts, keys):
            coin_shares = _from_remaining(coins, coins_left)
            gem_shares = _from_remaining(gems, gems_left)
            allocation = {recipient["telegram_id"]: {"coins": coin_shares[i], "gems": gem_shares[i]}
                          for i, recipient in enumerate(recipients)}
            correction = correct_award(
                session, tour, honour.id, coins=coins, gems=gems, reason=reason,
                request_key=key, expected_revision=honour.prize_revision or 0,
                actor=actor, shares=percentages, _allocations=allocation)
            # Display percentages may round equal thirds to 33.34/33.33/33.33.
            # Preserve the actual weights so a later amount-only correction
            # keeps the exact management policy rather than that display rounding.
            details = json.loads(correction.new_details_json)
            details["recipients"] = recipients
            details["redistribution_request_key"] = request_key
            correction.new_details_json = json.dumps(details)
            corrected.append(correction)
            coins_left = [units - coin_shares[i] for i, units in enumerate(coins_left)]
            gems_left = [units - gem_shares[i] for i, units in enumerate(gems_left)]
        team.prize_split_json = json.dumps(recipients) if request["shares"] is not None else None
        team.prize_split_snapshot_json = json.dumps(recipients)
        team.prize_split_locked_at = datetime.utcnow()
        team.prize_split_reopened_at = None
        session.flush()
        return corrected
