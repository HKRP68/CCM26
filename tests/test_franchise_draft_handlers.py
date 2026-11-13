"""Draft commands keep pasted tables, group scope and current button authority."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ADMIN, OWNER, COOWNER, OTHER, MANAGER = 884099, 1234567890123, 884002, 884003, 884004
CHAT = -100884001


@pytest.fixture
def room(tmp_path, monkeypatch):
    from database import Base
    import models
    from services import admin_ids, auction_service as A, franchise_draft as D
    from handlers import franchise_draft as H
    for key in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS):
        monkeypatch.setenv(key, '')
    monkeypatch.setenv('BOT_ADMIN_IDS', str(ADMIN))
    monkeypatch.setattr(admin_ids, 'get_config', lambda: {})
    engine = create_engine('sqlite:///' + str(tmp_path / 'draft-handlers.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    season = A.create_season(db, 'Draft Handlers', opening_purse_lakh=10000)
    D.enable(db, season, rounds=2, by_tg_id=ADMIN)
    A.bind_chat(db, season, CHAT)
    one = A.create_franchise(db, season, 'Team One', owner_tg_id=OWNER)
    two = A.create_franchise(db, season, 'Team Two', owner_tg_id=OTHER)
    A.set_co_owners(db, one, [COOWNER])
    A.set_team_managers(db, one, [MANAGER])
    for name in ('Joe Root', 'Ben Stokes', 'Pat Cummins', 'Jos Buttler', 'Jasprit Bumrah', 'Virat Kohli'):
        player = models.Player(name=name, rating=96, category='Batsman', country='England',
                               version='Base', is_active=True, bat_rating=96, bowl_rating=50,
                               bat_hand='Right', bowl_hand='Right', bowl_style='Medium Pacer')
        db.add(player)
        db.flush()
        A.add_players_to_pool(db, season, [player])
    D.set_rating_prices(db, season, '96:23cr', by_tg_id=ADMIN)
    D.set_order(db, season, [one.id, two.id], by_tg_id=ADMIN)
    db.commit()
    monkeypatch.setattr(H, 'get_session', sessions)
    yield SimpleNamespace(db=db, sessions=sessions, engine=engine, season=season,
                          one=one, two=two, A=A, D=D, H=H, models=models)
    db.close()
    engine.dispose()


def run(awaitable):
    return asyncio.run(awaitable)


class Message:
    def __init__(self, text='', chat_id=CHAT):
        self.text, self.chat_id, self.message_id = text, chat_id, 7
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self


class Query:
    def __init__(self, actor, data, message):
        self.from_user, self.data, self.message = SimpleNamespace(id=actor), data, message
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.message.replies.append((text, kwargs))


def update(actor=OWNER, text='', chat=CHAT, callback=None):
    message = Message(text, chat)
    return SimpleNamespace(effective_user=SimpleNamespace(id=actor),
                           effective_chat=SimpleNamespace(id=chat, type='supergroup' if chat < 0 else 'private'),
                           effective_message=message,
                           callback_query=Query(actor, callback, message) if callback else None)


def context(sid=None):
    return SimpleNamespace(args=[], user_data={'franchise_season_id': sid} if sid else {},
                           bot=SimpleNamespace(username='DraftTestBot'))


def live(room):
    room.D.start(room.db, room.season, by_tg_id=ADMIN)
    room.db.commit()


def test_new_team_command_adds_owner_coowners_and_full_purse(room):
    packet = room.H.execute(room.db, ADMIN, CHAT, room.season.id, 'dteam', 'Expansion | 884010 | 884011,884012')
    room.db.commit()
    team = next(team for team in room.A.franchises(room.db, room.season.id) if team.name == 'Expansion')
    assert team.owner_tg_id == 884010
    assert room.A.co_owner_ids(team) == [884011, 884012]
    assert team.purse_remaining_lakh == 10000 and team.draft_picks_total == 2
    assert 'Expansion' in packet['text']
    with pytest.raises(room.A.AuctionError):
        room.H.execute(room.db, OWNER, CHAT, room.season.id, 'dteam', 'Forbidden | 884013')
    room.db.rollback()


def test_timer_command_and_buttons_require_current_manager_authority(room):
    packet = room.H.execute(room.db, ADMIN, CHAT, room.season.id, 'dtimer', '8')
    room.db.commit()
    assert '8 minutes' in packet['text']
    room.H.execute_callback(room.db, ADMIN, CHAT, room.season.id, f'fd_m_{ADMIN}_{room.season.id}_15')
    room.db.commit()
    assert room.D.get_draft(room.db, room.season).pick_minutes == 15
    with pytest.raises(room.A.AuctionError):
        room.H.execute_callback(room.db, OWNER, CHAT, room.season.id, f'fd_m_{OWNER}_{room.season.id}_5')
    room.db.rollback()
    assert room.D.get_draft(room.db, room.season).pick_minutes == 15


def test_owner_skip_button_is_scoped_to_actor_group_and_exact_turn(room):
    live(room)
    packet = room.H.execute(room.db, OWNER, CHAT, room.season.id, 'draft')
    button = next(b for row in packet['markup'].inline_keyboard for b in row if b.callback_data.startswith('fd_s_'))
    with pytest.raises(room.A.AuctionError):
        room.H.execute_callback(room.db, OTHER, CHAT, room.season.id, button.callback_data)
    room.db.rollback()
    with pytest.raises(room.A.AuctionError):
        room.H.execute_callback(room.db, OWNER, CHAT - 1, room.season.id, button.callback_data)
    room.db.rollback()
    room.H.execute_callback(room.db, OWNER, CHAT, room.season.id, button.callback_data)
    room.db.commit()
    assert room.one.purse_remaining_lakh == 10000
    assert room.D.turn(room.db, room.season).id == room.two.id
    with pytest.raises(room.A.AuctionError, match='stale'):
        room.H.execute_callback(room.db, OWNER, CHAT, room.season.id, button.callback_data)
    room.db.rollback()


def test_skip_command_is_group_only_and_queue_is_public(room):
    live(room)
    with pytest.raises(room.A.AuctionError, match='publicly'):
        room.H.execute(room.db, OWNER, OWNER, room.season.id, 'dskip')
    room.H.execute(room.db, COOWNER, CHAT, room.season.id, 'dskip')
    room.db.commit()
    packet = room.H.execute(room.db, OTHER, CHAT, room.season.id, 'dskips')
    assert 'Team One' in packet['text'] and 'skip order' in packet['text']
    assert room.one.squad_size == 0 and room.one.purse_remaining_lakh == 10000


def test_multiline_command_preserves_rating_table_and_rich_fallback(room):
    event = update(ADMIN, '/dprices@DraftTestBot\n96:23cr\n95:21cr')
    ctx = context(room.season.id)
    ctx.args = ['96:23cr', '95:21cr']
    run(room.H.command_handler(event, ctx))
    room.db.expire_all()
    prices = json.loads(room.D.get_draft(room.db, room.season).rating_prices_json)
    assert prices == {'96': 2300, '95': 2100}
    text, kwargs = event.effective_message.replies[-1]
    assert '23' in text and '21' in text and kwargs['parse_mode'] == 'HTML'
    blocks = room.H.build_blocks(text)
    assert blocks and json.dumps(blocks)


def test_pick_command_only_quotes_then_confirm_debits_once(room):
    live(room)
    event, ctx = update(OWNER, '/Dpick Joe Root'), context()
    run(room.H.command_handler(event, ctx))
    room.db.expire_all()
    assert room.db.get(room.models.AuctionFranchise, room.one.id).purse_remaining_lakh == 10000
    text, kwargs = event.effective_message.replies[-1]
    assert 'Joe Root' in text and '23' in text and '77' in text
    sign = kwargs['reply_markup'].inline_keyboard[0][0].callback_data
    assert sign.startswith('fd_y_')
    event = update(OWNER, callback=sign)
    run(room.H.callback_handler(event, ctx))
    room.db.expire_all()
    assert room.db.get(room.models.AuctionFranchise, room.one.id).purse_remaining_lakh == 7700
    assert 'signed' in event.effective_message.replies[-1][0]
    second = update(OWNER, callback=sign)
    run(room.H.callback_handler(second, ctx))
    assert second.callback_query.answers[-1][1].get('show_alert')
    room.db.expire_all()
    assert room.db.get(room.models.AuctionFranchise, room.one.id).purse_remaining_lakh == 7700


@pytest.mark.parametrize('actor', [ADMIN, OTHER, MANAGER, 999999])
def test_only_current_owner_or_coowner_can_request_a_pick(room, actor):
    live(room)
    with pytest.raises(room.A.AuctionError):
        room.H.execute(room.db, actor, CHAT, None, 'dpick', 'Joe Root')
    room.db.rollback()
    assert room.db.query(room.models.FranchiseDraftConfirmation).count() == 0


def test_coowner_can_pick_but_another_coowner_cannot_answer_the_quote(room):
    live(room)
    packet = room.H.execute(room.db, COOWNER, CHAT, None, 'dpick', 'Joe Root')
    room.db.commit()
    sign = packet['markup'].inline_keyboard[0][0].callback_data
    with pytest.raises(room.A.AuctionError, match='owner who requested'):
        room.H.execute_callback(room.db, OWNER, CHAT, None, sign)
    room.db.rollback()
    packet = room.H.execute_callback(room.db, COOWNER, CHAT, None, sign.replace('fd_y_', 'fd_n_'))
    room.db.commit()
    assert 'cancelled' in packet['text']
    assert room.one.purse_remaining_lakh == 10000


def test_pick_and_confirmation_are_scoped_to_bound_group(room):
    live(room)
    with pytest.raises(room.A.AuctionError, match='publicly'):
        room.H.execute(room.db, OWNER, 1, room.season.id, 'dpick', 'Joe Root')
    room.db.rollback()
    packet = room.H.execute(room.db, OWNER, CHAT, None, 'dpick', 'Joe Root')
    room.db.commit()
    sign = packet['markup'].inline_keyboard[0][0].callback_data
    for chat in (1, CHAT - 1):
        with pytest.raises(room.A.AuctionError):
            room.H.execute_callback(room.db, OWNER, chat, room.season.id, sign)
        room.db.rollback()
    assert room.one.purse_remaining_lakh == 10000


def test_navigation_has_persistent_owner_and_group_binding(room):
    data = room.H._cb(OWNER, room.season.id, 'rules')
    # Real Telegram IDs often exceed six digits.
    packet = room.H.execute_callback(room.db, OWNER, CHAT, None, data)
    assert 'No limit' in packet['text']
    with pytest.raises(room.A.AuctionError, match='another user'):
        room.H.execute_callback(room.db, COOWNER, CHAT, None, data)
    with pytest.raises(room.A.AuctionError, match='different draft group'):
        room.H.execute_callback(room.db, OWNER, CHAT - 10, None, data)


def test_management_buttons_recheck_current_authority(room, monkeypatch):
    data = f'fd_a_{ADMIN}_{room.season.id}_start'
    monkeypatch.setenv('BOT_ADMIN_IDS', '')
    with pytest.raises(room.A.AuctionError, match='Managers'):
        room.H.execute_callback(room.db, ADMIN, CHAT, None, data)


def test_order_names_ids_and_random_and_keyboard_limits(room):
    assert room.H.parse_order(room.db, room.season, 'Team Two\nTeam One') == [room.two.id, room.one.id]
    assert room.H.parse_order(room.db, room.season, f'{room.one.id}|{room.two.id}') == [room.one.id, room.two.id]
    assert room.H.parse_order(room.db, room.season, 'random') is None
    with pytest.raises(room.A.AuctionError):
        room.H.parse_order(room.db, room.season, 'Team One|Missing')
    for actor in (ADMIN, OWNER):
        keyboard = room.H._keyboard(room.db, room.season, actor, page=1)
        for row in keyboard.inline_keyboard:
            assert all(len(button.callback_data.encode()) <= 64 for button in row)


def test_new_private_draft_selects_it_for_following_setup(room):
    event, ctx = update(ADMIN, '/dnew Next Season', chat=1), context()
    run(room.H.command_handler(event, ctx))
    assert ctx.user_data['franchise_season_id'] != room.season.id
    new = room.db.get(room.models.AuctionSeason, ctx.user_data['franchise_season_id'])
    assert new.name == 'Next Season' and room.D.get_draft(room.db, new)


def test_registration_exposes_the_draft_commands_and_callback():
    from services.franchise_bot_registry import COMMANDS, CALLBACKS
    commands = {command for module, names, _ in COMMANDS if module == 'draft' for command in names}
    assert {'draft', 'dpick', 'dnew', 'dprices', 'dcustom', 'dorder', 'drounds', 'dstart', 'dpause', 'dundo'} <= commands
    assert ('draft', 'callback_handler', '^fd_') in CALLBACKS
