"""Deferred picks, exact timer boundaries, restart recovery and undo history."""
from datetime import datetime, timedelta
import json

import pytest

from test_franchise_draft import room  # noqa: F401

NOW = datetime(2030, 1, 2, 12, 0, 0)


def launch(room, *, rounds=1, minutes=10):
    room.D.configure_rounds(room.db, room.season, rounds)
    room.D.set_timer(room.db, room.season, minutes,timeout_policy='skip')
    room.D.start(room.db, room.season, now=NOW)
    room.db.commit()


def sign(room, *, now=NOW, name=None):
    team = room.D.turn(room.db, room.season)
    if name is None:
        name = next(p.name for p in room.A.lots(room.db, room.season.id, status=room.A.LOT_QUEUED)
                    if p.name not in {p.name for p in room.A.squad(room.db, team.id)})
    quote = room.D.preview_pick(room.db, room.season, team.owner_tg_id, name, now=now)
    room.db.commit()
    result = room.D.confirm_pick(room.db, room.season, quote.token, team.owner_tg_id, now=now)
    room.db.commit()
    return result


def skip(room, *, now=NOW):
    actor = room.D.turn(room.db, room.season).owner_tg_id
    result = room.D.skip_turn(room.db, room.season, actor, now=now)
    room.db.commit()
    return result


def test_new_franchise_joins_saved_order_with_full_purse_and_rounds(room):
    added = room.D.add_team(room.db, room.season, 'New Franchise', 80001, co_owners=[80002])
    room.db.commit()
    assert added.purse_remaining_lakh == added.purse_total_lakh == 10000
    assert room.A.ledger_total(room.db, added.id) == 10000
    assert room.A.co_owner_ids(added) == [80002]
    assert added.draft_picks_total == 2 and added.draft_picks_used == 0
    assert [t.id for t in room.D.round_order(room.db, room.season, 1)][-1] == added.id
    assert len(room.D.schedule(room.db, room.season)) == 12
    launch(room)
    with pytest.raises(room.A.AuctionError, match='during setup'):
        room.D.add_team(room.db, room.season, 'Too late', 80003)
    room.db.rollback()


@pytest.mark.parametrize('owner', [0, -1, True, 'not-an-id'])
def test_invalid_new_team_owner_is_rejected(room, owner):
    with pytest.raises(room.A.AuctionError):
        room.D.add_team(room.db, room.season, 'Invalid owner', owner)
    assert len(room.A.franchises(room.db, room.season.id)) == 5


def test_skip_costs_nothing_advances_and_invalidates_pick_confirmation(room):
    launch(room)
    quote = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root', now=NOW)
    room.db.commit()
    skip(room)
    draft = room.D.get_draft(room.db, room.season)
    assert draft.cursor == 1
    assert room.D.turn(room.db, room.season).id == room.teams[1].id
    assert [entry['franchise_id'] for entry in room.D.skipped_turns(room.db, room.season)] == [room.teams[0].id]
    assert room.teams[0].purse_remaining_lakh == room.A.ledger_total(room.db, room.teams[0].id) == 10000
    assert room.teams[0].squad_size == 0
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, quote.token, 1000, now=NOW)
    room.db.rollback()


@pytest.mark.parametrize('actor', [1001, 3000, 999999])
def test_only_current_owner_or_coowner_can_skip(room, actor):
    launch(room)
    with pytest.raises(room.A.AuctionError, match='owner or co-owner'):
        room.D.skip_turn(room.db, room.season, actor, now=NOW)
    room.db.rollback()
    assert room.D.get_draft(room.db, room.season).cursor == 0
    assert not room.D.skipped_turns(room.db, room.season)
    room.D.skip_turn(room.db, room.season, 2000, now=NOW)
    room.db.commit()
    assert room.D.get_draft(room.db, room.season).cursor == 1


def test_catchup_round_follows_skip_order_and_repeat_skip_moves_to_back(room):
    launch(room)
    skip(room)  # Team 1
    skip(room)  # Team 2
    for _ in range(3):
        sign(room)
    assert room.D.turn_info(room.db, room.season)['catch_up']
    assert room.season.status == room.A.STATUS_LIVE
    assert room.D.turn(room.db, room.season).id == room.teams[0].id
    skip(room)
    assert [entry['franchise_id'] for entry in room.D.skipped_turns(room.db, room.season)] == [room.teams[1].id, room.teams[0].id]
    sign(room)
    sign(room)
    assert room.season.status == room.A.STATUS_COMPLETED
    assert room.D.get_draft(room.db, room.season).cursor == 5
    assert not room.D.skipped_turns(room.db, room.season)
    assert all(t.squad_size == 1 for t in room.teams)
    league = room.A.publish_to_league(room.db, room.season)
    room.db.commit()
    assert league.id == room.season.league_id


