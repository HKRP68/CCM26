"""Bulk rewards use real inventory rows and one atomic transaction."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class BulkRewardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import admin
        cls.admin = admin

    def setUp(self):
        from database import Base
        from models import Pack, User
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        now = datetime.utcnow()
        self.db.add_all([
            User(id=1, telegram_id=101, username='target_one', total_coins=10,
                 total_gems=2, quest_points=3, created_at=now),
            User(id=2, telegram_id=102, username='target_two', created_at=now),
            User(id=3, telegram_id=103, username='target_banned', is_banned=True),
            User(id=4, telegram_id=104, username='other', created_at=now-timedelta(days=60)),
            Pack(id=1, slot_number=1, name='Reward Pack', is_active=False),
        ])
        self.db.commit()
        self.patch = patch.object(self.admin, 'get_session', self.sessions)
        self.patch.start()
        self.config_patch = patch.dict(self.admin.app.config, TESTING=True, WTF_CSRF_ENABLED=False)
        self.config_patch.start()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as session:
            session['admin'] = True

    def tearDown(self):
        self.config_patch.stop()
        self.patch.stop()
        self.db.close()
        self.engine.dispose()

    def post(self, **values):
        data = dict(action='grant_pack', pack_id='1', q='target', confirm_text='CONFIRM')
        data.update(values)
        response = self.client.post('/users/bulk-action', data=data)
        self.assertEqual(response.status_code, 302)
        self.db.expire_all()
        return response

    def inventory(self):
        from models import UnopenedPack
        return self.db.query(UnopenedPack).order_by(UnopenedPack.user_id).all()

    def test_one_unopened_pack_per_filtered_non_banned_user_and_audit(self):
        from models import ActivityLog, AdminLog, User
        self.post(qty='20', amount='999')  # A forged quantity cannot multiply packs.
        self.assertEqual([(p.user_id, p.pack_id, p.source) for p in self.inventory()],
                         [(1, 1, 'admin'), (2, 1, 'admin')])
        self.assertEqual(self.db.get(User, 1).total_coins, 10)
        self.assertEqual(self.db.query(ActivityLog).filter_by(action='admin_grant_pack').count(), 2)
        audit = self.db.query(AdminLog).filter_by(action='bulk_grant').one()
        self.assertIn('pack #1', audit.detail)
        self.assertEqual(audit.target_name, '2 users')

    def test_invalid_pack_or_missing_confirmation_creates_nothing(self):
        for values in ({'pack_id': 'garbage'}, {'pack_id': '999'}, {'confirm_text': ''}):
            with self.subTest(values=values):
                response = self.post(**values)
                self.assertIn('q=target', response.location)
                self.assertEqual(self.inventory(), [])

    def test_joined_date_filter_applies_to_packs(self):
        cutoff = (datetime.utcnow()-timedelta(days=7)).strftime('%Y-%m-%d')
        self.post(q='', joined_after=cutoff)
        self.assertEqual([p.user_id for p in self.inventory()], [1, 2])

    def test_failure_rolls_back_packs_and_audit_for_every_recipient(self):
        from models import ActivityLog, AdminLog
        from services.pack_service import grant_pack
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                raise RuntimeError('simulated inventory failure')
            return grant_pack(*args, **kwargs)

        with patch('services.pack_service.grant_pack', side_effect=fail_second):
            with self.assertLogs('admin', level='ERROR'):
                self.post()
        self.assertEqual(self.inventory(), [])
        self.assertEqual(self.db.query(ActivityLog).count(), 0)
        self.assertEqual(self.db.query(AdminLog).count(), 0)

    def test_currency_rewards_and_clamped_deduction_audit(self):
        from models import ActivityLog, User
        for action, field, amount, expected in [
            ('grant_coins', 'total_coins', '100', 110),
            ('grant_gems', 'total_gems', '5', 7),
            ('grant_quest_points', 'quest_points', '7', 10),
            ('grant_coins', 'total_coins', '-200', 0),
        ]:
            self.post(action=action, amount=amount, q='target_one')
            self.assertEqual(getattr(self.db.get(User, 1), field), expected)
        logs = self.db.query(ActivityLog).filter_by(user_id=1).order_by(ActivityLog.id).all()
        self.assertEqual(logs[-1].coins_change, -110)
        self.assertEqual(self.inventory(), [])

    def test_currency_caps_reject_bad_amounts(self):
        from models import User
        for amount in ('0', '1.5', '5000001'):
            response = self.post(action='grant_coins', amount=amount)
            self.assertIn('q=target', response.location)
            self.assertEqual(self.db.get(User, 1).total_coins, 10)

    def test_recipient_limit_rejects_entire_batch(self):
        from models import User
        self.db.bulk_insert_mappings(User, [
            dict(telegram_id=1000+i, username=f'target_{i}') for i in range(4999)
        ])
        self.db.commit()
        self.post()
        self.assertEqual(self.inventory(), [])

    def test_empty_or_banned_only_audience_does_not_write_audit(self):
        from models import AdminLog
        for query in ('no_such_manager', 'target_banned'):
            self.post(q=query)
            self.assertEqual(self.inventory(), [])
            self.assertEqual(self.db.query(AdminLog).count(), 0)

    def test_page_shows_eligible_count_and_pack_choice(self):
        response = self.client.get('/users?q=target')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Bulk Rewards · 2 eligible users', body)
        self.assertIn('Reward Pack (inactive)', body)
        self.assertIn('one per user', body)


if __name__ == '__main__':
    unittest.main()
