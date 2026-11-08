"""Admin account recovery by rebinding identity, preserving the users.id graph.

Caller owns the transaction. Existing destinations are disabled and archived
before replacement. Never merge balances or grant admin privileges. Historical
actor fields stay intact.
"""
import hashlib
import json
import re

from sqlalchemy import or_, update
from services.account_archive import ARCHIVE_ID_BASE

from models import (ActivityLog, AdReward, AdminLog, AuctionFranchise,
                    AuctionLeagueSave, DraftTeam, GiveawayEntry,
                    MatchControlAccount, MatchControlAction, TeamLogoRequest,
                    TournamentPrizePayout, TournamentTeam, User, UserRoster,
                    UserTeamLastXI)


class TransferError(ValueError):
    pass


def telegram_id(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", raw.strip()):
        raise TransferError("Enter a positive numeric Telegram user ID (up to 16 digits).")
    value = int(raw.strip())
    if value > 2**52 - 1:
        raise TransferError("This Telegram user ID is outside the supported range.")
    return value


# These fields drive ownership, delivery, eligibility, or cooldowns. Audit actor
# fields (issued_by, by_tg_id, etc.) and AuctionAdmin are deliberately excluded.
IDENTITY_FIELDS = (
    (TeamLogoRequest, "telegram_id"), (AdReward, "telegram_id"),
    (GiveawayEntry, "telegram_id"), (UserTeamLastXI, "user_tg_id"),
    (TournamentTeam, "user_tg_id"), (TournamentTeam, "owner_tg_id"),
    (DraftTeam, "owner_tg_id"), (AuctionFranchise, "owner_tg_id"),
    (AuctionLeagueSave, "user_tg_id"),
    (TournamentPrizePayout, "recipient_tg_id"),
    (MatchControlAccount, "telegram_id"), (MatchControlAction, "telegram_id"),
)
JSON_FIELDS = (
    (TournamentTeam, "co_owner_ids_json", False),
    (TournamentTeam, "prize_split_json", True),
    (TournamentTeam, "prize_split_snapshot_json", True),
    (DraftTeam, "co_owner_ids_json", False),
    (AuctionFranchise, "co_owner_ids_json", False),
    (AuctionFranchise, "team_manager_ids_json", False),
)


def _plan(db, old, new, *, lock=False, archive_id=None):
    changes = []
    identities = {old: new}
    if archive_id is not None:
        identities[new] = archive_id
    for model, field in IDENTITY_FIELDS:
        column = getattr(model, field)
        query = db.query(model).filter(column.in_(identities)).order_by(model.__mapper__.primary_key[0])
        if lock:
            query = query.with_for_update()
        for row in query.all():
            before = getattr(row, field)
            changes.append((row, field, before, identities[before]))
    for model, field, split in JSON_FIELDS:
        query = db.query(model).filter(or_(*(getattr(model, field).contains(str(tg))
                                           for tg in identities))).order_by(model.id)
        if lock:
            query = query.with_for_update()
        for row in query.all():
            raw = getattr(row, field)
            try:
                values = json.loads(raw)
                if not isinstance(values, list):
                    raise ValueError
                if split:
                    replaced = [dict(item, telegram_id=identities[int(item["telegram_id"])])
                                if int(item["telegram_id"]) in identities else item for item in values]
                else:
                    replaced = [identities.get(int(item), item) for item in values]
            except (TypeError, ValueError, KeyError):
                raise TransferError("A saved ownership list is invalid. Repair it before transferring.") from None
            if replaced != values:
                # Avoid silently merging two separately assigned owners/shares.
                ids = [int(item["telegram_id"]) if split else int(item) for item in replaced]
                if len(ids) != len(set(ids)):
                    raise TransferError("The new ID already shares ownership of a team. Resolve that conflict first.")
                changes.append((row, field, raw, json.dumps(replaced)))
    return changes


def prepare(db, old_raw, new_raw, *, lock=False):
    old, new = telegram_id(old_raw), telegram_id(new_raw)
    if old == new:
        raise TransferError("Old and new Telegram IDs must be different.")
    # Lock both accounts in a fixed order, including opposite-direction requests.
    query = (db.query(User).filter(User.telegram_id.in_([old, new]))
             .order_by(User.id).populate_existing())
    rows = (query.with_for_update() if lock else query).all()
    accounts = {row.telegram_id: row for row in rows}
    user, destination = accounts.get(old), accounts.get(new)
    if user is None:
        raise TransferError("No saved game account exists for the old Telegram ID. Deleted game data requires a backup.")
    archive_id = -(ARCHIVE_ID_BASE + destination.id) if destination is not None else None
    if archive_id is not None and db.query(User.id).filter(User.telegram_id == archive_id).first():
        raise TransferError("The destination archive ID is already occupied. Ask support to check the archived account.")
    changes = _plan(db, old, new, lock=lock, archive_id=archive_id)
    summary = dict(user_id=user.id, old_id=old, new_id=new,
                   name=user.first_name or user.username or "Player", team=user.team_name,
                   coins=user.total_coins or 0, gems=user.total_gems or 0,
                   quest_points=user.quest_points or 0,
                   squad=db.query(UserRoster).filter_by(user_id=user.id).count(),
                   matches=user.matches_played or 0, subscription=user.subscription_tier,
                   banned=bool(user.is_banned or (destination and destination.is_banned)),
                   related_records=len(changes), destination=None)
    if destination is not None:
        summary["destination"] = dict(user_id=destination.id, archive_id=archive_id,
            name=destination.first_name or destination.username or "Player", team=destination.team_name,
            coins=destination.total_coins or 0, gems=destination.total_gems or 0,
            quest_points=destination.quest_points or 0,
            squad=db.query(UserRoster).filter_by(user_id=destination.id).count(),
            matches=destination.matches_played or 0, subscription=destination.subscription_tier)
    state = {column.name: getattr(user, column.name) for column in User.__table__.columns}
    destination_state = ({column.name: getattr(destination, column.name)
                          for column in User.__table__.columns} if destination is not None else None)
    refs = [(r.__tablename__, tuple(getattr(r, col.key) for col in r.__mapper__.primary_key), f, before)
            for r, f, before, _ in changes]
    digest = hashlib.sha256(json.dumps([summary, state, destination_state, refs], sort_keys=True, default=str).encode()).hexdigest()
    return user, summary, digest, changes


def transfer(db, old, new, *, expected_user_id, expected_digest, reason, ip_address=None,
             replace_destination=False):
    reason = (reason or "").strip()
    if not 10 <= len(reason) <= 200:
        raise TransferError("Record how ownership was verified (10–200 characters).")
    user, summary, digest, changes = prepare(db, old, new, lock=True)
    if user.id != expected_user_id or digest != expected_digest:
        raise TransferError("Account data changed after preview. Review a fresh preview before confirming.")
    destination_summary = summary["destination"]
    if destination_summary and not replace_destination:
        raise TransferError("Confirm replacement of the new ID's existing game account before transferring.")
    destination = db.get(User, destination_summary["user_id"]) if destination_summary else None
    profile = dict(username=None, first_name=None, dm_blocked=False, last_miniapp_chat_id=None)
    if destination is not None:
        profile.update(username=destination.username, first_name=destination.first_name)
        # Keep moderation in force when replacing a banned destination account.
        if destination.is_banned and not user.is_banned:
            profile.update(is_banned=True, warning_ban=False, banned_at=destination.banned_at,
                           ban_reason=destination.ban_reason or "Destination account was suspended.")
        previous_status = dict(is_banned=destination.is_banned,
            warning_ban=destination.warning_ban,
            notifications_enabled=destination.notifications_enabled,
            dm_blocked=destination.dm_blocked,
            last_miniapp_chat_id=destination.last_miniapp_chat_id,
            had_ban_reason=bool(destination.ban_reason))
        archived = db.execute(update(User).where(User.id == destination.id,
            User.telegram_id == summary["new_id"]).values(
                telegram_id=destination_summary["archive_id"], is_banned=True,
                warning_ban=False, ban_reason=destination.ban_reason or
                    f"Archived by account transfer to game account #{user.id}.",
                notifications_enabled=False, dm_blocked=True, last_miniapp_chat_id=None)
            .execution_options(synchronize_session=False))
        if archived.rowcount != 1:
            raise TransferError("The destination account changed. Create a fresh preview.")
        # Free destination unique keys before moving source records into them.
        # Both flushes remain in the caller's single transaction.
        for row, field, before, after in changes:
            if before == summary["new_id"] and not isinstance(before, str):
                setattr(row, field, after)
        db.flush()
        db.expire(destination)
        db.add(AdminLog(action="account_transfer_archive", target_type="user",
            target_id=destination.id, target_name=destination_summary["team"] or destination_summary["name"],
            detail=json.dumps(dict(replaced_telegram_id=summary["new_id"],
                archive_id=destination_summary["archive_id"], recovered_user_id=user.id,
                coins=destination_summary["coins"], gems=destination_summary["gems"],
                squad=destination_summary["squad"],
                previous_status=previous_status), ensure_ascii=False),
            ip_address=ip_address))
    # Conditional write also serializes competing transfers on SQLite, where
    # SELECT FOR UPDATE is ignored. The unique ID constraint handles target races.
    changed = db.execute(update(User).where(User.id == user.id, User.telegram_id == summary["old_id"])
                         .values(telegram_id=summary["new_id"], **profile)
                         .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise TransferError("This account was already transferred. Review its current Telegram ID.")
    for row, field, before, after in changes:
        setattr(row, field, after)
    detail = json.dumps(dict(old_id=summary["old_id"], new_id=summary["new_id"],
                             coins=summary["coins"], gems=summary["gems"],
                             squad=summary["squad"], verification=reason,
                             archived_user_id=destination.id if destination is not None else None), ensure_ascii=False)
    # Same transaction: audit failure must roll back the identity change too.
    receipt = AdminLog(action="account_transfer", target_type="user", target_id=user.id,
                       target_name=summary["team"] or summary["name"], detail=detail,
                       ip_address=ip_address)
    db.add(receipt)
    db.add(ActivityLog(user_id=user.id, action="account_transfer",
                       detail=f'Telegram account recovered: {summary["old_id"]} → {summary["new_id"]}.'))
    db.flush()
    db.expire(user)
    return user, receipt
