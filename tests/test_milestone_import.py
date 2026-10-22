import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import EventMedia
from services.milestone_import import parse_import, apply_import, import_template
from services.event_media_service import EVENT_KEYS, MESSAGE_SETTINGS, messages_enabled


class ImportValidationTests(unittest.TestCase):
    def test_template_is_ready_to_import(self):
        values = parse_import(import_template())
        self.assertEqual(set(values), {'fifty', 'century', 'five_fer'})
        self.assertIs(values['five_fer'], True)

    def test_rejects_invalid_unknown_duplicate_and_oversized_inputs(self):
        for raw in ('{', '[]', '{}', '{"centur": "text"}',
                    '{"fifty": true, "fifty": false}', '{"fifty": 12}',
                    json.dumps({'fifty': 'x' * 32001}), ' ' * (4 * 1024 * 1024 + 1)):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                parse_import(raw)

    def test_explicit_off_and_blank_messages_are_off(self):
        self.assertEqual(parse_import('{"century":true,"fifty":false,"six":"","four":null}'),
                         {'century': True})


class ImportPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        EventMedia.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.db.close)
        self.db.add_all([
            EventMedia(event_key='century', source_type='telegram', source='CLIP', enabled=True, caption='old'),
            EventMedia(event_key='six', source_type='telegram', source='SIX', enabled=True, caption='old six'),
            EventMedia(event_key='fifty', source_type='url', source='old.gif', enabled=False),
        ])
        self.db.commit()

    def test_import_replaces_selection_preserves_media_and_disables_every_omission(self):
        apply_import(self.db, {'century': 'Century for {player}', 'fifty': 'Fifty!', 'five_fer': True})
        rows = self.db.query(EventMedia).all()
        for key, _, _ in EVENT_KEYS:
            entries = [r for r in rows if r.event_key == key]
            self.assertEqual(messages_enabled(entries), key in ('century', 'fifty', 'five_fer'))
        century = next(r for r in rows if r.source == 'CLIP')
        self.assertTrue(century.enabled)
        self.assertEqual(century.caption, 'Century for {player}')
        self.assertTrue(next(r for r in rows if r.source == 'SIX').enabled)
        self.assertTrue(any(r.event_key == 'fifty' and r.enabled and not r.source for r in rows))
        apply_import(self.db, {'century': True})
        self.db.refresh(century)
        self.assertIsNone(century.caption)
        self.assertFalse(messages_enabled(self.db.query(EventMedia).filter_by(event_key='fifty').all()))
        self.assertEqual(self.db.query(EventMedia).filter_by(source_type=MESSAGE_SETTINGS).count(), len(EVENT_KEYS))

    def test_failure_rolls_back_all_changes(self):
        with patch.object(self.db, 'commit', side_effect=RuntimeError('storage failed')):
            with self.assertRaises(RuntimeError):
                apply_import(self.db, {'century': 'new'})
        self.assertEqual(self.db.query(EventMedia).count(), 3)
        self.assertEqual(self.db.query(EventMedia).filter_by(source='CLIP').one().caption, 'old')

class TelegramImportTests(unittest.TestCase):
    def test_document_import_downloads_and_saves_then_closes_editor(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from handlers import setmilestone as sm
        payload = b'{"century":"Hundred for {player}","five_fer":true}'
        document = SimpleNamespace(file_size=len(payload), get_file=AsyncMock(
            return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(payload)))))
        msg = SimpleNamespace(text=None, document=document, reply_text=AsyncMock())
        context = SimpleNamespace(user_data={sm.AWAIT_KEY: {'mode': 'bulk', 'chat_id': 1}})
        db = Mock()
        with patch.object(sm, '_is_admin_update', return_value=True), \
             patch.object(sm, 'get_session', return_value=db), \
             patch('services.milestone_import.apply_import') as apply, \
             patch.object(sm, '_invalidate'):
            self.assertTrue(asyncio.run(sm.milestone_reply_handler(SimpleNamespace(
                effective_message=msg, effective_chat=SimpleNamespace(id=1)), context)))
        apply.assert_called_once_with(db, {'century': 'Hundred for {player}', 'five_fer': True})
        self.assertNotIn(sm.AWAIT_KEY, context.user_data)
        self.assertIn('Messages ON: 2', msg.reply_text.call_args.args[0])

    def test_invalid_pasted_import_keeps_editor_and_does_not_open_database(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from handlers import setmilestone as sm
        msg = SimpleNamespace(text='{"typo":true}', document=None, reply_text=AsyncMock())
        context = SimpleNamespace(user_data={sm.AWAIT_KEY: {'mode': 'bulk', 'chat_id': 1}})
        with patch.object(sm, '_is_admin_update', return_value=True), \
             patch.object(sm, 'get_session') as get_session:
            asyncio.run(sm.milestone_reply_handler(SimpleNamespace(
                effective_message=msg, effective_chat=SimpleNamespace(id=1)), context))
        get_session.assert_not_called()
        self.assertIn(sm.AWAIT_KEY, context.user_data)
        self.assertIn('No settings changed', msg.reply_text.call_args.args[0])


class ExtendedImportSizeTests(unittest.TestCase):
    def test_accepts_full_length_unicode_message(self):
        text = '🏏' * 32000
        self.assertEqual(parse_import(json.dumps({'century': text}))['century'], text)
