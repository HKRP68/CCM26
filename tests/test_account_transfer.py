"""Recovery preserves the account graph and rejects unsafe confirmations."""
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from database import Base
from models import (ActivityLog, AdminLog, AuctionAdmin, AuctionFranchise,
                    MatchControlAccount, Player, Tournament, TournamentTeam,
                    User, UserRoster, UserStats, UserWarning)
from services import account_transfer_service as service

OLD, NEW = 712345678, 798765432
REASON = "Verified original purchase receipt and support ticket #123."


@pytest.fixture
def account(tmp_path, monkeypatch):
    import database
    import admin
    engine = create_engine(f"sqlite:///{tmp_path / 'transfer.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(admin, "get_session", factory)
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", True)
    db = factory()
    user = User(telegram_id=OLD, first_name="Old Player", username="old_player",
                total_coins=123456, total_gems=58, quest_points=70,
                matches_played=20, subscription_tier="gold", is_banned=True,
                ban_reason="Preserve restrictions", dm_blocked=True)
    db.add(user)
    db.flush()
    player = Player(name="Career player", rating=85, category="Batsman", country="India",
                    bat_hand="Right", bowl_hand="Right", bowl_style="Medium",
                    is_career=True, career_owner_user_id=user.id)
    db.add(player)
    db.flush()
    slot = UserRoster(user_id=user.id, player_id=player.id, order_position=1)
    db.add_all([slot, UserStats(user_id=user.id),
                UserWarning(user_id=user.id, reason="Saved warning", issued_by=OLD),
                MatchControlAccount(telegram_id=OLD, blocked=True),
                AuctionAdmin(tg_id=OLD)])
    tour = Tournament(name="Recovery cup", kind="letsplay")
    db.add(tour)
    db.flush()
    team = TournamentTeam(tournament_id=tour.id, user_tg_id=OLD, owner_tg_id=OLD,
                          name="Recover XI", co_owner_ids_json="[555]",
                          prize_split_json=json.dumps([{"telegram_id": OLD, "weight": 1}]),
                          prize_split_snapshot_json=json.dumps([{"telegram_id": OLD, "weight": 1}]))
    db.add(team)
    db.commit()
    yield SimpleNamespace(db=db, factory=factory, user=user, player=player, slot=slot,
                          team=team, app=admin.app, client=admin.app.test_client())
    db.close()
    engine.dispose()


def execute(account, **overrides):
    _, _, digest, _ = service.prepare(account.db, str(OLD), str(NEW))
    return service.transfer(account.db, str(OLD), str(NEW), expected_user_id=account.user.id,
                            expected_digest=digest, reason=REASON, **overrides)


def test_full_account_and_restrictions_survive(account):
    uid, rid, pid = account.user.id, account.slot.id, account.player.id
    user, receipt = execute(account)
    account.db.commit()
    assert user.id == uid and user.telegram_id == NEW
    assert (user.total_coins, user.total_gems, user.quest_points) == (123456, 58, 70)
    assert user.is_banned and user.ban_reason == "Preserve restrictions"
    assert user.subscription_tier == "gold" and user.matches_played == 20
    assert account.db.get(UserRoster, rid).user_id == uid
    assert account.db.get(Player, pid).career_owner_user_id == uid
    assert account.db.get(UserStats, uid)
    assert account.db.query(UserWarning).one().issued_by == OLD
    assert user.username is None and user.first_name is None and not user.dm_blocked
    assert account.db.query(User).filter_by(telegram_id=OLD).first() is None
    assert account.team.user_tg_id == NEW and account.team.owner_tg_id == NEW
    assert json.loads(account.team.prize_split_snapshot_json)[0]["telegram_id"] == NEW
    assert account.db.get(MatchControlAccount, NEW).blocked
    assert account.db.query(AuctionAdmin).one().tg_id == OLD
    assert receipt.action == "account_transfer" and json.loads(receipt.detail)["old_id"] == OLD
    assert account.db.query(ActivityLog).one().user_id == uid


@pytest.mark.parametrize("raw", ["", "-1", "0", "1.5", "abc", "@player", "１２３", "4503599627370496"])
def test_invalid_ids_rejected(account, raw):
    with pytest.raises(service.TransferError):
        service.prepare(account.db, str(OLD), raw)
    assert account.user.telegram_id == OLD


def test_same_missing_and_occupied_targets(account):
    for old, new in [(OLD, OLD), (999, NEW)]:
        with pytest.raises(service.TransferError):
            service.prepare(account.db, str(old), str(new))
    account.db.add(User(telegram_id=NEW, total_coins=99))
    account.db.commit()
    with pytest.raises(service.TransferError, match="already has"):
        execute(account)
    assert account.db.query(User).count() == 2
    assert account.user.total_coins == 123456


def test_changed_preview_and_replay_rejected(account):
    _, _, digest, _ = service.prepare(account.db, str(OLD), str(NEW))
    account.user.total_coins += 1
    account.db.commit()
    with pytest.raises(service.TransferError, match="changed"):
        service.transfer(account.db, str(OLD), str(NEW), expected_user_id=account.user.id,
                         expected_digest=digest, reason=REASON)
    execute(account)
    account.db.commit()
    with pytest.raises(service.TransferError, match="No saved"):
        execute(account)
    assert account.db.query(AdminLog).filter_by(action="account_transfer").count() == 1


def test_failure_rolls_back_identity_ownership_and_audit(account):
    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO admin_logs"):
            raise RuntimeError("simulated audit storage failure")
    event.listen(account.db.get_bind(), "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="audit storage"):
            execute(account)
        account.db.rollback()
    finally:
        event.remove(account.db.get_bind(), "before_cursor_execute", fail)
    account.db.expire_all()
    assert account.user.telegram_id == OLD
    assert account.team.user_tg_id == OLD
    assert account.db.query(AdminLog).count() == 0
    assert account.db.query(ActivityLog).count() == 0


def login(account):
    with account.client.session_transaction() as state:
        state["admin"] = True
    response = account.client.get("/users/transfer")
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def preview(account, csrf):
    response = account.client.post("/users/transfer", data=dict(action="preview", csrf_token=csrf,
        old_id=str(OLD), new_id=str(NEW), reason=REASON))
    assert response.status_code == 200, response.text
    assert "123,456" in response.text and "Confirm Transfer" in response.text
    return re.search(r'name="transfer_token" value="([^"]+)"', response.text).group(1)


def test_route_requires_admin_and_csrf(account):
    assert account.client.get("/users/transfer").status_code == 302
    csrf = login(account)
    response = account.client.post("/users/transfer", data=dict(action="preview", old_id=OLD, new_id=NEW))
    assert response.status_code == 302  # existing CSRF handler redirects to login
    assert account.db.query(AdminLog).count() == 0


def test_preview_confirm_and_duplicate_post(account):
    csrf = login(account)
    token = preview(account, csrf)
    account.db.expire_all()
    assert account.user.telegram_id == OLD  # preview is read-only
    body = dict(action="confirm", csrf_token=csrf, transfer_token=token,
                ownership_verified="yes", confirm_new_id=str(NEW))
    assert account.client.post("/users/transfer", data=body).status_code == 302
    assert account.client.post("/users/transfer", data=body).status_code == 400
    account.db.expire_all()
    assert account.user.telegram_id == NEW
    assert account.db.query(AdminLog).count() == 1


@pytest.mark.parametrize("change", [dict(ownership_verified=""), dict(confirm_new_id=str(OLD)),
                                   dict(transfer_token="tampered")])
def test_confirmation_cannot_bypass_review(account, change):
    csrf = login(account)
    token = preview(account, csrf)
    body = dict(action="confirm", csrf_token=csrf, transfer_token=token,
                ownership_verified="yes", confirm_new_id=str(NEW))
    body.update(change)
    assert account.client.post("/users/transfer", data=body).status_code == 400
    account.db.expire_all()
    assert account.user.telegram_id == OLD
    assert account.db.query(AdminLog).count() == 0


def test_destination_record_conflict_rolls_back_everything(account):
    from sqlalchemy.exc import IntegrityError
    account.db.add(MatchControlAccount(telegram_id=NEW, blocked=False))
    account.db.commit()
    with pytest.raises(IntegrityError):
        execute(account)
    account.db.rollback()
    assert account.user.telegram_id == OLD
    assert account.team.user_tg_id == OLD
    assert account.db.query(AdminLog).count() == 0


def test_shared_ownership_conflict_rejected(account):
    account.team.co_owner_ids_json = json.dumps([OLD, NEW])
    account.db.commit()
    with pytest.raises(service.TransferError, match="already shares"):
        execute(account)
    assert account.user.telegram_id == OLD


def test_preview_cannot_be_used_by_another_admin_session(account):
    csrf = login(account)
    token = preview(account, csrf)
    account.client = account.app.test_client()
    other_csrf = login(account)
    response = account.client.post('/users/transfer', data=dict(action='confirm', csrf_token=other_csrf,
        transfer_token=token, confirm_new_id=str(NEW), ownership_verified='yes'))
    assert response.status_code == 400
    assert account.user.telegram_id == OLD


def test_expired_preview_rejected(account, monkeypatch):
    from itsdangerous import TimestampSigner, URLSafeTimedSerializer
    csrf = login(account)
    current = preview(account, csrf)
    signer = URLSafeTimedSerializer(account.app.secret_key, salt='account-transfer-v1')
    payload = signer.loads(current)
    timestamp = TimestampSigner.get_timestamp
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, 'get_timestamp', lambda self: timestamp(self) - 601)
        token = signer.dumps(payload)
    response = account.client.post('/users/transfer', data=dict(action='confirm', csrf_token=csrf,
        transfer_token=token, confirm_new_id=str(NEW), ownership_verified='yes'))
    assert response.status_code == 400
    assert 'expired' in response.text
    assert account.user.telegram_id == OLD
