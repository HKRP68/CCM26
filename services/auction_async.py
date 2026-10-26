"""Bounded workers for synchronous auction transactions.

SQLAlchemy sessions must be created, used and closed inside the submitted
function. Telegram calls stay on the application's event loop. A separate
executor keeps expensive administration from occupying the bid workers.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading


class AuctionWorkerBusy(RuntimeError):
    pass


_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="auction-db")
_slots = threading.BoundedSemaphore(4)
BUSY_MESSAGE = "⏳ Auction management is busy. Please retry in a moment."


async def run_db(work):
    """Run one transaction without blocking the bot or building a backlog."""
    if not _slots.acquire(blocking=False):
        raise AuctionWorkerBusy(BUSY_MESSAGE)
    try:
        future = _workers.submit(work)
    except BaseException:
        _slots.release()
        raise
    future.add_done_callback(lambda _: _slots.release())
    # Cancellation must not free a slot while its write is still running.
    return await asyncio.shield(asyncio.wrap_future(future))


async def run_session(session_factory, work, *, commit=False):
    """Keep a session and its transaction wholly inside one database worker."""
    def transact():
        session = session_factory()
        try:
            result = work(session)
            if commit:
                session.commit()
            return result
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return await run_db(transact)
