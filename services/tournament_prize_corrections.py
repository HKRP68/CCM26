"""Audited reversals and replacement prizes; all balance changes are atomic."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation


class PrizeCorrectionError(ValueError):
    pass


def _normalise_shares(shares):
    if not isinstance(shares, dict) or not shares:
        raise PrizeCorrectionError("Give Telegram IDs and percentages totalling 100%.")
    result, seen = [], set()
    try:
        for raw_tg, raw_percent in shares.items():
            tg = int(raw_tg)
            percent = Decimal(str(raw_percent).strip().removesuffix("%"))
            bps = percent * 100
            if (tg <= 0 or tg in seen or not percent.is_finite()
                    or not 0 < percent <= 100 or bps != bps.to_integral_value()):
                raise ValueError
            seen.add(tg)
            result.append({"telegram_id": tg, "weight": int(bps)})
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        raise PrizeCorrectionError("Use distinct positive Telegram IDs and positive percentages with at most two decimal places.") from None
    if sum(row["weight"] for row in result) != 10000:
        raise PrizeCorrectionError("Prize percentages must total 100%.")
    return result


def parse_shares(text):
    shares = {}
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        pieces = line.split(":")
        if len(pieces) != 2:
            raise PrizeCorrectionError("Enter one Telegram ID:percentage per line.")
        try:
            tg = int(pieces[0].strip())
        except ValueError:
            raise PrizeCorrectionError("Enter a positive Telegram ID before each percentage.") from None
        if tg in shares:
            raise PrizeCorrectionError("A recipient can appear only once.")
        shares[tg] = pieces[1].strip()
    if not shares:
        return {}
    rows = _normalise_shares(shares)
    return {row["telegram_id"]: f"{row['weight'] / 100:.2f}" for row in rows}


def _amount(value, maximum, currency):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value() or not 0 <= number <= maximum:
            raise ValueError
        return int(number)
    except (ValueError, TypeError, InvalidOperation):
        raise PrizeCorrectionError(f"Give whole {currency} between 0 and {maximum:,}.") from None


def _details(honour, rows):
    def stamp(value):
        return value.isoformat() if value else None
    return {
        "award": honour.award, "team_id": honour.tournament_team_id,
        "team_name": honour.team_name, "player_name": honour.player_name,
        "user_id": honour.user_id, "value": honour.value,
        "revision": honour.prize_revision or 0,
        "coins": honour.prize_snapshot_coins or 0,
        "gems": honour.prize_snapshot_gems or 0,
        "receipts": [{
            "id": row.id, "telegram_id": row.recipient_tg_id,
            "user_id": row.user_id, "coins": row.coins, "gems": row.gems,
            "share_bps": row.share_bps, "status": row.status,
            "paid_at": stamp(row.paid_at), "reversed_at": stamp(row.reversed_at),
            "balance_coins_after": row.balance_coins_after,
            "balance_gems_after": row.balance_gems_after,
            "reversal_coins_after": row.reversal_coins_after,
            "reversal_gems_after": row.reversal_gems_after,
        } for row in rows],
    }


def _existing_shares(session, tour, honour, rows):
    from models import TournamentPrizeCorrection, TournamentTeam
    from services.tournament_prize_splits import _load, split_amount
    if honour.prize_revision:
        previous = (session.query(TournamentPrizeCorrection)
                    .filter_by(honour_id=honour.id, revision=honour.prize_revision).first())
        if previous:
            saved = json.loads(previous.new_details_json).get("recipients")
            # Recovery can rebind a paid account's Telegram identity. Keep the
            # historical correction receipt intact, but use current payout
            # identities when they no longer match that historical snapshot.
            if saved and (not rows or {int(r["telegram_id"]) for r in saved}
                          == {r.recipient_tg_id for r in rows}):
                return saved
    if rows:
        team = session.get(TournamentTeam, honour.tournament_team_id) if honour.tournament_team_id else None
        frozen = _load(team.prize_split_snapshot_json) if team and team.prize_split_snapshot_json else None
        # Preserve exact equal fractions, rather than turning thirds into a
        # rounded 33.34/33.33/33.33 custom split during a correction.
        if frozen and {r.recipient_tg_id for r in rows} == {r["telegram_id"] for r in frozen}:
            bps = dict(zip([r["telegram_id"] for r in frozen], split_amount(10000, frozen)))
            if all(row.share_bps == bps[row.recipient_tg_id] for row in rows):
                return frozen
        if any(not row.recipient_tg_id for row in rows):
            raise PrizeCorrectionError("Choose a corrected team or explicit recipients for this unassigned prize.")
        return [{"telegram_id": row.recipient_tg_id, "weight": row.share_bps} for row in rows]
    from services.tournament_payouts import _recipients
    return _recipients(session, tour, honour)[1]


def correct_award(session, tour, honour_id, coins, gems, reason, request_key,
                  expected_revision, actor=None, team_id=None, player_name=None,
                  shares=None, _allocations=None):
    """Reverse an award and issue its replacement, or leave everything unchanged.

    Caller commits. An explicit savepoint also protects callers that catch an
    error and commit other work. Request keys and displayed revisions prevent
    duplicate browser submits from reversing a replacement a second time.
    """
    from models import TournamentHonour, TournamentPrizeCorrection, TournamentPrizePayout, TournamentTeam, User
    from services import tournament_awards as A, tournament_payouts as P
    from services.tournament_prize_splits import recipients, split_amount
    coins = _amount(coins, A.MAX_PRIZE_COINS, "coins")
    gems = _amount(gems, A.MAX_PRIZE_GEMS, "gems")
    reason = str(reason or "").strip()
    request_key = str(request_key or "").strip()
    if not reason or len(reason) > 500:
        raise PrizeCorrectionError("Give a reason for the correction (up to 500 characters).")
    if not request_key or len(request_key) > 64:
        raise PrizeCorrectionError("Refresh the correction form before submitting.")
    try:
        expected_revision, honour_id = int(expected_revision), int(honour_id)
        team_id = int(team_id) if team_id is not None else None
    except (ValueError, TypeError):
        raise PrizeCorrectionError("Choose a valid award and refresh its displayed revision.") from None
    if player_name is not None:
        player_name = str(player_name).strip()
        if len(player_name) > 150:
            raise PrizeCorrectionError("The winning player name is too long.")
    chosen = _normalise_shares(shares) if shares is not None else None
    allocations = ({str(int(tg)): {"coins": int(value["coins"]), "gems": int(value["gems"])}
                    for tg, value in _allocations.items()} if _allocations is not None else None)
    request = dict(honour_id=honour_id, coins=coins, gems=gems, reason=reason,
                   team_id=team_id, player_name=player_name, shares=chosen,
                   allocations=allocations)
    session.flush()
    try:
        with session.begin_nested():
            tour = A._lock_tournament(session, tour)
            duplicate = session.query(TournamentPrizeCorrection).filter_by(request_key=request_key).first()
            if duplicate:
                if (duplicate.tournament_id != tour.id or duplicate.honour_id != honour_id
                        or json.loads(duplicate.new_details_json).get("request") != request):
                    raise PrizeCorrectionError("This correction key was already used for a different change. Refresh the form.")
                return duplicate
            honour = (session.query(TournamentHonour)
                      .filter_by(id=honour_id, tournament_id=tour.id).populate_existing().first())
            if honour is None:
                raise PrizeCorrectionError("Award not found in this tournament.")
            if (honour.prize_revision or 0) != expected_revision:
                raise PrizeCorrectionError("This prize changed. Refresh before correcting its latest revision.")
            P.plan_honours(session, tour, [honour])
            old_rows = (session.query(TournamentPrizePayout)
                        .filter_by(honour_id=honour.id, revision=expected_revision)
                        .order_by(TournamentPrizePayout.recipient_tg_id, TournamentPrizePayout.id).all())
            # Lock receipts before the honour, matching registration retries.
            old_rows = [P._lock_payout(session, row) for row in old_rows]
            honour = (session.query(TournamentHonour).filter_by(id=honour.id)
                      .populate_existing().with_for_update().one())
            target = None
            if team_id is not None:
                target = session.query(TournamentTeam).filter_by(id=team_id, tournament_id=tour.id).first()
                if target is None:
                    raise PrizeCorrectionError("Corrected team not found in this tournament.")
            if chosen is None:
                chosen = ((recipients(session, tour, target) if target else _existing_shares(session, tour, honour, old_rows))
                          if coins or gems else [])
            if (coins or gems) and (not chosen or any(not row["telegram_id"] for row in chosen)):
                raise PrizeCorrectionError("Assign a corrected team owner or enter explicit recipient percentages.")
            users, required = {}, {}
            for row in old_rows:
                if row.status != "paid" or not (row.coins or row.gems):
                    continue
                user = session.get(User, row.user_id) if row.user_id else None
                if user is None or user.telegram_id != row.recipient_tg_id:
                    raise PrizeCorrectionError(f"The original paid account for receipt #{row.id} is unavailable; no balances changed.")
                users[user.id] = user
                entry = required.setdefault(user.id, [0, 0])
                entry[0] += row.coins
                entry[1] += row.gems
            for uid in sorted(users):
                user = (session.query(User).filter_by(id=uid).populate_existing().with_for_update().one())
                due_coins, due_gems = required[uid]
                if (user.total_coins or 0) < due_coins or (user.total_gems or 0) < due_gems:
                    raise PrizeCorrectionError(
                        f"Insufficient balance for {user.telegram_id}: reversing this prize requires "
                        f"{due_coins:,} coins and {due_gems:,} gems. No balances changed.")
                users[uid] = user
            old_details = _details(honour, old_rows)
            correction = TournamentPrizeCorrection(
                tournament_id=tour.id, honour_id=honour.id, request_key=request_key,
                previous_revision=expected_revision, revision=expected_revision + 1,
                reason=reason, corrected_by=str(actor or "admin")[:120],
                old_details_json=json.dumps(old_details), new_details_json="{}")
            session.add(correction)
            session.flush()
            from services.activity_service import log_activity
            from sqlalchemy import func
            for row in old_rows:
                row.correction_id = correction.id
                if row.status == "paid":
                    if row.coins or row.gems:
                        user = users[row.user_id]
                        session.query(User).filter_by(id=user.id).update({
                            User.total_coins: func.coalesce(User.total_coins, 0) - row.coins,
                            User.total_gems: func.coalesce(User.total_gems, 0) - row.gems,
                        }, synchronize_session="fetch")
                        session.refresh(user, attribute_names=["total_coins", "total_gems"])
                        row.reversal_coins_after, row.reversal_gems_after = user.total_coins, user.total_gems
                        log_activity(session, user.id, "tournament_prize_reversal",
                                     f"Correction #{correction.id}, receipt #{row.id} — {tour.name}: {reason}",
                                     coins_change=-row.coins, gems_change=-row.gems)
                    row.status = "reversed"
                    row.reversed_at = datetime.utcnow()
                else:
                    row.status = "cancelled"
            if target:
                if honour.tournament_team_id != target.id:
                    honour.value = None
                honour.tournament_team_id, honour.team_name = target.id, target.name
            if player_name is not None and player_name != honour.player_name:
                honour.player_name, honour.value = player_name or None, None
            honour.prize_revision = expected_revision + 1
            honour.prize_snapshot_coins, honour.prize_snapshot_gems = coins, gems
            honour.prize_coins = honour.prize_gems = 0
            honour.user_id = None
            A.set_prize(tour, honour.award, coins, gems)
            if allocations is not None:
                if (set(allocations) != {str(row["telegram_id"]) for row in chosen}
                        or any(min(v["coins"], v["gems"]) < 0 for v in allocations.values())
                        or sum(v["coins"] for v in allocations.values()) != coins
                        or sum(v["gems"] for v in allocations.values()) != gems):
                    raise PrizeCorrectionError("Corrected allocation does not match the award total.")
                coin_parts = [allocations[str(row["telegram_id"])]["coins"] for row in chosen]
                gem_parts = [allocations[str(row["telegram_id"])]["gems"] for row in chosen]
            elif shares is None and honour.tournament_team_id:
                # A correction to one team award must preserve the rounding
                # of its other awards, including an unchanged odd-sized prize.
                from services.tournament_prize_splits import _load
                team = session.get(TournamentTeam, honour.tournament_team_id)
                frozen = _load(team.prize_split_snapshot_json) if team and team.prize_split_snapshot_json else None
                own = {r["telegram_id"]: r["weight"] for r in chosen}
                locked = {r["telegram_id"]: r["weight"] for r in frozen or []}
                matches = (set(own) == set(locked) and all(
                    own[tg] * sum(locked.values()) == locked[tg] * sum(own.values()) for tg in own))
                if matches:
                    session.flush()
                    reserved = (session.query(TournamentPrizePayout)
                                .join(TournamentHonour, TournamentPrizePayout.honour_id == TournamentHonour.id)
                                .filter(TournamentHonour.tournament_id == tour.id,
                                        TournamentHonour.tournament_team_id == team.id,
                                        TournamentPrizePayout.revision == TournamentHonour.prize_revision,
                                        TournamentPrizePayout.status.in_(("paid", "pending", "failed"))).all())
                    coin_parts = P._pool_targets(coins, chosen, reserved, "coins")
                    gem_parts = P._pool_targets(gems, chosen, reserved, "gems")
                else:
                    coin_parts, gem_parts = split_amount(coins, chosen), split_amount(gems, chosen)
            else:
                coin_parts, gem_parts = split_amount(coins, chosen), split_amount(gems, chosen)
            bps = split_amount(10000, chosen)
            new_rows = []
            for i, share in enumerate(chosen):
                row = P._new_row(honour, share["telegram_id"], coin_parts[i], gem_parts[i], bps[i],
                                 status="pending", correction_id=correction.id)
                session.add(row)
                new_rows.append(row)
            session.flush()
            P._settle_rows(session, new_rows)
            if any(row.status == "failed" for row in new_rows):
                raise PrizeCorrectionError("The replacement payment failed. Previous credits and all balances were restored; retry later.")
            P._sync_honours(session, new_rows)
            new_details = _details(honour, new_rows)
            new_details.update(request=request, recipients=chosen)
            correction.new_details_json = json.dumps(new_details)
            session.flush()
            return correction
    except PrizeCorrectionError:
        raise
    except Exception as exc:
        raise PrizeCorrectionError("The prize correction could not complete. No balances changed; retry after resolving the payment error.") from exc


def reopen_split(*args, **kwargs):
    from services.tournament_prize_redistribution import reopen_split as apply
    return apply(*args, **kwargs)


def redistribute_team(*args, **kwargs):
    from services.tournament_prize_redistribution import redistribute_team as apply
    return apply(*args, **kwargs)
