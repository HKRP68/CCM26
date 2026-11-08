"""Tournament-wide team prize sharing, frozen before the first match.

Every award earned by a team uses the same owner/co-owner allocation, including
individual performance awards. Recipient Telegram ids survive registration
delays; account lookup happens separately when a payout is credited.
"""

import json
from services.account_archive import is_archived_identity
from datetime import datetime
from decimal import Decimal, InvalidOperation


def _manager_ids(team):
    from services.tournament_service import team_member_ids
    primary = getattr(team, "user_tg_id", None)
    ids = [int(primary)] if primary else []
    for tg in team_member_ids(team):
        if tg not in ids:
            ids.append(tg)
    # Archived co-owners retain their earned prize shares, without receiving
    # gameplay access through the positive-only ownership helpers.
    try:
        archived = json.loads(getattr(team, "co_owner_ids_json", None) or "[]")
    except (TypeError, ValueError):
        archived = []
    for tg in archived if isinstance(archived, list) else []:
        if is_archived_identity(tg) and int(tg) not in ids:
            ids.append(int(tg))
    return ids


def _load(raw):
    if raw is None:
        return None
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError
        out, seen = [], set()
        for row in rows:
            tg, weight = int(row["telegram_id"]), int(row["weight"])
            if (tg <= 0 and not is_archived_identity(tg)) or weight <= 0 or tg in seen:
                raise ValueError
            seen.add(tg)
            out.append({"telegram_id": tg, "weight": weight})
        return out
    except (TypeError, ValueError, KeyError):
        raise ValueError("The saved prize split is invalid. Ask an admin to check it.") from None


def _current(team):
    managers = _manager_ids(team)
    custom = _load(team.prize_split_json)
    if custom is not None:
        if {r["telegram_id"] for r in custom} != set(managers):
            raise ValueError("Team ownership changed. Reset or update its custom prize split before starting.")
        by_id = {r["telegram_id"]: r for r in custom}
        return [by_id[tg] for tg in managers]
    return [{"telegram_id": tg, "weight": 1} for tg in managers]


