"""Exercise the group /rcpl registration, ownership and auction against a real DB.

Uses temporary SQLite by default. RCPL_TEST_DATABASE_URL can point at a test
PostgreSQL server whose user may create schemas. Each run gets its own schema.
"""
import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope="module")
def league(tmp_path_factory):
    import database
    import models  # register tables before create_all
    from test_auction_league_flow import _seed_league

    # Patch the session factory so every real repository, handler and test
    # thread writes to the same private database without swapping ORM classes.
    url = os.getenv("RCPL_TEST_DATABASE_URL")
    if url:
        admin = create_engine(url)
        schema = "test_rcpl_" + uuid4().hex
        with admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    else:
        path = tmp_path_factory.mktemp("multiplayer") / "rooms.db"
        engine = create_engine(f"sqlite:///{path}")
    try:
        database.Base.metadata.create_all(engine)
        with patch.object(database, "SessionLocal", sessionmaker(bind=engine, autoflush=False)):
            yield _seed_league()
    finally:
        engine.dispose()
        if url:
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


def race(*actions):
    from services.auction_league_service import AuctionLeagueError

    start = Barrier(len(actions))

    def run(action):
        start.wait(5)
        try:
            return action()
        except AuctionLeagueError as error:
            return error

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def test_simultaneous_claims_and_host_permissions(league):
    from services import auction_league_service as al, rcpl_multiplayer_service as mp

    chat, host, guest = -401, 51, 52
    room = mp.create_room(chat, host, "Host", league)
    claims = race(*(lambda uid=uid: mp.claim_team(chat, room.id, uid, str(uid), 0) for uid in (host, guest)))
    assert sum(isinstance(c, al.AuctionLeagueError) for c in claims) == 1
    claimed = mp.get_room(chat, room.id).state["managers"]
    assert len(claimed) == 1
    winner = next(iter(claimed.values()))["id"]
    with pytest.raises(al.AuctionLeagueError):
        mp.start_room(chat, room.id, guest)
    with pytest.raises(al.AuctionLeagueError):
        mp.claim_team(-402, room.id, guest, "Guest", 1)
    mp.leave_team(chat, room.id, winner)
    assert not mp.get_room(chat, room.id).state["managers"]
    mp.claim_team(chat, room.id, host, "Host", 0)
    mp.claim_team(chat, room.id, guest, "Guest", 1)
    starts = race(*(lambda: mp.start_room(chat, room.id, host) for _ in range(2)))
    assert any(not isinstance(r, al.AuctionLeagueError) for r in starts)
    started = mp.get_room(chat, room.id)
    assert started.state["phase"] == al.PHASE_AUCTION
    assert len(started.state["managers"]) == 2
    with pytest.raises(al.AuctionLeagueError):
        mp.claim_team(chat, room.id, 53, "Late arrival", 2)
    mp.cancel_room(chat, room.id, host)


class GroupBot:
    """Only the Telegram transport is replaced. Buttons go through registered callbacks."""

    def __init__(self, chat):
        from handlers import auction_league

        self.chat = NS(id=chat, type="supergroup", title="RCPL test group")
        self.messages = []
        self.handlers = []
        self.jobs = {}
        def schedule(callback, *, interval, first, name):
            self.jobs[name] = callback
        queue = NS(run_repeating=schedule)
        auction_league.register(NS(add_handler=self.handlers.append, job_queue=queue, bot_data={}))
        self.bot = NS(id=1000, username="testbot", send_message=self.send,
                      edit_message_text=self.send, edit_message_reply_markup=AsyncMock())
        self.context = NS(bot=self.bot, bot_data={}, user_data={}, args=[], job_queue=None)

    async def send(self, *args, text="", reply_markup=None, **kwargs):
        if not text and args:
            text = args[-1]
        buttons = [b.callback_data for row in getattr(reply_markup, "inline_keyboard", ()) for b in row]
        self.messages.append((text, buttons))
        return NS(chat_id=self.chat.id, chat=self.chat, message_id=len(self.messages),
                  reply_text=self.send, edit_text=self.send)

    def callback(self, prefix):
        return next(data for _, rows in reversed(self.messages) for data in rows if data.startswith(prefix))

    async def act(self, uid, *, command=None, args=(), button=None):
        tg = NS(id=uid, first_name=f"Manager {uid}", username=f"user{uid}", is_bot=False)
        message = NS(chat=self.chat, chat_id=self.chat.id, message_id=1, text=command,
                     reply_text=self.send, edit_text=self.send)
        q = NS(data=button, from_user=tg, message=message, answer=AsyncMock(),
               edit_message_text=self.send, edit_message_reply_markup=AsyncMock()) if button else None
        upd = NS(effective_chat=self.chat, effective_message=message, message=message, effective_user=tg, callback_query=q)
        self.context.args = list(args)
        if button:
            handler = next(h for h in self.handlers if getattr(h, "pattern", None) and h.pattern.match(button))
        else:
            handler = next(h for h in self.handlers if command in getattr(h, "commands", ()))
        await handler.callback(upd, self.context)