def test_one_team_can_defer_separate_rounds_and_fill_both_in_catchup(room):
    launch(room, rounds=2)
    for _ in range(10):
        if room.D.turn(room.db, room.season).id == room.teams[0].id:
            skip(room)
        else:
            sign(room)
    queue = room.D.skipped_turns(room.db, room.season)
    assert [entry['round_no'] for entry in queue] == [1, 2]
    sign(room)
    sign(room)
    assert all(t.squad_size == 2 for t in room.teams)
    assert room.season.status == room.A.STATUS_COMPLETED


def test_confirmed_pick_gives_next_team_a_fresh_full_timer(room):
    launch(room, minutes=8)
    assert room.D.get_draft(room.db, room.season).pick_deadline_at == NOW + timedelta(minutes=8)
    signed_at = NOW + timedelta(seconds=75)
    sign(room, now=signed_at)
    assert room.D.get_draft(room.db, room.season).pick_deadline_at == signed_at + timedelta(minutes=8)


def test_timeout_skips_once_without_debit_and_announces_next_owner(room):
    from models import AuctionEvent
    launch(room, minutes=5)
    expired = NOW + timedelta(minutes=5)
    result = room.D.resolve_timeout(room.db, room.season, now=expired)
    room.db.commit()
    assert result.id == room.teams[0].id
    assert room.D.get_draft(room.db, room.season).pick_deadline_at == expired + timedelta(minutes=5)
    assert room.D.resolve_timeout(room.db, room.season, now=expired) is None
    room.db.commit()
    assert room.D.get_draft(room.db, room.season).cursor == 1
    assert room.teams[0].purse_remaining_lakh == 10000
    event = room.db.query(AuctionEvent).filter_by(season_id=room.season.id, kind='fd_skipped').one()
    text = json.loads(event.detail_json)['html']
    assert 'ran out of pick time' in text and 'tg://user?id=1001' in text


def test_preview_or_confirmation_at_deadline_cannot_beat_timeout(room):
    launch(room, minutes=5)
    quote = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root', now=NOW)
    room.db.commit()
    expired = NOW + timedelta(minutes=5)
    with pytest.raises(room.A.AuctionError, match='time has expired'):
        room.D.preview_pick(room.db, room.season, 1000, 'Joe Root', now=expired)
    room.db.rollback()
    with pytest.raises(room.A.AuctionError, match='time has expired'):
        room.D.confirm_pick(room.db, room.season, quote.token, 1000, now=expired)
    room.db.rollback()
    assert room.teams[0].squad_size == 0 and room.teams[0].purse_remaining_lakh == 10000


def test_pause_freezes_remaining_seconds_and_resume_restores_them(room):
    launch(room, minutes=5)
    room.D.pause(room.db, room.season, now=NOW + timedelta(minutes=2))
    room.db.commit()
    draft = room.D.get_draft(room.db, room.season)
    assert draft.pick_deadline_at is None and draft.paused_seconds_left == 180
    assert room.D.resolve_timeout(room.db, room.season, now=NOW + timedelta(days=2)) is None
    resumed = NOW + timedelta(days=2)
    room.D.start(room.db, room.season, now=resumed)
    room.db.commit()
    assert draft.pick_deadline_at == resumed + timedelta(seconds=180)


@pytest.mark.parametrize('minutes', [-1, True, 1.5, 'bad', 1441])
def test_timer_rejects_invalid_minutes(room, minutes):
    with pytest.raises(room.A.AuctionError):
        room.D.set_timer(room.db, room.season, minutes)


def test_timer_can_be_disabled_or_changed_while_paused(room):
    launch(room, minutes=0)
    assert room.D.get_draft(room.db, room.season).pick_deadline_at is None
    assert room.D.resolve_timeout(room.db, room.season, now=NOW + timedelta(days=10)) is None
    with pytest.raises(room.A.AuctionError, match='Pause'):
        room.D.set_timer(room.db, room.season, 15)
    room.db.rollback()
    room.D.pause(room.db, room.season, now=NOW)
    room.D.set_timer(room.db, room.season, 15)
    room.D.start(room.db, room.season, now=NOW)
    room.db.commit()
    assert room.D.get_draft(room.db, room.season).pick_deadline_at == NOW + timedelta(minutes=15)


