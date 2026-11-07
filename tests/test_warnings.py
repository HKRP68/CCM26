"""Persistent warning moderation, ban ownership, delivery and concurrent changes."""

import ast
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from telegram.ext import ApplicationHandlerStop

from database import Base
from handlers import warnings as commands
from models import AdminLog, User, UserWarning
from services import admin_ids, warning_service as service

ADMIN, PLAYER, OTHER = 90001, 70001, 70002


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'warnings.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, UserWarning.__table__, AdminLog.__table__])
    factory = sessionmaker(bind=engine, autoflush=False)
    for key in set(admin_ids.ADMIN_ID_ENV_VARS + admin_ids.OWNER_ID_ENV_VARS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_ADMIN_IDS", str(ADMIN))
    monkeypatch.setattr(commands, "get_session", factory)
    session = factory()
    player = User(telegram_id=PLAYER, username="Cricket_Player", first_name="Player")
    other = User(telegram_id=OTHER, username="Other_Player")
    session.add_all([player, other])
    session.commit()
    yield NS(engine=engine, factory=factory, session=session, player=player, other=other)
    session.close()
    engine.dispose()


def change(db, action="warn", target=str(PLAYER), reason="Repeated abuse", key=None, actor=ADMIN):
    with db.factory() as session:
        result = service.change_warning(session, target, action=action, reason=reason,
                                        admin_telegram_id=actor, message_key=key)
        session.commit()
    db.session.expire_all()
    return result


def command(db, args=(), *, action="warn", reply=None, actor=ADMIN, bot=None, key=None):
    message = NS(reply_text=AsyncMock(), reply_to_message=(
        NS(from_user=NS(id=reply, is_bot=False), sender_chat=None) if reply else None),
        chat_id=key[0] if key else None, message_id=key[1] if key else None)
    update = NS(effective_user=NS(id=actor), effective_message=message)
    context = NS(args=list(args), bot=bot, bot_data={"_ban_cache": {PLAYER: (0, False, "", False)}})
    handler = {"warn": commands.cmuwarn_handler, "unwarn": commands.cmuunwarn_handler,
               "history": commands.cmuwarns_handler}[action]
    asyncio.run(handler(update, context))
    db.session.expire_all()
    return message.reply_text.call_args.args[0], context


def test_fifth_warning_bans_and_removal_lifts_only_warning_ban(db):
    for number in range(1, 6):
        result = change(db, key=(-100, number))
        assert result.count == number
        assert result.is_banned == (number == 5)
        assert db.player.is_banned == (number == 5)
    assert db.player.warning_ban
    assert db.player.banned_at is not None
    assert "5 active warnings" in db.player.ban_reason
    fifth = result.warning_id
    result = change(db, "unwarn", reason="Appeal accepted", key=(-100, 6))
    assert (result.count, result.unbanned, result.is_banned) == (4, True, False)
    assert not db.player.warning_ban
    assert db.player.ban_reason is None
    assert db.player.banned_at is None
    removed = db.session.get(UserWarning, fifth)
    assert removed.revoked_by == ADMIN
    assert removed.revoke_reason == "Appeal accepted"
    assert removed.reason == "Repeated abuse"
    assert db.session.query(UserWarning).count() == 5
    actions = [entry.action for entry in db.session.query(AdminLog).all()]
    assert actions.count("user_warn") == 5
    assert actions.count("user_unwarn") == 1
    assert actions.count("user_warning_ban") == 1
    assert actions.count("user_warning_unban") == 1
    assert "Telegram admin 90001" in db.session.query(AdminLog).first().detail
    # A subsequent fifth active warning bans again.
    assert change(db).is_banned


def test_retries_never_add_or_remove_another_warning(db):
    original = change(db, key=(-100, 1))
    assert not change(db, key=(-100, 1)).changed
    assert db.session.query(UserWarning).count() == 1
    change(db, key=(-100, 2))
    removal = change(db, "unwarn", key=(-100, 3))
    assert removal.count == 1
    assert not change(db, "unwarn", key=(-100, 3)).changed
    assert service.active_warnings(db.session, db.player.id).count() == 1
    # Revoking the original does not allow replaying its issue command.
    change(db, "unwarn", key=(-100, 4))
    assert not change(db, key=(-100, 1)).changed
    assert service.active_warnings(db.session, db.player.id).count() == 0
    assert original.warning_id != removal.warning_id


def test_duplicate_fifth_warning_is_successfully_recognized_while_banned(db):
    for number in range(1, 6):
        change(db, key=(-100, number))
    retry = change(db, key=(-100, 5))
    assert not retry.changed
    assert retry.count == 5
    assert retry.is_banned
    with pytest.raises(ValueError, match="already banned"):
        change(db, key=(-100, 6))


def test_removal_preserves_manual_ban(db):
    change(db)
    db.player.is_banned = True
    db.player.ban_reason = "Manual investigation"
    db.player.banned_at = stamp = datetime(2026, 11, 1)
    db.session.commit()
    result = change(db, "unwarn")
    assert result.is_banned
    assert not result.unbanned
    assert db.player.ban_reason == "Manual investigation"
    assert db.player.banned_at == stamp


def test_manual_ban_after_warning_ban_cannot_be_lifted_by_unwarn(db):
    for _ in range(5):
        change(db)
    # Execute the real web route body without booting the whole Flask app.
    source = Path(__file__).resolve().parents[1].joinpath("admin.py").read_text()
    route = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == "user_ban")
    route.decorator_list = []
    namespace = dict(get_session=db.factory, User=User, datetime=datetime,
                     request=NS(form={"action": "ban", "reason": "Manual investigation"}),
                     flash=lambda *args: None, log_admin=lambda *args, **kw: None,
                     redirect=lambda url: url, url_for=lambda *args, **kw: "users",
                     logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=[route], type_ignores=[]), "admin.py", "exec"), namespace)
    namespace["user_ban"](db.player.id)
    db.session.expire_all()
    assert not db.player.warning_ban
    assert change(db, "unwarn").is_banned
    assert db.player.ban_reason == "Manual investigation"


