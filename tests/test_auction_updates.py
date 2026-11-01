"""Shared-database live notifications preserve auction authentication/access."""

import json

import pytest
from sqlalchemy import event

from test_franchise_website import (ADMIN, DELEGATE, OTHER, OWNER, auction,
                                    login, site)  # noqa: F401


def updates(site, token, **body):
    return site.client.post('/api/webapp/auction/updates',
                            json={'season_id': site.season.id, **body},
                            headers={'X-CSRF-Token': token}, buffered=False)


def message(chunk):
    if isinstance(chunk, bytes):
        chunk = chunk.decode()
    name, data = chunk.strip().split('\n', 1)
    return name.removeprefix('event: '), json.loads(data.removeprefix('data: '))


@pytest.fixture(autouse=True)
def quick_stream(monkeypatch):
    from services import auction_updates as U
    monkeypatch.setattr(U, 'POLL_SECONDS', .001)
    monkeypatch.setattr(U, 'STREAM_SECONDS', 1.0)
    monkeypatch.setattr(U, 'HEARTBEAT_SECONDS', .01)


def test_updates_require_verified_identity_and_csrf(site):
    assert updates(site, 'missing').status_code == 401
    token = login(site)
    assert updates(site, 'incorrect').status_code == 403
    response = site.client.post('/api/webapp/auction/updates',
                                json={'season_id': site.season.id},
                                headers={'X-CSRF-Token': token, 'Authorization': 'tma DEV_' + str(OWNER)})
    assert response.status_code == 401
    assert response.get_json()['error'] == 'bad_init_data'


def test_snapshot_observes_bid_committed_by_another_database_session(site):
    from services import auction_updates as U
    token = login(site)
    response = updates(site, token)
    assert response.status_code == 200
    assert response.mimetype == 'text/event-stream'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Accel-Buffering'] == 'no'
    stream = iter(response.response)
    kind, before = message(next(stream))
    assert kind == 'ready'
    assert set(before) == {'revision', 'season_id'}
    assert before['season_id'] == site.season.id

    # This is a separate session, just as the Telegram bot and web services use.
    writer = site.sessions()
    try:
        from models import AuctionSeason
        season = writer.get(AuctionSeason, site.season.id)
        lot = site.A.start(writer, season)
        writer.commit()
        lot = site.A.current_lot(writer, season)
        franchise = writer.get(type(site.other), site.other.id)
        site.A.place_bid(writer, season, lot, franchise,
                         site.A.next_min_bid(season, lot), by_tg_id=OTHER)
        writer.commit()
    finally:
        writer.close()
    kind, after = message(next(stream))
    assert kind == 'changed'
    assert after['revision'] != before['revision']
    assert set(after) == {'revision', 'season_id'}, 'No bid/private identity payload is streamed'
    response.close()


def test_probe_uses_one_query_and_detects_deadline_purse_and_roles(site):
    from datetime import datetime, timedelta
    from services import auction_updates as U
    from models import AuctionAdmin
    site.A.start(site.db, site.season)
    site.db.commit()
    statements = []
    engine = site.db.get_bind()
    def statement(*args):
        statements.append(args[2])
    event.listen(engine, 'before_cursor_execute', statement)
    try:
        initial = U.snapshot(site.db, site.season.id, OWNER)
    finally:
        event.remove(engine, 'before_cursor_execute', statement)
    assert len(statements) == 1
    assert initial.authority[2] == ((site.owner.id, ('team_owner',)),)
    lot = site.A.current_lot(site.db, site.season)
    lot.deadline_at = datetime.utcnow() + timedelta(seconds=60)
    site.db.commit()
    extended = U.snapshot(site.db, site.season.id, OWNER)
    assert extended.revision != initial.revision
    site.owner.purse_remaining_lakh += 50
    site.db.commit()
    corrected = U.snapshot(site.db, site.season.id, OWNER)
    assert corrected.revision != extended.revision
    lot.rtm_stage = 'intent'
    lot.status = 'rtm_offered'
    site.db.commit()
    assert U.snapshot(site.db, site.season.id, OWNER).revision != corrected.revision
    site.owner.owner_tg_id = OTHER
    site.db.commit()
    removed = U.snapshot(site.db, site.season.id, OWNER)
    assert removed.authority != initial.authority
    before_manager = U.snapshot(site.db, site.season.id, DELEGATE)
    site.db.add(AuctionAdmin(tg_id=DELEGATE))
    site.db.commit()
    assert U.snapshot(site.db, site.season.id, DELEGATE).authority != before_manager.authority