def test_group_command_to_live_auction(league):
    from database import get_session
    from models import AuctionLeagueSave
    from services import auction_league_service as al, rcpl_multiplayer_service as mp

    async def run():
        chat, host, guest = -403, 101, 102
        bot = GroupBot(chat)
        await bot.act(host, command="rcpl", args=["IPL"])
        room = mp.get_room(chat)
        assert room and room.host_id == host
        await bot.act(host, button=bot.callback(f"rcpl:claim:{room.id}:0"))
        await bot.act(guest, button=bot.callback(f"rcpl:claim:{room.id}:1"))
        await bot.act(host, button=bot.callback(f"rcpl:start:{room.id}"))
        confirm = bot.callback(f"rcpl:go:{room.id}")
        await bot.act(guest, button=confirm)
        assert mp.get_room(chat).state["phase"] == "lobby"
        await bot.act(host, button=confirm)
        assert mp.get_room(chat).state["phase"] == al.PHASE_AUCTION
        # A fresh process/context knows the same owners and lot from the DB.
        reopened = GroupBot(chat)
        await reopened.act(guest, command="rcpl")
        assert reopened.messages
        assert set(x["id"] for x in mp.get_room(chat).state["managers"].values()) == {host, guest}
        await reopened.act(guest, button=reopened.callback(f"rcpl:a:{room.id}:bid:"))
        leading = mp.get_room(chat).state["lot"]
        assert mp.get_room(chat).state["managers"][leading["leader"]]["id"] == guest

        def expire_lot(current):
            current.state["lot"]["deadline"] = time.time() - 1
        mp.change_room(chat, room.id, expire_lot)
        await reopened.jobs["rcpl_auction_clock"](reopened.context)
        updated = mp.get_room(chat)
        assert updated.state["last_lot"]["winner"] == leading["leader"]
        assert updated.state["lot"]["seq"] == leading["seq"] + 1
        await reopened.act(guest, button=reopened.callback(f"rcpl:a:{room.id}:teams"))
        await reopened.act(guest, button=reopened.callback(f"rcpl:a:{room.id}:squad:1"))
        assert al.card(updated.state, leading["pid"])["name"] in reopened.messages[-1][0]
        with get_session() as s:
            assert s.query(AuctionLeagueSave).count() == 0

    asyncio.run(run())


def test_bids_build_both_human_and_ai_squads_and_playoffs(league):
    from services import auction_league_service as al, rcpl_multiplayer_service as mp
    from services import rcpl_multiplayer_auction as auction, rcpl_season as season

    chat, host, guest = -404, 61, 62
    room = mp.create_room(chat, host, "Host", league)
    rid = room.id
    mp.claim_team(chat, rid, host, "Host", 0)
    mp.claim_team(chat, rid, guest, "Guest", 1)
    started = mp.start_room(chat, rid, host)
    names = started.state["team_order"]
    lot = started.state["lot"]
    price = al.next_price(lot)
    with pytest.raises(al.AuctionLeagueError, match="franchise manager"):
        mp.bid(chat, rid, 999, lot["seq"], price)
    bids = race(*(lambda uid=uid: mp.bid(chat, rid, uid, lot["seq"], price) for uid in (host, guest)))
    assert sum(isinstance(b, al.AuctionLeagueError) for b in bids) == 1
    state = mp.get_room(chat, rid).state
    high = state["lot"]
    leader = high["leader"]
    assert leader in names[:2]
    with pytest.raises(al.AuctionLeagueError, match="changed"):
        mp.bid(chat, rid, host, lot["seq"], price)
    timed = mp.change_room(chat, rid, lambda current: auction.tick(current.state, now=high["deadline"]))
    assert timed.state["teams"][leader]["purse"] == al.PURSE_LAKH - price
    assert timed.state["last_lot"]["winner"] == leader
    with pytest.raises(al.AuctionLeagueError, match="ended"):
        mp.bid(chat, rid, guest, lot["seq"], al.next_price(high))

    # The next open lot gives AI franchises a chance to put in their own bids.
    for _ in range(20):
        current = timed.state["lot"]
        if current["ai_at"] is not None:
            timed = mp.change_room(chat, rid, lambda r: auction.tick(r.state, now=current["ai_at"]))
            if timed.state["lot"]["leader"] in names[2:]:
                break
        timed = mp.change_room(chat, rid, lambda r: auction.tick(r.state, now=r.state["lot"]["deadline"]))
    else:
        pytest.fail("No AI team bid on the first 20 lots")
    lot = timed.state["lot"]
    ai = lot["leader"]
    with pytest.raises(al.AuctionLeagueError, match="Only the host"):
        mp.finish_auction(chat, rid, guest, lot["seq"], al.next_price(lot))
    closed = mp.finish_auction(chat, rid, host, lot["seq"], al.next_price(lot))
    assert closed.state["phase"] == al.PHASE_SEASON
    assert closed.state["teams"][ai]["purse"] <= al.PURSE_LAKH - lot["price"]
    squads = {name: data["squad"] for name, data in closed.state["teams"].items()}
    assert all(al.squad_is_legal(closed.state, team) for team in squads)
    players = [e["pid"] for squad in squads.values() for e in squad]
    assert len(players) == len(set(players))

    room = mp.change_room(chat, rid, lambda r: season.prepare(r.state))
    while room.state["phase"] == al.PHASE_SEASON:
        fixture = al.pending_fixtures(room.state)[0]
        captains = [room.state["managers"][team]["id"] for team in (fixture["home"], fixture["away"])
                    if team in room.state["managers"]]
        assert captains
        with pytest.raises(al.AuctionLeagueError, match="Only a manager"):
            mp.change_room(chat, rid, lambda r: season.play(r.state, 999, fixture["no"]))
        for index, uid in enumerate(captains):
            room = mp.change_room(chat, rid, lambda r: season.play(r.state, uid, fixture["no"]))
            if index == 0 and len(captains) == 2:
                assert al.pending_fixtures(room.state)[0]["no"] == fixture["no"]
    assert room.state["champion"] in names
    assert all(fx["status"] == "done" for fx in room.state["fixtures"])
    assert mp.get_room(chat) is None