def test_nonadmin_and_maintenance_bypass_cannot_moderate_or_read(db, monkeypatch):
    monkeypatch.setattr(admin_ids, "get_config", lambda: {"maintenance_bypass_ids": str(OTHER)})
    assert admin_ids.is_admin(OTHER)
    for action in ("warn", "unwarn", "history"):
        output, _ = command(db, [str(PLAYER)], action=action, actor=OTHER)
        assert "Only bot admins" in output
    with pytest.raises(ValueError, match="Only bot admins"):
        change(db, actor=OTHER)
    assert db.session.query(UserWarning).count() == 0


def test_admins_and_owners_are_protected(db, monkeypatch):
    monkeypatch.setenv("BOT_OWNER_IDS", str(OTHER))
    with pytest.raises(ValueError, match="cannot receive warnings"):
        change(db, target=str(OTHER))
    # The configured owner can issue warnings even without BOT_ADMIN_IDS.
    monkeypatch.delenv("BOT_ADMIN_IDS")
    assert change(db, actor=OTHER).count == 1


def test_targeting_exact_usernames_reply_and_explicit_target(db):
    assert "Warning issued" in command(db, ["@CRICKET_PLAYER", "Abuse"])[0]
    assert "Warning issued" in command(db, ["Reply", "reason"], reply=PLAYER)[0]
    command(db, [str(OTHER), "Explicit", "target"], reply=PLAYER)
    assert service.active_warnings(db.session, db.player.id).count() == 2
    assert service.active_warnings(db.session, db.other.id).count() == 1
    db.player.username = "CricketXPlayer"
    db.session.commit()
    assert "Player not found" in command(db, ["@Cricket_Player"])[0]
    db.player.username = "Cricket_Player"
    db.other.username = "cricket_player"
    db.session.commit()
    assert "More than one" in command(db, ["@Cricket_Player"])[0]
    assert "Player not found" in command(db, ["123456789"])[0]


