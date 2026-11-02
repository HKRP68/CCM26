"""Fresh auction bots can import a catalogue without overwrites or role bypasses."""
import asyncio
import csv
import io
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.franchise_player_import import MAX_BYTES, MAX_ROWS, import_players, parse_csv

ADMIN, MANAGER, OWNER = 884001, 884002, 884003
CHAT = -100884001
CSV = b'name,rating,role,country,base_lakh\nFirst Batter,85,BAT,India,20\nSecond Bowler,86,BOWL,Australia,30\n'


@pytest.fixture
def room(tmp_path, monkeypatch):
    from database import Base
    import models
    from services import admin_ids, auction_service as A
    for key in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS):
        monkeypatch.setenv(key, '')
    monkeypatch.setenv('BOT_ADMIN_IDS', str(ADMIN))
    monkeypatch.setattr(admin_ids, 'get_config', lambda: {})
    engine = create_engine('sqlite:///' + str(tmp_path/'import.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    season = A.create_season(db, 'Fresh import', min_squad_size=0, max_squad_size=5)
    A.bind_chat(db, season, CHAT)
    A.create_franchise(db, season, 'One', owner_tg_id=OWNER)
    A.create_franchise(db, season, 'Two', owner_tg_id=OWNER+1)
    A.add_auction_admin(db, MANAGER, by_tg_id=ADMIN)
    db.commit()
    yield SimpleNamespace(db=db, engine=engine, sessions=sessions, season=season, A=A)
    db.close()
    engine.dispose()


def test_fresh_catalogue_pool_and_idempotent_prices(room):
    from models import AuctionLot, Player
    rows = parse_csv(CSV)
    result = import_players(room.db, room.season, rows, actor_tg_id=MANAGER)
    room.db.commit()
    assert result['added'] == result['catalogue_created'] == 2
    lots = room.db.query(AuctionLot).order_by(AuctionLot.lot_no).all()
    assert [(p.name,p.base_price_lakh,p.is_overseas) for p in lots] == [('First Batter',20,False),('Second Bowler',30,True)]
    assert room.season.min_base_price_lakh == 20
    second = import_players(room.db, room.season, rows, actor_tg_id=MANAGER)
    room.db.commit()
    assert second == dict(added=0, skipped=2, catalogue_created=0, prices_changed=0)
    assert room.db.query(Player).count() == room.db.query(AuctionLot).count() == 2
    assert all(team.purse_remaining_lakh == team.purse_total_lakh for team in room.A.franchises(room.db, room.season.id))


@pytest.mark.parametrize('payload', [
    b'name,rating,role\nA,85,BAT\n',
    b'name,rating,role,country,unknown\nA,85,BAT,India,x\n',
    b'name,name,rating,role,country\nA,A,85,BAT,India\n',
    b'name,rating,role,country\nA,85.5,BAT,India\n',
    b'name,rating,role,country\nA,0,BAT,India\n',
    b'name,rating,role,country\nA,85,invalid,India\n',
    b'name,rating,role,country,base_lakh\nA,85,BAT,India,1.5\n',
    b'name,rating,role,country,base_lakh\nA,85,BAT,India,2cr\n',
    b'name,rating,role,country\nA,85,BAT,India,extra\n',
    b'name,rating,role,country\nA,85,BAT\n',
    b'name,rating,role,country\n', b'\xff', b'\x00',
])
def test_invalid_whole_csv_is_rejected_before_import(payload):
    from services.auction_service import AuctionError
    with pytest.raises(AuctionError):
        parse_csv(payload)


def test_size_rows_bom_duplicates_and_conflicts():
    from services.auction_service import AuctionError
    with pytest.raises(AuctionError, match='2 MB'):
        parse_csv(b'x'*(MAX_BYTES+1))
    with pytest.raises(AuctionError, match='2,000'):
        parse_csv(b'name,rating,role,country\n'+b'A,85,BAT,India\n'*(MAX_ROWS+1))
    rows = parse_csv(b'\xef\xbb\xbfname,rating,role,country\nA,85,BAT,India\nA,85,BAT,India\n')
    assert len(rows) == 1
    with pytest.raises(AuctionError, match='conflicting'):
        parse_csv(b'name,rating,role,country\nA,85,BAT,India\nA,86,BAT,India\n')


def test_existing_catalogue_is_reused_but_cannot_be_overwritten(room):
    from models import AuctionLot, Player
    import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=MANAGER)
    room.db.commit()
    original = room.db.query(Player).filter_by(name='First Batter').one()
    with pytest.raises(room.A.AuctionError, match='conflicts'):
        import_players(room.db, room.season, parse_csv(b'name,rating,role,country\nNew Player,80,WK,India\nFirst Batter,99,BAT,India\n'), actor_tg_id=MANAGER)
    assert room.db.query(Player).filter_by(name='New Player').count() == 0
    assert original.rating == 85
    room.db.rollback()
    result = import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=ADMIN)
    assert result['catalogue_created'] == result['added'] == 0
    assert room.db.query(AuctionLot).count() == 2


def test_permissions_phase_and_overseas_are_authoritative(room):
    from models import Player
    for actor in (OWNER, 999999):
        with pytest.raises(room.A.AuctionError, match='Auction Managers'):
            import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=actor)
    with pytest.raises(room.A.AuctionError, match='overseas'):
        import_players(room.db, room.season, parse_csv(b'name,rating,role,country,overseas\nA,85,BAT,India,true\n'), actor_tg_id=MANAGER)
    assert room.db.query(Player).count() == 0
    room.season.status = room.A.STATUS_LIVE
    room.db.commit()
    with pytest.raises(room.A.AuctionError, match='setup'):
        import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=MANAGER)
    assert room.db.query(Player).count() == 0


