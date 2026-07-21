"""Tests for the giveaway service — winner draw, one-entry enforcement, and
anti-cheat guarantees.

The test environment has neither SQLAlchemy nor python-telegram-bot installed,
so (like the other service tests here, e.g. test_subscription_lifecycle) we stub
the heavy imports and exercise the pure logic of ``services.giveaway_service``
against lightweight fakes.
"""

import sys
import types
import random
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace


def _load_service():
    # Stub sqlalchemy.exc.IntegrityError (imported at module top).
    sa = types.ModuleType("sqlalchemy")
    exc = types.ModuleType("sqlalchemy.exc")

    class IntegrityError(Exception):
        pass

    exc.IntegrityError = IntegrityError
    sa.exc = exc
    sys.modules["sqlalchemy"] = sa
    sys.modules["sqlalchemy.exc"] = exc

    # Stub the models the service references.
    models = types.ModuleType("models")

    class GiveawayEntry:
        # Class-level attrs so the service's `GiveawayEntry.giveaway_id == ...`
        # filter expressions resolve (SQLAlchemy columns, stubbed as None here).
        giveaway_id = None
        user_id = None

        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.is_winner = kw.get("is_winner", False)
            self.won_at = None
            self.prize_detail = None

    class User:
        id = None
        is_banned = None

    class Player:
        id = None

    class UserRoster:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    models.GiveawayEntry = GiveawayEntry
    models.User = User
    models.Player = Player
    models.UserRoster = UserRoster
    sys.modules["models"] = models

    # Stub the activity ledger + config used by the grant path.
    act = types.ModuleType("services.activity_service")
    act.log_activity = lambda *a, **k: None
    sys.modules["services.activity_service"] = act
    cfg = types.ModuleType("config")
    cfg.MAX_ROSTER = 25
    sys.modules["config"] = cfg

    sys.modules.pop("services.giveaway_service", None)
    from services import giveaway_service
    return giveaway_service, models, IntegrityError


class _FakeQuery:
    def __init__(self, all_result=None, first_result=None):
        self._all = all_result if all_result is not None else []
        self._first = first_result

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._all

    def first(self):
        return self._first

    def count(self):
        return len(self._all)


class _FakeSession:
    """Minimal session: query(GiveawayEntry) → entries; query(User) → the user
    for the current entry (calls happen once per entry, in order)."""

    def __init__(self, entries, users_by_id):
        self.entries = entries
        self.users_by_id = users_by_id
        self.committed = False
        self._user_idx = 0

    def query(self, model):
        from models import GiveawayEntry as GE, User as U
        if model is GE:
            return _FakeQuery(all_result=list(self.entries))
        if model is U:
            entry = self.entries[self._user_idx]
            self._user_idx += 1
            return _FakeQuery(first_result=self.users_by_id.get(entry.user_id))
        return _FakeQuery()

    def add(self, obj):
        pass

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _user(uid, banned=False):
    return SimpleNamespace(id=uid, telegram_id=1000 + uid,
                           username=f"u{uid}", first_name=f"User{uid}",
                           is_banned=banned, total_coins=0, total_gems=0,
                           quest_points=0, roster_count=0, matches_played=1)


def _giveaway(prize_type="coins", amount=5000, winners=2):
    return SimpleNamespace(id=1, title="Test GA", prize_type=prize_type,
                           prize_amount=amount, prize_player=None,
                           prize_player_id=None, num_winners=winners,
                           status="running", winners_drawn_at=None,
                           end_time=datetime.utcnow() + timedelta(hours=1),
                           start_time=datetime.utcnow() - timedelta(hours=1))


class DrawWinnersTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, self.IntegrityError = _load_service()
        random.seed(42)

    def _entries(self, uids):
        return [self.models.GiveawayEntry(giveaway_id=1, user_id=u,
                                          telegram_id=1000 + u) for u in uids]

    def test_grants_currency_and_marks_winners(self):
        users = {u: _user(u) for u in (1, 2, 3)}
        entries = self._entries([1, 2, 3])
        session = _FakeSession(entries, users)
        g = _giveaway("coins", amount=5000, winners=2)

        winners = self.svc.draw_winners(session, g)

        self.assertEqual(len(winners), 2)
        self.assertEqual(g.status, "ended")
        self.assertIsNotNone(g.winners_drawn_at)
        for w in winners:
            self.assertEqual(users[w["user_id"]].total_coins, 5000)
            self.assertIn("coins", w["prize_detail"])
        # Winning entries flagged.
        won = [e for e in entries if e.is_winner]
        self.assertEqual(len(won), 2)

    def test_no_duplicate_winners(self):
        users = {u: _user(u) for u in range(1, 6)}
        entries = self._entries(list(range(1, 6)))
        session = _FakeSession(entries, users)
        g = _giveaway(winners=3)
        winners = self.svc.draw_winners(session, g)
        ids = [w["user_id"] for w in winners]
        self.assertEqual(len(ids), len(set(ids)))  # all distinct

    def test_clamps_to_participant_count(self):
        users = {u: _user(u) for u in (1, 2)}
        entries = self._entries([1, 2])
        session = _FakeSession(entries, users)
        g = _giveaway(winners=5)  # more winners than entrants
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(len(winners), 2)  # everyone wins, no more

    def test_banned_users_excluded(self):
        users = {1: _user(1), 2: _user(2, banned=True), 3: _user(3)}
        entries = self._entries([1, 2, 3])
        session = _FakeSession(entries, users)
        g = _giveaway(winners=3)
        winners = self.svc.draw_winners(session, g)
        ids = {w["user_id"] for w in winners}
        self.assertNotIn(2, ids)
        self.assertEqual(ids, {1, 3})

    def test_gc_membership_filter_at_draw(self):
        # eligible_user_ids simulates the live Official-GC re-check: user 3 left.
        users = {u: _user(u) for u in (1, 2, 3)}
        entries = self._entries([1, 2, 3])
        session = _FakeSession(entries, users)
        g = _giveaway(winners=3)
        winners = self.svc.draw_winners(session, g, eligible_user_ids={1, 2})
        ids = {w["user_id"] for w in winners}
        self.assertEqual(ids, {1, 2})

    def test_no_eligible_entries_ends_void(self):
        users = {1: _user(1, banned=True)}
        entries = self._entries([1])
        session = _FakeSession(entries, users)
        g = _giveaway(winners=1)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(winners, [])
        self.assertEqual(g.status, "ended")
        self.assertIsNotNone(g.winners_drawn_at)

    def test_idempotent_no_double_payout(self):
        users = {1: _user(1)}
        entries = self._entries([1])
        session = _FakeSession(entries, users)
        g = _giveaway(winners=1)
        g.winners_drawn_at = datetime.utcnow()  # already drawn
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(winners, [])
        self.assertEqual(users[1].total_coins, 0)  # not credited again

    def test_gems_and_quest_points(self):
        for ptype, attr in (("gems", "total_gems"),
                            ("quest_points", "quest_points")):
            users = {1: _user(1)}
            entries = self._entries([1])
            session = _FakeSession(entries, users)
            g = _giveaway(ptype, amount=300, winners=1)
            self.svc.draw_winners(session, g)
            self.assertEqual(getattr(users[1], attr), 300)


class RecordEntryTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, self.IntegrityError = _load_service()

    def test_first_entry_joins(self):
        session = _FakeSession([], {})
        g = _giveaway()
        user = _user(7)
        status = self.svc.record_entry(session, g, user, user.telegram_id)
        self.assertEqual(status, "joined")

    def test_duplicate_entry_rejected_by_unique_index(self):
        # Simulate the DB unique index firing on the second insert. Raise the
        # exact IntegrityError class the service caught (avoids reload-identity
        # mismatch between test-created and module-imported exception classes).
        integrity_error = self.svc.IntegrityError

        class DupSession(_FakeSession):
            def flush(self):
                raise integrity_error("duplicate")

        session = DupSession([], {})
        g = _giveaway()
        user = _user(7)
        status = self.svc.record_entry(session, g, user, user.telegram_id)
        self.assertEqual(status, "already")


class TextBuilderTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, _ = _load_service()

    def test_prize_labels(self):
        self.assertIn("coins", self.svc.prize_label(_giveaway("coins", 5000)))
        self.assertIn("gems", self.svc.prize_label(_giveaway("gems", 50)))
        self.assertIn("quest points",
                      self.svc.prize_label(_giveaway("quest_points", 50)))
        g = _giveaway("player")
        g.prize_player = SimpleNamespace(name="Virat Kohli", rating=91)
        self.assertIn("Virat Kohli", self.svc.prize_label(g))

    def test_announcement_has_title_and_cta(self):
        text = self.svc.announcement_text(_giveaway("coins", 5000, winners=2))
        self.assertIn("Test GA", text)
        self.assertIn("GIVEAWAY", text)
        self.assertIn("Official GC", text)

    def test_winners_text_empty_vs_populated(self):
        empty = self.svc.winners_text(_giveaway(), [])
        self.assertIn("no eligible", empty.lower())
        populated = self.svc.winners_text(_giveaway(), [
            {"user_id": 1, "telegram_id": 1001, "username": "alice",
             "first_name": "Alice", "prize_detail": "5,000 coins"},
        ])
        self.assertIn("@alice", populated)
        self.assertIn("5,000 coins", populated)

    def test_to_ist_offset(self):
        utc = datetime(2026, 7, 21, 12, 0, 0)
        ist = self.svc._to_ist(utc)
        self.assertEqual(ist, utc + timedelta(hours=5, minutes=30))


if __name__ == "__main__":
    unittest.main()