@pytest.mark.parametrize("args", [["-100"], ["0"], [str(2**63)], ["@invalid-name"],
                                  [str(PLAYER), "x" * 501]])
def test_invalid_targets_and_reasons_never_mutate(db, args):
    assert command(db, args)[0].startswith("❌")
    assert db.session.query(UserWarning).count() == 0


def test_empty_warning_removal_cannot_underflow(db):
    assert "no active warnings" in command(db, [str(PLAYER)], action="unwarn")[0]
    assert db.session.query(AdminLog).count() == 0
    assert "CMU warnings" in command(db)[0]


def test_player_dm_cache_invalidation_final_warning_and_failed_dm(db):
    bot = NS(send_message=AsyncMock())
    for _ in range(3):
        change(db)
    output, context = command(db, [str(PLAYER), "<abuse>&"], bot=bot, key=(-100, 4))
    assert "Final warning" in output
    assert "&lt;abuse&gt;&amp;" in output
    assert PLAYER not in context.bot_data["_ban_cache"]
    notice = bot.send_message.await_args.kwargs
    assert notice["chat_id"] == PLAYER
    assert notice["parse_mode"] is None
    assert "<abuse>&" in notice["text"]
    output, _ = command(db, [str(PLAYER)], bot=bot, key=(-100, 4))
    assert "already processed" in output
    assert bot.send_message.await_count == 1
    bot.send_message.side_effect = RuntimeError("DM blocked")
    output, _ = command(db, [str(PLAYER)], bot=bot, key=(-100, 5))
    assert "Could not DM" in output
    assert db.player.is_banned
    output, context = command(db, [str(PLAYER), "Appeal"], action="unwarn", bot=bot)
    assert "ban lifted" in output
    assert "Could not DM" in output
    assert PLAYER not in context.bot_data["_ban_cache"]
    assert not db.player.is_banned


def test_history_preserves_issuance_and_removal_and_paginates(db):
    for _ in range(4):
        change(db, reason="Bad <behavior>&")
    change(db, "unwarn", reason="Appeal <accepted>&")
    first, _ = command(db, [str(PLAYER)], action="history")
    assert "1/2" in first
    assert "3/5" in first
    assert first.count("By admin") == 3
    assert "Removed" in first
    assert "Bad &lt;behavior&gt;&amp;" in first
    assert "Appeal &lt;accepted&gt;&amp;" in first
    assert "UTC" in first
    second, _ = command(db, [str(PLAYER), "2"], action="history")
    assert "2/2" in second
    assert second.count("By admin") == 1
    assert "There are 2 pages" in command(db, [str(PLAYER), "3"], action="history")[0]
    assert "starting at page 1" in command(db, [str(PLAYER), "0"], action="history")[0]
    assert "3/5" in command(db, action="history", reply=PLAYER)[0]
    # Explicit numeric target still wins over a reply for history.
    assert "0/5" in command(db, [str(OTHER)], action="history", reply=PLAYER)[0]


def test_html_replies_fit_telegram_limit_with_maximum_expansion(db):
    db.player.username = None
    db.player.first_name = '"' * 100
    db.session.commit()
    for _ in range(4):
        output, _ = command(db, [str(PLAYER), '"' * 500])
        assert len(output) <= 4096
    for _ in range(3):
        output, _ = command(db, [str(PLAYER), '"' * 500], action="unwarn")
        assert len(output) <= 4096
    db.player.is_banned = True
    db.player.ban_reason = '"' * 500
    db.session.commit()
    output, _ = command(db, [str(PLAYER)], action="history")
    assert len(output) <= 4096
    assert "&quot;" in output


def test_commit_failure_rolls_back_warning_audit_and_ban(db, monkeypatch):
    for _ in range(4):
        change(db)
    session = db.factory()
    monkeypatch.setattr(commands, "get_session", lambda: session)
    monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
    bot = NS(send_message=AsyncMock())
    output, _ = command(db, [str(PLAYER)], bot=bot)
    assert "Could not process" in output
    assert not db.player.is_banned
    assert db.session.query(UserWarning).count() == 4
    assert db.session.query(AdminLog).count() == 4
    bot.send_message.assert_not_awaited()


