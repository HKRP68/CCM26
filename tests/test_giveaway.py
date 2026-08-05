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


# Modules this file stubs in sys.modules. We snapshot and restore them around
# each test so stubbing the (uninstalled-here but real-in-CI) sqlalchemy package
# can't poison later tests that import the real database layer.
_STUBBED_MODULES = (
    "sqlalchemy", "sqlalchemy.exc", "models",
    "services.activity_service", "services.overflow_service",
    "config", "services.giveaway_service",
)


def _restore_modules(saved):
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _load_service(test):
    # Snapshot originals and schedule their restoration after the test.
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULES}
    test.addCleanup(_restore_modules, saved)

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

    class Giveaway:
        id = None
        winners_drawn_at = None

    models.Giveaway = Giveaway
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

    def with_for_update(self, *a, **k):
        return self

    def all(self):
        return self._all

    def first(self):
        return self._first

    def count(self):
        return len(self._all)


class _FakeSession:
    """Minimal session: query(Giveaway) → the giveaway under test (row-lock
    claim); query(GiveawayEntry) → entries; query(User) → the user for the
    current entry (calls happen once per entry, in order)."""

    def __init__(self, entries, users_by_id, giveaway=None, player=None):
        self.entries = entries
        self.users_by_id = users_by_id
        self.giveaway = giveaway
        self.player = player
        self.committed = False
        self._user_idx = 0

    def query(self, model):
        from models import Giveaway as GA, GiveawayEntry as GE, User as U, Player as P
        if model is GA:
            return _FakeQuery(first_result=self.giveaway)
        if model is P:
            return _FakeQuery(first_result=self.player)
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
        self.svc, self.models, self.IntegrityError = _load_service(self)
        random.seed(42)

    def _entries(self, uids):
        return [self.models.GiveawayEntry(giveaway_id=1, user_id=u,
                                          telegram_id=1000 + u) for u in uids]

    def test_grants_currency_and_marks_winners(self):
        users = {u: _user(u) for u in (1, 2, 3)}
        entries = self._entries([1, 2, 3])
        g = _giveaway("coins", amount=5000, winners=2)
        session = _FakeSession(entries, users, g)

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
        g = _giveaway(winners=3)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        ids = [w["user_id"] for w in winners]
        self.assertEqual(len(ids), len(set(ids)))  # all distinct

    def test_clamps_to_participant_count(self):
        users = {u: _user(u) for u in (1, 2)}
        entries = self._entries([1, 2])
        g = _giveaway(winners=5)  # more winners than entrants
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(len(winners), 2)  # everyone wins, no more

    def test_banned_users_excluded(self):
        users = {1: _user(1), 2: _user(2, banned=True), 3: _user(3)}
        entries = self._entries([1, 2, 3])
        g = _giveaway(winners=3)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        ids = {w["user_id"] for w in winners}
        self.assertNotIn(2, ids)
        self.assertEqual(ids, {1, 3})

    def test_gc_membership_filter_at_draw(self):
        # eligible_user_ids simulates the live Official-GC re-check: user 3 left.
        users = {u: _user(u) for u in (1, 2, 3)}
        entries = self._entries([1, 2, 3])
        g = _giveaway(winners=3)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g, eligible_user_ids={1, 2})
        ids = {w["user_id"] for w in winners}
        self.assertEqual(ids, {1, 2})

    def test_no_eligible_entries_ends_void(self):
        users = {1: _user(1, banned=True)}
        entries = self._entries([1])
        g = _giveaway(winners=1)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(winners, [])
        self.assertEqual(g.status, "ended")
        self.assertIsNotNone(g.winners_drawn_at)

    def test_idempotent_no_double_payout(self):
        users = {1: _user(1)}
        entries = self._entries([1])
        g = _giveaway(winners=1)
        g.winners_drawn_at = datetime.utcnow()  # already drawn
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(winners, [])
        self.assertEqual(users[1].total_coins, 0)  # not credited again

    def test_gems_and_quest_points(self):
        for ptype, attr in (("gems", "total_gems"),
                            ("quest_points", "quest_points")):
            users = {1: _user(1)}
            entries = self._entries([1])
            g = _giveaway(ptype, amount=300, winners=1)
            session = _FakeSession(entries, users, g)
            self.svc.draw_winners(session, g)
            self.assertEqual(getattr(users[1], attr), 300)

    def test_a_gems_giveaway_credits_gems_and_labels_them(self):
        # The prize type an admin picks as "💎 Gems" on the create form, end to
        # end: granted to the winner's gem balance and named in the results post.
        users = {1: _user(1)}
        entries = self._entries([1])
        g = _giveaway("gems", amount=250, winners=1)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(users[1].total_gems, 250)
        self.assertEqual(users[1].total_coins, 0)
        self.assertIn("gems", winners[0]["prize_detail"])
        self.assertIn("💎", self.svc.prize_label(g))
        self.assertIn("gems", self.svc.winners_text(g, winners))