def test_stream_closes_when_ownership_is_revoked(site):
    token = login(site)
    response = updates(site, token)
    stream = iter(response.response)
    assert message(next(stream))[0] == 'ready'
    site.owner.owner_tg_id = OTHER
    site.db.commit()
    name, payload = message(next(stream))
    assert name == 'access'
    assert payload['status'] == 403
    assert payload['error'] == 'forbidden'
    with pytest.raises(StopIteration):
        next(stream)
    response.close()


def test_stream_closes_when_manager_is_removed(site):
    site.A.add_auction_admin(site.db, DELEGATE, by_tg_id=ADMIN)
    site.db.commit()
    token = login(site, DELEGATE)
    response = updates(site, token)
    stream = iter(response.response)
    assert message(next(stream))[0] == 'ready'
    site.A.remove_auction_admin(site.db, DELEGATE, by_tg_id=ADMIN)
    site.db.commit()
    name, payload = message(next(stream))
    assert name == 'access'
    assert payload['status'] == 403
    response.close()


def test_stream_admission_leaves_api_available_and_close_releases_slot(site):
    site.app.config['AUCTION_STREAM_MAX_CLIENTS'] = 1
    token = login(site)
    first = updates(site, token)
    assert first.status_code == 200
    busy = updates(site, token)
    assert busy.status_code == 503
    assert busy.get_json()['error'] == 'stream_capacity'
    assert int(busy.headers['Retry-After']) >= 5
    assert auction(site, token).status_code == 200
    first.close()
    reopened = updates(site, token)
    assert reopened.status_code == 200
    reopened.close()


def test_stream_releases_database_between_yields_and_ends(site, monkeypatch):
    from services import auction_updates as U
    monkeypatch.setattr(U, 'STREAM_SECONDS', .04)
    token = login(site)
    pool = site.db.get_bind().pool
    original_pause = U.time.sleep
    pauses = []
    def pause(seconds):
        assert pool.checkedout() == 0, 'No database connection stays checked out while waiting'
        pauses.append(seconds)
        original_pause(seconds)
    monkeypatch.setattr(U.time, 'sleep', pause)
    response = updates(site, token)
    frames = [message(chunk) for chunk in response.response]
    response.close()
    assert frames[0][0] == 'ready'
    assert frames[-1][0] == 'end'
    assert any(name == 'heartbeat' for name, _data in frames)
    assert pauses


def test_no_season_stream_discovers_new_assignments(site):
    from services import auction_updates as U
    before = U.snapshot(site.db, None, OWNER)
    site.A.create_season(site.db, 'Another season', min_squad_size=0)
    site.db.commit()
    assert U.snapshot(site.db, None, OWNER).revision != before.revision


def test_open_stream_rechecks_account_gates(site, monkeypatch):
    from services import auction_updates as U
    monkeypatch.setattr(U, 'REAUTHORIZE_SECONDS', 0)
    token = login(site, ADMIN)
    response = updates(site, token)
    stream = iter(response.response)
    assert message(next(stream))[0] == 'ready'
    site.cfg['is_maintenance'] = True
    site.cfg['maintenance_bypass_ids'] = ''
    # Bot admins have a maintenance exemption; use owner for this check.
    response.close()
    site.cfg['is_maintenance'] = False
    token = login(site, OWNER)
    response = updates(site, token)
    stream = iter(response.response)
    next(stream)
    site.cfg['is_maintenance'] = True
    name, payload = message(next(stream))
    assert name == 'access' and payload['error'] == 'maintenance'
    response.close()
