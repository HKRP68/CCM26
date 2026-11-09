"""Real database coverage for Mini App and Telegram preview/confirm flows."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_chemistry_planner import squad


@pytest.fixture
def planner_site(tmp_path, monkeypatch):
    import admin
    import database
    from handlers import chemistry_planner as handler
    from models import Base, GameConfig, Player, User, UserRoster
    from services import config_service

    engine = create_engine('sqlite:///' + str(tmp_path / 'planner.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        user = User(telegram_id=987001, first_name='Planner')
        other = User(telegram_id=987002, first_name='Other')
        db.add_all([user, other, GameConfig()])
        db.flush()
        uid, other_uid = user.id, other.id
        ids = []
        for entry, player in squad():
            row = Player(name=player.name, rating=player.rating, country=player.country,
                         category=player.category, version=player.version,
                         bat_hand='Right', bowl_hand='Right', bowl_style='Fast',
                         bat_rating=player.bat_rating, bowl_rating=player.bowl_rating)
            db.add(row)
            db.flush()
            roster = UserRoster(user_id=uid, player_id=row.id, order_position=entry.order_position)
            db.add(roster)
            db.flush()
            ids.append(roster.id)
        foreign = UserRoster(user_id=other_uid, player_id=row.id, order_position=1)
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
        db.commit()
    monkeypatch.setattr(admin, 'get_session', sessions)
    monkeypatch.setattr(database, 'get_session', sessions)
    monkeypatch.setattr(handler, 'get_session', sessions)
    monkeypatch.setattr(config_service, '_CACHE', {'data': None, 'loaded_at': None})
    monkeypatch.setattr(config_service, '_CHEMISTRY_CACHE', {'data': None, 'loaded_at': 0.0})
    site = SimpleNamespace(client=admin.app.test_client(), sessions=sessions, uid=uid,
                           other_uid=other_uid, ids=ids, foreign_id=foreign_id,
                           handler=handler, locked=False)
    def auth():
        db = sessions()
        user = db.get(User, site.uid)
        return (db, user, user.telegram_id), user.telegram_id, None
    monkeypatch.setattr(admin, '_webapp_auth', auth)
    monkeypatch.setattr(admin, '_team_locked_response', lambda *a, **kw:
                        ({'ok': False, 'error': 'match_in_progress'}, 409) if site.locked else None)
    monkeypatch.setattr(handler, 'match_lock_message', lambda *a, **kw: 'Match locked' if site.locked else None)
    monkeypatch.setitem(admin.app.config, 'TESTING', True)
    yield site
    engine.dispose()


def positions(site):
    from models import UserRoster
    with site.sessions() as db:
        return {row.id: row.order_position for row in db.query(UserRoster).all()}


def body(site):
    return dict(xi_roster_id=site.ids[3], bench_roster_id=site.ids[11])


def preview(site):
    response = site.client.post('/api/webapp/xi/preview_swap', json=body(site))
    assert response.status_code == 200, response.get_json()
    return response.get_json()['preview']


def test_miniapp_recommendations_and_preview_are_read_only(planner_site):
    site = planner_site
    before = positions(site)
    response = site.client.post('/api/webapp/xi', json={})
    assert response.status_code == 200
    data = response.get_json()
    assert data['chemistry']['total'] > 0
    assert data['chemistry_suggestions'][0]['delta'] > 0
    result = preview(site)
    assert result['delta'] > 0 and result['rating_delta'] == -6
    assert positions(site) == before


def test_confirm_applies_exactly_the_preview_once_and_keeps_the_chosen_xi(planner_site):
    from models import ActivityLog, User
    site = planner_site
    result = preview(site)
    request_body = dict(body(site), preview_token=result['preview_token'])
    response = site.client.post('/api/webapp/xi/swap_bench', json=request_body)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['chemistry'] == result['after']
    saved = positions(site)
    assert saved[site.ids[3]] == 12 and saved[site.ids[11]] == 4
    assert site.client.post('/api/webapp/xi/swap_bench', json=request_body).status_code == 400
    assert positions(site) == saved
    with site.sessions() as db:
        assert db.get(User, site.uid).batting_order_set_at is not None
        assert db.query(ActivityLog).filter_by(action='swap').count() == 1


@pytest.mark.parametrize('change', ['roster', 'settings', 'player'])
def test_stale_previews_cannot_apply_after_the_inputs_change(planner_site, change):
    from models import GameConfig, Player, UserRoster
    site = planner_site
    result = preview(site)
    with site.sessions() as db:
        if change == 'roster':
            first, second = db.get(UserRoster, site.ids[0]), db.get(UserRoster, site.ids[1])
            first.order_position, second.order_position = second.order_position, first.order_position
        elif change == 'settings':
            db.query(GameConfig).one().chemistry_variety_target = 2
        else:
            db.get(Player, result['incoming']['player_id']).country = 'England'
        db.commit()
    changed = positions(site)
    response = site.client.post('/api/webapp/xi/swap_bench', json=dict(
        body(site), preview_token=result['preview_token']))
    assert response.status_code == 409 and response.get_json()['error'] == 'stale_preview'
    assert positions(site) == changed


def test_confirmation_rechecks_match_lock_and_needs_a_preview(planner_site):
    site = planner_site
    before = positions(site)
    assert site.client.post('/api/webapp/xi/swap_bench', json=body(site)).status_code == 409
    result = preview(site)
    site.locked = True
    assert preview(site)['after'] == result['after'], 'Read-only previews remain available during matches'
    response = site.client.post('/api/webapp/xi/swap_bench', json=dict(
        body(site), preview_token=result['preview_token']))
    assert response.status_code == 409 and response.get_json()['error'] == 'match_in_progress'
    assert positions(site) == before


def test_a_preview_only_confirms_the_two_selected_cards(planner_site):
    site = planner_site
    result = preview(site)
    before = positions(site)
    data = dict(body(site), preview_token=result['preview_token'])
    data['bench_roster_id'] = site.ids[12]
    response = site.client.post('/api/webapp/xi/swap_bench', json=data)
    assert response.status_code == 409
    assert positions(site) == before


@pytest.mark.parametrize('bad', ['foreign', 'same', 'reversed', 'missing', 'bad_type'])
def test_api_rejects_unowned_and_invalid_swap_inputs(planner_site, bad):
    site = planner_site
    data = body(site)
    if bad == 'foreign': data['bench_roster_id'] = site.foreign_id
    elif bad == 'same': data['bench_roster_id'] = data['xi_roster_id']
    elif bad == 'reversed': data['bench_roster_id'], data['xi_roster_id'] = data['xi_roster_id'], data['bench_roster_id']
    elif bad == 'missing': data['bench_roster_id'] = 0
    else: data['bench_roster_id'] = 'not-an-id'
    before = positions(site)
    for endpoint in ['preview_swap', 'swap_bench']:
        assert site.client.post('/api/webapp/xi/' + endpoint, json=data).status_code == 400
    assert positions(site) == before


def command(site):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=987001),
                             message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(args=['4', '12'], user_data={})
    asyncio.run(site.handler.swap_preview_command(update, context))
    assert 'SWAP PREVIEW' in update.message.reply_text.await_args.args[0]
    return update, context


def callback(site, context, action, actor=987001):
    pending = context.user_data.get('chemistry_swap_preview')
    data = f"chem_{action}_{pending['key']}"
    query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=actor),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(site.handler.chemistry_swap_callback(
        SimpleNamespace(callback_query=query), context))
    return query


def test_telegram_requires_confirmation_and_cancel_writes_nothing(planner_site):
    site = planner_site
    before = positions(site)
    _, context = command(site)
    assert positions(site) == before
    callback(site, context, 'cancel')
    assert positions(site) == before and not context.user_data
    _, context = command(site)
    callback(site, context, 'apply')
    assert positions(site)[site.ids[11]] == 4


def test_telegram_uses_pxi_display_numbers_not_batting_positions(planner_site):
    from models import UserRoster
    site = planner_site
    with site.sessions() as db:
        db.get(UserRoster, site.ids[4]).order_position = 1
        for index in range(4):
            db.get(UserRoster, site.ids[index]).order_position = index + 2
        db.commit()
    update, context = command(site)
    assert 'Card 4' in update.message.reply_text.await_args.args[0]
    callback(site, context, 'apply')
    assert positions(site)[site.ids[11]] == 5


def test_cmuchem_shows_bench_upgrades_and_their_buttons_open_a_preview(planner_site, monkeypatch):
    from handlers import cmuchem

    site = planner_site
    monkeypatch.setattr(cmuchem, 'get_session', site.sessions)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=987001),
                             message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(user_data={})
    before = positions(site)
    asyncio.run(cmuchem.cmuchem_handler(update, context))
    sent = update.message.reply_text.await_args
    assert 'Bench cards that improve chemistry' in sent.args[0]
    assert 'OVR -6' in sent.args[0]
    keyboard = sent.kwargs['reply_markup']
    query = SimpleNamespace(data=keyboard.inline_keyboard[0][0].callback_data,
                            from_user=SimpleNamespace(id=987001),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(site.handler.chemistry_swap_callback(SimpleNamespace(callback_query=query), context))
    assert 'SWAP PREVIEW' in query.edit_message_text.await_args.args[0]
    assert positions(site) == before


@pytest.mark.parametrize('blocker', ['owner', 'expired', 'roster', 'settings', 'match'])
def test_telegram_confirmations_recheck_ownership_expiry_state_and_locks(planner_site, blocker):
    from models import GameConfig, UserRoster
    site = planner_site
    _, context = command(site)
    actor = 987002 if blocker == 'owner' else 987001
    if blocker == 'expired': context.user_data['chemistry_swap_preview']['expires_at'] = 0
    elif blocker == 'match': site.locked = True
    elif blocker in {'roster', 'settings'}:
        with site.sessions() as db:
            if blocker == 'roster': db.get(UserRoster, site.ids[0]).order_position = 0
            else: db.query(GameConfig).one().chemistry_variety_target = 2
            db.commit()
    before = positions(site)
    query = callback(site, context, 'apply', actor=actor)
    assert query.answer.await_args.kwargs['show_alert'] is True
    assert positions(site) == before