def test_restart_recovers_overdue_turn_once_and_keeps_fifo_queue(room):
    from models import AuctionSeason
    launch(room, minutes=5)
    future = NOW + timedelta(days=1)
    for _ in range(2):
        with room.sessions() as restarted:
            season = restarted.get(AuctionSeason, room.season.id)
            room.D.resolve_timeout(restarted, season, now=future)
            restarted.commit()
    room.db.expire_all()
    assert room.D.get_draft(room.db, room.season).cursor == 1
    assert room.D.get_draft(room.db, room.season).pick_deadline_at == future + timedelta(minutes=5)
    assert len(room.D.skipped_turns(room.db, room.season)) == 1


def test_stale_timeout_worker_cannot_skip_the_new_team_after_a_pick(room):
    from models import AuctionSeason
    launch(room, minutes=5)
    with room.sessions() as stale:
        cached = stale.get(AuctionSeason, room.season.id)
        keep_config = room.D.get_draft(stale, cached)
        sign(room, now=NOW + timedelta(minutes=1))
        with pytest.raises(room.A.AuctionError, match='draft changed'):
            room.D.resolve_timeout(stale, cached, now=NOW + timedelta(minutes=5))
        stale.rollback()
        assert keep_config.cursor == 1
    assert not room.D.skipped_turns(room.db, room.season)


def test_undo_restores_catchup_pick_and_skip_without_duplicate_refunds(room):
    launch(room)
    skip(room)
    for _ in range(4):
        sign(room)
    picked = sign(room)
    assert room.season.status == room.A.STATUS_COMPLETED
    restored = room.D.undo_last(room.db, room.season)
    room.db.commit()
    assert restored.id == picked.id and picked.status == room.A.LOT_QUEUED
    assert room.teams[0].purse_remaining_lakh == room.A.ledger_total(room.db, room.teams[0].id) == 10000
    assert room.D.get_draft(room.db, room.season).cursor == 5
    assert room.D.turn(room.db, room.season).id == room.teams[0].id
    room.D.start(room.db, room.season, now=NOW)
    room.db.commit()
    skip(room)
    room.D.pause(room.db, room.season, now=NOW)
    room.D.undo_last(room.db, room.season)
    room.db.commit()
    assert room.D.skipped_turns(room.db, room.season)[0]['retries'] == 0
    assert room.teams[0].purse_remaining_lakh == 10000


def test_pending_skip_round_blocks_publishing_even_if_status_was_changed(room):
    launch(room)
    skip(room)
    for _ in range(4):
        sign(room)
    room.season.status = room.A.STATUS_COMPLETED
    room.db.commit()
    with pytest.raises(room.A.AuctionError, match='every draft round'):
        room.A.publish_to_league(room.db, room.season)
    room.db.rollback()


def test_skips_and_turn_journal_are_deleted_with_the_season(room):
    from models import FranchiseDraftTurn
    launch(room)
    skip(room)
    assert room.db.query(FranchiseDraftTurn).count() == 1
    room.db.delete(room.season)
    room.db.commit()
    assert room.db.query(FranchiseDraftTurn).count() == 0


def test_pre_clock_live_draft_gets_a_full_window_after_upgrade(room):
    launch(room, minutes=8)
    room.D.get_draft(room.db, room.season).pick_deadline_at = None
    room.db.commit()
    room.D.resolve_timeout(room.db, room.season, now=NOW)
    room.db.commit()
    assert room.D.get_draft(room.db, room.season).pick_deadline_at == NOW + timedelta(minutes=8)
    assert room.D.get_draft(room.db, room.season).cursor == 0


def test_scheduler_commits_timeout_before_notifying_next_team(room, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from models import AuctionSeason
    from services import franchise_draft_delivery as DD

    launch(room, minutes=5)
    room.D.get_draft(room.db, room.season).pick_deadline_at = datetime.utcnow() - timedelta(seconds=1)
    room.db.commit()

    async def notified(*args, **kwargs):
        with room.sessions() as reader:
            season = reader.get(AuctionSeason, room.season.id)
            assert room.D.get_draft(reader, season).cursor == 1
            assert room.D.turn(reader, season).id == room.teams[1].id
            assert len(room.D.skipped_turns(reader, season)) == 1
        return 1

    monkeypatch.setattr(DD, 'drain_events', AsyncMock(side_effect=notified))
    monkeypatch.setattr(DD, 'refresh_board', AsyncMock())
    asyncio.run(DD.tick(SimpleNamespace(), room.db, room.season))
    asyncio.run(DD.tick(SimpleNamespace(), room.db, room.season))
    assert room.D.get_draft(room.db, room.season).cursor == 1
