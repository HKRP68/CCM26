"""Unit coverage for the shared in-process live-state cache and the
in-process per-match action guard added for the /wpm Mini App hot path.

The cache (services.match_state_store._CACHE) is write-through: the DB stays
the durable source of truth, but reads within MATCH_STATE_CACHE_TTL are
served from memory. These tests run against a fake session so they exercise
exactly the read-through / write-through / TTL / CAS interactions without a
database.
"""

import json
import unittest
from unittest.mock import patch

import services.match_state_store as store
import services.match_webapp_service as svc
from models import MatchState


class _FakeRow:
    def __init__(self, match_id, state, version=1, next_action="PICK_DELIVERY",
                 ball_seq=0):
        self.match_id = match_id
        self.state_json = json.dumps(state)
        self.version = version
        self.next_action = next_action
        self.ball_seq = ball_seq
        self.last_modified = None
        self.last_prompt_msg_id = None


class _FakeQuery:
    def __init__(self, session, conditions=None):
        self._session = session
        self.conditions = conditions or {}

    def filter(self, *exprs):
        conditions = dict(self.conditions)
        for e in exprs:
            conditions[e.left.key] = e.right.value
        return _FakeQuery(self._session, conditions)

    def _matches(self, row):
        return all(getattr(row, k) == v for k, v in self.conditions.items())

    def first(self):
        self._session.reads += 1
        for row in self._session.table:
            if self._matches(row):
                return row
        return None

    def update(self, values, synchronize_session=False):
        matched = [row for row in self._session.table if self._matches(row)]
        for row in matched:
            for col, val in values.items():
                setattr(row, col.key, val)
        return len(matched)


class _FakeSession:
    """Shares its row table and read counter across instances via the
    factory below, mirroring how every store call opens a fresh session."""

    def __init__(self, shared):
        self._shared = shared

    @property
    def table(self):
        return self._shared["table"]

    @property
    def reads(self):
        return self._shared["reads"]

    @reads.setter
    def reads(self, v):
        self._shared["reads"] = v

    def query(self, model):
        assert model is MatchState
        return _FakeQuery(self)

    def add(self, row):
        self._shared["table"].append(row)

    def delete(self, row):
        self._shared["table"].remove(row)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _DummyCtx:
    def __init__(self):
        self.bot_data = {}


class _StoreTestCase(unittest.TestCase):
    """Patches the store onto a fake DB and resets the shared cache."""

    def setUp(self):
        store.clear_cache()
        self.shared = {"table": [], "reads": 0}
        patcher = patch.object(store, "get_session",
                               lambda: _FakeSession(self.shared))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(store.clear_cache)

    @property
    def reads(self):
        return self.shared["reads"]

    def add_row(self, *args, **kwargs):
        row = _FakeRow(*args, **kwargs)
        self.shared["table"].append(row)
        return row


class ReadThroughTests(_StoreTestCase):
    def test_miss_populates_cache_then_serves_from_memory(self):
        self.add_row(1, {"total_runs": 7}, version=3, next_action="PICK_SHOT",
                     ball_seq=11)

        state = store.get_state(_DummyCtx(), 1)
        self.assertEqual(state["total_runs"], 7)
        self.assertEqual(self.reads, 1)

        # Second read (fresh ctx, like every Flask request) → zero DB reads.
        again = store.get_state(_DummyCtx(), 1)
        self.assertEqual(again["total_runs"], 7)
        self.assertEqual(self.reads, 1)

        # Sibling accessors are also cache hits off the same full-row entry.
        self.assertEqual(store.get_next_action(_DummyCtx(), 1), "PICK_SHOT")
        self.assertEqual(store.get_ball_seq(1), 11)
        self.assertEqual(self.reads, 1)

    def test_next_action_miss_caches_full_row_for_get_state(self):
        self.add_row(2, {"x": 1}, next_action="PICK_DELIVERY")
        self.assertEqual(store.get_next_action(_DummyCtx(), 2), "PICK_DELIVERY")
        self.assertEqual(self.reads, 1)
        self.assertEqual(store.get_state(_DummyCtx(), 2), {"x": 1})
        self.assertEqual(self.reads, 1)

    def test_missing_row_never_cached(self):
        self.assertIsNone(store.get_state(_DummyCtx(), 99))
        self.assertEqual(self.reads, 1)
        # Still hits the DB next time — a re-init must be seen immediately.
        self.assertIsNone(store.get_state(_DummyCtx(), 99))
        self.assertEqual(self.reads, 2)


