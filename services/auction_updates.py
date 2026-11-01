"""Bounded, database-backed auction change notifications.

Only opaque revisions leave this module. The normal auction endpoint still
authenticates each request and builds the viewer-specific payload. Probes use
one SQL round trip and never keep a connection open between notifications.
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func, select

POLL_SECONDS = 0.5
STREAM_SECONDS = 25.0
HEARTBEAT_SECONDS = 5.0
REAUTHORIZE_SECONDS = 5.0


@dataclass(frozen=True)
class Snapshot:
    revision: str
    season_id: int | None
    authority: tuple

    def public(self):
        return {"revision": self.revision, "season_id": self.season_id}


class AccessChanged(Exception):
    def __init__(self, payload, status):
        self.payload, self.status = payload, status


class StreamSlots:
    """Leave worker threads available for the ordinary auction APIs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0

    def acquire(self, limit):
        with self.lock:
            if self.active >= limit:
                return False
            self.active += 1
            return True

    def release(self):
        with self.lock:
            self.active -= 1


def snapshot(db, season_id, tg_id):
    """Observe committed writes from any bot or web worker in one query.

    Including actual lot/franchise values catches stage, deadline, balance and
    ownership changes even when a writer does not append an auction event.
    Season updated_at covers rule edits; the event cursor covers pool/ledger
    and preseason operations. The hash never exposes owner IDs or shortlists.
    """
    from models import AuctionAdmin, AuctionEvent, AuctionFranchise, AuctionLot, AuctionSeason
    from services import auction_service as A
    from services.auction_webapp import global_roles, team_roles
    from services.admin_ids import is_bot_admin

    delegated = select(AuctionAdmin.id).where(AuctionAdmin.tg_id == tg_id).exists()
    bot_admin = is_bot_admin(tg_id)
    if season_id is None:
        # An empty dashboard must discover a newly created/assigned season.
        statement = select(
            select(func.max(AuctionSeason.id)).scalar_subquery(),
            select(func.max(AuctionSeason.updated_at)).scalar_subquery(),
            select(func.count(AuctionFranchise.id)).scalar_subquery(),
            select(func.max(AuctionFranchise.updated_at)).scalar_subquery(),
            delegated.label("delegated"),
        )
        rows = db.execute(statement).all()
        authority = (bot_admin, tuple(global_roles(tg_id, appointed=bool(rows[0][-1]))), ())
    else:
        event_cursor = select(func.max(AuctionEvent.id)).where(
            AuctionEvent.season_id == season_id).scalar_subquery()
        # A separate label per table keeps ownership fields unambiguous.
        columns = [column.label("season__" + column.name)
                   for column in AuctionSeason.__table__.columns]
        columns += [column.label("lot__" + column.name)
                    for column in AuctionLot.__table__.columns]
        columns += [column.label("franchise__" + column.name)
                    for column in AuctionFranchise.__table__.columns]
        statement = select(*columns, event_cursor.label("event_cursor"),
                           delegated.label("delegated")).select_from(AuctionSeason).outerjoin(
            AuctionLot, AuctionLot.id == AuctionSeason.current_lot_id).outerjoin(
            AuctionFranchise, AuctionFranchise.season_id == AuctionSeason.id).where(
            AuctionSeason.id == season_id).order_by(AuctionFranchise.id)
        rows = db.execute(statement).all()
        owned = []
        for result in rows:
            values = result._mapping
            franchise = SimpleNamespace(owner_tg_id=values["franchise__owner_tg_id"],
                                        co_owner_ids_json=values["franchise__co_owner_ids_json"],
                                        team_manager_ids_json=values.get("franchise__team_manager_ids_json"))
            if A.may_bid_for(franchise, tg_id):
                owned.append((values["franchise__id"], tuple(team_roles(franchise, tg_id))))
        authority = (bot_admin, tuple(global_roles(tg_id, appointed=bool(rows[0]._mapping["delegated"]) if rows else False)),
                     tuple(owned), bool(rows))
    raw = json.dumps([authority, [list(row) for row in rows]], default=str,
                     ensure_ascii=True, separators=(",", ":"))
    return Snapshot(hashlib.sha256(raw.encode()).hexdigest(), season_id, authority)


def encode(event, data):
    return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=True,
                                                     separators=(",", ":")) + "\n\n"


def notifications(read_snapshot, initial, *, clock=None, pause=None):
    """Yield a prompt initial cursor, changes, heartbeats, then release worker."""
    clock = clock or time.monotonic
    pause = pause or time.sleep
    started = last_sent = clock()
    previous = initial
    yield encode("ready", initial.public())
    while clock() - started < STREAM_SECONDS:
        pause(POLL_SECONDS)
        if clock() - started >= STREAM_SECONDS:
            break
        try:
            current = read_snapshot()
        except AccessChanged as exc:
            yield encode("access", {**exc.payload, "status": exc.status})
            return
        if current.revision != previous.revision:
            previous = current
            last_sent = clock()
            yield encode("changed", current.public())
        elif clock() - last_sent >= HEARTBEAT_SECONDS:
            last_sent = clock()
            yield encode("heartbeat", {"server_time": datetime.utcnow().isoformat() + "Z"})
    yield encode("end", previous.public())