def test_signed_lot_is_not_repriced_and_queued_price_updates_restamp(room):
    from models import AuctionLot
    import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=MANAGER)
    room.db.commit()
    lots = room.db.query(AuctionLot).order_by(AuctionLot.id).all()
    lots[0].status = room.A.LOT_SOLD
    lots[0].sold_price_lakh = 99
    room.db.commit()
    changed = parse_csv(CSV.replace(b'India,20',b'India,15'))
    with pytest.raises(room.A.AuctionError, match='left the queue'):
        import_players(room.db, room.season, changed, actor_tg_id=MANAGER)
    assert lots[0].sold_price_lakh == 99
    same = import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=MANAGER)
    assert same['prices_changed'] == 0
    rows = parse_csv(CSV.replace(b'Australia,30',b'Australia,10'))
    updated = import_players(room.db, room.season, rows, actor_tg_id=MANAGER)
    assert updated['prices_changed'] == 1 and room.season.min_base_price_lakh == 10


def test_caller_rollback_removes_every_new_catalogue_and_pool_row(room):
    from models import AuctionLot, Player
    import_players(room.db, room.season, parse_csv(CSV), actor_tg_id=MANAGER)
    room.db.rollback()
    assert room.db.query(Player).count() == room.db.query(AuctionLot).count() == 0


def fake_update(*, actor=MANAGER, chat_id=CHAT, payload=CSV, filename='players.csv', file_size=None, text=None):
    replies, downloads = [], []
    async def reply_text(message, **kwargs):
        replies.append(message)
    async def get_file():
        async def download():
            downloads.append(True)
            return bytearray(payload)
        return SimpleNamespace(download_as_bytearray=download)
    doc = SimpleNamespace(file_name=filename, file_size=len(payload) if file_size is None else file_size, get_file=get_file)
    msg = SimpleNamespace(text=text, document=doc if text is None else None, reply_text=reply_text)
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id,type='supergroup'),
                           effective_user=SimpleNamespace(id=actor), effective_message=msg), replies, downloads


def test_upload_flow_rechecks_current_roles_and_consumes_only_explicit_imports(room, monkeypatch):
    from handlers import franchise_player_import as H
    from telegram.ext import ApplicationHandlerStop
    from models import Player
    monkeypatch.setattr(H,'get_session',room.sessions)
    update,replies,downloads=fake_update()
    context=SimpleNamespace(args=[],user_data={})
    asyncio.run(H.player_document_router(update,context))
    assert not downloads and not replies
    asyncio.run(H.importplayers_handler(update,context))
    assert H._PENDING in context.user_data
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(H.player_document_router(update,context))
    assert len(downloads) == 1 and 'Player import complete' in replies[-1]
    assert H._PENDING not in context.user_data
    assert room.db.query(Player).count() == 2
    asyncio.run(H.importplayers_handler(update,context))
    room.A.remove_auction_admin(room.db,MANAGER,by_tg_id=ADMIN)
    room.db.commit()
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(H.player_document_router(update,context))
    assert len(downloads) == 1, 'A revoked manager cannot even start another download'
    assert 'Auction Managers' in replies[-1]


def test_upload_cancel_wrong_chat_stale_season_and_oversize(room, monkeypatch):
    from handlers import franchise_player_import as H
    from telegram.ext import ApplicationHandlerStop
    monkeypatch.setattr(H,'get_session',room.sessions)
    context=SimpleNamespace(args=[],user_data={})
    update,replies,downloads=fake_update()
    asyncio.run(H.importplayers_handler(update,context))
    other,_,other_downloads=fake_update(chat_id=CHAT-1)
    asyncio.run(H.player_document_router(other,context))
    assert not other_downloads
    large,large_replies,large_downloads=fake_update(file_size=MAX_BYTES+1)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(H.player_document_router(large,context))
    assert not large_downloads and '2 MB' in large_replies[-1]
    context.user_data[H._PENDING]['season_id'] += 100
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(H.player_document_router(update,context))
    assert not downloads and 'different auction' in replies[-1]
    cancel,cancel_replies,_=fake_update(text='/cancel')
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(H.player_document_router(cancel,context))
    assert H._PENDING not in context.user_data and 'cancelled' in cancel_replies[-1]


def test_concurrent_uploads_cannot_start_duplicate_catalogue_imports(room,monkeypatch):
    from handlers import franchise_player_import as H
    from telegram.ext import ApplicationHandlerStop
    from models import Player
    monkeypatch.setattr(H,'get_session',room.sessions)
    first,replies,_=fake_update()
    second,second_replies,second_downloads=fake_update()
    context=SimpleNamespace(args=[],user_data={})
    async def exercise():
        entered,release=asyncio.Event(),asyncio.Event()
        async def get_file():
            async def download():
                entered.set();await release.wait();return bytearray(CSV)
            return SimpleNamespace(download_as_bytearray=download)
        first.effective_message.document.get_file=get_file
        await H.importplayers_handler(first,context)
        task=asyncio.create_task(H.player_document_router(first,context))
        await asyncio.wait_for(entered.wait(),3)
        with pytest.raises(ApplicationHandlerStop):await H.player_document_router(second,context)
        assert not second_downloads and 'already processing' in second_replies[-1]
        release.set()
        with pytest.raises(ApplicationHandlerStop):await task
    asyncio.run(exercise())
    assert room.db.query(Player).count()==2
    assert H._BUSY not in context.user_data and H._PENDING not in context.user_data