class WriteThroughTests(_StoreTestCase):
    def test_save_state_updates_cache_and_db(self):
        row = self.add_row(1, {"total_runs": 0}, version=1)
        store.save_state(_DummyCtx(), 1, {"total_runs": 4},
                         next_action="PICK_SHOT")
        # DB row updated (durability) …
        self.assertEqual(json.loads(row.state_json), {"total_runs": 4})
        self.assertEqual(row.version, 2)
        self.assertEqual(row.next_action, "PICK_SHOT")
        reads_after_save = self.reads
        # … and reads come from the cache without another DB query.
        self.assertEqual(store.get_state(_DummyCtx(), 1), {"total_runs": 4})
        self.assertEqual(store.get_next_action(_DummyCtx(), 1), "PICK_SHOT")
        self.assertEqual(self.reads, reads_after_save)

    def test_save_state_bump_ball_seq_single_commit(self):
        row = self.add_row(1, {"r": 1}, version=1, ball_seq=5)
        new_seq = store.save_state(_DummyCtx(), 1, {"r": 2},
                                   next_action="PICK_DELIVERY",
                                   bump_ball_seq=True)
        self.assertEqual(new_seq, 6)
        self.assertEqual(row.ball_seq, 6)
        reads_after_save = self.reads
        self.assertEqual(store.get_ball_seq(1), 6)
        self.assertEqual(self.reads, reads_after_save)

    def test_save_state_without_bump_returns_none(self):
        self.add_row(1, {"r": 1})
        self.assertIsNone(store.save_state(_DummyCtx(), 1, {"r": 2}))

    def test_increment_ball_seq_updates_cache(self):
        self.add_row(1, {"r": 1}, ball_seq=3)
        store.get_state(_DummyCtx(), 1)  # populate cache
        self.assertEqual(store.increment_ball_seq(_DummyCtx(), 1), 4)
        reads_before = self.reads
        self.assertEqual(store.get_ball_seq(1), 4)
        self.assertEqual(self.reads, reads_before)

    def test_set_next_action_merges_into_cache(self):
        self.add_row(1, {"r": 1}, next_action="PICK_DELIVERY")
        store.get_state(_DummyCtx(), 1)  # populate cache
        store.set_next_action(_DummyCtx(), 1, "PICK_SHOT")
        reads_before = self.reads
        self.assertEqual(store.get_next_action(_DummyCtx(), 1), "PICK_SHOT")
        self.assertEqual(self.reads, reads_before)


class CopyIsolationTests(_StoreTestCase):
    def test_returned_dicts_are_isolated_copies(self):
        self.add_row(1, {"bat_stats": {"100": {"runs": 10}}})
        first = store.get_state(_DummyCtx(), 1)
        first["bat_stats"]["100"]["runs"] = 999
        first["injected"] = True

        second = store.get_state(_DummyCtx(), 1)
        self.assertEqual(second["bat_stats"]["100"]["runs"], 10)
        self.assertNotIn("injected", second)

    def test_cache_reads_keep_json_str_key_normalization(self):
        # Saving a state with int keys must round-trip to str keys on read,
        # exactly like a DB round-trip (code depends on str-keyed stats).
        self.add_row(1, {"seed": 1})
        store.save_state(_DummyCtx(), 1, {"bat_stats": {100: {"runs": 1}}})
        state = store.get_state(_DummyCtx(), 1)
        self.assertIn("100", state["bat_stats"])
        self.assertNotIn(100, state["bat_stats"])


