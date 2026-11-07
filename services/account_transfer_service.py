"""Admin account recovery by rebinding identity, preserving the users.id graph.

Caller owns the transaction. Never merge/delete a destination account, grant
admin privileges, or commit partial changes. Historical actor fields stay intact.
"""
import hashlib
import json
import re

from sqlalchemy import update

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


def _plan(db, old, new, *, lock=False):
    changes = []
    for model, field in IDENTITY_FIELDS:
        column = getattr(model, field)
        query = db.query(model).filter(column == old).order_by(model.__mapper__.primary_key[0])
        if lock:
            query = query.with_for_update()
        for row in query.all():
            # A destination with saved XI/control/prize entries can conflict even
            # before /debut. Fail the entire transfer rather than combine records.
            changes.append((row, field, old, new))
    for model, field, split in JSON_FIELDS:
        query = db.query(model).filter(getattr(model, field).contains(str(old))).order_by(model.id)
        if lock:
            query = query.with_for_update()
        for row in query.all():
            raw = getattr(row, field)
            try:
                values = json.loads(raw)
                if not isinstance(values, list):
                    raise ValueError
                if split:
                    replaced = [dict(item, telegram_id=new)
                                if int(item["telegram_id"]) == old else item for item in values]
                else:
                    replaced = [new if int(item) == old else item for item in values]
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
    query = db.query(User).filter(User.telegram_id == old).populate_existing()
    user = (query.with_for_update() if lock else query).first()
    if user is None:
        raise TransferError("No saved game account exists for the old Telegram ID. Deleted game data requires a backup.")
    if db.query(User.id).filter(User.telegram_id == new).first():
        raise TransferError("The new Telegram ID already has a game account. Transfer is blocked to protect both accounts; use an unused ID.")
    changes = _plan(db, old, new, lock=lock)
    summary = dict(user_id=user.id, old_id=old, new_id=new,
                   name=user.first_name or user.username or "Player", team=user.team_name,
                   coins=user.total_coins or 0, gems=user.total_gems or 0,
                   quest_points=user.quest_points or 0,
                   squad=db.query(UserRoster).filter_by(user_id=user.id).count(),
                   matches=user.matches_played or 0, subscription=user.subscription_tier,
                   banned=bool(user.is_banned), related_records=len(changes))
    state = {column.name: getattr(user, column.name) for column in User.__table__.columns}
    refs = [(r.__tablename__, tuple(getattr(r, col.key) for col in r.__mapper__.primary_key), f, before)
            for r, f, before, _ in changes]
    digest = hashlib.sha256(json.dumps([summary, state, refs], sort_keys=True, default=str).encode()).hexdigest()
    return user, summary, digest, changes


def transfer(db, old, new, *, expected_user_id, expected_digest, reason, ip_address=None):
    reason = (reason or "").strip()
    if not 10 <= len(reason) <= 200:
        raise TransferError("Record how ownership was verified (10–200 characters).")
    user, summary, digest, changes = prepare(db, old, new, lock=True)
    if user.id != expected_user_id or digest != expected_digest:
        raise TransferError("Account data changed after preview. Review a fresh preview before confirming.")
    # Conditional write also serializes competing transfers on SQLite, where
    # SELECT FOR UPDATE is ignored. The unique ID constraint handles target races.
    changed = db.execute(update(User).where(User.id == user.id, User.telegram_id == summary["old_id"])
                         .values(telegram_id=summary["new_id"], username=None, first_name=None,
                                 dm_blocked=False, last_miniapp_chat_id=None)
                         .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise TransferError("This account was already transferred. Review its current Telegram ID.")
    for row, field, before, after in changes:
        setattr(row, field, after)
    detail = json.dumps(dict(old_id=summary["old_id"], new_id=summary["new_id"],
                             coins=summary["coins"], gems=summary["gems"],
                             squad=summary["squad"], verification=reason), ensure_ascii=False)
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
