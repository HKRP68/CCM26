"""Initial match summaries use ready artwork without slowing other requests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services import tg_storage_service as storage


class FastSummaryAssetsTests(unittest.TestCase):
    def test_remote_downloads_are_default_and_restored_after_nested_error(self):
        self.assertTrue(storage.remote_downloads_allowed())
        with self.assertRaisesRegex(ValueError, 'render failed'):
            with storage.local_assets_only():
                self.assertFalse(storage.remote_downloads_allowed())
                with storage.local_assets_only():
                    self.assertFalse(storage.remote_downloads_allowed())
                self.assertFalse(storage.remote_downloads_allowed())
                raise ValueError('render failed')
        self.assertTrue(storage.remote_downloads_allowed())

    def test_sync_download_does_not_start_telegram_or_worker_in_fast_scope(self):
        with patch.object(storage, 'Bot') as bot, patch.object(storage, '_has_bot_token', return_value=True):
            with storage.local_assets_only():
                self.assertIsNone(storage.download_file_bytes_sync('stored-logo'))
            bot.assert_not_called()

    def test_async_download_does_not_start_telegram_in_fast_scope(self):
        async def run():
            with storage.local_assets_only():
                return await storage.download_file_bytes_async('stored-logo')
        with patch.object(storage, 'Bot') as bot, patch.object(storage, '_has_bot_token', return_value=True):
            self.assertIsNone(asyncio.run(run()))
            bot.assert_not_called()

    def test_regular_download_still_restores_art(self):
        data = b'original artwork'
        bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(data)))))
        with patch.object(storage, 'Bot', return_value=bot), patch.object(storage, '_has_bot_token', return_value=True):
            self.assertEqual(data, asyncio.run(storage.download_file_bytes_async('stored-logo')))
            bot.get_file.assert_awaited_once_with(file_id='stored-logo')

    def test_fast_scope_reaches_render_worker_without_affecting_other_tasks(self):
        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def render():
                with storage.local_assets_only():
                    entered.set()
                    await release.wait()
                    return await asyncio.to_thread(storage.remote_downloads_allowed)

            task = asyncio.create_task(render())
            await entered.wait()
            self.assertTrue(storage.remote_downloads_allowed())
            self.assertTrue(await asyncio.to_thread(storage.remote_downloads_allowed))
            release.set()
            self.assertFalse(await task)
            self.assertTrue(storage.remote_downloads_allowed())
        asyncio.run(run())

    def test_database_and_disk_assets_remain_available(self):
        from services import asset_store
        data = b'ready database artwork'
        session = Mock()
        session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(data=data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crest.png'
            with patch.object(asset_store, '_relative_key', return_value='static/challenge_leagues/crest.png'), \
                 patch.object(asset_store, 'absolute_path', return_value=str(path)), \
                 patch.object(asset_store, '_session', return_value=session), \
                 patch.object(asset_store, '_from_telegram') as restore:
                with storage.local_assets_only():
                    self.assertTrue(asset_store.ensure(str(path)))
                    self.assertEqual(data, path.read_bytes())
                    self.assertTrue(asset_store.ensure(str(path)))
                restore.assert_not_called()