def split_amount(amount, recipients):
    """Return exact whole-unit shares; earlier recipients break equal remainders."""
    amount = int(amount)
    if amount < 0:
        raise ValueError("Prize amounts cannot be negative.")
    if not recipients:
        return []
    weights = [int(row["weight"]) for row in recipients]
    if any(weight <= 0 for weight in weights):
        raise ValueError("Prize split weights must be positive.")
    total = sum(weights)
    shares = [amount * weight // total for weight in weights]
    remainder_order = sorted(range(len(weights)),
                             key=lambda i: (-(amount * weights[i] % total), i))
    for index in remainder_order[:amount - sum(shares)]:
        shares[index] += 1
    return shares


def _lock_tournament(session, tour):
    from models import Tournament
    session.flush()
    if session.get_bind().dialect.name == "sqlite":
        session.query(Tournament).filter_by(id=tour.id).update({
            Tournament.updated_at: Tournament.updated_at,
        }, synchronize_session=False)
    return (session.query(Tournament).filter_by(id=tour.id)
            .populate_existing().with_for_update().one())


def _started(session, tour):
    from models import TournamentMatch
    if tour.activated_at or tour.awards_given_at:
        return True
    return (session.query(TournamentMatch.id)
            .filter(TournamentMatch.tournament_id == tour.id,
                    TournamentMatch.status.in_(("live", "completed")))
            .first() is not None)


def set_split(session, team_id, shares):
    """Save percentages or reset to equal shares; caller commits.

    ``shares`` maps every current owner's Telegram id to a percentage. Each
    percentage must be positive, have at most two decimal places and total 100.
    None or an empty mapping chooses the equal default.
    """
    from models import Tournament, TournamentTeam
    session.flush()
    team = session.get(TournamentTeam, int(team_id))
    if team is None:
        raise ValueError("Tournament team not found.")
    tour = _lock_tournament(session, session.get(Tournament, team.tournament_id))
    team = (session.query(TournamentTeam).filter_by(id=team.id)
            .populate_existing().one())
    if (team.prize_split_locked_at or _started(session, tour)) and not team.prize_split_reopened_at:
        raise ValueError("Prize splits are locked after the tournament starts.")
    if not shares:
        team.prize_split_json = None
        return team
    if not isinstance(shares, dict):
        raise ValueError("Give a percentage for every owner and co-owner.")
    cleaned = {}
    try:
        for raw_tg, raw_percent in shares.items():
            tg = int(raw_tg)
            if tg <= 0 or tg in cleaned:
                raise ValueError
            percent = Decimal(str(raw_percent))
            if not percent.is_finite() or not 0 < percent <= 100:
                raise ValueError
            bps = percent * 100
            if bps != bps.to_integral_value():
                raise ValueError
            cleaned[tg] = int(bps)
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError("Use positive percentages with at most two decimal places.") from None
    managers = _manager_ids(team)
    if set(cleaned) != set(managers):
        raise ValueError("Give a percentage for every current owner and co-owner, and no one else.")
    if sum(cleaned.values()) != 10000:
        raise ValueError("Prize percentages must total 100%.")
    team.prize_split_json = json.dumps([
        {"telegram_id": tg, "weight": cleaned[tg]} for tg in managers])
    return team


def _snapshot(team):
    frozen = _load(team.prize_split_snapshot_json)
    if frozen is not None:
        return frozen
    # An ownerless team can still have an unfinished redistribution draft.
    # Only the explicit apply action may freeze or pay that new allocation.
    if team.prize_split_reopened_at:
        return []
    current = _current(team)
    # Ownerless teams can acquire their first recipient later. Once any known
    # owners exist, even unregistered ones, the entire allocation is immutable.
    if current:
        team.prize_split_snapshot_json = json.dumps(current)
        team.prize_split_locked_at = datetime.utcnow()
    return current


def lock_splits(session, tour):
    """Snapshot every team's recipients on activation or its first match."""
    from models import TournamentTeam
    tour = _lock_tournament(session, tour)
    teams = (session.query(TournamentTeam).filter_by(tournament_id=tour.id)
             .populate_existing().order_by(TournamentTeam.sort_order, TournamentTeam.id).all())
    for team in teams:
        _snapshot(team)
    return teams


def recipients(session, tour, team):
    """Return and lazily freeze this team's recipient ids and exact weights."""
    from models import TournamentTeam
    _lock_tournament(session, tour)
    team = (session.query(TournamentTeam).filter_by(id=team.id, tournament_id=tour.id)
            .populate_existing().one())
    return _snapshot(team)


def split_settings(session, tour, team):
    """Read-only values for the split editor, including the immutable snapshot."""
    error = None
    reopened = bool(team.prize_split_reopened_at)
    try:
        frozen = _load(team.prize_split_snapshot_json)
        rows = _current(team) if reopened else frozen if frozen is not None else _current(team)
    except ValueError as exc:
        # Keep the editor accessible when owners change before activation.
        # Starting/settling still validates the saved allocation strictly.
        error = str(exc)
        rows = ([] if team.prize_split_snapshot_json is not None and not reopened else
                [{"telegram_id": tg, "weight": 1} for tg in _manager_ids(team)])
    bps = split_amount(10000, rows)
    members = [dict(row, percent=f"{percentage / 100:.2f}")
               for row, percentage in zip(rows, bps)]
    return {
        "locked": bool(team.prize_split_locked_at or _started(session, tour)) and not reopened,
        "reopened": reopened,
        "snapshot_token": team.prize_split_snapshot_json or "",
        "custom": bool(team.prize_split_json),
        "recipients": members,
        "percentages": {str(row["telegram_id"]): row["percent"] for row in members},
        "locked_at": team.prize_split_locked_at,
        "error": error,
    }