class PriorityWinnerTest(unittest.TestCase):
    """Admin-marked participants are seated first at the draw."""

    def setUp(self):
        self.svc, self.models, self.IntegrityError = _load_service(self)
        random.seed(7)

    def _entries(self, uids, priority=()):
        out = []
        for i, u in enumerate(uids, start=1):
            e = self.models.GiveawayEntry(giveaway_id=1, user_id=u,
                                          telegram_id=1000 + u)
            e.id = i
            e.is_priority = u in priority
            e.priority_set_at = (datetime(2026, 1, 1) + timedelta(minutes=u)
                                 if e.is_priority else None)
            out.append(e)
        return out

    def test_a_marked_participant_always_wins(self):
        # One slot, 20 entrants — random alone would pick #7 once in 20.
        users = {u: _user(u) for u in range(1, 21)}
        entries = self._entries(range(1, 21), priority=(7,))
        g = _giveaway(winners=1)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual([w["user_id"] for w in winners], [7])

    def test_remaining_slots_are_still_random(self):
        users = {u: _user(u) for u in range(1, 11)}
        entries = self._entries(range(1, 11), priority=(3,))
        g = _giveaway(winners=3)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        ids = [w["user_id"] for w in winners]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertIn(3, ids)

    def test_priority_does_not_bypass_the_ban_check(self):
        # A reserved seat is not a licence to pay a banned account.
        users = {1: _user(1), 2: _user(2, banned=True)}
        entries = self._entries([1, 2], priority=(2,))
        g = _giveaway(winners=1)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual([w["user_id"] for w in winners], [1])

    def test_priority_does_not_bypass_the_gc_recheck(self):
        users = {u: _user(u) for u in (1, 2, 3)}
        entries = self._entries([1, 2, 3], priority=(3,))
        g = _giveaway(winners=1)
        session = _FakeSession(entries, users, g)
        # User 3 left the Official GC between joining and the draw.
        winners = self.svc.draw_winners(session, g, eligible_user_ids={1, 2})
        self.assertNotIn(3, [w["user_id"] for w in winners])

    def test_more_marked_than_slots_seats_the_earliest_marked(self):
        users = {u: _user(u) for u in (1, 2, 3, 4)}
        entries = self._entries([1, 2, 3, 4], priority=(2, 3, 4))
        g = _giveaway(winners=2)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        # priority_set_at ascends with user id in the fixture, so 2 then 3.
        self.assertEqual(sorted(w["user_id"] for w in winners), [2, 3])

    def test_an_unmarked_giveaway_is_a_plain_random_draw(self):
        users = {u: _user(u) for u in range(1, 6)}
        entries = self._entries(range(1, 6))
        g = _giveaway(winners=2)
        session = _FakeSession(entries, users, g)
        winners = self.svc.draw_winners(session, g)
        self.assertEqual(len(winners), 2)
        self.assertEqual(len({w["user_id"] for w in winners}), 2)


class RecordEntryTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, self.IntegrityError = _load_service(self)

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


class GrantPlayerFallbackTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, _ = _load_service(self)

    def test_overflow_failure_falls_back_to_coins(self):
        # Roster full + overflow parking blows up → the winner must still get the
        # card's sell value in coins, never a "you won" with nothing granted.
        ov = types.ModuleType("services.overflow_service")

        def _boom(*a, **k):
            raise RuntimeError("db down")

        ov.record_overflow = _boom
        sys.modules["services.overflow_service"] = ov
        sys.modules["config"].get_sell_value = lambda rating: 1234

        player = SimpleNamespace(id=5, name="Star Player", rating=80)
        session = _FakeSession([], {}, player=player)
        user = _user(1)
        user.roster_count = 25  # full
        g = _giveaway("player")
        g.prize_player_id = 5

        label = self.svc._grant_player(session, g, user)
        self.assertEqual(user.total_coins, 1234)
        self.assertIn("coins", label)


class OfficialGroupHelperTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, _ = _load_service(self)

    def _cfg(self, username=None, link=None):
        return SimpleNamespace(branding_group_username=username,
                               official_group_link=link)

    def test_handle_from_username(self):
        self.assertEqual(self.svc.group_handle(self._cfg(username="cmugames")),
                         "@cmugames")
        self.assertEqual(self.svc.group_handle(self._cfg(username="@cmugames")),
                         "@cmugames")

    def test_handle_parsed_from_public_link(self):
        self.assertEqual(
            self.svc.group_handle(self._cfg(link="https://t.me/cmugames")),
            "@cmugames")

    def test_handle_none_for_private_invite_or_missing(self):
        # Private invite links (t.me/+…) have no public handle.
        self.assertIsNone(self.svc.group_handle(self._cfg(link="https://t.me/+AbC123")))
        self.assertIsNone(self.svc.group_handle(self._cfg()))
        self.assertIsNone(self.svc.group_handle(None))

    def test_join_url_prefers_link_then_builds_from_username(self):
        self.assertEqual(
            self.svc.group_join_url(self._cfg(link="https://t.me/+AbC123")),
            "https://t.me/+AbC123")
        self.assertEqual(
            self.svc.group_join_url(self._cfg(username="cmugames")),
            "https://t.me/cmugames")
        self.assertIsNone(self.svc.group_join_url(self._cfg()))


class TextBuilderTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.models, _ = _load_service(self)

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
