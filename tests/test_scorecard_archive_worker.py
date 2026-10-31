"""Scorecard persistence in a worker preserves uploads on the bot's loop."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import services.match_webapp_service as scorecards
from services import tg_storage_service


def _archive_stubs(monkeypatch):
    tasks = set()
    monkeypatch.setattr(scorecards, '_TEXT_ARCHIVE_TASKS', tasks)
    monkeypatch.setattr(tg_storage_service, 'is_configured', lambda: True)
    monkeypatch.setattr(scorecards.mwa, 'get_state', lambda mid: {'match_id': mid})
    monkeypatch.setattr(scorecards, '_build_text_scorecard',
                        lambda mid, innings, result, so=None, **kw: f'Match {mid}: {result}')
    return tasks


def test_worker_snapshot_schedules_and_retains_archive_on_main_loop(monkeypatch):
    tasks = _archive_stubs(monkeypatch)
    monkeypatch.setattr(scorecards, 'build_scorecard', lambda *a: {
        'ok': True, 'innings': [{'number': 1}], 'current_innings': 2, 'target': 51,
    })
    added = []
    worker_threads = []

    class Session:
        def query(self, *args):
            worker_threads.append(threading.get_ident())
            return SimpleNamespace(filter=lambda *a: SimpleNamespace(first=lambda: None))

        def add(self, row):
            added.append(row)

    async def run():
        loop = asyncio.get_running_loop()
        bot_thread = threading.get_ident()
        started = asyncio.Event()
        release = asyncio.Event()
        uploads = []

        async def upload(text, filename, **kwargs):
            assert asyncio.get_running_loop() is loop
            assert threading.get_ident() == bot_thread
            uploads.append((text, filename, kwargs))
            started.set()
            await release.wait()

        monkeypatch.setattr(tg_storage_service, 'upload_text_async', upload)
        assert await asyncio.to_thread(
            scorecards.save_final_scorecard, Session(), 42, 'A won',
            extra_innings=[{'number': 3}], archive_loop=loop)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert len(tasks) == 1
        assert not next(iter(tasks)).done()
        assert uploads[0][0:2] == ('Match 42: A won', 'MatchNo42.txt')
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert not tasks
        assert worker_threads and worker_threads[0] != bot_thread

    asyncio.run(run())
    assert len(added) == 1
    assert json.loads(added[0].scorecard_json)['innings'] == [{'number': 1}, {'number': 3}]


def test_ordinary_loop_caller_still_uploads_and_retains_task(monkeypatch):
    tasks = _archive_stubs(monkeypatch)

    async def run():
        release = asyncio.Event()
        started = asyncio.Event()

        async def upload(text, filename, **kwargs):
            assert text == 'Match 7: B won'
            assert filename == 'MatchNo7.txt'
            started.set()
            await release.wait()

        monkeypatch.setattr(tg_storage_service, 'upload_text_async', upload)
        scorecards._archive_text_scorecard(7, [], 'B won')
        assert len(tasks) == 1
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert not tasks

    asyncio.run(run())


def test_without_running_loop_archive_is_a_safe_noop(monkeypatch):
    tasks = _archive_stubs(monkeypatch)
    upload = AsyncMock()
    monkeypatch.setattr(tg_storage_service, 'upload_text_async', upload)
    scorecards._archive_text_scorecard(7, [], 'B won')
    upload.assert_not_called()
    assert not tasks