class TtlTests(_StoreTestCase):
    def test_expired_entry_revalidates_against_db(self):
        row = self.add_row(1, {"v": "old"})
        store.get_state(_DummyCtx(), 1)
        self.assertEqual(self.reads, 1)

        # Another writer (e.g. overlapping deploy) changes the DB directly.
        row.state_json = json.dumps({"v": "new"})
        row.version = 9

        # Within TTL: still the cached value.
        self.assertEqual(store.get_state(_DummyCtx(), 1)["v"], "old")
        self.assertEqual(self.reads, 1)

        # Age the entry past the TTL → next read goes to the DB.
        with store._CACHE_LOCK:
            store._CACHE[1]["fetched_at"] -= (store._CACHE_TTL + 1)
        self.assertEqual(store.get_state(_DummyCtx(), 1)["v"], "new")
        self.assertEqual(self.reads, 2)

    def test_ttl_zero_disables_caching(self):
        self.add_row(1, {"v": 1})
        old_ttl = store._CACHE_TTL
        store._CACHE_TTL = 0
        try:
            store.get_state(_DummyCtx(), 1)
            store.get_state(_DummyCtx(), 1)
            self.assertEqual(self.reads, 2)  # every read hits the DB
            self.assertEqual(store._CACHE, {})
        finally:
            store._CACHE_TTL = old_ttl


class CasCacheTests(_StoreTestCase):
    def test_cas_success_refreshes_cache(self):
        self.add_row(1, {"flag": False}, version=1)
        store.get_state(_DummyCtx(), 1)  # cache the old state

        def _mutate(state):
            state["flag"] = True
            return "done", "PICK_SHOT"

        self.assertEqual(store.update_state_cas(_DummyCtx(), 1, _mutate), "done")
        reads_before = self.reads
        self.assertTrue(store.get_state(_DummyCtx(), 1)["flag"])
        self.assertEqual(store.get_next_action(_DummyCtx(), 1), "PICK_SHOT")
        self.assertEqual(self.reads, reads_before)

    def test_cas_abort_leaves_cache_untouched(self):
        self.add_row(1, {"flag": False}, version=1)
        store.get_state(_DummyCtx(), 1)
        with store._CACHE_LOCK:
            cached_before = dict(store._CACHE[1])

        def _mutate(state):
            raise store.CasAbort("rejected")

        self.assertEqual(store.update_state_cas(_DummyCtx(), 1, _mutate),
                         "rejected")
        with store._CACHE_LOCK:
            self.assertEqual({k: v for k, v in store._CACHE[1].items()},
                             cached_before)

    def test_stale_version_write_is_dropped(self):
        self.add_row(1, {"v": 1}, version=5)
        store.get_state(_DummyCtx(), 1)  # caches version 5
        # A slow writer that committed version 3 must not roll the cache back.
        store._cache_apply_write(1, state_json=json.dumps({"v": "stale"}),
                                 version=3)
        self.assertEqual(store.get_state(_DummyCtx(), 1), {"v": 1})

    def test_save_autoplay_users_refreshes_cache(self):
        self.add_row(1, {"autoplay_users": {}}, version=1)
        store.get_state(_DummyCtx(), 1)
        state = store.save_autoplay_users(_DummyCtx(), 1, 42, True)
        self.assertEqual(state["autoplay_users"], {"42": True})
        reads_before = self.reads
        cached = store.get_state(_DummyCtx(), 1)
        self.assertEqual(cached["autoplay_users"], {"42": True})
        self.assertEqual(self.reads, reads_before)


class CleanupTests(_StoreTestCase):
    def test_cleanup_state_evicts_cache(self):
        self.add_row(1, {"v": 1})
        store.get_state(_DummyCtx(), 1)
        store.cleanup_state(_DummyCtx(), 1)
        self.assertEqual(store._CACHE, {})
        self.assertIsNone(store.get_state(_DummyCtx(), 1))


class ActionGuardTests(unittest.TestCase):
    def setUp(self):
        with svc._ACTION_GUARD_LOCK:
            svc._ACTION_GUARD.clear()
        self.addCleanup(lambda: svc._ACTION_GUARD.clear())

    def test_claim_reject_release_cycle(self):
        self.assertTrue(svc._claim_action(1))
        self.assertTrue(svc.action_in_progress(1))
        self.assertFalse(svc._claim_action(1))   # double-tap rejected
        self.assertTrue(svc._claim_action(2))    # other matches unaffected
        svc._release_action(1)
        self.assertFalse(svc.action_in_progress(1))
        self.assertTrue(svc._claim_action(1))

    def test_expired_claim_self_heals(self):
        self.assertTrue(svc._claim_action(1))
        with svc._ACTION_GUARD_LOCK:
            svc._ACTION_GUARD[1] -= (svc._ACTION_GUARD_TTL + 1)
        self.assertFalse(svc.action_in_progress(1))
        self.assertTrue(svc._claim_action(1))


if __name__ == "__main__":
    unittest.main()