def test_simultaneous_warnings_stop_at_five(db):
    def issue(number):
        try:
            with db.factory() as session:
                result = service.change_warning(session, str(PLAYER), action="warn",
                    reason=f"Concurrent {number}", admin_telegram_id=ADMIN, message_key=(-100, number))
                session.commit()
                return result.count
        except ValueError as exc:
            assert "already banned" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(issue, range(8)))
    assert sorted(count for count in counts if count is not None) == [1, 2, 3, 4, 5]
    db.session.expire_all()
    assert db.player.is_banned
    assert db.session.query(UserWarning).count() == 5


def test_simultaneous_removals_do_not_revoke_same_warning_twice(db):
    change(db)
    def remove(number):
        try:
            with db.factory() as session:
                result = service.change_warning(session, str(PLAYER), action="unwarn",
                    reason="Appeal", admin_telegram_id=ADMIN, message_key=(-100, number))
                session.commit()
                return result.changed
        except ValueError as exc:
            assert "no active warnings" in str(exc)
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(remove, range(4)))
    assert results.count(True) == 1
    assert service.active_warnings(db.session, db.player.id).count() == 0


def test_warning_table_and_legacy_ban_migration_are_idempotent(db, monkeypatch):
    import database
    # The existing startup migration also upgrades unrelated game tables.
    Base.metadata.create_all(db.engine)
    db.player.is_banned = True
    db.player.ban_reason = "Existing ban"
    db.session.commit()
    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE user_warnings"))
        connection.execute(text("ALTER TABLE users DROP COLUMN warning_ban"))
    monkeypatch.setattr(database, "engine", db.engine)
    for _ in range(2):
        Base.metadata.create_all(db.engine, tables=[UserWarning.__table__])
        database._migrate_add_columns()
    assert "user_warnings" in inspect(db.engine).get_table_names()
    db.session.expire_all()
    assert db.player.is_banned
    assert db.player.ban_reason == "Existing ban"
    assert not db.player.warning_ban


def test_real_ban_middleware_blocks_after_fifth_and_allows_after_removal(db, monkeypatch):
    # Compile the actual nested middleware; avoid starting polling/schedulers.
    import database
    import time
    source = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text()
    guard = next(node for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ban_check")
    cache = {PLAYER: (time.monotonic(), False, "", False)}
    namespace = dict(ban_cache=cache, ban_cache_ttl=60, time=time, asyncio=asyncio,
                     logger=logging.getLogger(__name__), ApplicationHandlerStop=ApplicationHandlerStop,
                     _is_storage_only_command=lambda update: False)
    exec(compile(ast.Module(body=[guard], type_ignores=[]), "bot.py", "exec"), namespace)
    monkeypatch.setattr(database, "get_session", db.factory)
    for _ in range(4):
        change(db)
    message = NS(reply_text=AsyncMock(), reply_to_message=None, chat_id=-100, message_id=5)
    update = NS(effective_message=message, effective_user=NS(id=ADMIN))
    context = NS(args=[str(PLAYER), "Bad <behavior>&"], bot=None, bot_data={"_ban_cache": cache})
    asyncio.run(commands.cmuwarn_handler(update, context))
    assert PLAYER not in cache
    blocked = NS(effective_user=NS(id=PLAYER),
                 message=NS(text="/claim", reply_text=AsyncMock()), callback_query=None)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(namespace["_ban_check"](blocked, context))
    assert "Bad &lt;behavior&gt;&amp;" in blocked.message.reply_text.await_args.args[0]
    callback = NS(effective_user=NS(id=PLAYER), message=None,
                  callback_query=NS(answer=AsyncMock()))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(namespace["_ban_check"](callback, context))
    callback.callback_query.answer.assert_not_awaited()
    message.message_id = 6
    asyncio.run(commands.cmuunwarn_handler(update, context))
    assert PLAYER not in cache
    assert asyncio.run(namespace["_ban_check"](blocked, context)) is None
