"""Administration cannot exhaust the bid executor or queue unbounded writes."""
import asyncio
import threading
import unittest

from services.auction_async import AuctionWorkerBusy, run_db, run_session


class AuctionWorkersTests(unittest.TestCase):
    def test_failed_transaction_rolls_back_closes_and_does_not_poison_next_command(self):
        calls = []
        class Session:
            def rollback(self): calls.append(('rollback', threading.get_ident()))
            def close(self): calls.append(('close', threading.get_ident()))
            def commit(self): calls.append(('commit', threading.get_ident()))
        def fail(session):
            raise RuntimeError('database operation failed')
        async def run():
            with self.assertRaisesRegex(RuntimeError, 'operation failed'):
                await run_session(Session, fail, commit=True)
            self.assertEqual('next command', await run_session(Session, lambda _: 'next command', commit=True))
        main_thread = threading.get_ident()
        asyncio.run(run())
        self.assertEqual(['rollback', 'close', 'commit', 'close'], [name for name, _ in calls])
        self.assertTrue(all(thread != main_thread for _, thread in calls))

    def test_saturation_refuses_promptly_and_recovers_after_workers_finish(self):
        async def run():
            gate = threading.Event()
            started = [threading.Event() for _ in range(4)]

            def work(index):
                started[index].set()
                gate.wait(timeout=3)
                return index

            tasks = [asyncio.create_task(run_db(lambda i=i: work(i))) for i in range(4)]
            try:
                for flag in started:
                    self.assertTrue(await asyncio.to_thread(flag.wait, 1))
                with self.assertRaises(AuctionWorkerBusy):
                    await run_db(lambda: self.fail('busy work was admitted'))
                # Bid/middleware work uses the default executor independently.
                self.assertEqual('pong', await asyncio.to_thread(lambda: 'pong'))
            finally:
                gate.set()
                self.assertEqual(list(range(4)), await asyncio.gather(*tasks))
            self.assertEqual('recovered', await run_db(lambda: 'recovered'))

        asyncio.run(run())

    def test_cancelled_caller_keeps_its_slot_until_transaction_finishes(self):
        async def run():
            gate, started, finished = threading.Event(), threading.Event(), threading.Event()

            def transaction():
                started.set()
                gate.wait(timeout=3)
                finished.set()

            task = asyncio.create_task(run_db(transaction))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            gate.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

        asyncio.run(run())
